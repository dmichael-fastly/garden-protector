# Garden Protector — Audit Index

Synthesis of all seven domain audits: **edge, cloud, pi, ui, ux, tests, ci**. Findings in any
domain's `## Refuted` section are dropped from the backlog below — **EDGE-001** (the
`make deploy-edge` "ships nothing" claim; the stale-guard is intentional and the documented
deploy works) and **CLOUD-002** (the registry no-CAS guarantee; it is conditioned on the
strongly-consistent local mirror, not KV read freshness, and the hydrate path is a tested
best-effort recovery). No P0 findings survive in any domain.

## Repo health scorecard

| Domain | Health (1-5) | One-line verdict |
|--------|--------------|------------------|
| edge | 4 | Safety/auth/CDN/SSOT invariants are fail-closed and tested; the live gaps are deploy mechanics (no CDN purge), not the running code. |
| cloud | 4 | Solid, policy-aligned control plane; main real risks are an unpaginated find-or-create and a stale-after-wipe archive LIST. |
| pi | 4 | Mature, fail-closed-disciplined tier; live issues are footprint bloat (test deps shipped) and an IPv6 brute-force gap, not safety. |
| ui | 4 | Strong SSOT discipline (nav + `?v=` test-gated); notable gaps are History nav-active rendered wrong + the known Pi-health duplication. |
| ux | 3 | Coherent design language let down by zero reduced-motion support, a jarringly technical Logs page, and leaked internal jargon. |
| tests | 4 | Strong, well-disciplined suite; fail-closed cores exhaustively pinned, with real holes on cross-tier error paths and the header SSOT. |
| ci | 4 | `make ci` is green and the drift gate is well-built; the gate never compiles the actual wasm deploy artifact and has no automated runner. |

**Overall: 3.9 / 5** — average of the seven verified domains (4+4+4+4+3+4+4 = 27 ÷ 7 = 3.86,
rounded to 3.9). The backend tiers, UI plumbing, tests, and CI are uniformly strong (4); the
single sub-4 domain (ux) is accessibility/voice polish, not a safety or correctness risk. No P0
anywhere, and the safety-critical fail-closed invariants are correct across all tiers.

## Prioritized cross-tier backlog

Refuted findings (EDGE-001, CLOUD-002) dropped. Cross-domain duplicates merged into a single row
(merged IDs in the Title), with the best owning agent. Sorted by Severity (P0→P3) then Effort (S→L).

