#!/usr/bin/env bash
# deploy/install.sh — install + enable the Fastly Garden Protector systemd units so the
# project auto-starts on boot and the dashboard is served on :80.
#
# Run this ON THE PI (it uses sudo for the systemd bits):
#   cd ~/garden-protector && ./deploy/install.sh
#
# It renders the unit templates in this directory with THIS machine's user, repo
# path, and python interpreter (preferring the ~/garden-env venv that has
# `requests`), installs them to /etc/systemd/system, and enables them.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # repo root
USER_NAME="${SUDO_USER:-$USER}"
HOME_DIR="$(eval echo "~$USER_NAME")"

# Prefer the project venv python (has `requests`); fall back to system python3.
if [[ -x "$HOME_DIR/garden-env/bin/python3" ]]; then
  PY="$HOME_DIR/garden-env/bin/python3"
else
  PY="$(command -v python3)"
fi

echo "[install] repo:   $DIR"
echo "[install] user:   $USER_NAME"
echo "[install] python: $PY"

if [[ ! -f "$DIR/.env" ]]; then
  echo "[install] WARNING: $DIR/.env not found."
  echo "          cp deploy/.env.example .env  &&  edit it to set GP_ADMIN_PASSCODE + GP_BACKEND"
fi

# Python deps into the venv so the portal/gateway/gp-provision actually run on first
# boot (update.sh also re-syncs these on every boot). Without this a fresh install
# crash-loops on `ModuleNotFoundError: requests` / `typer`.
if [[ -x "$HOME_DIR/garden-env/bin/pip" ]]; then
  echo "[install] installing python deps into garden-env ..."
  "$HOME_DIR/garden-env/bin/pip" install --quiet -r "$DIR/hardware/requirements.txt" || echo "[install] hardware deps install failed (continuing)"
  "$HOME_DIR/garden-env/bin/pip" install --quiet -r "$DIR/provision/requirements.txt" || echo "[install] provision deps install failed (continuing)"
fi

# Camera access for the service user: libcamera/rpicam need /dev/dma_heap/*, which
# default to root-only — so the wizard can't see the CSI camera as a non-root service.
# Grant the `video` group (the service user should be a member) access, persistently.
echo "[install] installing dma_heap udev rule (CSI camera access for the video group) ..."
echo 'SUBSYSTEM=="dma_heap", GROUP="video", MODE="0660"' | sudo tee /etc/udev/rules.d/99-dma-heap.rules >/dev/null
sudo udevadm control --reload-rules 2>/dev/null || true
sudo udevadm trigger --subsystem-match=dma_heap 2>/dev/null || true

# Scoped sudoers so the NON-root portal can bounce ONLY garden-camera (it does this on
# a camera add/edit/remove so the live feed reflects the change — see portal.py
# _restart_camera_daemon). The rule whitelists EXACTLY that one systemctl invocation,
# NOPASSWD, and nothing else — no wildcards, no other unit, no other verb. The portal
# unit also sets NoNewPrivileges=no (sudo is setuid; NNP=true would block the elevation
# even with this rule). Validate with `visudo -c` BEFORE installing so a malformed rule
# can never lock out sudo on the box.
SYSTEMCTL="$(command -v systemctl || echo /usr/bin/systemctl)"
SUDOERS_FILE="/etc/sudoers.d/garden-portal-camera"
SUDOERS_LINE="$USER_NAME ALL=(root) NOPASSWD: $SYSTEMCTL restart --no-block --job-mode=ignore-dependencies garden-camera.service"
echo "[install] installing scoped sudoers rule (portal may restart garden-camera only) ..."
SUDOERS_TMP="$(mktemp)"
printf '# Managed by deploy/install.sh — let the non-root portal bounce ONLY garden-camera.\n%s\n' "$SUDOERS_LINE" > "$SUDOERS_TMP"
if sudo visudo -cf "$SUDOERS_TMP" >/dev/null 2>&1; then
  sudo install -m 0440 -o root -g root "$SUDOERS_TMP" "$SUDOERS_FILE"
  echo "[install] wrote $SUDOERS_FILE"
else
  echo "[install] WARNING: sudoers rule failed validation — NOT installed (live-feed auto-refresh will be skipped)"
fi
rm -f "$SUDOERS_TMP"

render() {  # template -> /etc/systemd/system
  local src="$1" dst="$2"
  sed -e "s|__USER__|$USER_NAME|g" \
      -e "s|__DIR__|$DIR|g" \
      -e "s|__PY__|$PY|g" \
      "$src" | sudo tee "$dst" >/dev/null
  echo "[install] wrote $dst"
}

# garden-update is a oneshot pulled in via the portal/gateway Wants= (no [Install]
# section), so it is rendered but NOT enabled directly.
render "$DIR/deploy/garden-update.service"  /etc/systemd/system/garden-update.service
render "$DIR/deploy/garden-portal.service"  /etc/systemd/system/garden-portal.service
render "$DIR/deploy/garden-gateway.service" /etc/systemd/system/garden-gateway.service
render "$DIR/deploy/garden-camera.service"  /etc/systemd/system/garden-camera.service

sudo systemctl daemon-reload
sudo systemctl enable --now garden-portal.service garden-gateway.service garden-camera.service

echo ""
echo "[install] done. The dashboard should now be at: http://$(hostname).local/  (and http://localhost/ on the Pi)"
echo "[install] portal status:"
sudo systemctl --no-pager --full status garden-portal.service || true
echo ""
echo "[install] logs:  journalctl -u garden-portal -f"
