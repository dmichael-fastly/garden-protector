# Garden Protector — developer entrypoints.
#
# AGENTS.md references `make ci`; this is it. The Rust build/tests need the rustup
# toolchain (which has the wasm32-wasip1 target) ahead of Homebrew's cargo on PATH
# — see scripts/serve_backend.sh and AGENTS.md "Known Traps".
#
# Quick start (4 terminals, or background the first three):
#   make serve            # 1. Fastly Compute edge on :7878 (Viceroy)
#   make gateway          # 2. indoor Pi gateway on :8088 -> edge
#   make sim              # 3. XIAO node simulator -> gateway (all peripherals)
#   make console          # 4. admin console on :8050 (MOCK mode, safe)
# then open http://localhost:7878 (dashboard) and http://localhost:8050 (console).

CARGO_PATH := PATH="$(HOME)/.cargo/bin:$$PATH"
EDGE ?= http://localhost:7878
GATEWAY ?= http://localhost:8088

.PHONY: ci build-check test test-rust test-py scan hooks gen gen-check css css-check build serve gateway portal sim console fake-edge demo fmt clean help smoke

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

ci: gen-check css-check build-check test scan ## Full check: contract drift + CSS drift + wasm compile + Rust + Python tests + secret scan (run after every change)
	@echo "✓ CI green"

build-check: ## Compile the wasm deploy artifact (wasm32-wasip1) — catches wasm-only breaks invisible to native cargo test
	cd backend && $(CARGO_PATH) cargo build --release --target wasm32-wasip1
	@echo "✓ wasm deploy artifact compiles"

test: test-rust test-py ## Run both test suites

gen: ## Regenerate the cross-language contract modules from contract/spec.toml
	python3 contract/gen.py

gen-check: ## Fail if the generated contract modules drift from contract/spec.toml
	python3 contract/gen.py --check

css: ## Compile ui/static/app.css from ui/tailwind/input.css (Tailwind v4 + DaisyUI; build-time Node only — the Pi + Compute just serve the committed CSS)
	@command -v npm >/dev/null 2>&1 || { echo "✗ npm not installed — Node is a BUILD-TIME dep for the CSS (see AGENTS.md). The Pi + Compute only SERVE the committed ui/static/app.css."; exit 1; }
	@[ -d node_modules ] || npm ci
	npx @tailwindcss/cli -i ui/tailwind/input.css -o ui/static/app.css --minify
	@echo "✓ built ui/static/app.css"

css-check: ## Fail if ui/static/app.css drifts from a fresh Tailwind build (edited a template but forgot `make css`) — the CSS drift gate, mirrors gen-check
	@command -v npm >/dev/null 2>&1 || { echo "✗ npm not installed — Node is required for css-check (CI machines need Node; the Pi + Compute do not)."; exit 1; }
	@[ -d node_modules ] || npm ci
	@tmp=$$(mktemp); \
	  npx @tailwindcss/cli -i ui/tailwind/input.css -o $$tmp --minify >/dev/null 2>&1 || { echo "✗ css-check: Tailwind build failed"; rm -f $$tmp; exit 1; }; \
	  if ! diff -q $$tmp ui/static/app.css >/dev/null 2>&1; then \
	    echo "✗ css-check: ui/static/app.css is STALE — run \`make css\` and commit the result"; \
	    rm -f $$tmp; exit 1; \
	  fi; \
	  rm -f $$tmp; \
	  echo "✓ css-check: ui/static/app.css matches a fresh build"

scan: ## Secret scan with gitleaks over git history (also runs inside `make ci`)
	@command -v gitleaks >/dev/null 2>&1 || { echo "✗ gitleaks not installed — 'brew install gitleaks' (see CHARTER.md / AGENTS.md)"; exit 1; }
	gitleaks git --no-banner --redact
	@echo "✓ no secrets in history"

hooks: ## Install the tracked git hooks (pre-commit gitleaks scan of staged changes)
	git config core.hooksPath .githooks
	@echo "✓ git hooks installed (core.hooksPath=.githooks)"

test-rust: ## Rust unit tests (native target, debug — faster compile; build-check covers the release wasm)
	cd backend && $(CARGO_PATH) cargo test

test-py: ## Python tests (provision + hardware + simulation e2e)
	python3 -m pytest -q

build: ## Build the Compute wasm and stage it at backend/bin/main.wasm
	cd backend && $(CARGO_PATH) cargo build --release --target wasm32-wasip1
	cp backend/target/wasm32-wasip1/release/garden-protector-backend.wasm backend/bin/main.wasm
	@echo "✓ staged backend/bin/main.wasm"

serve: ## Build + serve the Compute edge locally on 0.0.0.0:7878 (Viceroy)
	./scripts/serve_backend.sh

gateway: ## Run the indoor Pi gateway (Tier-1) -> $(EDGE)
	python3 hardware/gateway.py --listen 0.0.0.0:8088 --edge $(EDGE)

portal: ## Run the LAN admin portal locally on :8080 (dev passcode) -> $(EDGE)
	GP_ADMIN_PASSCODE=$${GP_ADMIN_PASSCODE:-changeme-dev} \
	  python3 hardware/portal.py --listen 127.0.0.1:8080 --edge $(EDGE)

sim: ## Run the XIAO node simulator (all optional peripherals) -> $(GATEWAY)
	python3 hardware/node_sim.py --gateway $(GATEWAY) --fitted all

fake-edge: ## Run the deterministic fake edge (offline demos/tests) on :7878
	python3 tests/fake_edge.py --port 7878

smoke: ## MANUAL integration gate: real edge<->Pi smoke (requires: fastly CLI, ~90 s compile, live network). NOT part of `make ci`.
	bash tests/smoke_test.sh

console: ## Run the admin console on :8050 in MOCK mode (no Fastly calls)
	python3 -m provision.console --listen 127.0.0.1:8050 --edge $(EDGE) --mock

demo: ## Narrated end-to-end walkthrough (every decision branch, no hardware)
	python3 scripts/demo.py

deploy-edge: build ## Redeploy the Compute package to the LIVE Fastly edge (clone+activate)
	python3 scripts/deploy_edge.py

provision-viewer: ## Set/clear the edge viewer password from GP_VIEWER_PASSCODE (.env)
	python3 scripts/provision_viewer_pass.py

fmt: ## Format Rust
	cd backend && $(CARGO_PATH) cargo fmt

clean: ## Remove local sim/console scratch (frame archives, pyc)
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
