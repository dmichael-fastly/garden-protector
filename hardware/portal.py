#!/usr/bin/env python3
"""hardware/portal.py — the indoor Pi *LAN admin portal* (the human surface).

The garden node talks to the gateway (hardware/gateway.py, :8088) machine-to-machine.
This module is the **human** surface: it serves the same dashboard the Fastly edge
serves (backend/src/dashboard.html), but on the Pi at port 80 so that, from any
device on the home LAN, ``http://raspberrypi.local`` opens the dashboard.

Security model (decided): **you only get to MANAGE the garden from the LAN.** The
portal asks for an admin passcode (set in a ``.env`` on the Pi); a correct passcode
makes you an admin with full control. View-only/remote users go to the Fastly edge
dashboard instead (that surface is view-only + optionally viewer-password gated).

  GET  /            admin cookie -> dashboard; else -> login page
  GET  /login       login page
  POST /login       {passcode} (form or JSON) -> set gp_session cookie -> 303 /
  POST /logout      clear the cookie -> 303 /
  GET  /api/state    (admin) -> proxy edge GET  /api/state
  GET  /api/snapshot (admin) -> proxy edge GET  /api/snapshot
  POST /api/control  (admin) -> proxy edge POST /api/control
  GET  /healthz     unauthenticated liveness (systemd / curl)

The dashboard's existing same-origin ``fetch('/api/state'|'/api/snapshot'|
'/api/control')`` calls land on the portal, which proxies them to the edge with the
forward-compat identity headers (X-Garden-Id / X-Device-Id / X-Node-Id /
X-Garden-Auth) — so the dashboard runs **unmodified** behind the portal.

Design rules carried from the rest of the codebase:
  * **Stdlib + ``requests`` only** (matches hardware/gateway.py's http.server pattern).
  * **Pure, testable cores.** Session mint/verify, passcode compare, and the
    rate-limit decision take the clock as an argument and do no I/O, so they are
    unit-tested directly (see tests/test_portal.py). The HTTP handlers are a thin
    shell over them.
  * **Brute-force resistant.** Failed logins are rate-limited per client IP
    (sliding window -> lockout), so a device on the LAN can't grind the passcode.
"""
import argparse
import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import datetime
import time
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

# The portal lives in hardware/; it now imports the shared SSE plumbing from the
# provision package and its sibling hardware modules package-qualified. Put the repo
# root on sys.path so this works whether launched as `python -m hardware.portal` or
# `python hardware/portal.py`. (Package-qualified imports below avoid the stale
# repo-root client.py shadowing footgun.)
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from hardware import pi_config as pi_cfg  # noqa: E402
from hardware import telemetry            # noqa: E402
from hardware import timelapse as tl      # noqa: E402  (GIF/MP4 export: selection + encoders)
from provision import cost_rates          # noqa: E402  (shared cost model — pure math + rates)
from provision import fastly_api          # noqa: E402  (Secret Store push for the viewer gate)
from provision import streaming           # noqa: E402  (shared sse_event/stream_cli)
from provision import usage_stats         # noqa: E402  (Fastly Stats + FOS inventory fetchers)
from provision.ids import token_secret_name  # noqa: E402
from provision.contract_gen import ASSET_VERSION, render_nav  # noqa: E402  (shared ?v= + nav SSOT)
from provision.auth import (              # noqa: E402  ONE shared LAN-admin auth library
    RateLimiter,                          #            (CHARTER: one Python service lib);
    rate_decision,                        #            re-exported as portal.<name> for callers/tests.
    rate_limit_key,                       #            /64-bucket the brute-force key (PI-001)
    is_lan_addr,
)
from provision.edge_client import EdgeClient  # noqa: E402  ONE shared admin edge proxy client
from provision.envfile import load_env_file   # noqa: E402  ONE shared .env loader (re-exported
#                                                          as portal.load_env_file for callers/tests)

# Secret-Store slot for a garden's OPTIONAL viewer password — parity with the
# backend `VIEWER_PASS_SLOT` (see backend/src/main.rs `viewer_gate_for`).
VIEWER_PASS_SLOT = "viewer_pass"


# .env loading now lives in the shared `provision.envfile` module (imported above as
# `load_env_file`, re-exported as `portal.load_env_file`) so the portal and the admin
# console load the SAME .env the SAME way — see CHARTER "one shared Python service lib".


# ---------------------------------------------------------------------------
# PURE AUTH CORE (no I/O, clock passed in). Exhaustively unit-tested.
# ---------------------------------------------------------------------------

def check_passcode(provided, expected):
    """Constant-time passcode comparison. Empty ``expected`` never matches (so an
    unconfigured passcode can't be satisfied by an empty submission)."""
    if not expected or provided is None:
        return False
    return hmac.compare_digest(str(provided), str(expected))


def mint_session(role, now, ttl, secret):
    """Mint a signed session token: ``base64url(role|exp).hexHMAC``. ``exp`` is an
    absolute epoch-second deadline (``now + ttl``); the HMAC (SHA-256 over the
    base64 payload) makes it unforgeable without ``secret``."""
    exp = int(now) + int(ttl)
    payload = f"{role}|{exp}".encode()
    b = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    mac = hmac.new(_as_bytes(secret), b.encode(), hashlib.sha256).hexdigest()
    return f"{b}.{mac}"


def verify_session(token, now, secret):
    """Return the role from a valid, unexpired token, else ``None``. Verifies the
    HMAC in constant time, then checks expiry. Any malformed token -> ``None``."""
    if not token or "." not in token:
        return None
    b, _, mac = token.partition(".")
    expected = hmac.new(_as_bytes(secret), b.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        return None
    try:
        payload = base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)).decode()
        role, _, exp = payload.partition("|")
        if int(exp) <= int(now):
            return None
        return role or None
    except (ValueError, UnicodeDecodeError):
        return None


def _as_bytes(s):
    return s if isinstance(s, (bytes, bytearray)) else str(s).encode()


def derive_secret(explicit, admin_passcode):
    """The HMAC signing key for session cookies. Use ``explicit`` (GP_PORTAL_SECRET)
    when set; otherwise derive a STABLE key from the admin passcode so sessions
    survive restarts without persisting a secret to disk. Rotating the passcode
    therefore also invalidates outstanding sessions (intended)."""
    if explicit:
        return _as_bytes(explicit)
    return hashlib.sha256(b"garden-portal|v1|" + _as_bytes(admin_passcode)).digest()


# The pure auth cores — `is_lan_addr`, `rate_decision`, and the thread-safe
# `RateLimiter` shell — now live in the shared `provision.auth` module and are
# imported above (re-exported as `portal.<name>`). The stateless session helpers
# (`mint_session`/`verify_session`/`derive_secret`/`check_passcode`) stay here:
# the portal signs cookies statelessly (survives restarts) rather than using the
# console's in-memory SessionStore.


# ---------------------------------------------------------------------------
# DASHBOARD RENDERING — reuse backend/src/dashboard.html verbatim, injecting one
# flag so the SAME file can render with controls (portal/admin) or view-only
# (the edge sets it true; here it is always false).
# ---------------------------------------------------------------------------

def render_dashboard(html, view_only):
    """Inject ``window.GP_VIEW_ONLY`` ahead of the page script so the dashboard can
    hide its admin controls when served read-only. Idempotent-ish: always inserts
    a fresh flag tag right before ``</head>`` (falls back to prepending)."""
    tag = f"<script>window.GP_VIEW_ONLY={'true' if view_only else 'false'};</script>"
    marker = "</head>"
    if marker in html:
        return html.replace(marker, tag + "\n" + marker, 1)
    return tag + html


# The sentinel each page places inside its <header>; the shared header partial
# (hardware/portal_header.html) is spliced in here. Mirrored on the edge by
# backend/src/main.rs (dashboard_header_html). Keep the two in sync.
PORTAL_HEADER_MARKER = "<!--PORTAL_HEADER-->"

# Hard ceiling on a request body the portal will accept. Every POST here is small JSON
# or a tiny form (login passcode, wizard config, settings) — there is no upload path —
# so a few MB is already generous. The Pi is memory-constrained and the /login + wizard
# POSTs are reachable PRE-auth, so an attacker sending a huge (or lying) Content-Length
# could make _read_body over-allocate. We reject >ceiling with 413 WITHOUT reading, and
# cap the actual read so a lying Content-Length can never allocate past the ceiling.
MAX_BODY_BYTES = 2 * 1024 * 1024  # 2 MiB


class _BodyTooLarge(Exception):
    """Raised by _read_body when a request body exceeds MAX_BODY_BYTES. The 413 is
    already sent; do_POST catches this so no handler proceeds to send a second response
    on the (now closing) connection. Internal control-flow only — never surfaced."""


def render_header(template, active, garden_name, view_only=False):
    """Fill the shared header partial: bake the garden name in (HTML-escaped, so it
    paints immediately with no async pop-in/reflow) and render the nav SERVER-SIDE from
    the single nav model (contract_gen.render_nav — same source the edge uses), marking
    the active link. ``view_only`` omits admin links; the Pi portal is admin -> False.

    ``template`` is the raw partial text; ``active`` is the id of the current page's
    nav link (e.g. ``"nav-costs"``). An empty template returns "" so a missing
    partial degrades to "no injected header" rather than crashing."""
    if not template:
        return ""
    out = template.replace("__GARDEN_NAME__", html.escape(garden_name or ""))
    out = out.replace("<!--NAV_LINKS-->", render_nav(active or "", view_only))
    return out


def inject_header(page_html, header_html):
    """Splice the rendered header into a page at PORTAL_HEADER_MARKER. A no-op when
    the marker is absent (e.g. stub HTML in tests), so any page can be passed safely."""
    if PORTAL_HEADER_MARKER in page_html:
        return page_html.replace(PORTAL_HEADER_MARKER, header_html, 1)
    return page_html


def _default_dashboard_path():
    return os.path.normpath(os.path.join(
        os.path.dirname(__file__), "..", "backend", "src", "dashboard.html"))


def _default_timelapse_path():
    return os.path.normpath(os.path.join(
        os.path.dirname(__file__), "..", "backend", "src", "timelapse.html"))


def _default_event_path():
    return os.path.normpath(os.path.join(
        os.path.dirname(__file__), "..", "backend", "src", "event.html"))


def _default_alarms_path():
    return os.path.normpath(os.path.join(
        os.path.dirname(__file__), "..", "backend", "src", "alarms.html"))


def _default_wizard_path():
    return os.path.normpath(os.path.join(os.path.dirname(__file__), "wizard.html"))


def _default_devices_path():
    return os.path.normpath(os.path.join(os.path.dirname(__file__), "devices.html"))


def _default_settings_path():
    return os.path.normpath(os.path.join(os.path.dirname(__file__), "settings.html"))


def _default_costs_path():
    return os.path.normpath(os.path.join(os.path.dirname(__file__), "costs.html"))


def _default_logs_path():
    return os.path.normpath(os.path.join(os.path.dirname(__file__), "logs.html"))


def _default_storage_path():
    return os.path.normpath(os.path.join(os.path.dirname(__file__), "storage.html"))


def _default_help_path():
    return os.path.normpath(os.path.join(os.path.dirname(__file__), "help.html"))


def _default_portal_header_path():
    return os.path.normpath(os.path.join(os.path.dirname(__file__), "portal_header.html"))


FAVICON_ICO = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "favicon.ico"))
# The ONE shared UI asset layer (CHARTER) — served from disk at /static/<name>.
UI_STATIC_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "ui", "static"))


LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fastly Garden Protector — Sign in</title>
<link rel="stylesheet" href="/static/app.css?v=__ASSET_VERSION__">
<link rel="stylesheet" href="/static/gp.css?v=__ASSET_VERSION__">
<script src="/static/gp.js?v=__ASSET_VERSION__"></script>
<style>
  /* gp.css supplies the palette + .card panel + .modal + .theme-toggle; the form field +
     buttons are DaisyUI (.input / .btn) from app.css. Only login layout + copy here. */
  body{{min-height:100vh;display:flex;align-items:center;justify-content:center}}
  .theme-toggle{{position:fixed;top:14px;right:14px;z-index:10}}
  .card{{padding:28px;width:320px;max-width:92vw}}
  h1{{font-size:18px;margin:0 0 4px;font-weight:600}} h1 .leaf{{color:var(--green)}}
  p.sub{{color:var(--muted);font-size:13px;margin:0 0 20px}}
  label{{display:block;font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px}}
  .err{{color:var(--red);font-size:13px;margin-top:14px;min-height:18px}}
  .muted{{color:var(--muted);font-size:13px;text-align:center;margin-top:16px}}
  .muted a{{color:var(--green);cursor:pointer;text-decoration:none}}
  .muted a:hover{{text-decoration:underline}}
  .modal-card h2{{margin:0 0 10px;font-size:16px}}
  .modal-card p{{color:var(--muted);font-size:13px;line-height:1.55;margin:0 0 12px}}
  .modal-card ol{{color:var(--text);font-size:13px;line-height:1.7;margin:0 0 18px;padding-left:20px}}
  .modal-card code{{background:var(--panel-2);border:1px solid var(--border);border-radius:5px;
    padding:1px 6px;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}}
  .modal-card button{{margin-top:0}}
</style></head>
<body><button id="theme-toggle" class="theme-toggle" type="button" aria-label="Switch theme"></button>
<form class="card" method="POST" action="/login">
  <h1><span class="leaf"><svg class="gp-ic" aria-hidden="true"><use href="#gp-leaf"/></svg></span> Fastly Garden Protector</h1>
  <p class="sub">Local admin access — enter the passcode.</p>
  <label for="passcode">Passcode</label>
  <input id="passcode" name="passcode" type="password" class="input w-full" autocomplete="current-password" autofocus required>
  <button type="submit" class="btn btn-primary w-full mt-4">Sign in</button>
  <div class="err">{error}</div>
  <div class="muted"><a onclick="document.getElementById('forgot').style.display='flex';return false">Forgot your passcode?</a></div>
</form>
<div id="forgot" class="modal" onclick="if(event.target===this)this.style.display='none'">
  <div class="modal-card">
    <h2>Reset the admin passcode</h2>
    <p>The passcode lives only on the Pi — there's no email reset. You'll need terminal
       access to clear it; the setup wizard then reopens so you can choose a new one.</p>
    <ol>
      <li>SSH into the Pi: <code>ssh pi@raspberrypi.local</code></li>
      <li>Clear the saved passcode — remove the <code>admin_passcode_hash</code> entry from
          <code>configs/secrets.json</code> (deleting the whole file works too, but that also
          clears your saved Fastly token). If you set <code>GP_ADMIN_PASSCODE</code> in
          <code>.env</code>, remove that line as well.</li>
      <li>Restart the portal: <code>sudo systemctl restart garden-portal</code> (or reboot).</li>
    </ol>
    <button type="button" class="btn w-full" onclick="document.getElementById('forgot').style.display='none'">Got it</button>
  </div>