| ID | Title | Severity | Owning agent | Effort | Depends-on |
|----|-------|----------|--------------|--------|------------|
| TEST-001 | Pi client evidence-POST fail-closed branches (503 / timeout / malformed-200) untested — the key cross-tier disarm contract is unpinned | P1 | test-engineer | S | — |
| TEST-002 | Header SSOT not enforced on Pi senders — hardcoded header strings, no conformance test (real fix: Pi senders use `contract_gen.HEADER_*`) | P1 | test-engineer | S | — |
| CLOUD-001 | find-or-create reads only one unpaginated page of `GET /service` → duplicate service/bucket/key on busy accounts | P1 | fastly-cloud-expert | S | — |
| UX-001 | No `prefers-reduced-motion` fallback for any animation (pulsing safety pills, spinners, blinking cursor) — WCAG 2.2 2.3.3 | P1 | ui-expert | S | — |
| EDGE-002 | Deploy path performs no `purge_all`; dashboard/timelapse HTML routes set no Cache-Control (merged: CLOUD-005 purge mitigation) | P1 | fastly-edge-expert | S | CI-001 |
| UX-002 | Logs page copy violates the non-technical voice ("audit trail", "FSM State", "Correlation Trace") | P1 | ux-designer | M | — |
| CI-001 | `make ci` never compiles the wasm32 deploy artifact — a wasm-only break passes CI but breaks `make build`/deploy | P1 | ci-guardian | M | — |
| PI-002 | Test-only deps (pytest/responses/hypothesis) shipped + re-resolved into the service venv on every Pi boot — footprint hit | P2 | raspberry-pi-expert | S | — |
| TEST-003 | Cross-tenant archive-delete prefix guard (`g/{gid}/evidence/`) untested — extract a pure helper and pin it | P2 | test-engineer | S | — |
| CLOUD-003 | `kv_get` re-serializes JSON / is lossy for binary KV values (same store holds raw ONNX model bytes) | P2 | fastly-cloud-expert | S | — |
| CLOUD-004 | CDN-read SSRF allowlist is suffix-only (`*.fastly.net` passes) vs the specific host shape generated | P2 | fastly-cloud-expert | S | — |
| UI-001 | History route serves wrong server-rendered active nav link (both tiers); JS patches it — SSOT + a11y (merged: edge + pi route handlers) | P2 | ui-expert | S | — |
| UX-003 | Native `confirm()`/`alert()` on Logs break design language + a11y; use the shared themed modal + toast | P2 | ux-designer | S | — |
| UX-004 | Internal jargon in user-visible copy ("the Pi", "Secret Store", "Node ID", "edge registry") | P2 | ux-designer | S | — |
| UX-005 | Async-updated values (cost estimate, costs hero, timelapse progress) lack `aria-live` regions | P2 | ui-expert | S | — |
| UX-007 | "Stop Mitigation" control label ambiguous about the safe (no-spray) state — fail-closed clarity | P2 | ux-designer | S | — |
| CI-003 | `GP_TELEMETRY=0` trap has no in-suite defense — add an autouse conftest fixture | P2 | ci-guardian | S | — |
| CLOUD-005 | Stale archive LIST / deleted images served from cache after wipe/prune — needs `CacheOverride::pass()` on the FOS LIST send | P2 | fastly-edge-expert | M | EDGE-002 |
| PI-001 | Login rate-limit keyed per-IP is bypassable over the dual-stack IPv6 listener (/64 rotation) | P2 | raspberry-pi-expert | M | — |
| TEST-004 | No Rust property tests for safety cores (rain veto, traversal guard) — example-based only | P2 | test-engineer | M | — |
| UX-006 | Console lacks shared header safety pills + cross-tier nav, breaking "one experience" parity | P2 | ui-expert | M | — |
| EDGE-003 | In-memory IP lockout is per-instance, not global (brute-force throttle weaker than intended) | P2 | fastly-edge-expert | M | — |
| UI-002 | `.pi-health*` styles inlined + divergent across 6 pages — fold one canonical block into gp.css | P2 | ui-expert | M | UI-003 |
| UI-003 | Pi-health SSE consumer hand-rolled in 5 pages, two patterns — add a `GP.initSystemHealth()` helper | P2 | ui-expert | M | — |
| CI-002 | No automated CI runner — `make ci` is manual-only; add a push-to-`main` workflow | P2 | ci-guardian | M | — |
| EDGE-004 | Rate-limit Config Store read is wasm-only; native path is env-only (thresholds untested on native) | P3 | fastly-edge-expert | S | — |
| EDGE-005 | "Property tests in tests/" doc expectation — edge tests are inline, not a `backend/tests/` crate | P3 | fastly-edge-expert | S | — |
| EDGE-007 | Unauthenticated `/api/control` writes the `default` garden's flags (by design — document + ensure dedicated services never expose `default`) | P3 | fastly-edge-expert | S | — |
| PI-004 | `_extract_jpeg` docstring claims a "raw list of ints" path the code doesn't implement (fail-safe; doc drift) | P3 | raspberry-pi-expert | S | — |
| PI-005 | Portal unit-template comment stale: claims "refuses to start without GP_ADMIN_PASSCODE" (now bootstrap mode) | P3 | raspberry-pi-expert | S | — |
| PI-007 | `camera_view.py` binds `0.0.0.0` with no auth (diagnostic tool, not in systemd topology) — default to `127.0.0.1` | P3 | raspberry-pi-expert | S | — |
| CLOUD-006 | CDN read-gate secret compared with non-constant-time VCL string compare (penaltybox mitigates) | P3 | fastly-cloud-expert | S | — |
| CLOUD-007 | `default_garden_name` written to Config Store without charset validation (defense-in-depth; edge escapes) | P3 | fastly-cloud-expert | S | — |
| CLOUD-008 | `rotate-token --prior-token` passed via argv → lands in SSE echo / process list; move to env | P3 | fastly-cloud-expert | S | — |
| CLOUD-009 | Auto-rollback bucket teardown sleeps a fixed 12s for key propagation — replace with a bounded poll | P3 | fastly-cloud-expert | S | — |
| CLOUD-010 | `unregister_device` swallows Secret Store delete failure → deprovisioned garden keeps a live token | P3 | fastly-cloud-expert | S | — |
| TEST-005 | `classify_logits` empty/NaN-logit edge unpinned (safe default unasserted) | P3 | test-engineer | S | — |
| TEST-006 | Rust `gen_tests` omits threshold/command/header consts from its conformance surface | P3 | test-engineer | S | TEST-002 |
| UI-004 | Login/console/portal headers use the leaf emoji, not the `#gp-leaf` sprite | P3 | ui-expert | S | — |
| UI-005 | `gp-alert` and `gp-warn` sprite symbols are byte-identical — alias or drop one | P3 | ui-expert | S | — |
| UI-007 | dashboard `.callout .ic { font-size:22px }` is dead emoji-era CSS (contents are now SVG) | P3 | ui-expert | S | — |
| UX-008 | Theme toggle initial glyph hard-coded ◐ can mismatch state (flash + stale AT label) | P3 | ui-expert | S | — |
| UX-009 | Wizard step rail not exposed as a progress indicator to AT (no `aria-current`, no "Step N of 4") | P3 | ux-designer | S | — |
| UX-010 | `.info` help pills use `title` only; not reliably announced to screen readers / on touch | P3 | ui-expert | S | — |
| CI-004 | `mitigate_threshold` rendered via `str(float)` — fragile cross-language literal if thresholds change | P3 | ci-guardian | S | — |
| CI-005 | `gen.py` does not validate spec inputs — missing/dup keys surface as raw tracebacks | P3 | ci-guardian | S | — |
| PI-003 | Gateway `/frame` archive writes JPEGs with no cap/rotation/retention (dormant — opt-in flag unset) | P3 | raspberry-pi-expert | S | — |
| PI-006 | Self-update has no known-good fallback ref — bad `main` reaches the field on next boot; add a post-pull import gate | P3 | raspberry-pi-expert | M | — |
| EDGE-006 | `archive_evidence` PUT is best-effort with no observable landing (silent bucket failures) | P3 | fastly-edge-expert | M | — |
| TEST-007 | Archive read handlers (CDN-first wiring) have no orchestration test (Viceroy harness or pure `archive_read_plan()`) | P3 | test-engineer | M | — |
| CI-006 | `smoke_test.sh` not wired into any make target — the only true integration smoke is easy to forget | P3 | ci-guardian | S | CI-001 |

