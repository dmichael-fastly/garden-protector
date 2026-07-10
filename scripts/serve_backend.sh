#!/usr/bin/env bash
# serve_backend.sh — build + serve the Fastly Compute backend locally, bound to
# all interfaces so a Raspberry Pi on the LAN can POST evidence to it.
#
# WHY THIS SCRIPT EXISTS:
#   This Mac has TWO Rust installs. `/opt/homebrew/bin/cargo` (Homebrew) comes
#   first on PATH but has NO `wasm32-wasip1` std component, so a bare
#   `fastly compute serve` builds with it and dies on:
#       error[E0463]: can't find crate for `core`
#   The rustup toolchain at ~/.cargo/bin DOES have the target. This script forces
#   the rustup shims onto the front of PATH so the build uses the right cargo.
#   (See AGENTS.md "Known Traps".)
#
# Usage:
#   ./scripts/serve_backend.sh                 # serve on 0.0.0.0:7878 (rebuilds first)
#   ./scripts/serve_backend.sh 0.0.0.0:8080    # custom addr
#   SKIP_BUILD=1 ./scripts/serve_backend.sh    # serve the existing bin/main.wasm as-is
set -euo pipefail

ADDR="${1:-0.0.0.0:7878}"

# Force rustup's cargo/rustc (which has wasm32-wasip1) ahead of Homebrew's.
export PATH="$HOME/.cargo/bin:$PATH"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT/backend"

echo "[serve] cargo:   $(command -v cargo)"
echo "[serve] rustc:   $(rustc --version)"
echo "[serve] addr:    http://$ADDR"

if [[ "${SKIP_BUILD:-0}" == "1" ]]; then
  echo "[serve] SKIP_BUILD=1 -> serving existing bin/main.wasm"
  exec fastly compute serve --skip-build --addr "$ADDR"
fi

# Normal path: let the Fastly CLI build (now using the rustup toolchain) and serve.
# If the CLI's toolchain gate ever rejects the rustc version again, fall back to:
#   cargo build --release --target wasm32-wasip1
#   cp target/wasm32-wasip1/release/garden-protector-backend.wasm bin/main.wasm
#   SKIP_BUILD=1 ./scripts/serve_backend.sh
exec fastly compute serve --addr "$ADDR"
