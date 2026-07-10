#!/usr/bin/env bash
# deploy/update.sh — pull the latest origin/main into the on-disk repo.
#
# Run on the Pi: directly, by deploy/restart.sh, or as the garden-update.service
# oneshot that runs at boot BEFORE the portal/gateway start. BEST-EFFORT by
# design: if the Pi is offline, isn't a git clone, or the pull fails, it logs and
# exits 0 so boot proceeds with whatever code is already on disk.
#
# Wrapped in main() so a self-update (git reset --hard rewriting this file) can't
# corrupt the running script — bash reads the whole function before executing it.
set -uo pipefail

# sanity_gate REPO_DIR PRIOR_HEAD — verify the just-pulled tree is importable, and
# roll back to PRIOR_HEAD if it is not (PI-006). BEST-EFFORT: returns 0 on every
# path (good pull, bad pull rolled back, or gate skipped) so the caller never aborts
# boot. The check imports the three long-running entrypoints in the SERVICE venv —
# this catches a syntax error, a removed/renamed core module, or a dropped runtime
# dependency (portal imports `requests` + the provision package at module load) that
# would otherwise crash-loop the units against code with no way back.
sanity_gate() {
  local DIR="$1" PRIOR_HEAD="$2"

  # Use the service venv python (the one the units actually run under, with the
  # installed deps); fall back to system python3 so the gate still runs on a dev
  # box. If neither exists we can't validate — skip rather than block (degrade
  # gracefully; an un-importable tree would surface at service start instead).
  local VENV_PY="$(dirname "$DIR")/garden-env/bin/python3"
  local PY=""
  if [ -x "$VENV_PY" ]; then PY="$VENV_PY"
  elif command -v python3 >/dev/null 2>&1; then PY="$(command -v python3)"; fi
  if [ -z "$PY" ]; then
    echo "[update] no python3 available — skipping post-pull sanity gate."
    return 0
  fi

  # Import from the repo root so `import hardware.*` resolves (PYTHONPATH belt; -c
  # already prepends cwd). A non-zero exit = the new tree is broken.
  if PYTHONPATH="$DIR" "$PY" -c 'import hardware.portal, hardware.gateway, hardware.camera_daemon' 2>&1; then
    echo "[update] sanity gate passed: core modules import under $PY."
    return 0
  fi

  echo "[update] SANITY GATE FAILED: the pulled commit does not import cleanly."
  if [ -z "$PRIOR_HEAD" ]; then
    echo "[update] no prior HEAD recorded — cannot roll back; leaving the tree as-is."
    return 0
  fi
  echo "[update] rolling back to last known-good $(git rev-parse --short "$PRIOR_HEAD" 2>/dev/null || echo "$PRIOR_HEAD") ..."
  if git reset --hard "$PRIOR_HEAD"; then
    echo "[update] rolled back to $(git rev-parse --short HEAD): $(git log -1 --pretty=%s)"
  else
    echo "[update] rollback reset failed — using whatever is on disk."
  fi
  return 0
}

main() {
  local DIR BRANCH
  DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  cd "$DIR" || return 0

  if [ ! -d .git ]; then
    echo "[update] $DIR is not a git clone; skipping pull (using on-disk code)."
    echo "         To enable pull-on-boot, clone the repo on the Pi (see deploy/README.md)."
    return 0
  fi

  # PRIOR_HEAD is the known-good ref we're running, captured BEFORE we move HEAD so a
  # bad commit on origin/$BRANCH can be rolled back to exactly what was on disk a
  # moment ago (PI-006). Empty unless a reset actually happens (offline / not-a-clone
  # paths leave it empty and the sanity gate below is a no-op).
  local PRIOR_HEAD=""
  BRANCH="${GP_DEPLOY_BRANCH:-main}"
  echo "[update] fetching origin/$BRANCH ..."
  if git fetch --quiet origin "$BRANCH"; then
    # Captured first: once `git reset --hard` runs, the old HEAD is only reachable
    # via this saved sha. Empty if rev-parse fails (then we skip rollback rather than
    # reset to nothing).
    PRIOR_HEAD="$(git rev-parse --verify --quiet HEAD || true)"
    if git reset --hard "origin/$BRANCH"; then
      echo "[update] now at $(git rev-parse --short HEAD): $(git log -1 --pretty=%s)"
    else
      echo "[update] reset failed — using on-disk code."
      PRIOR_HEAD=""   # nothing moved; nothing to roll back
    fi
  else
    echo "[update] fetch failed (offline / no credentials?) — using on-disk code."
  fi

  # Keep the service venv's Python deps in sync with the pulled code (best-effort;
  # never blocks boot). This is the fix for "portal won't start after a reboot": the
  # portal imports `requests` (and discovery optionally imports `zeroconf`), which
  # must exist in ~/garden-env. pip is idempotent + fast when already satisfied.
  # Runs BEFORE the sanity gate so a commit that legitimately ADDS a runtime dep
  # (and lists it in requirements) is satisfied before we import-check it — otherwise
  # the gate would false-rollback a perfectly good upgrade.
  local VENV_PIP="$(dirname "$DIR")/garden-env/bin/pip"
  if [ -x "$VENV_PIP" ]; then
    # hardware RUNTIME deps (portal/gateway/discovery) + provision deps (gp-provision
    # runs ON the Pi for the self-provisioning wizard: typer/boto3/tenacity). The
    # requirements files carry `>=` floors; constraints.txt pins the known-good
    # versions (CHARTER.md "Pinned toolchains") when present.
    # NOTE (PI-002): hardware/requirements-dev.txt (pytest/responses/hypothesis) is
    # deliberately NOT installed here — those are CI/test-only, never on the field Pi.
    local CONSTRAINTS=()
    [ -f "$DIR/constraints.txt" ] && CONSTRAINTS=(-c "$DIR/constraints.txt")
    for req in hardware/requirements.txt provision/requirements.txt; do
      if [ -f "$DIR/$req" ]; then
        echo "[update] syncing $req into garden-env ..."
        "$VENV_PIP" install --quiet -r "$DIR/$req" "${CONSTRAINTS[@]}" \
          || echo "[update] dep sync ($req) failed — continuing with the existing venv"
      fi
    done
  fi

  # Post-pull sanity gate (PI-006): a broken commit on $BRANCH would otherwise reach
  # the field and the long-running units would only Restart=on-failure against it
  # forever. Now that the venv is synced, verify the freshly-pulled tree at least
  # IMPORTS the core modules; on failure, roll back to PRIOR_HEAD. Best-effort — any
  # problem still ends in exit 0 so boot is never bricked. No-op when nothing was
  # pulled (PRIOR_HEAD empty: offline / no-change / not-a-clone).
  if [ -n "$PRIOR_HEAD" ]; then
    sanity_gate "$DIR" "$PRIOR_HEAD"
  fi
  return 0
}

main "$@"
