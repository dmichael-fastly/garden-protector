#!/usr/bin/env bash
# sync_to_pi.sh — push the local working tree to the Raspberry Pi over SSH.
#
# Run this ON YOUR MAC after editing files. It rsyncs the repo to the Pi
# (excluding build artifacts, caches, the big ONNX model, and wasm), so the Pi
# gets your latest hardware/client.py, hardware/camera_probe.py, docs, etc. —
# including UNCOMMITTED changes (no git commit/push needed).
#
# Usage:
#   ./scripts/sync_to_pi.sh                          # uses the defaults below
#   ./scripts/sync_to_pi.sh drew@raspberrypi.local   # custom host
#   PI_HOST=drew@10.0.0.42 ./scripts/sync_to_pi.sh   # custom host via env
#   ./scripts/sync_to_pi.sh drew@raspberrypi.local garden-protector  # host + dir
#
# One-time setup (so SSH doesn't prompt for a password):
#   ssh-copy-id drew@raspberrypi.local
set -euo pipefail

PI_HOST="${1:-${PI_HOST:-drew@raspberrypi.local}}"
PI_DIR="${2:-${PI_DIR:-garden-protector}}"   # path on the Pi, relative to ~

# Repo root (this script lives in <repo>/scripts/).
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "[sync] repo:   $REPO_ROOT"
echo "[sync] target: $PI_HOST:~/$PI_DIR"

# Pre-flight: confirm passwordless SSH works (fails fast instead of hanging).
if ! ssh -o BatchMode=yes -o ConnectTimeout=5 "$PI_HOST" true 2>/dev/null; then
  echo "[sync] ERROR: cannot SSH to '$PI_HOST' without a password."
  echo "       Set up key auth once:   ssh-copy-id $PI_HOST"
  echo "       Or pass the right host:  ./scripts/sync_to_pi.sh user@host"
  exit 1
fi

ssh "$PI_HOST" "mkdir -p ~/$PI_DIR"

rsync -avz --human-readable \
  --exclude '.git/' \
  --exclude 'backend/target/' \
  --exclude 'backend/pkg/' \
  --exclude 'backend/bin/' \
  --exclude '**/__pycache__/' \
  --exclude '.pytest_cache/' \
  --exclude '.hypothesis/' \
  --exclude '*.onnx' \
  --exclude '*.wasm' \
  --exclude '.DS_Store' \
  --exclude 'camera_probe_out/' \
  "$REPO_ROOT/" "$PI_HOST:~/$PI_DIR/"

echo ""
echo "[sync] Done. Now, on the Pi:"
echo "    cd ~/$PI_DIR && python3 hardware/camera_probe.py"