### Merge / de-duplication notes
- **EDGE-002 ⊕ CLOUD-005 (purge / cache).** Both flag stale CDN content. CLOUD-005's own handoff
  says the authoritative fix is on the Rust edge and the control plane can only mitigate by
  purging. Kept as two rows (different root causes: EDGE-002 = no post-deploy purge of static/HTML;
  CLOUD-005 = no `CacheOverride::pass()` on the FOS LIST send), both owned by **fastly-edge-expert**
  so the purge isn't added in two places. CLOUD-005 depends on EDGE-002's purge plumbing.
- **TEST-002 ⊕ PI (header SSOT).** TEST-002 is the missing conformance *test* (test-engineer); the
  production *fix* (Pi senders importing `contract_gen.HEADER_*`) is a Pi-tier change folded into
  the same item. Sequence the Pi source change with the test.
- **UI-001 (edge ⊕ pi).** One issue spanning two route handlers: the edge route arm
  (`main.rs:909`) and the Pi portal (`portal.py:1040-1043`) both hardcode `nav-dashboard` for
  `/history`. Owned by **ui-expert**, who coordinates the one-line edge + pi changes and the
  cargo-test update.
- **CI-001 ⊕ EDGE.** The wasm build-step fix is owned by **ci-guardian**; the edge expert confirms
  which wasm-only paths are at risk and that they stay compile-clean.

