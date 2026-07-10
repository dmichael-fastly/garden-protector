# Pi deployment — boot autostart + LAN dashboard

On boot the Pi pulls the latest `main` and runs two systemd services:

| Service | Port | What it is |
| --- | --- | --- |
| `garden-update` | — | oneshot: `git reset --hard origin/main` before the services start (best-effort). |
| `garden-portal`  | **80** | LAN **admin** dashboard at `http://raspberrypi.local` (passcode-gated, rate-limited). The thing you open from a phone/laptop on your network. |
| `garden-gateway` | 8088   | Tier-1 gateway: the garden node POSTs here; it forwards to the Fastly edge. |

The portal serves the same dashboard the Fastly edge serves
(`backend/src/dashboard.html`) and proxies its `/api/state`, `/api/snapshot`,
and `/api/control` calls up to the edge (`$GP_BACKEND`). "Manage from the LAN"
lives here; remote, view-only users use the edge dashboard.

## One-time setup (on the Pi)

The pull-on-boot / `restart.sh` flow needs the repo to be a **git clone** on the
Pi (not just an rsync copy), with non-interactive pull access to the private repo.

```bash
# 1. Clone (once). Use a deploy key or a credential helper so `git pull` is
#    non-interactive. With the GitHub CLI:  gh auth login  (then gh auth setup-git)
cd ~ && git clone git@github.com:<you>/garden-protector.git
cd ~/garden-protector

# 2. Python venv with requests (see docs/pi-setup.md)
python3 -m venv --system-site-packages ~/garden-env
~/garden-env/bin/pip install requests gpiozero

# 3. Config — copy the example and set at least GP_ADMIN_PASSCODE + GP_BACKEND
cp deploy/.env.example .env && nano .env      # .env is gitignored; never committed

# 4. Install + enable the services (uses sudo)
./deploy/install.sh
```

> Already have an rsync copy from `scripts/sync_to_pi.sh`? Either `git clone`
> fresh into a new dir and point the services there, or `git init` + add the
> remote in the existing dir. Without a `.git`, pull-on-boot simply no-ops and the
> services run whatever code is on disk (so rsync still works for ad-hoc tests).

## Fast iteration loop

```bash
# on your Mac
git push                       # land changes on main

# on the Pi
./deploy/restart.sh            # pull main + reinstall units + restart everything
```

`install.sh` and `restart.sh` are idempotent — rerun any time.

## Verify

```bash
# on the Pi
curl -s http://localhost/healthz            # {"ok":true,...}
systemctl status garden-portal
journalctl -u garden-update -u garden-portal -f

# from your laptop, in a browser
open http://raspberrypi.local/              # login page -> enter GP_ADMIN_PASSCODE
```

## Notes

- **Pull-on-boot is best-effort:** `deploy/update.sh` always exits 0, so an
  offline Pi (or a missing credential) never blocks the services from starting —
  it just runs the last code on disk. Override the branch with `GP_DEPLOY_BRANCH`.
- **`git reset --hard`** makes the Pi mirror `origin/main` exactly. Your `.env`
  and the `~/garden-env` venv are untracked, so they are preserved. Don't keep
  local commits on the Pi — they'll be discarded on the next pull.
- **Port 80 as non-root:** the portal unit grants `CAP_NET_BIND_SERVICE`, so it
  binds `:80` while still running as your user (not root).
- **HTTPS:** `.local` names can't get a browser-trusted certificate, so the portal
  serves plain HTTP over your trusted LAN by design.
- **`.env` is the single source of truth** — both services read it, and it's
  gitignored. Never commit it.
- **Rate limiting:** 5 failed logins per IP in 60 s → a 5-minute lockout for that
  IP (tunable via `--max-fails/--window-s/--lockout-s`).
```