</div>
<script>GP.initTheme();</script>
</body></html>""".replace("__ASSET_VERSION__", ASSET_VERSION)


# ---------------------------------------------------------------------------
# PORTAL — config + auth glue + the edge proxy. Handlers call into this.
# ---------------------------------------------------------------------------

class Portal:
    SESSION_COOKIE = "gp_session"

    def __init__(self, *, admin_passcode="", session_secret=None, edge="",
                 dashboard_html="", wizard_html="", devices_html="", settings_html="",
                 costs_html="", logs_html="", storage_html="", timelapse_html="",
                 event_html="", alarms_html="", help_html="", portal_header_html="",
                 pi_config=None,
                 garden_id="default", device_id="default", node_id="default",
                 garden_token="", session_ttl=43200, edge_timeout=5.0,
                 rate_limiter=None):
        self.admin_passcode = admin_passcode
        self._secret = session_secret           # explicit secret; else derived lazily
        self.edge = (edge or "").rstrip("/")
        self.dashboard_html = dashboard_html
        self.timelapse_html = timelapse_html
        self.event_html = event_html
        self.alarms_html = alarms_html
        self.wizard_html = wizard_html
        self.devices_html = devices_html
        self.settings_html = settings_html
        self.costs_html = costs_html
        self.logs_html = logs_html
        self.storage_html = storage_html
        self.help_html = help_html
        self.portal_header_html = portal_header_html  # shared header partial (single source)
        self.pi_config = pi_config              # PiConfig or None (None == legacy/normal)
        self.garden_id = garden_id
        self.device_id = device_id
        self.node_id = node_id
        self.garden_token = garden_token
        self.session_ttl = session_ttl
        self.edge_timeout = edge_timeout
        self.rate = rate_limiter or RateLimiter()
        # Pi-local camera daemon (hardware/camera_daemon.py) that owns the cameras
        # and serves the live MJPEG feed. The portal proxies it so the LAN browser
        # only ever talks to the gated portal, never the daemon port directly.
        self.camd_url = os.environ.get("GP_CAMD_URL", "http://127.0.0.1:8090").rstrip("/")
        # Short-TTL cache for the (slow, live) cost gather, keyed by (service, window).
        # A homeowner toggling 7<->30 days or re-opening the page gets an instant
        # answer instead of re-listing the bucket + re-hitting the Stats API.
        self._cost_cache = {}
        # Short-TTL cache for the live storage-footprint listing (an S3 LIST walks
        # every object in the garden prefix + bills Class A ops), so polling/refresh
        # the Storage page doesn't re-list each time. (value, expires_at) or None.
        self._footprint_cache = None
        # Short-TTL cache for the garden name (read off pi-garden.json on every page
        # serve to bake into the header); (value, expires_at) or None.
        self._name_cache = None
        # Short-TTL cache for hot, slow proxied READ endpoints. The dashboard fans out
        # many of these per load AND polls them, each a Pi->Fastly round-trip; caching
        # collapses repeated/concurrent reads (incl. multiple browser tabs) so pages fill
        # fast instead of spinning. {full path+query: (expires_at, (code,ctype,content))}.
        # Only 200s are cached; any mutation (proxy_post) clears it. TTLs per endpoint:
        self._proxy_cache = {}
        self._proxy_cache_ttl = {
            "/api/state": 2.0,         # liveness tiles — 2s is far under the 150s DOWN window
            "/api/cameras": 30.0,      # the camera roster changes rarely
            "/api/gadget": 5.0,        # per-device status pill (keyed incl. ?device=)
            "/api/archive/days": 20.0, # History day chips
            "/api/archive": 15.0,      # a History day's events (keyed incl. ?date=)
            "/api/alarms": 5.0,        # Alarms page data (list + recs); busted on any tag/delete
        }
        # Timelapse GIF/MP4 export (admin only). Renders run one-at-a-time in a daemon
        # thread; job state lives here (GIL-safe dict writes, like the caches above) and
        # the output files in a temp dir that's swept on each new render.
        self.render_jobs = {}          # job_id -> {state, frames_done, total, bytes, format, error, path, created}
        self._render_active = False
        self._render_lock = threading.Lock()   # makes the start_render check-then-set atomic under ThreadingHTTPServer
        self._render_dir = os.path.join(tempfile.gettempdir(), "gp-timelapse")

    def garden_name(self, now=None):
        """The garden's display name, read from pi-garden.json and baked into the
        shared header server-side so it paints with no async reflow. Cached ~60s so
        we don't re-read+parse the config file on every page serve. Fail-soft to ""
        (no pi_config, unreadable file, or unnamed garden -> empty, header reserves
        the space regardless)."""
        now = time.time() if now is None else now
        if self._name_cache and now < self._name_cache[1]:
            return self._name_cache[0]
        name = ""
        if self.pi_config:
            try:
                name = ((self.pi_config.load() or {}).get("garden") or {}).get("name") or ""
            except Exception:
                name = ""
        self._name_cache = (name, now + 60)
        return name

    # -- mode (live; reads pi_config so bootstrap->normal flips without restart) --
    def provisioned(self):
        # No PiConfig (legacy/tests) == already provisioned/normal mode.
        return self.pi_config.is_provisioned() if self.pi_config else True

    def has_passcode(self):
        return bool(self.admin_passcode) or bool(self.pi_config and self.pi_config.has_passcode())

    def bootstrap_mode(self):
        """A fresh Pi has no passcode yet -> serve the OPEN (LAN-only, rate-limited)
        wizard until one is set. Once any passcode exists we're in normal mode."""
        return not self.has_passcode()

    def show_wizard(self):
        """GET / serves the setup wizard until the Pi is provisioned."""
        return not self.provisioned()

    def reload_identity(self):
        """Adopt the freshly-provisioned garden's edge/token/ids from pi-garden.json
        WITHOUT a restart, so post-deploy /api/state|control proxy correctly."""
        if not self.pi_config:
            return
        env = self.pi_config.to_env()
        if env.get("GP_BACKEND"):
            self.edge = env["GP_BACKEND"].rstrip("/")
        self.garden_id = env.get("GP_GARDEN_ID") or self.garden_id
        self.device_id = env.get("GP_DEVICE_ID") or self.device_id
        self.node_id = env.get("GP_NODE_ID") or self.node_id
        self.garden_token = env.get("GP_GARDEN_TOKEN") or self.garden_token

    # -- auth ------------------------------------------------------------
    def _session_secret(self):
        """The HMAC key for session cookies. An explicit value (passed at
        construction, e.g. tests, or GP_PORTAL_SECRET) wins; otherwise derive a
        STABLE key from whatever passcode material exists — plaintext env first,
        then the stored scrypt hash. In bootstrap there is no passcode and no
        session is minted, so the fallback value is never used to verify a real one."""
        if self._secret is not None:
            return self._secret
        explicit = os.environ.get("GP_PORTAL_SECRET")
        if self.admin_passcode:
            return derive_secret(explicit, self.admin_passcode)
        rec = self.pi_config.passcode_record() if self.pi_config else None
        if rec:
            return derive_secret(explicit, "hash:" + rec.get("hash", ""))
        return derive_secret(explicit, "")

    def _verify_passcode(self, passcode):
        if self.admin_passcode:
            return check_passcode(passcode, self.admin_passcode)
        if self.pi_config and self.pi_config.has_passcode():
            return self.pi_config.verify_passcode(passcode)
        return False

    def role_for(self, cookie_value, now=None):
        now = time.time() if now is None else now
        return verify_session(cookie_value, now, self._session_secret())

    def mint_admin_session(self, now=None):
        now = time.time() if now is None else now
        return mint_session("admin", now, self.session_ttl, self._session_secret())

    def try_login(self, ip, passcode, now=None):
        """Rate-limited login. Returns ``(status, value)`` where status is
        ``"ok"`` (value=token), ``"bad"`` (value=None), or ``"locked"``
        (value=retry_after_secs).

        The brute-force budget is keyed by ``rate_limit_key(ip)``, NOT the raw
        address: the portal binds dual-stack IPv6, and a LAN attacker holding a /64
        could otherwise rotate source addresses for a fresh failure budget each
        (PI-001). IPv6 collapses to its /64; IPv4 stays per-host."""
        now = time.time() if now is None else now
        key = rate_limit_key(ip)
        ok, retry = self.rate.allowed(key, now)
        if not ok:
            return "locked", retry
        if self._verify_passcode(passcode):
            self.rate.record_success(key)
            return "ok", mint_session("admin", now, self.session_ttl, self._session_secret())
        self.rate.record_failure(key, now)
        return "bad", None

    # -- edge proxy ------------------------------------------------------
    def _edge(self):
        """A shared EdgeClient bound to the portal's CURRENT garden/device/node/token.
        Built per call so it always reflects post-construction reloads of self.edge /
        self.garden_token (cheap — it only stashes attributes)."""
        return EdgeClient(self.edge, timeout=self.edge_timeout,
                          garden_id=self.garden_id, device_id=self.device_id,
                          node_id=self.node_id, token=self.garden_token)

    def proxy_get(self, path):
        ttl = self._proxy_cache_ttl.get(path.split("?", 1)[0])
        if not ttl:
            return self._edge().proxy_get(path)
        now = time.time()
        hit = self._proxy_cache.get(path)
        if hit and now < hit[0]:
            return hit[1]
        triple = self._edge().proxy_get(path)
        if triple and triple[0] == 200:   # never cache errors/timeouts
            self._proxy_cache[path] = (now + ttl, triple)
        return triple

    def proxy_post(self, path, body, *, timeout=None):
        # Any mutation (control / settings / wipe / prune) can change what the cached
        # reads return, so drop the read cache -> the next poll reflects it immediately.
        self._proxy_cache.clear()
        return self._edge().proxy_post(path, body, timeout=timeout)

    # -- timelapse export (admin only) -----------------------------------
    def _archive_days(self):
        code, _ct, body = self.proxy_get("/api/archive/days")
        if code != 200 or not body:
            return []
        try:
            return (json.loads(body) or {}).get("days", [])
        except (ValueError, TypeError):
            return []

    # The edge clamps ?limit to 1..=1000 and returns no total/truncated field, so a day
    # that returns exactly this many events was almost certainly truncated (a fine capture
    # cadence produces thousands/day). DAY_FETCH_LIMIT lets select_keys flag the render
    # PARTIAL when a day hit this ceiling.
    DAY_FETCH_LIMIT = 1000

    def _archive_events_for_day(self, date):
        code, _ct, body = self.proxy_get(
            "/api/archive?date=" + urllib.parse.quote(date)
            + "&limit=" + str(self.DAY_FETCH_LIMIT))
        if code != 200 or not body:
            return []
        try:
            return (json.loads(body) or {}).get("events", [])
        except (ValueError, TypeError):
            return []

    def _archive_image_bytes(self, key):
        # /api/archive/image isn't in the proxy cache, so this is a direct edge GET that
        # returns raw JPEG bytes. The key must be fully percent-encoded (incl. slashes),
        # exactly like the browser's encodeURIComponent.
        code, _ct, body = self.proxy_get(
            "/api/archive/image?key=" + urllib.parse.quote(key, safe=""))
        return body if code == 200 and body else None

    def _sweep_renders(self, max_age_s=3600):
        """Drop render jobs + their files older than ~1h (called on each new render so no
        extra thread is needed). Best-effort; never raises into the caller."""
        now = time.time()
        # Never sweep a still-RUNNING job: a slow render (e.g. an 1800-frame MP4 pulled
        # over a slow CDN) can outlive max_age_s, and deleting its dict/file mid-render
        # would corrupt the output. Age out only finished/errored jobs.
        stale = [j for j, v in self.render_jobs.items()
                 if v.get("state") != "running" and now - v.get("created", now) > max_age_s]
        for jid in stale:
            v = self.render_jobs.pop(jid, None)
            if v and v.get("path") and os.path.exists(v["path"]):
                try:
                    os.remove(v["path"])
                except OSError:
                    pass
        try:
            for fn in os.listdir(self._render_dir):
                p = os.path.join(self._render_dir, fn)
                if os.path.isfile(p) and now - os.path.getmtime(p) > max_age_s:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
        except OSError:
            pass

    def start_render(self, opts):
        """Kick off a timelapse render in a daemon thread. One at a time: returns
        (job_id, None) on start, or (None, "busy") if a render is already running.

        The check-then-set of ``_render_active`` runs under ``_render_lock`` so two
        near-simultaneous admin POSTs (ThreadingHTTPServer dispatches each on its own
        thread) can't both pass the busy check and launch concurrent renders."""
        with self._render_lock:
            if self._render_active:
                return None, "busy"
            self._render_active = True
        try:
            job_id = "tl_" + secrets.token_hex(6)
            self.render_jobs[job_id] = {"state": "running", "frames_done": 0, "total": 0,
                                        "bytes": 0, "format": opts.get("format", "gif"),
                                        "error": None, "path": None, "created": time.time(),
                                        "capped_days": False, "capped_frames": False,
                                        "capped_partial": False}
            threading.Thread(target=self._run_render, args=(job_id, opts),
                             name="tl-render", daemon=True).start()
        except Exception:                # launch failed: release the flag, don't wedge future renders
            with self._render_lock:
                self._render_active = False
            raise
        return job_id, None

    def _run_render(self, job_id, opts):
        job = self.render_jobs[job_id]
        try:
            self._sweep_renders()
            # If cv2 is unavailable, an MP4 request transparently falls back to the GIF
            # encoder (tl.encode), which caps at the much smaller GIF_MAX_FRAMES. Pick the
            # selection cap to MATCH the encoder that will actually run — otherwise we'd
            # select up to the 1800-frame MP4 cap and then fail LATE in encode_gif at 240
            # with a self-contradictory "too many frames … export MP4" error (after the
            # user already asked for MP4) and ~221 MB buffered for nothing.
            fmt = "mp4" if (opts.get("format") == "mp4" and tl.have_mp4()) else "gif"
            keys, meta = tl.select_keys(
                self._archive_days(), self._archive_events_for_day,
                date_from=opts.get("from"), date_to=opts.get("to"),
                cam=opts.get("cam") or "", action=opts.get("action") or "",
                max_days=tl.MAX_DAYS, max_frames=tl.cap_for(fmt),
                day_cap=self.DAY_FETCH_LIMIT)
            job["total"] = len(keys)
            # Per-frame capture-time labels (parallel to keys) burned onto each frame so a
            # day-spanning timelapse shows the date next to the time. UTC -> garden-local.
            labels = [tl.stamp_label(d, t) for d, t in (meta.get("stamps") or [])]
            # Surface selection caps so the UI can warn the export is partial.
            job["capped_days"] = bool(meta.get("capped_days"))
            job["capped_frames"] = bool(meta.get("capped_frames"))
            job["capped_partial"] = bool(meta.get("capped_partial"))
            if not keys:
                job["state"] = "error"
                job["error"] = "no photos match those filters"
                return
            os.makedirs(self._render_dir, exist_ok=True)
            out = os.path.join(self._render_dir, job_id + ("." + ("mp4" if fmt == "mp4" else "gif")))

            def frames_bytes():
                for i, k in enumerate(keys):
                    b = self._archive_image_bytes(k)
                    if b:
                        label = labels[i] if i < len(labels) else None
                        yield (b, label)

            def on_frame(n):
                job["frames_done"] = n

            # fps/width are validated + bounded to ints at the API boundary
            # (_timelapse_render), so no int() coercion is needed here.
            path = tl.encode(frames_bytes(), out, fmt=fmt,
                             fps=opts.get("fps") or tl.DEFAULT_FPS,
                             width=opts.get("width") or tl.DEFAULT_WIDTH,
                             on_frame=on_frame)
            job["path"] = path
            job["format"] = "mp4" if path.endswith(".mp4") else "gif"   # reflect any fallback
            job["bytes"] = os.path.getsize(path)
            job["state"] = "done"
        except Exception as e:  # noqa: BLE001 - report the failure to the UI, never crash the thread
            job["state"] = "error"
            job["error"] = str(e)
        finally:
            with self._render_lock:
                self._render_active = False

    # -- archive retention (the cron sweep) ------------------------------
    def maintenance_settings(self):
        """Retention-sweep config (non-secret), with safe defaults. Stored under the
        ``maintenance`` key in pi-garden.json so it survives restarts. FOS has no
        lifecycle, so the portal owns the schedule and the edge does the deletion."""
        m = (self.pi_config.load().get("maintenance") if self.pi_config else None) or {}
        days, hour = m.get("retention_days"), m.get("prune_hour")
        return {
            "retention_days": days if isinstance(days, int) and days >= 1 else 30,
            "prune_hour": hour if isinstance(hour, int) and 0 <= hour <= 23 else 3,
            "last_prune_date": m.get("last_prune_date") or None,
        }

    def set_maintenance_settings(self, days, hour):
        days = max(1, min(int(days), 3650))
        hour = max(0, min(int(hour), 23))
        if self.pi_config:
            self.pi_config.save_partial(
                {"maintenance": {"retention_days": days, "prune_hour": hour}})
        return self.maintenance_settings()

    def record_prune_date(self, date_str):
        if self.pi_config:
            self.pi_config.save_partial({"maintenance": {"last_prune_date": date_str}})

    def run_prune(self, days=None, *, trigger="schedule", clock=time.time, max_objects=1000):
        """Retention sweep: delete evidence OLDER than ``days``, DIRECTLY from the Pi via boto3
        (``usage_stats.fos_prune``) — NOT the edge, which caps backend sends per execution (~16)
        and so couldn't clear a large expired day. The cutoff is computed in UTC to match the
        archive key dates. ``max_objects`` bounds one run; the scheduler re-runs while
        ``remaining`` to drain a backlog. Records telemetry (maintenance/archive.prune); never
        raises."""
        cfg = self.maintenance_settings()
        days = cfg["retention_days"] if days is None else max(1, int(days))
        # Cutoff in UTC (archive keys embed UTC dates): days STRICTLY older than this go.
        cutoff = (datetime.datetime.now(datetime.timezone.utc).date()
                  - datetime.timedelta(days=days)).isoformat()
        t0 = clock()
        res = usage_stats.fos_prune(self._fos_svc(), f"g/{self.garden_id}/evidence/",
                                    cutoff, max_objects=max_objects)
        dur = int((clock() - t0) * 1000)
        if res is None:
            telemetry.emit("maintenance", "archive.prune", outcome="fail",
                           detail=f"Cleanup unavailable — no cloud storage [{trigger}]", dur_ms=dur)
            return {"ok": False, "error": "cloud storage not configured"}
        deleted = res.get("deleted", 0)
        days_pruned = res.get("days_pruned", 0)
        failed = res.get("failed", 0)
        remaining = bool(res.get("remaining"))
        sample = res.get("sample")
        detail = (f"Deleted {deleted} old photo{'' if deleted == 1 else 's'} "
                  f"(older than {days} days, {days_pruned} day"
                  f"{'' if days_pruned == 1 else 's'} swept)"
                  f"{f'; {failed} could not be removed' if failed else ''}"
                  f"{'; more remaining' if remaining else ''} [{trigger}]")
        telemetry.emit("maintenance", "archive.prune",
                       outcome="warn" if failed else "ok", detail=detail, dur_ms=dur)
        # A sweep just freed space — drop the cached footprint so the Storage page
        # re-lists and shows the new (smaller) number on the next load.
        if deleted:
            self._footprint_cache = None
        return {"ok": True, "deleted": deleted, "days_pruned": days_pruned,
                "failed": failed, "remaining": remaining, "sample": sample}

    def run_wipe(self, *, trigger="manual", clock=time.time, max_objects=1000):
        """Delete ALL evidence for this garden, DIRECTLY from the Pi via boto3
        (``usage_stats.fos_wipe``) — NOT the edge. The edge can't bulk-delete: Fastly Compute
        caps backend sends per execution (~16), so an edge wipe of thousands of frames only
        managed 16 before the platform refused more. The Pi has no such limit. ``max_objects``
        bounds one call so the browser stays responsive; the UI loops while ``remaining`` is
        true. Records telemetry (maintenance/archive.wipe). Never raises. Caller owns the
        type-the-garden-id confirmation."""
        t0 = clock()
        svc = self._fos_svc()
        res = usage_stats.fos_wipe(svc, f"g/{self.garden_id}/", max_objects=max_objects)
        dur = int((clock() - t0) * 1000)
        if res is None:
            telemetry.emit("maintenance", "archive.wipe", outcome="fail",
                           detail=f"Delete-all unavailable — no cloud storage configured [{trigger}]",
                           dur_ms=dur)
            return {"ok": False, "error": "cloud storage not configured"}
        deleted = res.get("deleted", 0)
        failed = res.get("failed", 0)
        remaining = bool(res.get("remaining"))
        sample = res.get("sample")
        detail = (f"Deleted {deleted} photo{'' if deleted == 1 else 's'}"
                  f"{f'; {failed} could not be removed' if failed else ''}"
                  f"{'; more remaining' if remaining else ''}"
                  f"{f' ({sample})' if (failed and sample) else ''} [{trigger}]")
        telemetry.emit("maintenance", "archive.wipe",
                       outcome="warn" if failed else "ok", detail=detail, dur_ms=dur)
        if deleted:
            self._footprint_cache = None
        return {"ok": True, "deleted": deleted, "failed": failed,
                "remaining": remaining, "sample": sample}

    # -- alarm retention (separate from image/History retention) ----------------
    # Alarms live in ONE edge KV doc (g/<gid>/alarm_log), so the Pi can't boto3-delete them
    # like images — these CALL THE EDGE (POST /api/alarms/{prune,wipe}, token forwarded). The
    # schedule + mode live in pi-garden.json under maintenance.alarm_retention (survive restarts),
    # mirroring the image-archive maintenance settings above.
    def alarm_retention_settings(self):
        """Alarm retention config (non-secret), with safe defaults. mode='days' keeps the last
        N days; mode='count' keeps the newest N alarms."""
        m = ((self.pi_config.load().get("maintenance") if self.pi_config else None) or {}).get(
            "alarm_retention") or {}
        mode = m.get("mode")
        kd, kc, hr = m.get("keep_days"), m.get("keep_count"), m.get("prune_hour")
        return {
            "mode": mode if mode in ("days", "count") else "days",
            "keep_days": kd if isinstance(kd, int) and kd >= 1 else 90,
            "keep_count": kc if isinstance(kc, int) and kc >= 1 else 500,
            "prune_hour": hr if isinstance(hr, int) and 0 <= hr <= 23 else 3,
            "last_alarm_prune_date": m.get("last_alarm_prune_date") or None,
        }

    def set_alarm_retention_settings(self, mode, keep_days=None, keep_count=None, hour=None):
        cur = self.alarm_retention_settings()
        patch = {"mode": mode if mode in ("days", "count") else cur["mode"]}
        if keep_days is not None:
            patch["keep_days"] = max(1, min(int(keep_days), 3650))
        if keep_count is not None:
            patch["keep_count"] = max(1, min(int(keep_count), 100000))
        if hour is not None:
            patch["prune_hour"] = max(0, min(int(hour), 23))
        if self.pi_config:
            self.pi_config.save_partial({"maintenance": {"alarm_retention": patch}})
        return self.alarm_retention_settings()

    def record_alarm_prune_date(self, date_str):
        if self.pi_config:
            self.pi_config.save_partial(
                {"maintenance": {"alarm_retention": {"last_alarm_prune_date": date_str}}})

    def run_alarm_prune(self, *, trigger="schedule", clock=time.time):
        """Alarm retention sweep via the EDGE (the alarm log is KV, not FOS). Forwards the garden
        token through proxy_post. Records telemetry (maintenance/alarm.prune); never raises."""
        cfg = self.alarm_retention_settings()
        mode = cfg["mode"]
        keep = cfg["keep_days"] if mode == "days" else cfg["keep_count"]
        t0 = clock()
        try:
            code, _ct, body = self.proxy_post(f"/api/alarms/prune?mode={mode}&keep={keep}", b"")
        except requests.RequestException:
            telemetry.emit("maintenance", "alarm.prune", outcome="fail",
                           detail=f"edge unreachable [{trigger}]")
            return {"ok": False, "error": "edge unreachable"}
        dur = int((clock() - t0) * 1000)
        if code != 200:
            telemetry.emit("maintenance", "alarm.prune", outcome="fail",
                           detail=f"edge HTTP {code} [{trigger}]", dur_ms=dur)
            return {"ok": False, "status": code}
        try:
            j = json.loads(body)
        except Exception:
            j = {}
        deleted = j.get("deleted", 0)
        detail = (f"Removed {deleted} old alarm{'' if deleted == 1 else 's'} "
                  f"(keep {mode}={keep}) [{trigger}]")
        telemetry.emit("maintenance", "alarm.prune", outcome="ok", detail=detail, dur_ms=dur)
        return {"ok": True, "deleted": deleted, "kept": j.get("kept")}

    def run_alarm_wipe(self, *, trigger="manual", clock=time.time):
        """Delete ALL alarms via the edge (token forwarded). Caller owns the type-to-confirm."""
        t0 = clock()
        try:
            code, _ct, body = self.proxy_post("/api/alarms/wipe", b"")
        except requests.RequestException:
            telemetry.emit("maintenance", "alarm.wipe", outcome="fail",
                           detail=f"edge unreachable [{trigger}]")
            return {"ok": False, "error": "edge unreachable"}
        dur = int((clock() - t0) * 1000)
        if code != 200:
            telemetry.emit("maintenance", "alarm.wipe", outcome="fail",
                           detail=f"edge HTTP {code} [{trigger}]", dur_ms=dur)
            return {"ok": False, "status": code}
        try:
            j = json.loads(body)
        except Exception:
            j = {}
        deleted = j.get("deleted", 0)
        telemetry.emit("maintenance", "alarm.wipe", outcome="ok",
                       detail=f"Deleted {deleted} alarm{'' if deleted == 1 else 's'} [{trigger}]",
                       dur_ms=dur)
        return {"ok": True, "deleted": deleted}

    # -- capture knobs (upload cadence + photo quality = the FOS-cost levers) ----
    def capture_settings(self):
        """How often to upload a photo (``interval_s``) + the photo-quality preset,
        with safe defaults. Stored under the ``capture`` key in pi-garden.json. Fewer,
        smaller photos = less Fastly Object Storage billed (every object bills a 30-day
        minimum, so cadence + size are the real levers — retention below 30 days is not)."""
        if self.pi_config:
            return self.pi_config.capture_settings()
        return {"interval_s": pi_cfg.DEFAULT_INTERVAL_S, "quality": pi_cfg.DEFAULT_QUALITY,
                "daylight_only": pi_cfg.DEFAULT_DAYLIGHT_ONLY,
                "night_mode": pi_cfg.DEFAULT_NIGHT_MODE, "dark_below": pi_cfg.DEFAULT_DARK_BELOW}

    def set_capture_settings(self, interval_s, quality, *, daylight_only=None,
                             night_mode=None, dark_below=None):
        """Persist the cloud-capture knobs. interval_s/quality are always set; the
        daylight-only knobs are written only when provided (partial update)."""
        patch = {
            "interval_s": max(5, min(int(interval_s), 3600)),
            "quality": quality if quality in pi_cfg.QUALITY_PRESETS else pi_cfg.DEFAULT_QUALITY,
        }
        if daylight_only is not None:
            patch["daylight_only"] = bool(daylight_only)
        if night_mode is not None:
            patch["night_mode"] = night_mode if night_mode in pi_cfg.NIGHT_MODES else pi_cfg.DEFAULT_NIGHT_MODE
        if dark_below is not None:
            patch["dark_below"] = max(0, min(int(dark_below), 255))
        if self.pi_config:
            self.pi_config.save_partial({"capture": patch})
        return self.capture_settings()

    # -- per-camera motion trigger (Pi-local detection knobs + monitor zone) -----
    def device_motion_settings(self, device_id):
        """A camera's motion-trigger config (Pi-local) with safe defaults. Read by the camera
        daemon each tick (hot-reload) and by the Gadgets edit modal. See pi_config.DEFAULT_MOTION."""
        if self.pi_config:
            return self.pi_config.motion_settings(device_id)
        return dict(pi_cfg.DEFAULT_MOTION)

    def set_device_motion_settings(self, device_id, raw):
        """Persist a camera's motion-trigger config (validated) under ``motion.<device_id>`` in
        pi-garden.json. Pure-local: the daemon hot-reloads it within a tick (no restart). The edge
        alarm ROLE (can_trigger_alarm) is set separately via the device-edit path so the marker the
        daemon sends actually creates an alarm. Returns the stored, normalized config."""
        norm = pi_cfg.normalize_motion(raw)
        if self.pi_config:
            self.pi_config.save_partial({"motion": {device_id: norm}})
        return norm

    def _camera_count(self):
        """How many cameras feed the cloud (active camera-type devices in pi-garden.json)."""
        if not self.pi_config:
            return 0
        return sum(1 for d in (self.pi_config.load().get("devices") or [])
                   if _is_camera_type(d.get("type")) and (d.get("status") or "active") != "removed")

    def capture_cost_estimates(self):
        """A ballpark monthly-FOS-cost table over every cadence×quality the Storage page
        offers, so the UI can show "~$X/month" live as the user picks (no client-side
        cost math — ONE model in cost_rates). Keyed ``"<interval_s>|<quality>"``. Rough:
        uses typical per-camera photo sizes and the current camera count + retention.

        Each entry also carries an ``ops`` per-operation breakdown (storage / FOS Class A
        upload / KV writes), in the SAME shape + with the SAME unit-rate strings as the
        Costs page op rows, so the estimator shows matching detail. Forward-looking, so
        every row is per-month (no measured window)."""
        configs_dir = str(self.pi_config.dir) if self.pi_config else "configs"
        rates = cost_rates.load_rates(configs_dir)
        ra = rates["class_a_rate_per_1k"]
        kva_rate = rates["kv_class_a_rate_per_100k"]   # KV writes (the per-photo notes)
        r_store = rates["storage_rate_per_gb_month"]
        retention_days = self.maintenance_settings()["retention_days"]
        cameras = self._camera_count()
        table = {}
        for iv in pi_cfg.UPLOAD_INTERVAL_OPTIONS:
            for q in pi_cfg.QUALITY_PRESETS:
                est = cost_rates.estimate_capture_monthly(
                    photos_per_day=(86400.0 / iv) * cameras,
                    bytes_per_photo=pi_cfg.APPROX_UPLOAD_BYTES[q],
                    retention_days=retention_days, rates=rates)
                photos_mo = int(round(est["photos_per_day"] * 30))
                stored_bytes = int(round(est["stored_mb"] * 1024 * 1024))
                ops = [
                    {"label": "Storage (GB-month)", "scope": "this garden", "basis": "estimated",
                     "kind": "bytes", "per_mo": stored_bytes, "rate": _rate_str(r_store, "GB-month"),
                     "cost": est["storage_usd"]},
                    {"label": "Photos uploaded — FOS Class A PUT", "scope": "this garden",
                     "basis": "estimated", "kind": "count", "per_mo": photos_mo,
                     "rate": _rate_str(ra, "1,000 ops"), "cost": est["ops_usd"]},
                    {"label": "Notes — KV Class A writes (2 / photo)", "scope": "this garden",
                     "basis": "estimated", "kind": "count", "per_mo": photos_mo * 2,
                     "rate": _rate_str(kva_rate, "100,000 ops"), "cost": est["kv_ops_usd"]},
                ]
                table[f"{iv}|{q}"] = {
                    "monthly_usd": round(est["monthly_usd"], 4),
                    "photos_per_day": int(round(est["photos_per_day"])),
                    "stored_mb": round(est["stored_mb"], 1),
                    "ops": ops,
                }
        return {"cameras": cameras, "retention_days": retention_days, "table": table}

    def _fos_svc(self):
        """Resolve this garden's FOS credentials (region/bucket/access+secret keys) from the
        provisioned service config — shared by the storage footprint read AND the Pi-side
        bulk wipe. The secret key lives in configs/<sid>.json on the Pi (not the repo)."""
        pc = self.pi_config
        cfg = pc.load() if pc else {}
        fa = cfg.get("fastly") or {}
        service_name = fa.get("service_name")
        svc = (_find_service_cfg(pc.dir, service_name) if (pc and service_name) else None) or {}
        svc.setdefault("service_id", fa.get("service_id"))
        return svc

    def storage_footprint(self):
        """Exact {objects, bytes} currently stored in FOS for THIS garden (the
        ``g/<gid>/`` prefix only — not the account-wide bucket the Costs page reports).
        Reuses the shared S3-listing fetcher (usage_stats.fos_inventory) and the same
        service-resolution the cost summary uses. Degrades to ``available: False``
        (never an error) when the Pi isn't provisioned / mock mode / boto3 is absent.

        Cached briefly: the listing walks every object in the prefix and bills Class A
        LIST ops, so re-opening or refreshing the page must not re-list each time."""
        cached = self._footprint_cache
        if cached and time.time() < cached[1]:
            return cached[0]

        svc = self._fos_svc()
        inv = usage_stats.fos_inventory(svc, garden_prefix=f"g/{self.garden_id}/") \
            if svc.get("service_id") else None
        if inv is None:
            result = {"available": False, "garden_id": self.garden_id,
                      "note": "No cloud storage is set up yet."}
        else:
            result = {"available": True, "garden_id": self.garden_id,
                      "objects": inv["objects"], "bytes": inv["bytes"]}
        self._footprint_cache = (result, time.time() + 120)
        return result


