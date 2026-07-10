#!/usr/bin/env bash
# deploy/restart.sh — run ON THE PI to pull latest main and restart everything.
#
# The fast iteration loop while developing:
#   (on your Mac)  git push                 # land changes on main
#   (on the Pi)    ./deploy/restart.sh      # pull + reinstall units + restart
#
# Pulls origin/main, re-renders/installs the systemd units (idempotent, picks up
# any unit-template or interpreter changes), then restarts both services.
#
# Wrapped in main() so the in-flight git reset --hard can't corrupt this script
# mid-run (bash reads the whole function before executing it). install.sh/update.sh
# are invoked as fresh processes AFTER the pull, so they always run the new code.
set -uo pipefail

main() {
  local DIR
  DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  echo "[restart] repo: $DIR"

  "$DIR/deploy/update.sh"          # pull latest main (best-effort)
  "$DIR/deploy/install.sh"         # idempotent: re-render units + daemon-reload + enable

  echo "[restart] restarting services ..."
  sudo systemctl restart garden-gateway.service garden-portal.service garden-camera.service
  echo "[restart] done. Dashboard: http://$(hostname).local/"
  sudo systemctl --no-pager --full status garden-portal.service || true
}

main "$@"