## Implementation waves

**Wave 1 — independent unblockers (no Depends-on). Ship first; several gate later waves.**
- **CI-001** *(ci-guardian)* — add a `cargo build --release --target wasm32-wasip1` step to
  `make ci`. Sequenced first because every edge change in later waves (EDGE-002, CLOUD-005, the
  UI-001 edge route) should be guarded by a real wasm compile; today the gate can't catch a
  wasm-only break. (Documented sandbox trap: must run with the sandbox disabled.)
- **EDGE-002** *(fastly-edge-expert)* — add post-deploy `purge_all` (+ wait) and Cache-Control on
  HTML routes. Prerequisite for CLOUD-005's wipe/prune purge mitigation, which reuses this plumbing.
  Depends only on CI-001 for the compile guard.
- **TEST-002** *(test-engineer + raspberry-pi-expert)* — the header SSOT conformance test **and**
  the Pi senders' switch to `contract_gen.HEADER_*`. Must precede any `spec.toml` header rename;
  unblocks TEST-006.
- **TEST-001, TEST-003, TEST-004, TEST-005** *(test-engineer)* — independent test-gap fills; the
  most important is TEST-001 (cross-tier disarm contract). Pure tests, zero production coupling.
- **CLOUD-001, CLOUD-003, CLOUD-004, CLOUD-006..010** *(fastly-cloud-expert)* — dependency-free
  control-plane hardening; CLOUD-001 (duplicate-provision) is the headline P1.
- **PI-001, PI-002, PI-003, PI-004, PI-005, PI-006, PI-007** *(raspberry-pi-expert)* — independent
  security/footprint/docs/resilience fixes.
- **UX-001, UX-002, UX-003, UX-004, UX-005, UX-007, UX-008, UX-009, UX-010** — independent a11y /
  copy fixes (copy by ux-designer, markup/CSS by ui-expert). UX-001 reduced-motion is a single
  additive gp.css block.
- **UI-001, UI-003, UI-004, UI-005, UI-007, UX-006** *(ui-expert)* — independent UI fixes. UI-001
  needs coordinated edge + pi one-line route changes plus a cargo-test update.
- **CI-002, CI-003, CI-004, CI-005** *(ci-guardian)* — independent runner / env / contract-robustness
  hardening.

**Wave 2 — depends on Wave 1.**
- **CLOUD-005** *(fastly-edge-expert)* — the authoritative `CacheOverride::pass()` on the FOS LIST
  send + the wipe/prune purge mitigation. Depends on **EDGE-002** (reuses the purge plumbing) and
  benefits from **CI-001** (wasm-gated).
- **UI-002** *(ui-expert)* — fold the canonical `.pi-health*` block into gp.css and strip per-page
  copies. Depends on **UI-003** so the JS consumer is consolidated (and behavior-proven) before the
  styles it targets are moved. Bumps `asset_version` + `make gen`.
- **TEST-006** *(test-engineer)* — extend the Rust `gen_tests` conformance surface to the
  threshold/command/header consts. Depends on **TEST-002** (the header-const baseline).
- **CI-006** *(ci-guardian)* — wire `smoke_test.sh` into a discoverable `smoke:` target. Depends on
  **CI-001** (build-step plumbing precedes a new integration target).