# ---------------------------------------------------------------------------
# HTTP SERVER — thin shell over the Portal object.
# ---------------------------------------------------------------------------

def make_handler(portal):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quiet default access log
            pass

        # -- low-level senders ------------------------------------------
        def _drain_unread_body(self):
            """On a keep-alive connection, an unread request body desyncs the next
            request (it gets parsed as garbage -> 400). Handlers that reject a POST
            early (401/403/404) without reading the body would trip this, so drain
            any leftover Content-Length bytes before we send the response."""
            if getattr(self, "_body_read", False):
                return
            if self.command in ("POST", "PUT", "PATCH"):
                n = int(self.headers.get("Content-Length") or 0)
                if n > 0:
                    try:
                        self.rfile.read(n)
                    except Exception:
                        self.close_connection = True
            self._body_read = True

        def _send(self, code, content_type, body, extra_headers=None):
            self._drain_unread_body()
            if isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra_headers or {}):
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _send_304(self, etag):
            """A 304 must carry no body/Content-Length; _send always writes both, so
            emit the bare revalidation response directly."""
            self._drain_unread_body()
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

        def _send_json(self, code, obj):
            self._send(code, "application/json", json.dumps(obj).encode())

        def _redirect(self, location, set_cookie=None):
            extra = [("Location", location)]
            if set_cookie:
                extra.append(("Set-Cookie", set_cookie))
            self._send(303, "text/plain; charset=utf-8", b"", extra)

        def _client_ip(self):
            return self.client_address[0] if self.client_address else "?"

        def _server_ip(self):
            """The local address THIS connection landed on. Lets is_lan_addr accept a
            global-IPv6 LAN client that shares the server's /64 (the dual-stack listener
            makes raspberrypi.local reachable over the Pi's global IPv6) — see
            provision.auth.is_lan_addr."""
            try:
                return self.connection.getsockname()[0]
            except (OSError, AttributeError, IndexError):
                return None

        def _cookie_value(self, name):
            raw = self.headers.get("Cookie")
            if not raw:
                return None
            try:
                jar = SimpleCookie()
                jar.load(raw)
                return jar[name].value if name in jar else None
            except Exception:
                return None

        def _is_admin(self):
            return portal.role_for(self._cookie_value(Portal.SESSION_COOKIE)) == "admin"

        def _set_cookie_header(self, token, max_age):
            # HttpOnly (no JS access) + SameSite=Lax (no cross-site POSTs). No
            # Secure flag: this is plain HTTP on the trusted LAN by design.
            return (f"{Portal.SESSION_COOKIE}={token}; HttpOnly; SameSite=Lax; "
                    f"Path=/; Max-Age={max_age}")

        def _read_body(self):
            self._body_read = True
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if n < 0:
                n = 0
            if n > MAX_BODY_BYTES:
                # Reject oversized bodies BEFORE reading a single byte — don't drain a
                # multi-GB body just to 413 it. Close the connection (we won't consume
                # the unread body, so the keep-alive stream is desynced). self._body_read
                # is already True so _send's _drain_unread_body won't try to read it. Send
                # the 413 here and RAISE so the caller can't go on to send a 2nd response
                # on the same connection (do_POST catches _BodyTooLarge).
                self.close_connection = True
                self._send_json(413, {"error": "request too large"})
                raise _BodyTooLarge()
            if not n:
                return b""
            # Cap the read length so a LYING Content-Length (header says n, but we never
            # trust it past the ceiling) cannot over-allocate. read() returns only what
            # actually arrives, so a short body is fine; a longer one is truncated at the
            # ceiling, which is already past any legitimate request here.
            return self.rfile.read(min(n, MAX_BODY_BYTES))

        # -- routes -----------------------------------------------------
        # Admin-gated route tables (Phase 2). The dispatcher applies _require_admin()
        # ONCE per category so the gate can't be forgotten when a route is added. The
        # special-cased routes (open assets, "/", the wizard, the bootstrap lockdown, and
        # the login-FALLBACK pages /admin|/history|/login that render the login page
        # instead of a 401) stay explicit below to preserve their bespoke precedence.
        # Values are handler method names, resolved via getattr.
        GET_PROXY_ROUTES = ("/api/state", "/api/snapshot", "/api/cameras", "/api/gadget",
                            "/api/archive", "/api/archive/days", "/api/archive/image",
                            "/api/alarms", "/api/alarm")
        # Full HTML admin pages — unauthed, these render the LOGIN page (not a JSON 401),
        # so signing out / landing on /storage directly shows the login form like /admin.
        GET_PAGE_ROUTES = ("/devices", "/settings", "/costs", "/logs", "/storage")
        GET_ADMIN_ROUTES = {
            "/devices": "_serve_devices", "/settings": "_serve_settings",
            "/costs": "_serve_costs", "/logs": "_serve_logs", "/storage": "_serve_storage",
            "/api/cost": "_cost", "/api/logs": "_get_logs", "/api/maintenance": "_get_maintenance",
            "/api/capture": "_get_capture", "/api/storage/footprint": "_storage_footprint",
            "/api/alarms/settings": "_get_alarm_settings",
            "/api/system/stream": "_serve_system_stream",
            "/api/state/stream": "_serve_state_stream",
            "/api/gadget/snapshot": "_gadget_snapshot", "/api/gadget/status": "_gadget_status",
            "/api/gadget/stream": "_gadget_stream",
            "/api/gadget/motion-settings": "_get_motion_settings",
            "/api/timelapse/status": "_timelapse_status",
            "/api/timelapse/download": "_timelapse_download",
        }
        POST_ADMIN_ROUTES = {
            "/api/timelapse/render": "_timelapse_render",
            "/api/settings/garden/update/stream": "_settings_garden_update_stream",
            "/api/settings/passcode": "_settings_change_passcode",
            "/api/settings/token": "_settings_update_token",
            "/api/settings/viewer-pass": "_settings_viewer_pass",
            "/api/settings/teardown/stream": "_settings_teardown_stream",
            "/api/logs/clear": "_clear_logs",
            "/api/settings/retention": "_settings_retention",
            "/api/settings/capture": "_settings_capture",
            "/api/maintenance/run-now": "_maintenance_run_now",
            "/api/maintenance/wipe-all": "_maintenance_wipe_all",
            "/api/settings/alarm-retention": "_settings_alarm_retention",
            "/api/alarms/run-now": "_alarm_run_now",
            "/api/alarms/wipe-all": "_alarm_wipe_all",
            "/api/gadget/motion-settings": "_set_motion_settings",
            "/api/cost-rates": "_save_cost_rates",
        }

        def do_GET(self):
            self._body_read = False
            route = self.path.split("?")[0]
            # Always-open: liveness + favicon (the wizard + login pages load the icon).
            if route == "/healthz":
                self._send_json(200, {"ok": True, "ts_ms": int(time.time() * 1000)})
                return
            if route == "/favicon.ico":
                self._serve_favicon()
                return
            if route in ("/static/app.css", "/static/gp.css", "/static/gp.js"):
                self._serve_static(route)
                return
            # Help is general, non-sensitive setup content (nav entry is viewer_ok). The Pi
            # portal has no viewer concept (it's admin-passcode gated), so serve /help OPEN —
            # like /login and /static — to anyone on the LAN who taps Help, and even before a
            # passcode is set (it only links the already-open /static assets). Placed above the
            # bootstrap lockdown so it stays reachable during first-run setup too.
            if route == "/help":
                self._serve_help()
                return

            # First-run gate: an unprovisioned Pi serves the setup wizard at /.
            if route == "/":
                if portal.show_wizard():
                    # Bootstrap (no passcode yet) -> the wizard's first step sets one.
                    # Once a passcode exists the operator must sign in to continue the
                    # wizard, so an un-authed GET / shows the login prompt (the page the
                    # wizard reloads into right after the passcode is set).
                    if portal.bootstrap_mode() or self._is_admin():
                        self._serve_wizard()
                    else:
                        self._send(200, "text/html; charset=utf-8", LOGIN_PAGE.format(error=""))
                elif self._is_admin():
                    self._send(200, "text/html; charset=utf-8",
                               render_dashboard(self._with_header(portal.dashboard_html, "nav-dashboard"),
                                                view_only=False))
                else:
                    self._send(200, "text/html; charset=utf-8", LOGIN_PAGE.format(error=""))
                return

            # Wizard API (open in bootstrap mode; session-gated once a passcode exists).
            if route.startswith("/api/wizard/"):
                self._wizard_get(route)
                return

            # Everything below is normal-mode only; lock it down during bootstrap so a
            # fresh Pi exposes ONLY the wizard.
            if portal.bootstrap_mode():
                self._send_json(403, {"error": "setup in progress — finish the wizard"})
                return

            if route in ("/admin", "/history"):
                if self._is_admin():
                    # Same dashboard SPA for both; mark History active SERVER-side on
                    # /history (mirrors the edge's history_header_html) so the right tab
                    # is selected without waiting on the dashboard JS to re-point it.
                    nav = "nav-history" if route == "/history" else "nav-dashboard"
                    self._send(200, "text/html; charset=utf-8",
                               render_dashboard(self._with_header(portal.dashboard_html, nav),
                                                view_only=False))
                else:
                    self._send(200, "text/html; charset=utf-8", LOGIN_PAGE.format(error=""))
            elif route == "/timelapse":
                # Same shared page as the edge, but admin (view_only=False) so the
                # GIF/MP4 export panel shows; nav marks Timelapse active.
                if self._is_admin():
                    self._send(200, "text/html; charset=utf-8",
                               render_dashboard(self._with_header(portal.timelapse_html, "nav-timelapse"),
                                                view_only=False))
                else:
                    self._send(200, "text/html; charset=utf-8", LOGIN_PAGE.format(error=""))
            elif route == "/event":
                # Single-event detail page (reached by a "View details" link, not the nav).
                # Same shared page as the edge; admin on the Pi. History is marked active for
                # orientation since /event has no nav item of its own. Its data comes from the
                # archive proxy routes (/api/archive*, /api/cameras) already in GET_PROXY_ROUTES.
                if self._is_admin():
                    self._send(200, "text/html; charset=utf-8",
                               render_dashboard(self._with_header(portal.event_html, "nav-history"),
                                                view_only=False))
                else:
                    self._send(200, "text/html; charset=utf-8", LOGIN_PAGE.format(error=""))
            elif route == "/alarms":
                # Alarms page (nav-visible). Admin on the Pi (view_only=False) so the per-alarm
                # edit/delete + cleanup controls show; data comes from /api/alarms (proxied).
                if self._is_admin():
                    self._send(200, "text/html; charset=utf-8",
                               render_dashboard(self._with_header(portal.alarms_html, "nav-alarms"),
                                                view_only=False))
                else:
                    self._send(200, "text/html; charset=utf-8", LOGIN_PAGE.format(error=""))
            elif route in self.GET_PAGE_ROUTES:
                # Full HTML admin pages: when unauthed, SHOW THE LOGIN PAGE (like /admin),
                # not a raw {"error":"unauthorized"} JSON 401 — only the /api/* routes below
                # answer JSON (their JS handles the 401). Fixes landing on /storage signed out.
                if self._is_admin():
                    getattr(self, self.GET_ADMIN_ROUTES[route])()
                else:
                    self._send(200, "text/html; charset=utf-8", LOGIN_PAGE.format(error=""))
            elif route == "/login":
                self._send(200, "text/html; charset=utf-8", LOGIN_PAGE.format(error=""))
            elif route in self.GET_PROXY_ROUTES:
                if not self._require_admin():
                    return
                # Forward the FULL path incl. query (?device=, ?date=, ?key=, ?limit=) —
                # `route` is the query-stripped path, so proxy `self.path` instead.
                self._proxy("GET", self.path)
            elif route in self.GET_ADMIN_ROUTES:
                if not self._require_admin():
                    return
                getattr(self, self.GET_ADMIN_ROUTES[route])()
            else:
                self._send_json(404, {"error": "not found"})

        def do_POST(self):
            self._body_read = False
            route = self.path.split("?")[0]
            # _read_body 413s + raises on an oversized body; catch it once here so no
            # handler tries to send a second response on the (closing) connection.
            try:
                self._dispatch_post(route)
            except _BodyTooLarge:
                pass  # 413 already sent inside _read_body; connection set to close

        def _dispatch_post(self, route):
            if route.startswith("/api/wizard/"):
                self._wizard_post(route)
                return
            if portal.bootstrap_mode():
                self._send_json(403, {"error": "setup in progress — finish the wizard"})
                return
            if route == "/login":
                self._handle_login()
            elif route == "/logout":
                self._redirect("/", set_cookie=self._set_cookie_header("", 0))
            elif route == "/api/control":
                if not self._require_admin():
                    return
                self._proxy("POST", route, self._read_body())
            elif route in ("/api/alarm-tag", "/api/alarm/delete"):
                # Alarm determination: tag/retag an alarm, or delete one. The Pi portal is
                # admin-only, so it forwards the garden token -> the edge sees an authorized
                # admin (can change/delete, not just add). proxy_post clears the read cache so
                # the next /api/alarms reflects the change.
                if not self._require_admin():
                    return
                self._proxy("POST", route, self._read_body())
            # Post-setup settings (admin-only, normal mode). The garden edit + teardown
            # stream like the wizard's deploy; passcode/token are quick JSON calls.
            elif route in self.POST_ADMIN_ROUTES:
                if not self._require_admin():
                    return
                getattr(self, self.POST_ADMIN_ROUTES[route])()
            else:
                self._send_json(404, {"error": "not found"})

        do_HEAD = do_GET

        # -- static surfaces --------------------------------------------
        def _serve_wizard(self):
            if portal.wizard_html:
                self._send(200, "text/html; charset=utf-8", portal.wizard_html)
            else:
                self._send(500, "text/html; charset=utf-8", "<h1>wizard.html missing</h1>")

        def _with_header(self, page_html, active):
            """Splice the shared header (single source of truth) into a page, with the
            garden name baked in and the right nav link marked active."""
            return inject_header(
                page_html,
                render_header(portal.portal_header_html, active, portal.garden_name()))

        def _serve_devices(self):
            if portal.devices_html:
                self._send(200, "text/html; charset=utf-8",
                           self._with_header(portal.devices_html, "nav-devices"))
            else:
                self._send_json(404, {"error": "devices page not available"})

        def _serve_settings(self):
            if portal.settings_html:
                self._send(200, "text/html; charset=utf-8",
                           self._with_header(portal.settings_html, "nav-settings"))
            else:
                self._send_json(404, {"error": "settings page not available"})

        def _serve_costs(self):
            if portal.costs_html:
                self._send(200, "text/html; charset=utf-8",
                           self._with_header(portal.costs_html, "nav-costs"))
            else:
                self._send_json(404, {"error": "costs page not available"})

        def _serve_logs(self):
            if portal.logs_html:
                self._send(200, "text/html; charset=utf-8",
                           self._with_header(portal.logs_html, "nav-logs"))
            else:
                self._send_json(404, {"error": "logs page not available"})

        def _serve_storage(self):
            if portal.storage_html:
                self._send(200, "text/html; charset=utf-8",
                           self._with_header(portal.storage_html, "nav-storage"))
            else:
                self._send_json(404, {"error": "storage page not available"})

        def _serve_help(self):
            # Open page (no admin gate). _with_header splices the shared header + nav with
            # nav-help active; __ASSET_VERSION__ was already substituted at load time by
            # _read_file, like every other page template.
            if portal.help_html:
                self._send(200, "text/html; charset=utf-8",
                           self._with_header(portal.help_html, "nav-help"))
            else:
                self._send_json(404, {"error": "help page not available"})

        def _get_maintenance(self):
            """Retention settings + schedule for the Storage page. Recent RUNS are read
            separately via /api/logs?component=maintenance (reuses the telemetry query)."""
            st = portal.maintenance_settings()
            st["edge_configured"] = bool(portal.garden_token)
            # The Storage page's "Delete all photos" modal asks the operator to type the
            # garden id to confirm (matching the teardown flow), so expose it here.
            st["garden_id"] = portal.garden_id
            self._send_json(200, st)

        def _maintenance_run_now(self):
            """Admin 'Run cleanup now' button -> trigger the edge prune immediately."""
            res = portal.run_prune(trigger="manual")
            if res.get("ok"):
                self._send_json(200, res)
            else:
                msg = res.get("error") or f"cleanup failed (HTTP {res.get('status')})"
                self._send_json(502, {"error": msg, **res})

        def _maintenance_wipe_all(self):
            """Admin 'Delete all photos' (Danger zone) -> wipe EVERY photo for this garden.
            Destructive, so it requires typing the garden id exactly to confirm (same guard
            as the garden teardown). The modal warns this won't lower the bill (FOS bills
            ~30 days minimum)."""
            body, err = self._wizard_body()
            if err:
                return
            confirm = str(body.get("confirm") or "").strip()
            if confirm != portal.garden_id:
                self._send_json(400, {"error": f"Type the garden name ({portal.garden_id}) exactly to confirm."})
                return
            res = portal.run_wipe(trigger="manual")
            if res.get("ok"):
                self._send_json(200, res)
            else:
                msg = res.get("error") or f"delete-all failed (HTTP {res.get('status')})"
                self._send_json(502, {"error": msg, **res})

        # -- alarm retention (Alarms-page cleanup card) -----------------
        def _get_alarm_settings(self):
            """Alarm retention config + garden id for the Alarms-page cleanup card."""
            st = portal.alarm_retention_settings()
            st["edge_configured"] = bool(portal.garden_token)
            st["garden_id"] = portal.garden_id
            self._send_json(200, st)

        def _settings_alarm_retention(self):
            """Persist alarm retention mode (days|count) + value + daily sweep hour."""
            body, err = self._wizard_body()
            if err:
                return
            mode = str(body.get("mode", "days")).strip()
            if mode not in ("days", "count"):
                self._send_json(400, {"error": "mode must be 'days' or 'count'"})
                return
            try:
                keep_days = int(body["keep_days"]) if body.get("keep_days") is not None else None
                keep_count = int(body["keep_count"]) if body.get("keep_count") is not None else None
                hour = int(body["prune_hour"]) if body.get("prune_hour") is not None else None
            except (TypeError, ValueError):
                self._send_json(400, {"error": "keep_days/keep_count/prune_hour must be integers"})
                return
            st = portal.set_alarm_retention_settings(mode, keep_days=keep_days,
                                                     keep_count=keep_count, hour=hour)
            telemetry.emit("system", "settings.alarm_retention", outcome="ok",
                           detail=(f"Alarm retention: {st['mode']}="
                                   f"{st['keep_days'] if st['mode'] == 'days' else st['keep_count']}, "
                                   f"daily cleanup at {st['prune_hour']:02d}:00"))
            self._send_json(200, {"ok": True, **st})

        def _get_motion_settings(self):
            """A camera's motion-trigger config for the Gadgets edit modal (?device=<id>)."""
            did = self._query_param("device")
            if not did:
                self._send_json(400, {"error": "device is required"})
                return
            st = portal.device_motion_settings(did)
            st["device_id"] = did
            st["cadence_options"] = list(pi_cfg.MOTION_CADENCE_OPTIONS)
            self._send_json(200, st)

        def _set_motion_settings(self):
            """Persist a camera's motion-trigger config (enabled / cadence / confirm-frames /
            sensitivity / cooldown / monitor-zone ROI). Pi-local; the daemon hot-reloads it within
            a tick. The matching can_trigger_alarm role is saved via the device-edit path."""
            body, err = self._wizard_body()
            if err:
                return
            did = str(body.get("device_id") or body.get("device") or "").strip()
            if not did:
                self._send_json(400, {"error": "device_id is required"})
                return
            raw = body.get("motion") if isinstance(body.get("motion"), dict) else body
            st = portal.set_device_motion_settings(did, raw)
            telemetry.emit("system", "settings.motion", outcome="ok",
                           detail=(f"Motion trigger {did}: {'on' if st['enabled'] else 'off'}, "
                                   f"every {st['cadence_s']}s, {st['confirm_frames']} frames, "
                                   f"zone={'custom' if st['roi'] else 'full frame'}"))
            self._send_json(200, {"ok": True, **st})

        def _alarm_run_now(self):
            """Admin 'Clean up now' for alarms -> trigger the edge alarm prune immediately."""
            res = portal.run_alarm_prune(trigger="manual")
            if res.get("ok"):
                self._send_json(200, res)
            else:
                msg = res.get("error") or f"cleanup failed (HTTP {res.get('status')})"
                self._send_json(502, {"error": msg, **res})

        def _alarm_wipe_all(self):
            """Admin 'Delete all alarms' -> wipe the alarm log (type-the-garden-id to confirm)."""
            body, err = self._wizard_body()
            if err:
                return
            confirm = str(body.get("confirm") or "").strip()
            if confirm != portal.garden_id:
                self._send_json(400, {"error": f"Type the garden name ({portal.garden_id}) exactly to confirm."})
                return
            res = portal.run_alarm_wipe(trigger="manual")
            if res.get("ok"):
                self._send_json(200, res)
            else:
                msg = res.get("error") or f"delete-all failed (HTTP {res.get('status')})"
                self._send_json(502, {"error": msg, **res})

        def _settings_retention(self):
            """Persist the retention window + daily sweep hour (non-secret, in pi-garden.json)."""
            body, err = self._wizard_body()
            if err:
                return
            try:
                days = int(body.get("retention_days"))
                hour = int(body.get("prune_hour"))
            except (TypeError, ValueError):
                self._send_json(400, {"error": "retention_days and prune_hour must be integers"})
                return
            if days < 1 or days > 3650 or hour < 0 or hour > 23:
                self._send_json(400, {"error": "retention_days 1..3650, prune_hour 0..23"})
                return
            st = portal.set_maintenance_settings(days, hour)
            telemetry.emit("system", "settings.retention", outcome="ok",
                           detail=f"Retention set to {st['retention_days']} days, "
                                  f"daily cleanup at {st['prune_hour']:02d}:00")
            self._send_json(200, {"ok": True, **st})

        def _get_capture(self):
            """Cloud-upload cadence + photo quality for the Storage page, plus a
            ballpark monthly-cost table so the UI can show savings live as you pick."""
            st = portal.capture_settings()
            st["presets"] = sorted(pi_cfg.QUALITY_PRESETS)
            st["estimates"] = portal.capture_cost_estimates()
            self._send_json(200, st)

        def _storage_footprint(self):
            """How much this garden is actually storing in the cloud right now
            (photo count + bytes for the g/<gid>/ prefix), so the Storage page can show
            the real footprint next to the retention/cadence levers that change it."""
            self._send_json(200, portal.storage_footprint())

        def _settings_capture(self):
            """Persist how often to upload a photo + the quality preset (non-secret, in
            pi-garden.json). No restart needed — the camera daemon hot-reads these every
            few seconds, so the new cadence/quality apply within moments with no feed
            blip (and without bouncing the stack the way a restart would)."""
            body, err = self._wizard_body()
            if err:
                return
            try:
                interval_s = int(body.get("interval_s"))
            except (TypeError, ValueError):
                self._send_json(400, {"error": "interval_s must be an integer (seconds)"})
                return
            quality = (body.get("quality") or "").strip().lower()
            if quality not in pi_cfg.QUALITY_PRESETS:
                self._send_json(400, {"error": f"quality must be one of {sorted(pi_cfg.QUALITY_PRESETS)}"})
                return
            if interval_s < 5 or interval_s > 3600:
                self._send_json(400, {"error": "interval_s must be 5..3600"})
                return
            # Daylight-only knobs are optional (only present when the Storage-page toggle
            # is shown); omit -> leave the saved value unchanged.
            daylight_only = body.get("daylight_only")
            if daylight_only is not None:
                daylight_only = bool(daylight_only)
            night_mode = body.get("night_mode")
            if night_mode is not None and night_mode not in pi_cfg.NIGHT_MODES:
                self._send_json(400, {"error": f"night_mode must be one of {list(pi_cfg.NIGHT_MODES)}"})
                return
            dark_below = body.get("dark_below")
            if dark_below is not None:
                try:
                    dark_below = max(0, min(int(dark_below), 255))
                except (TypeError, ValueError):
                    self._send_json(400, {"error": "dark_below must be an integer 0..255"})
                    return
            st = portal.set_capture_settings(interval_s, quality, daylight_only=daylight_only,
                                             night_mode=night_mode, dark_below=dark_below)
            telemetry.emit("system", "settings.capture", outcome="ok",
                           detail=f"Saving a photo every {st['interval_s']}s at {st['quality']} "
                                  f"quality; daylight-only {'on' if st['daylight_only'] else 'off'}"
                                  f" (night: {st['night_mode']})")
            self._send_json(200, {"ok": True, **st})

        def _get_logs(self):
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            limit = min(int((qs.get("limit") or ["100"])[0]), 1000)
            offset = max(int((qs.get("offset") or ["0"])[0]), 0)
            component = (qs.get("component") or [""])[0]
            outcome = (qs.get("outcome") or [""])[0]
            cid = (qs.get("cid") or [""])[0]
            q = (qs.get("query") or [""])[0]

            default_db_path = "/var/lib/garden-protector/telemetry.db"
            fallback_db_path = os.path.expanduser("~/.local/state/garden-protector/telemetry.db")
            path = os.environ.get("GP_TELEMETRY_DB") or default_db_path
            if not os.path.exists(path):
                path = fallback_db_path

            if not os.path.exists(path):
                self._send_json(200, {"events": [], "total": 0, "stats": {}})
                return

            try:
                # Read-only, but NOT immutable: the telemetry writer runs WAL mode, and
                # immutable=1 makes SQLite ignore the -wal file. With the main .db barely
                # checkpointed, that surfaces as "no such table: events" (a 500 on the
                # Storage page's Recent-cleanups list). mode=ro reads the WAL too.
                conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                where, params = [], []
                if component:
                    where.append("component = ?")
                    params.append(component)
                if outcome:
                    where.append("outcome = ?")
                    params.append(outcome)
                if cid:
                    where.append("cid = ?")
                    params.append(cid)
                if q:
                    where.append("(detail LIKE ? OR op LIKE ? OR caller LIKE ? OR cid LIKE ?)")
                    like_q = f"%{q}%"
                    params.extend([like_q, like_q, like_q, like_q])

                clause = ("WHERE " + " AND ".join(where)) if where else ""

                count_sql = f"SELECT COUNT(*) FROM events {clause}"
                total = conn.execute(count_sql, params).fetchone()[0]

                rows_sql = (
                    f"SELECT id, ts, cid, garden_id, device_id, node_id, component, op, caller, args, dur_ms, outcome, detail "
                    f"FROM events {clause} ORDER BY ts DESC LIMIT ? OFFSET ?"
                )
                rows_params = params + [limit, offset]
                rows = conn.execute(rows_sql, rows_params).fetchall()

                meta = {}
                try:
                    meta = dict(conn.execute("SELECT k, v FROM meta").fetchall())
                except Exception:
                    pass

                events_list = []
                for r in rows:
                    events_list.append({
                        "id": r[0],
                        "ts": r[1],
                        "cid": r[2],
                        "garden_id": r[3],
                        "device_id": r[4],
                        "node_id": r[5],
                        "component": r[6],
                        "op": r[7],
                        "caller": r[8],
                        "args": json.loads(r[9]) if r[9] else None,
                        "dur_ms": r[10],
                        "outcome": r[11],
                        "detail": r[12]
                    })

                conn.close()
                self._send_json(200, {
                    "events": events_list,
                    "total": total,
                    "stats": {
                        "dropped": int(meta.get("dropped", "0")),
                        "write_errors": int(meta.get("write_errors", "0")),
                    }
                })
            except Exception as e:
                self._send_json(500, {"error": str(e)})

        def _clear_logs(self):
            default_db_path = "/var/lib/garden-protector/telemetry.db"
            fallback_db_path = os.path.expanduser("~/.local/state/garden-protector/telemetry.db")
            path = os.environ.get("GP_TELEMETRY_DB") or default_db_path
            if not os.path.exists(path):
                path = fallback_db_path

            if not os.path.exists(path):
                self._send_json(200, {"ok": True, "count": 0})
                return

            try:
                conn = sqlite3.connect(path, timeout=5.0)
                conn.execute("PRAGMA journal_mode=WAL")
                cur = conn.execute("DELETE FROM events")
                count = cur.rowcount
                conn.commit()
                conn.close()
                self._send_json(200, {"ok": True, "count": count})
            except Exception as e:
                self._send_json(500, {"error": str(e)})

        def _cost(self):
            """Homeowner-friendly cloud-cost summary for this Pi's deployment.

            Reuses the SHARED cost core (provision.cost_rates + usage_stats): measures
            live FOS storage (S3 listing) + Fastly Stats (CDN/Compute/FOS ops) over a
            window and projects an estimated monthly bill. Server maps the raw
            breakdown to plain-language line items so costs.html stays jargon-free.
            Degrades gracefully — an unprovisioned/offline Pi returns available:false
            rather than an error."""
            pc = portal.pi_config
            cfg = pc.load() if pc else {}
            fa = cfg.get("fastly") or {}
            service_name = fa.get("service_name")
            token = pc.get_secret("fastly_api_token") if pc else None
            svc = (_find_service_cfg(pc.dir, service_name) if (pc and service_name) else None) or {}
            # Make sure the service ids are present even if the full {sid}.json wasn't
            # found, so CDN/Compute Stats still resolve from the adopted fastly block.
            svc.setdefault("service_id", fa.get("service_id"))
            if fa.get("cdn_service_id"):
                svc.setdefault("cdn_service_id", fa["cdn_service_id"])

            # Window is a token (1h/24h/7d/30d) or a legacy bare day count; default 30d.
            win = usage_stats.parse_window(self._query_param("window"), default="30d")

            if not svc.get("service_id"):
                self._send_json(200, {"available": False, "window": win["token"],
                                      "window_label": win["label"], "window_days": win["days"],
                                      "note": "No cloud deployment is set up yet."})
                return

            rates = cost_rates.load_rates(pc.dir if pc else ".")
            # The gather is the slow part (S3 listing + Stats API). Cache it briefly,
            # keyed by service + window, so re-opening the page or flipping between the
            # 1h/24h/7d/30d views is instant instead of re-measuring every time.
            ck = (svc.get("service_id"), svc.get("cdn_service_id"), win["token"])
            cached = portal._cost_cache.get(ck)
            now_ts = time.time()
            if cached and now_ts - cached[0] < 120:
                usage = cached[1]
            else:
                # Per-garden: scope FOS inventory to this garden's prefix. Keep the
                # account-wide /stats/aggregate FOS-ops fetch ON so the full operations
                # breakdown can show FOS Class A/B counts (clearly labeled account-wide —
                # the friendly per-garden cards still ignore it and use modeled saves).
                usage = usage_stats.gather_usage(
                    svc, token, win["token"],
                    garden_prefix=f"g/{portal.garden_id}/", include_fos_aggregate=True)
                portal._cost_cache[ck] = (now_ts, usage)

            monthly = cost_rates.compute_monthly(usage, rates)
            by = {l["key"]: l for l in monthly["lines"]}
            u = usage
            proj = cost_rates.HOURS_PER_MONTH / max(1, win["hours"])   # window -> month

            def gb(b):
                g = (b or 0) / (1024 ** 3)
                return f"{g:.2f} GB" if g >= 1 else f"{((b or 0) / (1024 ** 2)):.0f} MB"

            def money(c):
                """Homeowner-friendly dollars: never the bare '$0.0000' of fmt_usd."""
                c = float(c or 0)
                if c <= 0:
                    return "$0.00"
                if c < 0.01:
                    return "less than 1¢"
                return cost_rates.fmt_usd(c)

            def permo(n):
                return round((n or 0) * proj)

            rate_str = _rate_str
            ra = rates["class_a_rate_per_1k"]; rb = rates["class_b_rate_per_10k"]
            r_store = rates["storage_rate_per_gb_month"]; r_eg = rates["cdn_egress_rate_per_gb"]
            r_cmp = rates["compute_rate_per_10k_req"]

            def unavailable(label, icon, blurb):
                return {"label": label, "icon": icon, "detail": "couldn't measure right now",
                        "blurb": blurb, "available": False, "monthly": "—",
                        "help": "We couldn't reach this part of your garden just now, so this "
                                "figure is unavailable. It usually means the garden is offline "
                                "or still being set up — try again in a moment."}

            # Per-garden "saved/mo": this app archives every frame at a known cadence, so
            # model it from cadence × cameras (the SAME basis as the Storage page) instead of
            # the account-wide op count. estimate_capture_monthly gives the matching upload
            # ($ for the PUTs); the storage $ stays the real measured footprint below.
            cap = portal.capture_settings()
            cams = portal._camera_count()
            ret_days = portal.maintenance_settings().get("retention_days")
            ppd = (86400.0 / max(1, cap.get("interval_s") or pi_cfg.DEFAULT_INTERVAL_S)) * cams
            bpp = pi_cfg.APPROX_UPLOAD_BYTES.get(cap.get("quality"), pi_cfg.APPROX_UPLOAD_BYTES["standard"])
            cap_est = cost_rates.estimate_capture_monthly(
                photos_per_day=ppd, bytes_per_photo=bpp, retention_days=ret_days, rates=rates)
            saved_mo = int(round(cap_est["photos_per_day"] * 30))

            # Raw per-operation data — each card lists the Fastly operations that roll up into
            # it. fops = account-wide FOS object-storage ops (Fastly has no per-bucket split);
            # comp/cdn = real per-service counters. KV Store == the renamed Object Store (Stats
            # double-reports identical counts under both names) -> dedup per class via max.
            fos = u.get("fos") or {}
            fops = u.get("fos_ops") or {}
            comp = u.get("compute") or {}
            cdn = u.get("cdn") or {}
            kva_rate = rates["kv_class_a_rate_per_100k"]   # KV writes, $/100k
            kvb_rate = rates["kv_class_b_rate_per_1m"]     # KV reads,  $/1M
            _have_store = any(k in comp for k in
                              ("kv_class_a", "kv_class_b", "object_class_a", "object_class_b"))
            kv_a = max(comp.get("kv_class_a") or 0, comp.get("object_class_a") or 0) if _have_store else None
            kv_b = max(comp.get("kv_class_b") or 0, comp.get("object_class_b") or 0) if _have_store else None

            def op(label, scope, count, cost, *, kind="count", basis="measured",
                   rate=None, star=False):
                """One Fastly-operation detail row under a card. ``per_mo`` projects a measured
                count to a month; 'current'/'modeled' counts are already point-in-time/monthly.
                ``count``/``cost`` None render as '—'. ``rate`` is the human unit price string
                (e.g. "$0.12 / GB"). ``star`` flags a row whose cost IS in the total but is
                account-wide (shown with an asterisk + footnote)."""
                return {"label": label, "scope": scope, "kind": kind, "basis": basis,
                        "count": count,
                        "per_mo": (None if count is None else
                                   (count if basis != "measured" else int(round(count * proj)))),
                        "cost": cost, "rate": rate, "star": star}

            items = []
            card_costs = []   # headline total = sum of the cards the user actually sees

            def add(item, cost, ops=None):
                card_costs.append(cost)
                item["monthly"] = money(cost)
                item["available"] = True
                item["ops"] = ops or []     # the Fastly operations rolled up into this card
                items.append(item)

            # 📸 Photos kept safe = this garden's real stored footprint + the modeled cost of
            #    uploading each new photo at the current cadence.
            if u.get("fos"):
                add({
                    "label": "Photos kept safe", "icon": "📸",
                    "detail": f"{cost_rates.fmt_n(u['fos']['objects'])} kept · {gb(u['fos']['bytes'])} · {cost_rates.fmt_n(saved_mo)} saved/mo",
                    "blurb": "Storing your security photos in the cloud and saving each new one your cameras snap.",
                    "help": ("Every photo your cameras capture is uploaded and kept safely off-site, so it's "
                             "still there even if a camera is taken or damaged. This covers the space those "
                             f"photos use plus the upload of each new one (about {cost_rates.fmt_n(saved_mo)} a "
                             "month at the current pace — far more than are kept at once, because older footage "
                             "is cleared out automatically). Fewer cameras or a shorter history lower it.")},
                    by["storage"]["cost"] + cap_est["ops_usd"] + by["fos_class_a"]["cost"],
                    [op("Photos in storage", "this garden", fos.get("objects"), None,
                        basis="current"),
                     op("Storage used (GB-month)", "this garden", fos.get("bytes"),
                        by["storage"]["cost"], kind="bytes", basis="current",
                        rate=rate_str(r_store, "GB-month")),
                     op("Photos uploaded — FOS Class A PUT", "this garden", saved_mo,
                        cap_est["ops_usd"], basis="modeled", rate=rate_str(ra, "1,000 ops")),
                     op("FOS Class A ops — PUT/POST/COPY/LIST", "account-wide (all services)",
                        fops.get("class_a"), by["fos_class_a"]["cost"],
                        rate=rate_str(ra, "1,000 ops"), star=True)])
            else:
                items.append(unavailable("Photos kept safe", "📸",
                                         "Storing your security photos in the cloud."))
            # 📤 Photo deliveries = the bytes actually sent to your devices (Compute + CDN
            #    egress). Lead with real bytes sent — there's no per-garden "view count" to
            #    measure, and the request count belongs to "Always-on guarding" below.
            if u.get("compute") or u.get("cdn"):
                bw = (((u.get("compute") or {}).get("bandwidth_bytes") or 0)
                      + ((u.get("cdn") or {}).get("bandwidth_bytes") or 0))
                bw_mo = bw * proj
                add({
                    "label": "Photo deliveries", "icon": "📤",
                    "detail": f"{gb(bw_mo)} sent/mo",
                    "blurb": "Sending photos and live views to your phone or browser whenever you check in.",
                    "help": ("Each time you open the dashboard or watch a camera, the photos travel from the "
                             "cloud to your device. You're charged for how much is sent, so checking in often "
                             "or watching high-resolution video nudges this up a little.")},
                    by["cdn_egress"]["cost"] + by["fos_class_b"]["cost"],
                    [op("Data sent — Compute + CDN egress", "this service",
                        (comp.get("bandwidth_bytes") or 0) + (cdn.get("bandwidth_bytes") or 0),
                        by["cdn_egress"]["cost"], kind="bytes", rate=rate_str(r_eg, "GB")),
                     op("FOS Class B ops — GET/HEAD", "account-wide (all services)",
                        fops.get("class_b"), by["fos_class_b"]["cost"],
                        rate=rate_str(rb, "10,000 ops"), star=True)])
            else:
                items.append(unavailable("Photo deliveries", "📤",
                                         "Sending photos and live views to your devices."))
            # 🛡️ Always-on guarding = the cloud brain (Compute requests). Broken out from the
            #    settings/state look-ups + per-photo notes below (real per-service KV/store ops),
            #    so each shows its own count + cost. No FOS ops hidden in either.
            if u.get("compute"):
                checks_mo = permo(u["compute"].get("requests"))
                add({
                    "label": "Always-on guarding", "icon": "🛡️",
                    "detail": f"{cost_rates.fmt_n(checks_mo)} checks/mo",
                    "blurb": "The cloud brain that weighs up every motion alert and decides what to do.",
                    "help": ("When something moves, your garden's cloud brain takes a quick look — is it a "
                             "person, an animal, or nothing worth bothering you about? — and checks your "
                             "settings before acting. Each look is one 'check'. On Fastly's standard plan this "
                             "runs at no extra charge, so you'll usually see $0.00 here — but a custom contract "
                             "that bills per check would change that.")},
                    by["compute"]["cost"],
                    [op("Compute requests", "this service", comp.get("requests"),
                        by["compute"]["cost"], rate=rate_str(r_cmp, "10,000 requests"))])
                # 📝 Notes & look-ups = the per-photo 'latest snapshot' notes the garden writes
                #    plus the settings/state it reads — the real per-service KV/store ops.
                notes_mo = permo((kv_a or 0) + (kv_b or 0))
                add({
                    "label": "Notes & look-ups", "icon": "📝",
                    "detail": f"{cost_rates.fmt_n(notes_mo)} notes & look-ups/mo",
                    "blurb": "Remembering what each camera last saw and checking your settings, so your dashboard always shows the latest.",
                    "help": ("Every saved photo jots down a quick note — its newest snapshot and what was seen — "
                             "and your garden reads its settings and state as it works, so the dashboard stays "
                             "current. Writes and reads bill at different KV rates; on a custom contract that "
                             "bills KV, saving photos more often nudges this up.")},
                    by["store_ops"]["cost"],
                    [op("KV store Class A ops — writes", "this service (was Object Store)", kv_a,
                        cost_rates._price((kv_a or 0) * proj, kva_rate, 100_000),
                        rate=rate_str(kva_rate, "100,000 ops")),
                     op("KV store Class B ops — reads", "this service (was Object Store)", kv_b,
                        cost_rates._price((kv_b or 0) * proj, kvb_rate, 1_000_000),
                        rate=rate_str(kvb_rate, "1,000,000 ops"))])
            else:
                items.append(unavailable("Always-on guarding", "🛡️",
                                         "The always-on cloud brain that watches your garden."))
                items.append(unavailable("Notes & look-ups", "📝",
                                         "Remembering what your cameras see and checking your settings."))

            note = (f"Estimated from the last {win['label']} at Fastly's everyday prices — "
                    "your real bill can wiggle a bit either way. Lines marked * are billed "
                    "account-wide (every service on this Fastly account, not just this garden) — "
                    "Fastly doesn't split object-storage operations per garden, so they're "
                    "included in the total as-is.")
            if win["hours"] < 168:   # under a week of history -> a bouncier projection
                note += " Short windows bounce around more — the 7- or 30-day views give a steadier picture."
            # Headline = sum of the cards the user sees (the modeled-saves $ lives in card 1,
            # not in monthly["total"]), so the total always reconciles with the cards.
            monthly_total = sum(card_costs)
            self._send_json(200, {
                "available": any(i["available"] for i in items),
                "window": win["token"],
                "window_label": win["label"],
                "window_days": win["days"],
                "window_hours": win["hours"],
                "monthly_total": monthly_total,
                "monthly_total_str": money(monthly_total),
                "items": items,            # each carries its own `ops` (the Fastly ops it rolls up)
                "rates": rates,            # so the page can pre-fill the custom-pricing editor
                "note": note,
                "mock": usage_stats.client.is_mock_mode(),
            })

        def _save_cost_rates(self):
            """Persist custom Fastly pricing entered on the Costs page, so an operator
            on a negotiated contract sees their real numbers instead of list/rack rates.
            Shares configs/cost-rates.json with the admin console — one cost model, one
            source of truth. cost_rates.save_rates drops unknown/garbage keys and
            coerces types, so a malformed POST can't corrupt the model."""
            pc = portal.pi_config
            if not pc:
                self._send_json(409, {"error": "no config store yet — set up your garden first"})
                return
            try:
                body = json.loads(self._read_body() or b"{}")
            except ValueError:
                self._send_json(400, {"error": "bad JSON"})
                return
            merged = cost_rates.save_rates(pc.dir, body or {})
            # Cache holds raw usage (not priced), so the very next GET reprices it with
            # the new rates instantly — no need to bust it.
            self._send_json(200, {"ok": True, "rates": merged})

        def _serve_system_stream(self):
            """Stream real-time CPU and Memory percentages via SSE (not polling)."""
            from hardware import sysinfo
            self._sse_start()
            prev_idle, prev_total = None, None
            try:
                while True:
                    cpu_pct, prev_idle, prev_total = sysinfo.cpu_percent(prev_idle, prev_total)
                    mem_pct = sysinfo.memory_percent()
                    payload = {"cpu": cpu_pct, "memory": mem_pct}
                    self.wfile.write(streaming.sse_event(payload))
                    self.wfile.flush()
                    time.sleep(2.0)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            except Exception as e:
                print(f"System stream exception: {e}", file=sys.stderr)
            finally:
                self.close_connection = True

        def _serve_state_stream(self):
            """Stream garden state (armed / mitigation / node / latest event) via SSE so
            the header pills + dashboard update without polling. Mirrors
            _serve_system_stream. Only the Pi portal serves this — the edge can't hold an
            SSE socket within its ~5s Compute budget, so the edge view-only dashboard polls
            /api/state instead (see gp.js GP.subscribeState). Re-uses portal.proxy_get's 2s
            cache, emits only on change, and sends a keepalive comment so a dead client
            surfaces as a write error and the loop exits."""
            self._sse_start()
            last = None
            last_send = time.time()
            try:
                while True:
                    code, _ctype, content = portal.proxy_get("/api/state")
                    state = None
                    if code == 200 and content:
                        try:
                            state = json.loads(content)
                        except (ValueError, TypeError):
                            state = None
                    if state is not None:
                        payload = json.dumps(state, sort_keys=True)
                        now = time.time()
                        if payload != last:
                            self.wfile.write(streaming.sse_event(state))
                            self.wfile.flush()
                            last, last_send = payload, now
                        elif now - last_send >= 15.0:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                            last_send = now
                    time.sleep(2.0)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            except Exception as e:
                print(f"State stream exception: {e}", file=sys.stderr)
            finally:
                self.close_connection = True

        def _serve_favicon(self):
            try:
                with open(FAVICON_ICO, "rb") as f:
                    self._send(200, "image/x-icon", f.read())
            except OSError:
                self._send(404, "image/x-icon", b"")

        def _serve_static(self, route):
            """Serve a shared UI asset (gp.css/gp.js) from ui/static/. Ungated like the
            favicon — stylesheets/scripts aren't sensitive and the login page needs them."""
            name = route.rsplit("/", 1)[-1]
            ctype = "text/css; charset=utf-8" if name.endswith(".css") \
                else "application/javascript; charset=utf-8"
            try:
                with open(os.path.join(UI_STATIC_DIR, name), "rb") as f:
                    body = f.read()
            except OSError:
                self._send(404, "text/plain", b"not found")
                return
            # Content-hash ETag + no-cache: the browser always revalidates (cheap 304
            # when unchanged) but never serves a stale asset after a deploy. No CDN in
            # front of the Pi, so no s-maxage needed here.
            etag = '"%s"' % hashlib.sha256(body).hexdigest()[:16]
            if self.headers.get("If-None-Match") == etag:
                self._send_304(etag)
                return
            self._send(200, ctype, body,
                       extra_headers=[("ETag", etag), ("Cache-Control", "no-cache")])

        # -- gadget views (post-setup: per-device snapshot + live-ish status) -----
        def _query_param(self, name):
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            return (urllib.parse.parse_qs(q).get(name) or [""])[0].strip()

        # -- timelapse export (admin only) ------------------------------
        def _timelapse_render(self):
            """POST: start a GIF/MP4 render of the archive matching the given filters."""
            body = self._read_body()
            try:
                data = json.loads(body) if body else {}
            except (ValueError, TypeError):
                self._send_json(400, {"error": "invalid JSON"})
                return
            # Validate fps/width/dates at the API boundary so bad values 400 here rather
            # than blowing up mid-render in the daemon thread (the browser clamps these,
            # but a direct API call could pass a huge width => OOM, or junk => ValueError).
            def _int_in(val, default, lo, hi):
                try:
                    n = int(val) if val not in (None, "") else default
                except (ValueError, TypeError):
                    return None
                return n if lo <= n <= hi else None
            fps = _int_in(data.get("fps"), tl.DEFAULT_FPS, 1, 60)
            width = _int_in(data.get("width"), tl.DEFAULT_WIDTH, 160, 1920)
            if fps is None or width is None:
                self._send_json(400, {"error": "fps must be 1-60 and width 160-1920"})
                return
            d_from = (data.get("from") or "").strip() or None
            d_to = (data.get("to") or "").strip() or None
            if d_from and d_to and d_from > d_to:
                self._send_json(400, {"error": "'from' date must not be after 'to'"})
                return
            opts = {
                "from": d_from,
                "to": d_to,
                "cam": (data.get("cam") or "").strip(),
                "action": (data.get("action") or "").strip(),
                "format": "mp4" if data.get("format") == "mp4" else "gif",
                "fps": fps,
                "width": width,
            }
            job_id, err = portal.start_render(opts)
            if err == "busy":
                self._send_json(429, {"error": "a render is already running — try again shortly"})
                return
            self._send_json(200, {"job_id": job_id})

        def _timelapse_status(self):
            """GET ?job=ID: render progress for the export poller."""
            job = portal.render_jobs.get(self._query_param("job"))
            if not job:
                self._send_json(404, {"error": "no such job"})
                return
            self._send_json(200, {"state": job["state"], "frames_done": job["frames_done"],
                                  "total": job["total"], "bytes": job["bytes"],
                                  "format": job["format"], "error": job["error"],
                                  "capped_days": job.get("capped_days", False),
                                  "capped_frames": job.get("capped_frames", False),
                                  "capped_partial": job.get("capped_partial", False)})

        def _timelapse_download(self):
            """GET ?job=ID: stream the finished render as a file download. Copied to the
            socket in fixed-size chunks so a large MP4 is never fully buffered in RAM."""
            jid = self._query_param("job")
            job = portal.render_jobs.get(jid)
            if not job or job.get("state") != "done" or not job.get("path") or not os.path.exists(job["path"]):
                self._send_json(404, {"error": "not ready"})
                return
            path = job["path"]
            ext = "mp4" if job["format"] == "mp4" else "gif"
            ctype = "video/mp4" if ext == "mp4" else "image/gif"
            try:
                size = os.path.getsize(path)
                f = open(path, "rb")
            except OSError:
                self._send_json(404, {"error": "not ready"})
                return
            try:
                self._drain_unread_body()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition",
                                 'attachment; filename="timelapse-%s.%s"' % (jid, ext))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if self.command != "HEAD":
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            finally:
                f.close()

        def _gadget_snapshot(self):
            """Proxy the most-recent image for ONE camera gadget (per-garden token is
            attached server-side, never exposed to the browser). camera_pusher.py
            pushes a fresh frame to the edge every few seconds, so the Gadgets page
            polling this gives a near-live view without a streaming server on the Pi.

            Non-default gardens key images per device; the legacy ``default`` garden
            keeps a single shared latest image, so fall back to /api/snapshot there."""
            did = self._query_param("device")
            if not did:
                self._send_json(400, {"error": "device required"})
                return
            gid = portal.garden_id
            if gid and gid != "default":
                path = (f"/api/gardens/{urllib.parse.quote(gid)}"
                        f"/devices/{urllib.parse.quote(did)}/snapshot")
            else:
                path = "/api/snapshot"
            try:
                code, ctype, content = portal.proxy_get(path)
            except requests.RequestException as e:
                print(f"[portal] gadget snapshot proxy failed: {e}", flush=True)
                self._send_json(502, {"error": "edge unreachable"})
                return
            self._send(code, ctype, content, [("Cache-Control", "no-store")])

        def _gadget_status(self):
            """Latest sighting (event) + telemetry for ONE gadget, combined into one
            JSON blob so the Gadgets page can show online/last-seen + the most recent
            thing each gadget saw. Per-device data only exists for non-default gardens;
            on the legacy default garden we return empty (the page degrades to the
            registry status it already has)."""
            did = self._query_param("device")
            if not did:
                self._send_json(400, {"error": "device required"})
                return
            gid = portal.garden_id
            out = {"event": None, "telemetry": None}
            if gid and gid != "default":
                base = (f"/api/gardens/{urllib.parse.quote(gid)}"
                        f"/devices/{urllib.parse.quote(did)}")
                for leaf in ("event", "telemetry"):
                    try:
                        code, _ctype, content = portal.proxy_get(f"{base}/{leaf}")
                        if code == 200 and content:
                            out[leaf] = json.loads(content)
                    except (requests.RequestException, ValueError):
                        pass
            self._send_json(200, out)

        def _gadget_stream(self):
            """Proxy the LIVE MJPEG video for ONE camera from the Pi-local camera
            daemon (hardware/camera_daemon.py) to the browser. This is LAN-direct
            (never through the edge) — the daemon owns the camera and emits a
            multipart/x-mixed-replace stream; we pass it through chunk-by-chunk so
            the <img> on the Gadgets page shows real live video. Pulling through the
            portal keeps it same-origin + admin-gated; the daemon binds Pi-local."""
            did = self._query_param("device")
            if not did:
                self._send_json(400, {"error": "device required"})
                return
            url = f"{portal.camd_url}/stream?device={urllib.parse.quote(did)}"
            try:
                upstream = requests.get(url, stream=True, timeout=(5, None))
            except requests.RequestException as e:
                print(f"[portal] gadget stream proxy failed: {e}", flush=True)
                self._send_json(502, {"error": "camera feed unavailable"})
                return
            try:
                if upstream.status_code != 200:
                    self._send_json(502, {"error": "camera feed not ready"})
                    return
                ctype = upstream.headers.get("Content-Type",
                                             "multipart/x-mixed-replace; boundary=frame")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store, private")
                self.send_header("Pragma", "no-cache")
                self.end_headers()
                for chunk in upstream.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass  # viewer navigated away — normal for a long-lived stream
            finally:
                upstream.close()
                self.close_connection = True

        def _restart_camera_daemon(self, write=None):
            """Bounce garden-camera so a just added/removed/edited camera shows up in
            (or leaves) the live feed without a manual restart — the daemon re-reads
            the registry on start.

            ``--job-mode=ignore-dependencies`` restarts ONLY garden-camera: without it,
            its ``Wants=garden-update.service`` pulls in the git-pull oneshot, which
            cascades into restarting garden-portal too (killing in-flight requests). We
            want just the camera. ``--no-block`` returns immediately; best-effort, so a
            missing unit or no passwordless sudo (dev/tests) is non-fatal.

            PRIVILEGE: the portal runs as a NON-root user, so this `sudo -n` only works
            because deploy installs a scoped NOPASSWD sudoers rule for EXACTLY this
            command (/etc/sudoers.d/garden-portal-camera) AND the portal unit sets
            ``NoNewPrivileges=no`` (sudo is setuid; NNP=true silently blocks the
            elevation, so the refresh would no-op). The argv below MUST stay byte-for-
            byte identical to the sudoers Cmnd entry in deploy/install.sh — change one,
            change both, or sudo rejects it and the live feed stops auto-refreshing.

            NOTE: capture cadence/quality do NOT use this — the daemon hot-reads those
            (see camera_daemon._push_loop); this is only for camera add/remove/edit."""
            try:
                subprocess.run(["sudo", "-n", "systemctl", "restart", "--no-block",
                                "--job-mode=ignore-dependencies", "garden-camera.service"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=10, check=False)
                if write:
                    write(streaming.sse_event(
                        {"line": "Refreshing the live camera feed…", "level": "info"}))
            except Exception as e:  # noqa: BLE001 — never fail the device op on this
                print(f"[portal] camera daemon restart skipped: {e}", flush=True)

        def _require_lan(self):
            """Defense-in-depth LAN gate for the admin surfaces. The passcode stays the
            PRIMARY auth, but deploy/README promises "LAN-only management" — and an
            unfirewalled global IPv6 (GUA) makes /login + the admin routes reachable off
            the LAN, where they'd be guarded by passcode alone. So reject any non-LAN
            client up front (additive — the same two-arg form the wizard already uses,
            which accepts loopback, RFC1918/ULA, link-local, IPv4-mapped, and a global
            IPv6 client that shares the server's /64). Returns True if allowed, else
            sends 403 and returns False."""
            if is_lan_addr(self._client_ip(), self._server_ip()):
                return True
            self._send_json(403, {"error": "local network only"})
            return False

        def _require_admin(self):
            if not self._require_lan():
                return False
            if self._is_admin():
                return True
            self._send_json(401, {"error": "unauthorized"})
            return False

        # -- wizard (first-run) -----------------------------------------
        def _wizard_guard_ok(self):
            """Gate for /api/wizard/*: LAN-only ALWAYS; open in bootstrap mode (no
            passcode exists yet), else an admin session is required."""
            if not is_lan_addr(self._client_ip(), self._server_ip()):
                self._send_json(403, {"error": "local network only"})
                return False
            if portal.bootstrap_mode() or self._is_admin():
                return True
            self._send_json(401, {"error": "auth required"})
            return False

        def _wizard_get(self, route):
            if not self._wizard_guard_ok():
                return
            if route == "/api/wizard/state":
                self._send_json(200, self._wizard_state())
            elif route == "/api/wizard/detect":
                self._wizard_detect()
            elif route == "/api/wizard/provision/stream":
                self._wizard_provision_stream()
            elif route == "/api/wizard/devices/scan":
                self._wizard_devices_scan()
            else:
                self._send_json(404, {"error": "not found"})

        def _wizard_post(self, route):
            if not self._wizard_guard_ok():
                return
            if route == "/api/wizard/passcode":
                self._wizard_set_passcode()
            elif route == "/api/wizard/config":
                self._wizard_save_config()
            elif route == "/api/wizard/token":
                self._wizard_save_token()
            elif route == "/api/wizard/geocode":
                self._wizard_geocode()
            elif route == "/api/wizard/devices/register/stream":
                self._wizard_register_stream()
            elif route == "/api/wizard/devices/unregister/stream":
                self._wizard_unregister_stream()
            elif route == "/api/wizard/devices/edit/stream":
                self._wizard_edit_stream()
            else:
                self._send_json(404, {"error": "not found"})

        def _wizard_body(self):
            try:
                return json.loads(self._read_body() or b"{}"), None
            except ValueError:
                self._send_json(400, {"error": "bad json"})
                return None, True

        def _wizard_state(self):
            """Resumable wizard state — NO secrets (only has_token/has_passcode flags)."""
            cfg = portal.pi_config.load() if portal.pi_config else {}
            g = cfg.get("garden") or {}
            fa = cfg.get("fastly") or {}
            if fa.get("service_name") and portal.pi_config:
                svc = _find_service_cfg(portal.pi_config.dir, fa["service_name"])
                if svc:
                    fa = {**fa, **svc}
            has_token = bool(portal.pi_config and portal.pi_config.get_secret("fastly_api_token"))
            token_preview = None
            if portal.pi_config:
                tok_val = portal.pi_config.get_secret("fastly_api_token")
                if tok_val:
                    token_preview = tok_val[-4:] if len(tok_val) >= 4 else tok_val
            return {
                "provisioned": portal.provisioned(),
                "bootstrap": portal.bootstrap_mode(),
                "has_passcode": portal.has_passcode(),
                "has_token": has_token,
                "has_viewer_pass": bool(portal.pi_config and portal.pi_config.get_secret("viewer_pass")),
                "token_preview": token_preview,
                "step": cfg.get("step"),
                "node_id": cfg.get("node_id"),
                "pi": cfg.get("pi") or {},
                "network": cfg.get("network") or {},
                "garden": {k: g.get(k) for k in
                           ("garden_id", "name", "address", "country", "address_fields",
                            "lat", "lon", "tz", "notes")},
                "fastly": {k: fa.get(k) for k in
                           ("service_id", "service_name", "backend_url", "cdn_url", "region",
                            "active_version", "cdn_service_id")},
                "devices": cfg.get("devices") or [],
            }

        def _wizard_devices_scan(self):
            """Auto-discover cameras (camera_probe) + ESP32 nodes (mDNS + passive)
            for the onboarding step. Best-effort: never errors the page."""
            from hardware import discovery  # lazy: pulls zeroconf only when scanning
            from provision import taxonomy
            gw = os.environ.get("GP_GATEWAY_URL") or os.environ.get("GP_GATEWAY") or "http://127.0.0.1:8088"
            try:
                result = discovery.scan(gateway_url=gw)
            except Exception as e:  # noqa: BLE001
                result = {"cameras": [], "nodes": [], "error": str(e)}
            # Device kind/type vocabulary for the onboarding selects (from taxonomy.py).
            result["taxonomy"] = {
                "kinds": sorted(taxonomy.KINDS),
                "observer_types": sorted(taxonomy.OBSERVER_TYPES),
                "deterrent_types": sorted(taxonomy.DETERRENT_TYPES),
            }
            # Already-onboarded devices so the page can show them as done.
            result["registered"] = portal.pi_config.load().get("devices") or [] if portal.pi_config else []
            self._send_json(200, result)

        def _wizard_detect(self):
            from hardware import sysinfo  # lazy: pulls camera_probe/telemetry only when used
            info = sysinfo.detect()
            # Persist the detected facts, but do NOT advance `step`: detection runs
            # automatically on entry, so advancing here would make a refresh skip the
            # Detect step. The step only moves forward when the USER confirms (submits
            # the Garden form -> "token"), so Detect always waits for "Continue".
            if portal.pi_config:
                portal.pi_config.save_partial({
                    "pi": info.get("pi") or {},
                    "network": info.get("network") or {},
                })
            self._send_json(200, info)

        def _wizard_set_passcode(self):
            body, err = self._wizard_body()
            if err:
                return
            pw = str(body.get("passcode", ""))
            if len(pw) < 6:
                self._send_json(400, {"error": "passcode too short (min 6 characters)"})
                return
            if not portal.pi_config:
                self._send_json(500, {"error": "no config store"})
                return
            portal.pi_config.set_passcode(pw)
            portal.pi_config.save_partial({"step": "detect"})
            # Deliberately DON'T mint a session here. Setting the passcode leaves
            # bootstrap mode, so the wizard reloads into the login prompt and the
            # operator signs in with the passcode they just chose — that re-entry
            # confirms they know it before setup continues.
            self._send_json(200, {"ok": True})

        def _wizard_save_config(self):
            body, err = self._wizard_body()
            if err:
                return
            name = str(body.get("name", "")).strip()
            if not name:
                self._send_json(400, {"error": "garden name required"})
                return
            gid = body.get("garden_id") or pi_cfg.slugify_garden_id(name)
            garden = {
                "garden_id": gid,
                "name": name,
                # `address` is the one-line, country-formatted string the EDGE keeps;
                # `country` + `address_fields` are the structured local-style entry kept
                # Pi-locally so the wizard can re-render the right form on resume.
                "address": body.get("address") or None,
                "country": body.get("country") or None,
                "address_fields": body.get("address_fields") or None,
                "lat": body.get("lat"),
                "lon": body.get("lon"),
                "tz": body.get("tz") or None,
                "notes": body.get("notes") or None,
            }
            patch = {"garden": garden, "step": "token"}
            if body.get("node_id"):
                patch["node_id"] = str(body["node_id"])
            if portal.pi_config:
                portal.pi_config.save_partial(patch)
            self._send_json(200, {"ok": True, "garden_id": gid})

        def _wizard_save_token(self):
            body, err = self._wizard_body()
            if err:
                return
            tok = str(body.get("token", "")).strip()
            if not tok:
                self._send_json(400, {"error": "token required"})
                return
            if not portal.pi_config:
                self._send_json(500, {"error": "no config store"})
                return
            portal.pi_config.set_secret("fastly_api_token", tok)   # 0600; never echoed
            portal.pi_config.save_partial({"step": "deploy"})
            self._send_json(200, {"ok": True})

        def _wizard_geocode(self):
            """Resolve a garden address to {lat, lon} via OpenStreetMap Nominatim so
            the operator never has to know their own coordinates. The Pi proxies the
            call (we set a proper User-Agent per Nominatim's policy and could swap
            providers here later). Setup-time convenience only — failures are soft:
            the wizard still lets the operator type lat/lon by hand or skip them."""
            body, err = self._wizard_body()
            if err:
                return
            # Prefer structured fields (more accurate worldwide); else a free-form q.
            structured = {k: v for k, v in {
                "street": str(body.get("street") or "").strip(),
                "city": str(body.get("city") or "").strip(),
                "state": str(body.get("state") or "").strip(),
                "postalcode": str(body.get("postalcode") or "").strip(),
                "country": str(body.get("country") or "").strip(),   # ISO-2 code or name
            }.items() if v}
            q = str(body.get("q") or "").strip()
            params = {"format": "jsonv2", "limit": "1"}
            if structured:
                params.update(structured)
            elif q:
                params["q"] = q
            else:
                self._send_json(400, {"error": "address required"})
                return
            try:
                r = requests.get(
                    "https://nominatim.openstreetmap.org/search",
                    params=params,
                    headers={"User-Agent": "garden-protector setup wizard (github.com/garden-protector)",
                             "Accept": "application/json"},
                    timeout=8.0,
                )
                r.raise_for_status()
                results = r.json() or []
            except (requests.RequestException, ValueError) as e:
                self._send_json(502, {"error": f"geocoding service unavailable: {e}"})
                return
            if not results:
                self._send_json(404, {"error": "no match found"})
                return
            top = results[0]
            try:
                out = {"ok": True, "lat": float(top["lat"]), "lon": float(top["lon"]),
                       "display_name": top.get("display_name")}
            except (KeyError, TypeError, ValueError):
                self._send_json(502, {"error": "unexpected geocoder response"})
                return
            self._send_json(200, out)

        # -- wizard SSE: the deploy step (real gp-provision over SSE) ----
        def _sse_start(self):
            """Begin a Server-Sent Events response (Content-Length-less stream).
            portal._send can't be used — it always sets Content-Length."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

        def _run_provision_op(self, write, op, params, env_over):
            """Run one gp-provision op as a subprocess, streaming stdout as SSE.
            Token comes via env_over (FASTLY_API_KEY), NEVER argv. Returns the exit
            code, or None if the client disconnected."""
            argv = [sys.executable, "-m", "provision.cli"] + streaming.build_cli_args(op, params)
            env = dict(os.environ)
            env["PYTHONUNBUFFERED"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"   # emoji in progress lines, locale-independent
            env.update(env_over)
            shown = {k: v for k, v in params.items() if v not in (None, "")}
            write(streaming.sse_event(
                {"line": f"$ gp-provision {op} ({', '.join(sorted(shown))})", "level": "cmd"}))
            return streaming.pump_process(argv, env, write)

        def _wizard_provision_stream(self):
            """Deploy: run `provision` then `create-garden` for the named garden,
            streaming both. On success persist service coords + token + provisioned,
            adopt the new edge identity live, and tell the client to redirect.

            >>> This is where the first garden is created entirely from the UI. <<<"""
            pc = portal.pi_config
            cfg = pc.load() if pc else {}
            g = cfg.get("garden") or {}
            gid = g.get("garden_id")
            token = pc.get_secret("fastly_api_token") if pc else None

            self._sse_start()
            tok_bytes = token.encode() if token else b""

            def write(frame):
                if tok_bytes:                       # defensive: never leak the token
                    frame = frame.replace(tok_bytes, b"***")
                self.wfile.write(frame)
                self.wfile.flush()

            def fail(msg, code=-1):
                write(streaming.sse_event({"line": msg, "level": "error"}))
                write(streaming.sse_event({"done": True, "ok": False, "code": code}, event="done"))

            try:
                if not (pc and gid and token):
                    telemetry.emit("system", "provision", outcome="fail", detail="Provisioning failed: Missing details/token")
                    fail("Missing garden details or Fastly token — go back and complete the form.")
                    self.close_connection = True
                    return

                mock = bool(os.environ.get("FASTLY_MOCK_MODE"))
                env_over = {"GP_CONFIGS_DIR": str(pc.dir), "FASTLY_API_KEY": token or "MOCK-TOKEN"}
                if mock:
                    env_over["FASTLY_MOCK_MODE"] = "1"

                fastly = cfg.get("fastly") or {}
                service_name = fastly.get("service_name") or f"garden-protector-{gid}"

                write(streaming.sse_event(
                    {"line": f"Setting up “{g.get('name') or gid}” in the cloud…", "level": "info"}))
                code = self._run_provision_op(write, "provision", {"service_name": service_name}, env_over)
                if code is None:
                    return
                if code != 0:
                    telemetry.emit("system", "provision", outcome="fail", detail=f"Provisioning step 'provision' failed with exit code {code}")
                    fail(f"Provisioning failed (exit {code}).", code)
                    self.close_connection = True
                    return

                svc = _find_service_cfg(pc.dir, service_name)
                if not svc:
                    telemetry.emit("system", "provision", outcome="fail", detail="Provisioning failed: service config not found")
                    fail("Provisioned, but could not locate the service config.")
                    self.close_connection = True
                    return
                sid = svc["service_id"]

                write(streaming.sse_event(
                    {"line": f"Creating your garden “{g.get('name') or gid}”…", "level": "info"}))
                cg_params = {
                    "garden": gid, "name": g.get("name") or gid, "tz": g.get("tz") or "UTC",
                    "address": g.get("address"), "lat": g.get("lat"), "lon": g.get("lon"),
                    "service_id": sid,
                }
                code = self._run_provision_op(write, "create-garden", cg_params, env_over)
                if code is None:
                    return
                if code != 0:
                    telemetry.emit("system", "provision", outcome="fail", detail=f"Provisioning step 'create-garden' failed with exit code {code}")
                    fail(f"Garden creation failed (exit {code}).", code)
                    self.close_connection = True
                    return

                # Persist: garden token -> secrets (0600); service coords + provisioned -> config.
                gtok = _read_garden_token(pc.dir, gid)
                if gtok:
                    pc.set_secret("garden_token", gtok)
                pc.save_partial({
                    "provisioned": True, "step": "devices",
                    "fastly": {
                        "service_id": sid,
                        "service_name": svc.get("service_name"),
                        "backend_url": svc.get("backend_url"),
                        "cdn_url": svc.get("cdn_url"),
                        "region": svc.get("region"),
                    },
                })
                portal.reload_identity()    # proxy to the new edge + garden token, no restart
                write(streaming.sse_event(
                    {"line": "Provisioned and garden created.", "level": "ok"}))
                telemetry.emit("system", "provision", outcome="ok", detail=f"Successfully provisioned service {sid} for garden {gid}")
                write(streaming.sse_event(
                    {"done": True, "ok": True, "code": 0, "redirect": "/devices"}, event="done"))
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True

        def _wizard_register_stream(self):
            """Onboard ONE device over SSE (POST body = the scanned/edited device).
            ENFORCES THE TWO-TIER SPLIT: only the coarse identity
            {device_id,node_id,kind,type,name} goes to the edge via gp-provision
            register-device; the full transport + scan_meta land ONLY in
            pi-garden.json. The body is read BEFORE switching to the SSE response."""
            try:
                body = json.loads(self._read_body() or b"{}")
            except ValueError:
                self._send_json(400, {"error": "bad json"})
                return

            pc = portal.pi_config
            cfg = pc.load() if pc else {}
            g = cfg.get("garden") or {}
            gid = g.get("garden_id")
            sid = (cfg.get("fastly") or {}).get("service_id")
            token = pc.get_secret("fastly_api_token") if pc else None

            did = str(body.get("device_id") or "").strip()
            kind = str(body.get("kind") or "").strip()
            dtype = str(body.get("type") or "").strip()
            node_id = str(body.get("node_id") or cfg.get("node_id") or "pi-01").strip()
            name = str(body.get("name") or did).strip()
            transport = body.get("transport") or {}      # Pi-local ONLY
            scan_meta = body.get("scan_meta") or {}       # Pi-local ONLY

            self._sse_start()
            tok_bytes = token.encode() if token else b""

            def write(frame):
                if tok_bytes:
                    frame = frame.replace(tok_bytes, b"***")
                self.wfile.write(frame)
                self.wfile.flush()

            def fail(msg, code=-1):
                write(streaming.sse_event({"line": msg, "level": "error"}))
                write(streaming.sse_event({"done": True, "ok": False, "code": code}, event="done"))

            try:
                if not (pc and gid and sid and token):
                    telemetry.emit("system", "device.register", outcome="fail", detail="Registration failed: Pi not provisioned")
                    fail("Pi is not provisioned yet — finish the deploy step first.")
                    self.close_connection = True
                    return
                if not (did and kind and dtype):
                    telemetry.emit("system", "device.register", outcome="fail", detail="Registration failed: missing required fields")
                    fail("device_id, kind and type are required.")
                    self.close_connection = True
                    return

                env_over = {"GP_CONFIGS_DIR": str(pc.dir), "FASTLY_API_KEY": token}
                # Reuse the garden's existing token so adding this device does NOT
                # rotate it (rotating would knock the gateway + already-added cameras
                # offline — they all share the one per-garden token). Passed via env,
                # not argv. Absent only for the first device in a brand-new garden.
                existing_gtok = pc.get_secret("garden_token") if pc else None
                if existing_gtok:
                    env_over["GP_EXISTING_GARDEN_TOKEN"] = existing_gtok
                if os.environ.get("FASTLY_MOCK_MODE"):
                    env_over["FASTLY_MOCK_MODE"] = "1"

                # COARSE-ONLY params to the edge (transport is deliberately NOT here).
                params = {"garden": gid, "device": did, "kind": kind, "type": dtype,
                          "node": node_id, "name": name, "service_id": sid}
                code = self._run_provision_op(write, "register-device", params, env_over)
                if code is None:
                    return
                if code != 0:
                    telemetry.emit("system", "device.register", outcome="fail", detail=f"Device registration failed with exit code {code}")
                    fail(f"Device registration failed (exit {code}).", code)
                    self.close_connection = True
                    return

                # Persist the DETAILED device locally (transport/scan_meta stay on the Pi).
                _upsert_local_device(pc, {
                    "device_id": did, "name": name, "kind": kind, "type": dtype,
                    "node_id": node_id, "enabled": True, "status": "active",
                    "transport": transport, "scan_meta": scan_meta, "last_seen_ts": 0,
                })
                write(streaming.sse_event({"line": f"Registered '{name}'.", "level": "ok"}))
                telemetry.emit("system", "device.register", outcome="ok", detail=f"Registered device {did} ({name}) of type {dtype}")
                if _is_camera_type(dtype):
                    self._restart_camera_daemon(write)   # show the new camera live now
                write(streaming.sse_event(
                    {"done": True, "ok": True, "code": 0, "device_id": did}, event="done"))
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True

        def _wizard_unregister_stream(self):
            """Unregister ONE device over SSE (POST body = the device_id to remove).
            This invokes 'gp-provision unregister-device' to clean up edge state/tokens
            and removes the device locally from pi-garden.json."""
            try:
                body = json.loads(self._read_body() or b"{}")
            except ValueError:
                self._send_json(400, {"error": "bad json"})
                return

            pc = portal.pi_config
            cfg = pc.load() if pc else {}
            g = cfg.get("garden") or {}
            gid = g.get("garden_id")
            sid = (cfg.get("fastly") or {}).get("service_id")
            token = pc.get_secret("fastly_api_token") if pc else None

            did = str(body.get("device_id") or "").strip()

            self._sse_start()
            tok_bytes = token.encode() if token else b""

            def write(frame):
                if tok_bytes:
                    frame = frame.replace(tok_bytes, b"***")
                self.wfile.write(frame)
                self.wfile.flush()

            def fail(msg, code=-1):
                write(streaming.sse_event({"line": msg, "level": "error"}))
                write(streaming.sse_event({"done": True, "ok": False, "code": code}, event="done"))

            try:
                if not (pc and gid and sid and token):
                    telemetry.emit("system", "device.unregister", outcome="fail", detail="Unregistration failed: Pi not provisioned")
                    fail("Pi is not provisioned yet — finish the deploy step first.")
                    self.close_connection = True
                    return
                if not did:
                    telemetry.emit("system", "device.unregister", outcome="fail", detail="Unregistration failed: missing device_id")
                    fail("device_id is required.")
                    self.close_connection = True
                    return

                env_over = {"GP_CONFIGS_DIR": str(pc.dir), "FASTLY_API_KEY": token}
                if os.environ.get("FASTLY_MOCK_MODE"):
                    env_over["FASTLY_MOCK_MODE"] = "1"

                write(streaming.sse_event(
                    {"line": f"Unregistering device “{did}” from edge registry…", "level": "info"}))

                params = {"garden": gid, "device": did, "service_id": sid}
                code = self._run_provision_op(write, "unregister-device", params, env_over)
                if code is None:
                    return
                if code != 0:
                    telemetry.emit("system", "device.unregister", outcome="fail", detail=f"Device unregistration failed with exit code {code}")
                    fail(f"Device unregistration failed (exit {code}).", code)
                    self.close_connection = True
                    return

                was_camera = _is_camera_type(_local_device_type(pc, did))
                _delete_local_device(pc, did)
                write(streaming.sse_event({"line": f"Unregistered '{did}' locally and from edge.", "level": "ok"}))
                telemetry.emit("system", "device.unregister", outcome="ok", detail=f"Unregistered device {did}")
                if was_camera:
                    self._restart_camera_daemon(write)   # drop the removed camera from the feed
                write(streaming.sse_event(
                    {"done": True, "ok": True, "code": 0, "device_id": did}, event="done"))
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True

        def _wizard_edit_stream(self):
            """Edit ONE device's details over SSE (POST body = the edits to apply).
            Pushes coarse properties to the edge via gp-provision edit-device,
            and updates all detailed/local properties on the Pi in pi-garden.json."""
            try:
                body = json.loads(self._read_body() or b"{}")
            except ValueError:
                self._send_json(400, {"error": "bad json"})
                return

            pc = portal.pi_config
            cfg = pc.load() if pc else {}
            g = cfg.get("garden") or {}
            gid = g.get("garden_id")
            sid = (cfg.get("fastly") or {}).get("service_id")
            token = pc.get_secret("fastly_api_token") if pc else None

            did = str(body.get("device_id") or "").strip()

            self._sse_start()
            tok_bytes = token.encode() if token else b""

            def write(frame):
                if tok_bytes:
                    frame = frame.replace(tok_bytes, b"***")
                self.wfile.write(frame)
                self.wfile.flush()

            def fail(msg, code=-1):
                write(streaming.sse_event({"line": msg, "level": "error"}))
                write(streaming.sse_event({"done": True, "ok": False, "code": code}, event="done"))

            try:
                if not (pc and gid and sid and token):
                    telemetry.emit("system", "device.edit", outcome="fail", detail="Device edit failed: Pi not provisioned")
                    fail("Pi is not provisioned yet — finish the deploy step first.")
                    self.close_connection = True
                    return
                if not did:
                    telemetry.emit("system", "device.edit", outcome="fail", detail="Device edit failed: missing device_id")
                    fail("device_id is required.")
                    self.close_connection = True
                    return

                env_over = {"GP_CONFIGS_DIR": str(pc.dir), "FASTLY_API_KEY": token}
                if os.environ.get("FASTLY_MOCK_MODE"):
                    env_over["FASTLY_MOCK_MODE"] = "1"

                write(streaming.sse_event(
                    {"line": f"Updating device “{did}” in edge registry…", "level": "info"}))

                params = {"garden": gid, "device": did, "service_id": sid}
                for k in ("name", "kind", "type", "status"):
                    if k in body and body[k] is not None:
                        params[k] = str(body[k])
                if "node_id" in body and body["node_id"] is not None:
                    params["node"] = str(body["node_id"])
                # Alarm roles (booleans): forward whichever were sent so the edge registry +
                # the alarm pipeline learn this device's trigger/confirm role. "true"/"false"
                # both forward (only an absent key preserves the prior value).
                for k in ("can_trigger_alarm", "can_confirm_alarm"):
                    if k in body and body[k] is not None:
                        params[k] = "true" if body[k] else "false"

                code = self._run_provision_op(write, "edit-device", params, env_over)
                if code is None:
                    return
                if code != 0:
                    telemetry.emit("system", "device.edit", outcome="fail", detail=f"Device edit failed with exit code {code}")
                    fail(f"Device edit failed (exit {code}).", code)
                    self.close_connection = True
                    return

                # Compile and apply local updates (including any transport changes)
                local_updates = {}
                for k in ("name", "kind", "type", "status", "node_id"):
                    if k in body and body[k] is not None:
                        local_updates[k] = body[k]
                if "transport" in body and isinstance(body["transport"], dict):
                    local_updates["transport"] = body["transport"]

                was_camera = _is_camera_type(_local_device_type(pc, did))
                _edit_local_device(pc, did, local_updates)
                write(streaming.sse_event({"line": f"Saved edits for '{did}' locally and in the cloud.", "level": "ok"}))
                telemetry.emit("system", "device.edit", outcome="ok", detail=f"Edited device {did}")
                if was_camera or _is_camera_type(_local_device_type(pc, did)):
                    self._restart_camera_daemon(write)   # pick up transport / device changes
                write(streaming.sse_event(
                    {"done": True, "ok": True, "code": 0, "device_id": did}, event="done"))
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True

        # -- settings (post-setup management) ---------------------------
        def _settings_garden_update_stream(self):
            """Edit garden details after setup: save the new name/address/etc. on the
            Pi, then push the coarse {name,tz,location} to the edge via gp-provision
            `update-garden` (NO token change). Streamed like the wizard's deploy so
            the page can show progress."""
            try:
                body = json.loads(self._read_body() or b"{}")
            except ValueError:
                self._send_json(400, {"error": "bad json"})
                return
            pc = portal.pi_config
            cfg = pc.load() if pc else {}
            g = cfg.get("garden") or {}
            name = str(body.get("name") or "").strip()

            self._sse_start()
            token = pc.get_secret("fastly_api_token") if pc else None
            tok_bytes = token.encode() if token else b""

            def write(frame):
                if tok_bytes:
                    frame = frame.replace(tok_bytes, b"***")
                self.wfile.write(frame)
                self.wfile.flush()

            def fail(msg, code=-1):
                write(streaming.sse_event({"line": msg, "level": "error"}))
                write(streaming.sse_event({"done": True, "ok": False, "code": code}, event="done"))

            try:
                if not pc:
                    telemetry.emit("system", "settings.garden_update", outcome="fail", detail="Garden update failed: no config store")
                    fail("No config store.")
                    self.close_connection = True
                    return
                if not name:
                    telemetry.emit("system", "settings.garden_update", outcome="fail", detail="Garden update failed: missing garden name")
                    fail("Your garden needs a name.")
                    self.close_connection = True
                    return

                # The garden_id is the edge's primary key — a display-name change must
                # NEVER change it. Keep the existing id; only mint one if somehow absent.
                gid = g.get("garden_id") or pi_cfg.slugify_garden_id(name)
                garden = {
                    "garden_id": gid, "name": name,
                    "address": body.get("address") or None,
                    "country": body.get("country") or None,
                    "address_fields": body.get("address_fields") or None,
                    "lat": body.get("lat"), "lon": body.get("lon"),
                    "tz": body.get("tz") or None, "notes": body.get("notes") or None,
                }
                pc.save_partial({"garden": garden})
                write(streaming.sse_event({"line": f"Saved “{name}” on the Pi.", "level": "ok"}))

                sid = (cfg.get("fastly") or {}).get("service_id")
                if not (portal.provisioned() and token and sid):
                    write(streaming.sse_event(
                        {"line": "Saved locally (not deployed to the cloud yet).", "level": "info"}))
                    write(streaming.sse_event({"done": True, "ok": True, "code": 0}, event="done"))
                    telemetry.emit("system", "settings.garden_update", outcome="ok", detail=f"Saved garden '{name}' locally (not deployed yet)")
                    self.close_connection = True
                    return

                env_over = {"GP_CONFIGS_DIR": str(pc.dir), "FASTLY_API_KEY": token}
                if os.environ.get("FASTLY_MOCK_MODE"):
                    env_over["FASTLY_MOCK_MODE"] = "1"
                write(streaming.sse_event({"line": "Updating your garden in the cloud…", "level": "info"}))
                params = {"garden": gid, "name": name, "tz": garden["tz"] or "UTC",
                          "address": garden["address"], "lat": garden["lat"],
                          "lon": garden["lon"], "service_id": sid}
                code = self._run_provision_op(write, "update-garden", params, env_over)
                if code is None:
                    return
                if code != 0:
                    telemetry.emit("system", "settings.garden_update", outcome="fail", detail=f"Garden update failed with exit code {code}")
                    fail(f"Saved on the Pi, but the cloud update failed (exit {code}).", code)
                    self.close_connection = True
                    return
                write(streaming.sse_event({"line": "Garden updated everywhere.", "level": "ok"}))
                telemetry.emit("system", "settings.garden_update", outcome="ok", detail=f"Updated garden '{name}' in cloud and locally")
                write(streaming.sse_event({"done": True, "ok": True, "code": 0}, event="done"))
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True

        def _settings_change_passcode(self):
            """Change the admin passcode (verify the current one first). The live
            session cookie is unaffected, so the operator stays signed in; the new
            passcode applies at the next sign-in."""
            body, err = self._wizard_body()
            if err:
                return
            pc = portal.pi_config
            if not (pc and pc.has_passcode()):
                telemetry.emit("system", "settings.change_passcode", outcome="fail", detail="Passcode change failed: no passcode set")
                self._send_json(400, {"error": "no passcode set"})
                return
            if not pc.verify_passcode(str(body.get("current", ""))):
                telemetry.emit("system", "settings.change_passcode", outcome="fail", detail="Passcode change failed: current passcode incorrect")
                self._send_json(403, {"error": "current passcode is incorrect"})
                return
            new = str(body.get("new", ""))
            if len(new) < 6:
                telemetry.emit("system", "settings.change_passcode", outcome="fail", detail="Passcode change failed: new passcode too short")
                self._send_json(400, {"error": "new passcode too short (min 6 characters)"})
                return
            pc.set_passcode(new)
            telemetry.emit("system", "settings.change_passcode", outcome="ok", detail="Admin passcode updated successfully")
            self._send_json(200, {"ok": True})

        def _settings_update_token(self):
            """Replace the stored Fastly API token (0600; never echoed back)."""
            body, err = self._wizard_body()
            if err:
                return
            tok = str(body.get("token", "")).strip()
            if not tok:
                telemetry.emit("system", "settings.update_token", outcome="fail", detail="Token update failed: empty token")
                self._send_json(400, {"error": "token required"})
                return
            if not portal.pi_config:
                telemetry.emit("system", "settings.update_token", outcome="fail", detail="Token update failed: no config store")
                self._send_json(500, {"error": "no config store"})
                return
            portal.pi_config.set_secret("fastly_api_token", tok)
            preview = tok[-4:] if len(tok) >= 4 else tok
            telemetry.emit("system", "settings.update_token", outcome="ok", detail=f"Fastly API token updated (ends in ...{preview})")
            self._send_json(200, {"ok": True, "token_preview": preview})

        def _settings_viewer_pass(self):
            """Set or clear the OPTIONAL viewer password that gates the (view-only)
            edge dashboard. The Pi keeps the value (0600) as the source of truth and
            pushes it to the garden's Secret Store, where the edge enforces it (see
            backend `viewer_gate_for`); clearing makes the dashboard open to anyone
            on the LAN again. Edge changes take ~15-20s to propagate. A quick JSON
            call (like passcode/token) — body `{action:"set"|"clear", passcode}`."""
            body, err = self._wizard_body()
            if err:
                return
            pc = portal.pi_config
            if not pc:
                telemetry.emit("system", "settings.viewer_pass", outcome="fail", detail="Viewer pass failed: no config store")
                self._send_json(500, {"error": "no config store"})
                return
            clearing = str(body.get("action", "set")) == "clear"
            passcode = str(body.get("passcode", ""))
            if not clearing and len(passcode) < 4:
                telemetry.emit("system", "settings.viewer_pass", outcome="fail", detail="Viewer pass failed: password too short")
                self._send_json(400, {"error": "viewer password too short (min 4 characters)"})
                return

            # Push to the edge Secret Store (skip in mock/sim mode — no cloud there).
            if not os.environ.get("FASTLY_MOCK_MODE"):
                cfg = pc.load() or {}
                gid = (cfg.get("garden") or {}).get("garden_id")
                service_name = (cfg.get("fastly") or {}).get("service_name")
                token = pc.get_secret("fastly_api_token")
                svc = _find_service_cfg(pc.dir, service_name) if service_name else None
                store_id = (svc or {}).get("garden_tokens_store_id")
                if not (gid and store_id and token):
                    telemetry.emit("system", "settings.viewer_pass", outcome="fail", detail="Viewer pass failed: garden not deployed")
                    self._send_json(409, {"error": "deploy your garden first"})
                    return
                name = token_secret_name(gid, VIEWER_PASS_SLOT)
                try:
                    if clearing:
                        fastly_api.secret_delete(store_id, name, token)
                    else:
                        fastly_api.secret_put(store_id, name, passcode, token)
                except Exception as e:  # noqa: BLE001
                    telemetry.emit("system", "settings.viewer_pass", outcome="error", detail=f"Viewer pass edge update failed: {e}")
                    self._send_json(502, {"error": f"couldn't reach the cloud — try again ({e})"})
                    return

            # Persist the Pi-side source of truth (empty string == open / no gate).
            pc.set_secret("viewer_pass", "" if clearing else passcode)
            telemetry.emit("system", "settings.viewer_pass", outcome="ok", detail="Viewer password cleared" if clearing else "Viewer password updated")
            self._send_json(200, {"ok": True, "viewer_pass_set": not clearing})

        def _settings_teardown_stream(self):
            """DANGER ZONE: destroy the cloud garden (Compute service + stores + FOS
            bucket) and reset the Pi back to the setup wizard. Requires typing the
            garden id to confirm and a GLOBAL-scoped Fastly token (the teardown op
            enforces the latter and errors cleanly if it's missing)."""
            try:
                body = json.loads(self._read_body() or b"{}")
            except ValueError:
                self._send_json(400, {"error": "bad json"})
                return
            pc = portal.pi_config
            cfg = pc.load() if pc else {}
            g = cfg.get("garden") or {}
            gid = g.get("garden_id")
            sid = (cfg.get("fastly") or {}).get("service_id")
            token = pc.get_secret("fastly_api_token") if pc else None
            confirm = str(body.get("confirm") or "").strip()

            self._sse_start()
            tok_bytes = token.encode() if token else b""

            def write(frame):
                if tok_bytes:
                    frame = frame.replace(tok_bytes, b"***")
                self.wfile.write(frame)
                self.wfile.flush()

            def fail(msg, code=-1):
                write(streaming.sse_event({"line": msg, "level": "error"}))
                write(streaming.sse_event({"done": True, "ok": False, "code": code}, event="done"))

            try:
                if not (pc and gid and sid and token):
                    telemetry.emit("system", "teardown", outcome="fail", detail="Teardown failed: garden not deployed")
                    fail("Nothing to tear down — the Pi isn't deployed.")
                    self.close_connection = True
                    return
                if confirm != gid:
                    telemetry.emit("system", "teardown", outcome="fail", detail="Teardown failed: confirmation string mismatch")
                    fail(f"Type the garden id ({gid}) exactly to confirm.")
                    self.close_connection = True
                    return

                env_over = {"GP_CONFIGS_DIR": str(pc.dir), "FASTLY_API_KEY": token}
                if os.environ.get("FASTLY_MOCK_MODE"):
                    env_over["FASTLY_MOCK_MODE"] = "1"
                write(streaming.sse_event(
                    {"line": "Tearing down your cloud garden — this can take a minute…", "level": "warn"}))
                code = self._run_provision_op(
                    write, "teardown", {"service_id": sid, "remove_data": "yes"}, env_over)
                if code is None:
                    return
                if code != 0:
                    telemetry.emit("system", "teardown", outcome="fail", detail=f"Teardown failed: provision command exit {code}")
                    fail(f"Teardown failed (exit {code}). Nothing local was changed — "
                         "a teardown needs a GLOBAL-scoped Fastly token.", code)
                    self.close_connection = True
                    return

                # Reset local state so GET / cleanly returns to the setup wizard.
                pc.save_partial({"provisioned": False, "step": "detect", "devices": []})
                pc.set_secret("garden_token", "")
                pc.set_secret("viewer_pass", "")
                telemetry.emit("system", "teardown", outcome="ok", detail="Garden torn down successfully")
                write(streaming.sse_event({"line": "Torn down. Returning to setup…", "level": "ok"}))
                write(streaming.sse_event(
                    {"done": True, "ok": True, "code": 0, "redirect": "/"}, event="done"))
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True

        # -- helpers ----------------------------------------------------
        def _handle_login(self):
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
            wants_json = ctype == "application/json"
            # Defense-in-depth: never accept a passcode attempt from off the LAN (an
            # unfirewalled global IPv6 could otherwise brute the passcode from the
            # internet). Reject BEFORE reading the body / touching the rate limiter —
            # see _require_lan. Honor the JSON-vs-form content negotiation so a browser
            # gets the login page and an API client gets JSON.
            if not is_lan_addr(self._client_ip(), self._server_ip()):
                if wants_json:
                    self._send_json(403, {"error": "local network only"})
                else:
                    self._send(403, "text/html; charset=utf-8",
                               LOGIN_PAGE.format(error="Sign in from the local network."))
                return
            body = self._read_body()
            passcode = None
            if wants_json:
                try:
                    passcode = (json.loads(body) or {}).get("passcode")
                except Exception:
                    passcode = None
            else:
                form = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
                passcode = (form.get("passcode") or [None])[0]

            ip = self._client_ip()
            status, value = portal.try_login(ip, passcode)
            if status == "ok":
                print(f"[portal] login OK from {ip}", flush=True)
                telemetry.emit("system", "login", outcome="ok", detail=f"Login success from {ip}")
                cookie = self._set_cookie_header(value, portal.session_ttl)
                if wants_json:
                    self._send(200, "application/json", json.dumps({"ok": True}).encode(),
                               [("Set-Cookie", cookie)])
                else:
                    self._redirect("/", set_cookie=cookie)
            elif status == "locked":
                print(f"[portal] login LOCKED from {ip} (retry {value}s)", flush=True)
                telemetry.emit("system", "login", outcome="locked", detail=f"Login locked from {ip} (retry {value}s)")
                if wants_json:
                    self._send(429, "application/json",
                               json.dumps({"error": "locked", "retry_after": value}).encode(),
                               [("Retry-After", str(value))])
                else:
                    self._send(429, "text/html; charset=utf-8",
                               LOGIN_PAGE.format(error=f"Too many attempts — try again in {value}s."),
                               [("Retry-After", str(value))])
            else:  # bad
                print(f"[portal] login FAIL from {ip}", flush=True)
                telemetry.emit("system", "login", outcome="fail", detail=f"Login fail from {ip}")
                if wants_json:
                    self._send_json(401, {"error": "invalid passcode"})
                else:
                    self._send(401, "text/html; charset=utf-8",
                               LOGIN_PAGE.format(error="Incorrect passcode."))

        def _proxy(self, method, route, body=None):
            try:
                if method == "GET":
                    code, ctype, content = portal.proxy_get(route)
                else:
                    code, ctype, content = portal.proxy_post(route, body)
            except requests.RequestException as e:
                print(f"[portal] edge proxy {method} {route} failed: {e}", flush=True)
                self._send_json(502, {"error": "edge unreachable"})
                return
            self._send(code, ctype, content)

    return Handler


# ---------------------------------------------------------------------------
# Wiring + entrypoint.
# ---------------------------------------------------------------------------

class _DualStackServer(ThreadingHTTPServer):
    """A threaded HTTP server that accepts BOTH IPv6 and IPv4 (v4-mapped) on one socket, so
    a `.local` host advertising both address families connects fast over whichever the
    client's happy-eyeballs picks (no IPv6-then-IPv4 fallback stall). IPv4 clients arrive as
    ``::ffff:a.b.c.d`` — `is_lan_addr` unwraps that, so the LAN guard still works."""
    address_family = socket.AF_INET6

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except (AttributeError, OSError):
            pass  # platform without dual-stack -> IPv6-only (still better than v4-only here)
        super().server_bind()


def _read_file(path, *, required):
    """Read a served page/template; "" if missing (unless ``required``, then FATAL exit).

    Stamps the shared-asset cache-bust: every page links /static/gp.{css,js}?v=__ASSET_VERSION__
    and we substitute the ONE generated ASSET_VERSION here, so the Pi's ?v= can never drift
    from the edge's (both read it from contract/spec.toml)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().replace("__ASSET_VERSION__", ASSET_VERSION)
    except OSError as e:
        if required:
            print(f"[portal] FATAL: cannot read {path}: {e}", file=sys.stderr, flush=True)
            raise SystemExit(2)
        return ""


def _rate_str(v, per):
    """Human unit price for a cost-detail row, e.g. "$0.12 / GB" or "$0.005 / 1,000 ops".
    Trims trailing zeros; a 0 rate (Fastly standard plan) shows "$0.00 / <per>". Shared by
    the Costs page op rows and the Storage-page capture estimator so both read identically."""
    v = float(v or 0)
    if v <= 0:
        return f"$0.00 / {per}"
    return f"${f'{v:.4f}'.rstrip('0').rstrip('.')} / {per}"


def _find_service_cfg(configs_dir, service_name):
    """Find the gp-provision service config (configs/<sid>.json) by service_name.
    Skips the registry mirror + the wizard's own files. Returns the cfg dict or None."""
    import glob as _glob
    for p in sorted(_glob.glob(os.path.join(str(configs_dir), "*.json"))):
        base = os.path.basename(p)
        if base.endswith("-registry.json") or base == pi_cfg.CONFIG_NAME:
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            continue
        if cfg.get("service_name") == service_name and cfg.get("service_id"):
            return cfg
    return None


def _is_camera_type(t):
    """True for device types the camera daemon owns (camera_csi / camera_usb / …)."""
    return str(t or "").startswith("camera")


def _local_device_type(pc, did):
    """The locally-recorded type for a device id (pi-garden.json), or ''."""
    if not pc:
        return ""
    for d in (pc.load().get("devices") or []):
        if d.get("device_id") == did:
            return d.get("type") or ""
    return ""


def _read_garden_token(configs_dir, gid):
    """Read GP_GARDEN_TOKEN from the deviceless garden env gp-provision wrote."""
    p = os.path.join(str(configs_dir), f"{gid}-garden.env")
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("GP_GARDEN_TOKEN="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def _upsert_local_device(pc, record):
    """Append/replace a device (by device_id) in pi-garden.json. This is where the
    DETAILED transport/scan_meta live — never on the edge (two-tier split)."""
    devices = [d for d in (pc.load().get("devices") or [])
               if d.get("device_id") != record["device_id"]]
    devices.append(record)
    pc.save_partial({"devices": devices})


def _delete_local_device(pc, device_id):
    """Remove a device (by device_id) from pi-garden.json."""
    devices = [d for d in (pc.load().get("devices") or [])
               if d.get("device_id") != device_id]
    pc.save_partial({"devices": devices})


def _edit_local_device(pc, device_id, updates):
    """Edit fields of a device (by device_id) in pi-garden.json."""
    devices = []
    for d in (pc.load().get("devices") or []):
        if d.get("device_id") == device_id:
            merged = dict(d)
            for k, v in updates.items():
                if v is not None:
                    if k in ("transport", "scan_meta") and isinstance(v, dict):
                        merged[k] = {**merged.get(k, {}), **v}
                    else:
                        merged[k] = v
            devices.append(merged)
        else:
            devices.append(d)
    pc.save_partial({"devices": devices})




def _should_prune_now(settings, now_struct):
    """True if the daily retention sweep is due: not already run today (local date) AND
    the local hour is at/after the configured ``prune_hour``. PURE + unit-tested."""
    today = time.strftime("%Y-%m-%d", now_struct)
    if settings.get("last_prune_date") == today:
        return False
    return now_struct.tm_hour >= int(settings.get("prune_hour", 3))


def _prune_scheduler(portal, stop_evt, *, tick_s=600, clock=time.time):
    """The retention 'cron': once per local day, at/after the configured hour, trigger the
    edge prune. If the edge reports more days remaining (a backlog), run again next tick
    instead of waiting a full day. Idempotent via last_prune_date so a restart never
    double-runs. Runs as a daemon thread; exits when ``stop_evt`` is set. Never dies on
    error (a scheduler that crashes silently is worse than one that logs + retries)."""
    while not stop_evt.wait(tick_s):
        try:
            if portal.bootstrap_mode() or not portal.provisioned() or not portal.garden_token:
                continue
            st = portal.maintenance_settings()
            if not _should_prune_now(st, time.localtime(clock())):
                continue
            res = portal.run_prune(trigger="schedule", clock=clock)
            if res.get("ok") and not res.get("remaining"):
                portal.record_prune_date(time.strftime("%Y-%m-%d", time.localtime(clock())))
            # ok+remaining -> leave last_prune_date unset; next tick continues the backlog.
        except Exception as e:  # noqa: BLE001 - a scheduler must never die
            print(f"[portal] prune scheduler error: {e}", flush=True)


def _should_prune_alarms_now(settings, now_struct):
    """True if the daily ALARM sweep is due (same idempotent once-per-local-day-at/after-hour
    rule as _should_prune_now, keyed on last_alarm_prune_date). PURE + unit-tested."""
    today = time.strftime("%Y-%m-%d", now_struct)
    if settings.get("last_alarm_prune_date") == today:
        return False
    return now_struct.tm_hour >= int(settings.get("prune_hour", 3))


def _alarm_prune_scheduler(portal, stop_evt, *, tick_s=600, clock=time.time):
    """The alarm-retention 'cron': once per local day at/after the configured hour, trigger the
    edge alarm prune. Idempotent via last_alarm_prune_date so a restart never double-runs. Daemon
    thread; exits when ``stop_evt`` is set; never dies on error."""
    while not stop_evt.wait(tick_s):
        try:
            if portal.bootstrap_mode() or not portal.provisioned() or not portal.garden_token:
                continue
            st = portal.alarm_retention_settings()
            if not _should_prune_alarms_now(st, time.localtime(clock())):
                continue
            res = portal.run_alarm_prune(trigger="schedule", clock=clock)
            if res.get("ok"):
                portal.record_alarm_prune_date(time.strftime("%Y-%m-%d", time.localtime(clock())))
        except Exception as e:  # noqa: BLE001 - a scheduler must never die
            print(f"[portal] alarm prune scheduler error: {e}", flush=True)


def build_portal(args):
    # BEHAVIOR CHANGE (wizard): a fresh Pi has no GP_ADMIN_PASSCODE yet. Instead of
    # refusing to start, run in BOOTSTRAP MODE — serve only the setup wizard
    # (LAN-only, rate-limited) until the wizard sets a passcode + provisions the Pi.
    configs_dir = getattr(args, "configs_dir", None)
    pc = pi_cfg.PiConfig(configs_dir)

    admin_passcode = os.environ.get("GP_ADMIN_PASSCODE", "")
    provisioned = pc.is_provisioned()
    has_pass = bool(admin_passcode) or pc.has_passcode()

    dashboard_html = _read_file(args.dashboard, required=False)
    timelapse_html = _read_file(getattr(args, "timelapse", _default_timelapse_path()), required=False)
    event_html = _read_file(getattr(args, "event_page", _default_event_path()), required=False)
    alarms_html = _read_file(getattr(args, "alarms_page", _default_alarms_path()), required=False)
    wizard_html = _read_file(getattr(args, "wizard", _default_wizard_path()), required=False)
    devices_html = _read_file(getattr(args, "devices", _default_devices_path()), required=False)
    settings_html = _read_file(getattr(args, "settings", _default_settings_path()), required=False)
    costs_html = _read_file(getattr(args, "costs", _default_costs_path()), required=False)
    logs_html = _read_file(getattr(args, "logs", _default_logs_path()), required=False)
    storage_html = _read_file(getattr(args, "storage", _default_storage_path()), required=False)
    help_html = _read_file(getattr(args, "help_page", _default_help_path()), required=False)
    portal_header_html = _read_file(
        getattr(args, "portal_header", _default_portal_header_path()), required=False)

    # Identity: prefer the freshly-provisioned coordinates in pi-garden.json over
    # CLI/env defaults (so the portal proxies to the right edge after the wizard runs).
    env = pc.to_env()
    explicit_secret = os.environ.get("GP_PORTAL_SECRET")
    secret = _as_bytes(explicit_secret) if explicit_secret else None

    if not has_pass:
        print("[portal] BOOTSTRAP MODE: no admin passcode yet — serving the setup "
              "wizard at http://raspberrypi.local/ (LAN-only, rate-limited). Only "
              "/api/wizard/* is exposed until the passcode is set + the Pi is provisioned.",
              flush=True)
        if provisioned:
            print("[portal] WARNING: provisioned but no passcode found — set one via "
                  "the wizard to recover normal mode.", file=sys.stderr, flush=True)

    return Portal(
        admin_passcode=admin_passcode,
        session_secret=secret,
        edge=env.get("GP_BACKEND") or args.edge,
        dashboard_html=dashboard_html,
        timelapse_html=timelapse_html,
        event_html=event_html,
        alarms_html=alarms_html,
        wizard_html=wizard_html,
        devices_html=devices_html,
        settings_html=settings_html,
        costs_html=costs_html,
        logs_html=logs_html,
        storage_html=storage_html,
        help_html=help_html,
        portal_header_html=portal_header_html,
        pi_config=pc,
        garden_id=env.get("GP_GARDEN_ID") or args.garden_id,
        device_id=env.get("GP_DEVICE_ID") or args.device_id,
        node_id=env.get("GP_NODE_ID") or args.node_id,
        garden_token=env.get("GP_GARDEN_TOKEN") or args.garden_token,
        session_ttl=args.session_ttl,
        edge_timeout=args.edge_timeout,
        rate_limiter=RateLimiter(max_fails=args.max_fails,
                                 window_s=args.window_s,
                                 lockout_s=args.lockout_s),
    )


def main(argv=None):
    # Load the .env BEFORE argparse defaults read os.environ, so file values feed
    # the defaults but a real env var / explicit flag still wins.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--env-file", default=os.environ.get("GP_ENV_FILE"))
    known, _ = pre.parse_known_args(argv)
    env_file = known.env_file or os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", ".env"))
    load_env_file(env_file)

    parser = argparse.ArgumentParser(
        description="Fastly Garden Protector LAN admin portal (Pi, human surface)")
    parser.add_argument("--env-file", default=env_file, help="path to .env (default <repo>/.env)")
    parser.add_argument("--listen", default=os.environ.get("GP_PORTAL_LISTEN", "0.0.0.0:80"),
                        help="LAN bind addr (default 0.0.0.0:80; needs CAP_NET_BIND_SERVICE)")
    parser.add_argument("--edge", default=os.environ.get("GP_BACKEND", "http://localhost:7878"),
                        help="Fastly Compute edge base URL (env GP_BACKEND)")
    parser.add_argument("--dashboard", default=os.environ.get("GP_DASHBOARD_HTML", _default_dashboard_path()),
                        help="path to dashboard.html (default backend/src/dashboard.html)")
    parser.add_argument("--timelapse", default=os.environ.get("GP_TIMELAPSE_HTML", _default_timelapse_path()),
                        help="path to the timelapse page (default backend/src/timelapse.html)")
    parser.add_argument("--event-page", dest="event_page",
                        default=os.environ.get("GP_EVENT_HTML", _default_event_path()),
                        help="path to the event-detail page (default backend/src/event.html)")
    parser.add_argument("--alarms-page", dest="alarms_page",
                        default=os.environ.get("GP_ALARMS_HTML", _default_alarms_path()),
                        help="path to the alarms page (default backend/src/alarms.html)")
    parser.add_argument("--wizard", default=os.environ.get("GP_WIZARD_HTML", _default_wizard_path()),
                        help="path to the first-run wizard SPA (default hardware/wizard.html)")
    parser.add_argument("--devices", default=os.environ.get("GP_DEVICES_HTML", _default_devices_path()),
                        help="path to the device-onboarding page (default hardware/devices.html)")
    parser.add_argument("--settings", default=os.environ.get("GP_SETTINGS_HTML", _default_settings_path()),
                        help="path to the settings page (default hardware/settings.html)")
    parser.add_argument("--costs", default=os.environ.get("GP_COSTS_HTML", _default_costs_path()),
                        help="path to the costs page (default hardware/costs.html)")
    parser.add_argument("--logs", default=os.environ.get("GP_LOGS_HTML", _default_logs_path()),
                        help="path to the logs page (default hardware/logs.html)")
    parser.add_argument("--storage", default=os.environ.get("GP_STORAGE_HTML", _default_storage_path()),
                        help="path to the storage-cleanup page (default hardware/storage.html)")
    # NB: --help is reserved by argparse, so the help PAGE arg is --help-page (dest=help_page).
    parser.add_argument("--help-page", dest="help_page",
                        default=os.environ.get("GP_HELP_HTML", _default_help_path()),
                        help="path to the help page (default hardware/help.html)")
    parser.add_argument("--portal-header", default=os.environ.get("GP_PORTAL_HEADER_HTML", _default_portal_header_path()),
                        help="path to the shared header partial (default hardware/portal_header.html)")
    parser.add_argument("--telemetry-db", default=os.environ.get("GP_TELEMETRY_DB"),
                        help="Telemetry SQLite DB path")
    parser.add_argument("--configs-dir", default=os.environ.get("GP_CONFIGS_DIR"),
                        help="configs/ dir for pi-garden.json + secrets.json (default <repo>/configs)")
    parser.add_argument("--garden-id", default=os.environ.get("GP_GARDEN_ID", "default"))
    parser.add_argument("--device-id", default=os.environ.get("GP_DEVICE_ID", "default"))
    parser.add_argument("--node-id", default=os.environ.get("GP_NODE_ID", "default"))
    parser.add_argument("--garden-token", default=os.environ.get("GP_GARDEN_TOKEN", ""))
    parser.add_argument("--session-ttl", type=int,
                        default=int(os.environ.get("GP_SESSION_TTL", "43200")),
                        help="session cookie lifetime in seconds (default 12h)")
    parser.add_argument("--edge-timeout", type=float, default=5.0)
    parser.add_argument("--max-fails", type=int, default=5,
                        help="failed logins per IP within the window before lockout")
    parser.add_argument("--window-s", type=int, default=60)
    parser.add_argument("--lockout-s", type=int, default=300)
    args = parser.parse_args(argv)

    portal = build_portal(args)

    telemetry.init(
        db_path=args.telemetry_db,
        garden_id=portal.garden_id,
        device_id=portal.device_id,
        node_id=portal.node_id
    )

    host, _, port = args.listen.rpartition(":")
    host = host or "0.0.0.0"
    if host in ("0.0.0.0", "::", ""):
        # Dual-stack: listen on IPv6 AND IPv4. raspberrypi.local mDNS-advertises BOTH a
        # global IPv6 and IPv4; with no IPv6 listener the browser's IPv6 attempt to :80
        # stalls ~1s (SYN dropped) before falling back to v4, so every page "spins". With
        # an IPv6 listener the connect succeeds immediately whichever address it tries.
        server = _DualStackServer(("::", int(port)), make_handler(portal))
    else:
        server = ThreadingHTTPServer((host, int(port)), make_handler(portal))

    # The retention 'cron' (FOS has no lifecycle). A daemon thread that sweeps once/day;
    # the Storage page shows its schedule + recent runs (telemetry). Idempotent + safe to
    # run alongside the HTTP server.
    prune_stop = threading.Event()
    threading.Thread(target=_prune_scheduler, args=(portal, prune_stop),
                     name="prune-scheduler", daemon=True).start()
    # Sister cron for ALARM retention (the alarm log is an edge KV doc; this sweep calls the
    # edge). Same once/day idempotent cadence; the Alarms page shows its schedule + runs.
    alarm_prune_stop = threading.Event()
    threading.Thread(target=_alarm_prune_scheduler, args=(portal, alarm_prune_stop),
                     name="alarm-prune-scheduler", daemon=True).start()

    print(f"[portal] listening on {args.listen} -> edge {portal.edge} "
          f"(garden={portal.garden_id}); open http://raspberrypi.local/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        prune_stop.set()
        alarm_prune_stop.set()
        telemetry.shutdown()
        server.shutdown()


if __name__ == "__main__":
    main()