**Wave 3 — long-tail hardening / infrastructure (no blockers, lower payoff).**
- **EDGE-003, EDGE-006** *(fastly-edge-expert)* — M-effort depth-in-defense hardening.
- **PI-006** *(raspberry-pi-expert)* — post-pull import sanity gate / known-good fallback.
- **TEST-007** *(test-engineer)* — Viceroy integration harness for host-ABI route handlers; a
  build-tooling decision sequenced after the pure-layer coverage in Waves 1-2.
- Remaining P3 docs/polish (EDGE-004/005/007, PI-003/004/005/007, CLOUD-006/008/009/010,
  TEST-005, UI-004/005/007, UX-008/009/010, CI-004/005) — independent, batchable cleanup.

## Risks & sequencing notes

- **CI-001 before all edge work.** Until `make ci` compiles wasm, any wasm-only-gated edge change
  (EDGE-002 deploy hook, CLOUD-005 LIST CacheOverride, the UI-001 edge route) can pass CI yet break
  `make build`. Land the build step first; it must run with the sandbox disabled or it tests a
  stale artifact.
- **EDGE-002 → CLOUD-005 ordering / single owner for the cache story.** Both touch the same CDN
  surface from two tiers. Add the `purge_all` in ONE place (deploy/edge owner) and have the
  wipe/prune flow call it — otherwise it gets duplicated across `deploy_edge.py` and the control
  plane. The authoritative CLOUD-005 fix is the edge LIST `CacheOverride::pass()`; the purge is only
  mitigation for the immutable-cached images. Routing both to fastly-edge-expert avoids divergent
  purge logic and stale-listing regressions.
- **Header-rename hazard (TEST-002 must precede any spec.toml header change).** Today a rename of
  `X-Capture-Ts` / `X-Capture-Batch` would break the Pi→edge wire with *all tests green* because the
  Pi senders hardcode the literals. Land the Pi source change (senders use `contract_gen.HEADER_*`)
  **then** the strict conformance test, **before** TEST-006 or any header rename — or the SSOT gate
  is illusory.
- **UI-001 is a two-tier change.** The edge route arm (`main.rs:909`, groups `/ /admin /history`)
  and the Pi portal (`portal.py:1040-1043`) both hardcode `nav-dashboard`; both must pass
  `nav-history` and the existing cargo test at `main.rs:1477-1485` (which asserts the current
  Dashboard-active behavior) must be updated in the same change or it goes red.
- **gp.css changes gate the cache-bust contract.** UX-001, UI-002, UI-005, UI-007, UX-005, UX-008
  and any other gp.css/gp.js edit MUST bump `[ui].asset_version` in `contract/spec.toml` + run
  `make gen`, or the two tiers serve mismatched assets and `test_asset_version.py` /
  `test_contract.py` go red. Batch the gp.css-touching UI/UX items into one asset-version bump and
  pair the deploy with EDGE-002's purge so the reduced-motion / aria fixes actually reach browsers.
- **Shared RateLimiter coupling (PI-001).** The /64-normalization fix lives in `provision/auth.py`,
  shared by the Pi portal and the cloud console. The Pi expert owns the portal call sites, but the
  shared algorithm change must be reviewed by the cloud expert so the console's `locked()` /
  `record_fail()` facade doesn't regress.
- **Safety-label change is label-only (UX-007).** The "Stop Mitigation" → "Pause sprays" relabel
  must NOT alter `apply_control` semantics — it stays a persistent STOP override. Confirm with the
  edge expert that no behavior changes; regression risk is in accidentally implying a transient
  action.
- **No safety regressions in the queue.** Every surviving finding is hardening, footprint, a11y,
  voice, or test/CI coverage. The fail-closed request path (heartbeat, rain veto, cross-garden
  auth, traversal guard) is proven and untouched by this backlog — none of these waves should be
  allowed to weaken it.
