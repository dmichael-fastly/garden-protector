#!/usr/bin/env python3
"""provision/console.py — a local admin *console* for the control plane.

The Compute-served dashboard (backend/src/dashboard.html) is the lightweight
LAN/public **monitoring** surface. Provisioning and tenancy changes, however, are a
**control-plane** activity: they need the Fastly API token and run for tens of
seconds (deploy, FOS/CDN standup, token propagation), which is the wrong shape for
the edge (a ~5 s handler budget) and the wrong place for secrets (a browser). So
this console runs **locally**, holds the token server-side, and drives the existing
`gp-provision` CLI — streaming each long op's progress to the browser over **SSE**.

What it gives the admin:
  * a multi-garden **overview** (gardens + devices from the authoritative local
    registry mirror), with live arm/disarm state proxied from the edge,
  * **modals** to provision a deployment, add a garden + register a device, rotate a
    token, and tear a deployment down — each with a **live streaming log**, and
  * per-garden Arm / Disarm / Stop / Resume.

Safety: pass ``--mock`` (or set ``FASTLY_MOCK_MODE=1``) to exercise the entire flow
WITHOUT touching Fastly — the CLI's mock transport produces real *local* artifacts
(registry mirror, deploy-env files, locally-minted tokens) so the whole UX is
demoable and testable offline. Stdlib http.server only (matches camera_view.py).
"""
import gzip
import json
import os
import pathlib
import queue
import subprocess
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import requests

from . import auth, cost_rates, orchestrator, taxonomy, usage_stats
from .edge_client import EdgeClient  # ONE admin edge proxy (CHARTER: one Python service lib)
from .auth import (  # ONE shared LAN-admin auth library (CHARTER: one Python service lib);
    RateLimiter,       # re-exported here so callers/tests using console.<name> keep working.
    SessionStore,
    check_passcode,
    hash_passcode,
    is_lan_addr,
    make_auth_record,
    verify_passcode,
    SESSION_TTL_SECONDS,
)
from .envfile import load_env_file  # ONE shared .env loader (CHARTER: one Python service lib)
from .streaming import (  # SSE plumbing factored into a shared module (see streaming.py);
    build_cli_args,        # re-exported here so callers/tests using console.<name> keep working.
    pump_process,
    sse_event,
    stream_cli,
    _level_of,
    _read_deploy_env,
    _truthy,
)

from .contract_gen import ASSET_VERSION, CONTROL_COMMANDS  # ONE shared /static ?v= cache-bust + control vocab (spec.toml SSOT)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CONSOLE_HTML = pathlib.Path(__file__).resolve().parent / "console.html"
FAVICON_ICO = REPO_ROOT / "favicon.ico"
UI_STATIC = REPO_ROOT / "ui" / "static"  # the ONE shared UI asset layer (CHARTER)

# --- LAN admin auth (passcode + session cookie + LAN guard) ----------------
# The console started life as a localhost-only operator tool. Once it binds to the
# house LAN (so you can manage your garden from any device on your network), it needs
# real gating: a hashed passcode unlocks an HttpOnly session cookie, the peer IP must
# be on the local network, and failed logins are rate-limited. The passcode is the
# real authenticator; the LAN check is defense-in-depth.
AUTH_FILE_NAME = "console-auth.json"          # under configs/ (gitignored) — legacy fallback
PASSCODE_ENV = "GP_ADMIN_PASSCODE"            # plaintext passcode env var (shared with the portal)
SECRETS_FILE_NAME = "secrets.json"            # under configs/ — the SAME file the Pi portal uses
SECRETS_PASSCODE_KEY = "admin_passcode_hash"  # hashed {algo,salt,hash} record key in secrets.json
SESSION_COOKIE = "gp_session"
# SESSION_TTL_SECONDS is imported from provision.auth (single source).
RATE_LIMIT_MAX_FAILS = 5                       # failed logins per window before lockout
RATE_LIMIT_WINDOW_S = 300                      # lockout / counting window (5 min)

# Minimal sign-in page served at `/` when a passcode is set and the caller has no
# session. On success it reloads into the full console (console.html).
LOGIN_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fastly Garden Protector — Sign in</title>
<link rel="icon" href="/favicon.ico" sizes="any">
<meta name="theme-color" content="#FF282D">
<link rel="stylesheet" href="/static/app.css?v=__ASSET_VERSION__">
<link rel="stylesheet" href="/static/gp.css?v=__ASSET_VERSION__">
<script src="/static/gp.js?v=__ASSET_VERSION__"></script>
<style>
 /* gp.css supplies the palette + .theme-toggle; the form field + button are DaisyUI
    (.input / .btn) from app.css. Only login-specific bits here. */
 body{min-height:100vh;display:flex;align-items:center;justify-content:center;}
 .theme-toggle{position:fixed;top:14px;right:14px;z-index:10;}
 .box{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:28px;width:320px;}
 h1{font-size:17px;margin:0 0 4px;} .leaf{color:var(--green);}
 .sub{color:var(--muted);font-size:13px;margin-bottom:18px;}
 .err{color:var(--red);font-size:13px;min-height:18px;margin-top:6px;}
</style></head><body>
 <button id="theme-toggle" class="theme-toggle" type="button" aria-label="Switch theme"></button>
 <form class="box" id="f">
  <h1><span class="leaf"><svg class="gp-ic" aria-hidden="true"><use href="#gp-leaf"/></svg></span> Fastly Garden Protector</h1>
  <div class="sub">Enter the admin passcode to manage this garden.</div>
  <input type="password" id="pw" class="input w-full mb-3" placeholder="Passcode" autofocus autocomplete="current-password">
  <button type="submit" class="btn btn-primary w-full">Sign in</button>
  <div class="err" id="err"></div>
 </form>
 <script>
  GP.initTheme();
  const f=document.getElementById("f"),pw=document.getElementById("pw"),err=document.getElementById("err");
  f.addEventListener("submit",async(e)=>{e.preventDefault();err.textContent="";
   const r=await fetch("/api/console/login",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({passcode:pw.value})});
   if(r.ok){location.reload();}
   else if(r.status===429){err.textContent="Too many attempts — wait a few minutes.";}
   else{err.textContent="Incorrect passcode.";pw.value="";pw.focus();}
  });
 </script>
</body></html>""".replace("__ASSET_VERSION__", ASSET_VERSION)


# ---------------------------------------------------------------------------
# Console configuration.
# ---------------------------------------------------------------------------

class ConsoleConfig:
    def __init__(self, *, configs_dir=None, mock=False, edge=None, python_exe=None):
        self.configs_dir = pathlib.Path(configs_dir or orchestrator.CONFIGS_DIR)
        self.mock = mock
        self.edge = (edge or "").rstrip("/")
        self.python_exe = python_exe or sys.executable


# ---------------------------------------------------------------------------
# SSE plumbing (sse_event / build_cli_args / _truthy / stream_cli / pump_process /
# _level_of / _read_deploy_env) now lives in provision/streaming.py and is imported
# above, so the console and the Pi portal drive ONE provisioning pipeline. The names
# are re-exported here for backward-compatible callers/tests (console.sse_event, …).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# LAN admin auth. The pure cores — scrypt KDF (`hash_passcode`/`make_auth_record`/
# `verify_passcode`), the LAN gate (`is_lan_addr`), the in-memory `SessionStore`,
# and the brute-force `RateLimiter` — now live in the shared `provision.auth`
# module and are imported above (re-exported as `console.<name>`). Only the
# console-specific file persistence of the passcode record stays here.
# ---------------------------------------------------------------------------

def load_auth_record(configs_dir):
    """Read the console's own passcode record, or None if it has never been set.
    This is the LEGACY fallback source (see ``resolve_auth``)."""
    p = pathlib.Path(configs_dir) / AUTH_FILE_NAME
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def save_auth_record(configs_dir, rec):
    """Persist the passcode record (0600) and return its path."""
    d = pathlib.Path(configs_dir)
    d.mkdir(parents=True, exist_ok=True)
    p = d / AUTH_FILE_NAME
    p.write_text(json.dumps(rec))
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p


def load_secrets_record(configs_dir):
    """The shared hashed passcode record from ``configs/secrets.json`` — the SAME file
    and ``admin_passcode_hash`` key the Pi portal/``pi_config`` write. Lets the console
    and portal share ONE admin credential. Returns the ``{algo,salt,hash}`` dict or
    None (missing file / no key / malformed)."""
    p = pathlib.Path(configs_dir) / SECRETS_FILE_NAME
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    rec = data.get(SECRETS_PASSCODE_KEY) if isinstance(data, dict) else None
    return rec if isinstance(rec, dict) else None


class _AuthMethod:
    """How the console verifies an admin passcode, plus where the credential came
    from. ``enabled`` is False only when NO passcode is configured anywhere (an open
    localhost-dev console). The LAN guard applies regardless."""

    def __init__(self, verify=None, source="none"):
        self._verify = verify
        self.enabled = verify is not None
        self.source = source

    def verify(self, passcode):
        return bool(self._verify) and self._verify(passcode)


def resolve_auth(configs_dir):
    """Resolve the admin passcode from the first source that has one, highest
    precedence first:

      1. the ``GP_ADMIN_PASSCODE`` env var (plaintext; shared with the Pi portal and
         populated from the Pi's ``.env`` — this is what lets a freshly-booted Pi
         enforce the console without the interactive ``set-passcode`` step),
      2. ``configs/secrets.json`` ``admin_passcode_hash`` (the shared hashed record —
         set via the portal / ``pi_config``, so both surfaces share one credential),
      3. ``configs/console-auth.json`` (the console's own record — LEGACY fallback,
         written by ``console set-passcode``).

    Returns an ``_AuthMethod``; ``.enabled`` is False only if none are configured. A
    real env var always wins over the files (``load_env_file`` never overwrites one)."""
    env_pass = os.environ.get(PASSCODE_ENV, "")
    if env_pass:
        return _AuthMethod(lambda pw: check_passcode(pw, env_pass), source="env")
    rec = load_secrets_record(configs_dir)
    if rec:
        return _AuthMethod(lambda pw: verify_passcode(pw, rec), source="secrets.json")
    rec = load_auth_record(configs_dir)
    if rec:
        return _AuthMethod(lambda pw: verify_passcode(pw, rec), source="console-auth.json")
    return _AuthMethod()


# Secret-ish keys we must never echo back to the browser from a service config.
_SECRET_CFG_KEYS = {
    "fastly_api_key", "fos_secret_access_key", "cdn_secret", "fos_access_key_id",
}


def list_services(configs_dir):
    """Provisioned services from configs/*.json, with secrets stripped."""
    configs_dir = pathlib.Path(configs_dir)
    out = []
    if not configs_dir.exists():
        return out
    for p in sorted(configs_dir.glob("*.json")):
        if p.name.endswith("-registry.json"):
            continue
        try:
            cfg = json.loads(p.read_text())
        except Exception:
            continue
        out.append({
            "service_id": cfg.get("service_id", p.stem),
            "service_name": cfg.get("service_name"),
            "backend_url": cfg.get("backend_url"),
            "has_archive": bool(cfg.get("cdn_url")),
            "active_version": cfg.get("active_version"),
        })
    return out


def read_overview(configs_dir, service_id):
    """Aggregate the gardens + devices for a service from the AUTHORITATIVE local
    registry mirror (configs/{sid}-registry.json). No network — the live arm-state
    is fetched separately (best-effort) so the overview always renders."""
    configs_dir = pathlib.Path(configs_dir)
    cfg_path = configs_dir / f"{service_id}.json"
    mirror_path = configs_dir / f"{service_id}-registry.json"
    cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    mirror = json.loads(mirror_path.read_text()) if mirror_path.exists() else {"gardens": {}, "devices": {}}
    gardens = (mirror.get("gardens") or {}).get("gardens", [])
    devices = mirror.get("devices") or {}
    # Decorate each garden with its device list + count.
    enriched = []
    for g in gardens:
        gid = g["garden_id"]
        dev = (devices.get(gid) or {}).get("devices", [])
        enriched.append({**g, "devices": dev, "device_count": len(dev)})
    return {
        "service_id": service_id,
        "service_name": cfg.get("service_name"),
        "backend_url": cfg.get("backend_url"),
        "gardens": enriched,
    }


def garden_token(configs_dir, gid):
    """Best-effort lookup of a garden's current token from a deploy-env file
    (configs/{gid}-*.env). Server-side only; never sent to the browser."""
    configs_dir = pathlib.Path(configs_dir)
    if not configs_dir.exists() or gid == "default":
        return ""
    for p in sorted(configs_dir.glob(f"{gid}-*.env")):
        try:
            for line in p.read_text().splitlines():
                if line.startswith("GP_GARDEN_TOKEN="):
                    return line.split("=", 1)[1].strip()
        except Exception:
            continue
    return ""


def edge_control_target(edge, gid):
    """(url, is_default) for a per-garden control POST. The default garden uses the
    open /api/control; others use the auth-enforced /api/gardens/{gid}/control."""
    edge = edge.rstrip("/")
    if gid == "default":
        return f"{edge}/api/control", True
    return f"{edge}/api/gardens/{gid}/control", False


# ---------------------------------------------------------------------------
# SUBPROCESS STREAMER (stream_cli / pump_process) moved to provision/streaming.py
# and imported above; reused verbatim by the Pi portal's wizard SSE endpoints.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Live edge proxy: all edge reads/controls go through the shared EdgeClient
# (provision/edge_client.py) — best-effort; the console never blocks the UI on the
# edge. The per-handler instance is built in make_handler.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Live log tail — durable FOS poll (history + spine) merged with `fastly log-tail`
# (the live edge). The edge dashboard polls /api/state; the *logs* are a stream, and
# like provisioning that belongs off-edge (Compute's ~5 s handler budget).
# ---------------------------------------------------------------------------

def parse_log_object(gz_bytes):
    """Decompress a gzipped FOS log object into a list of non-empty text lines.
    Tolerates a plain (already-decompressed) body too, so the same helper works
    against fixtures and real objects."""
    try:
        raw = gzip.decompress(gz_bytes)
    except (OSError, EOFError):
        raw = gz_bytes
    return [ln for ln in raw.decode("utf-8", errors="replace").splitlines() if ln.strip()]


def service_config(configs_dir, service_id):
    """Load configs/<sid>.json (holds FOS creds + the Fastly key). Server-side only."""
    p = pathlib.Path(configs_dir) / f"{service_id}.json"
    return json.loads(p.read_text()) if p.exists() else {}


class FosLogSource:
    """Polls a service's FOS ``telemetry/`` prefix and yields NEW log lines.

    FOS log objects are written once per flush period and are immutable, so "new
    lines" reduces to "lines from object keys we have not emitted yet". The first
    ``poll()`` backfills the last few objects (recent history); later polls emit only
    unseen objects. ``_list_keys``/``_read_lines`` are the IO seam (overridden in
    tests) so ``poll()``'s diffing logic is exercised without boto/network."""

    def __init__(self, cfg, *, s3=None, backfill_objects=5, max_backfill_lines=200):
        self._bucket = cfg["fos_bucket"]
        if s3 is None:
            from . import fos_setup
            s3 = fos_setup._s3_client(cfg["fos_region"], cfg["fos_access_key_id"],
                                      cfg["fos_secret_access_key"])
        self._s3 = s3
        self._seen = set()
        self._primed = False
        self._backfill_objects = backfill_objects
        self._max_backfill_lines = max_backfill_lines

    def _list_keys(self):
        keys = []
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix="telemetry/"):
            for o in page.get("Contents", []):
                keys.append((o["Key"], o.get("LastModified")))
        # Oldest -> newest, so backfill keeps the most recent N and order reads sanely.
        keys.sort(key=lambda kv: (kv[1] is None, kv[1], kv[0]))
        return [k for k, _ in keys]

    def _read_lines(self, key):
        body = self._s3.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        return parse_log_object(body)

    def poll(self):
        keys = self._list_keys()
        fresh = [k for k in keys if k not in self._seen]
        if not self._primed:
            self._primed = True
            backfill = fresh[-self._backfill_objects:]
            self._seen.update(fresh)  # everything older than the backfill is "already seen"
            lines = []
            for k in backfill:
                lines.extend(self._read_lines(k))
            return lines[-self._max_backfill_lines:]
        lines = []
        for k in fresh:
            self._seen.add(k)
            lines.extend(self._read_lines(k))
        return lines


def _spawn_logtail(service_id, token):
    """Start `fastly log-tail` for the live edge stream, or return None if the CLI is
    missing / cannot start (best-effort — FOS poll still covers durable history)."""
    if not service_id:
        return None
    env = dict(os.environ)
    if token:
        env["FASTLY_API_TOKEN"] = token
    try:
        return subprocess.Popen(
            ["fastly", "log-tail", "--service-id", service_id],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, bufsize=1,
        )
    except (FileNotFoundError, OSError):
        return None


def _drain_proc(proc, line_queue, stop):
    """Pump a subprocess's stdout onto a queue until EOF or ``stop`` is set."""
    try:
        for line in proc.stdout:
            if stop.is_set():
                break
            line = line.rstrip("\n")
            if line:
                line_queue.put(line)
    except (ValueError, OSError):
        pass


def _emit_lines(write, lines, src):
    for ln in lines:
        write(sse_event({"line": ln, "level": _level_of(ln), "src": src}))


def run_log_stream(cfg, fastly_token, write, *, poll_interval=7.0, keepalive=15.0,
                   sleep=time.sleep, clock=time.time, source=None):
    """Merge the durable FOS poll with the live `fastly log-tail` subprocess, writing
    every line as an SSE frame via ``write`` (which raises on client disconnect)."""
    sid = cfg.get("service_id", "")
    if source is None:
        try:
            source = FosLogSource(cfg)
        except Exception as e:  # noqa: BLE001 — never crash the stream on a bad config
            source = None
            write(sse_event({"line": f"FOS log history unavailable: {e}", "level": "warn",
                             "src": "fos"}))

    lq = queue.Queue()
    stop = threading.Event()
    proc = _spawn_logtail(sid, fastly_token)
    tailer = None
    if proc is not None:
        tailer = threading.Thread(target=_drain_proc, args=(proc, lq, stop), daemon=True)
        tailer.start()
    else:
        write(sse_event({"line": "live tail unavailable (fastly CLI missing/failed) — "
                         "showing durable FOS logs only", "level": "warn", "src": "tail"}))

    write(sse_event({"hello": True, "service_id": sid}, event="ready"))
    last_poll = 0.0
    last_ka = clock()
    try:
        while True:
            busy = False
            # Live edge lines first (lowest latency).
            try:
                while True:
                    line = lq.get_nowait()
                    write(sse_event({"line": line, "level": _level_of(line), "src": "tail"}))
                    busy = True
            except queue.Empty:
                pass
            now = clock()
            if source is not None and now - last_poll >= poll_interval:
                try:
                    _emit_lines(write, source.poll(), "fos")
                except Exception as e:  # noqa: BLE001
                    write(sse_event({"line": f"FOS poll error: {e}", "level": "warn",
                                     "src": "fos"}))
                last_poll = now
                busy = True
            if now - last_ka >= keepalive:
                write(b": keepalive\n\n")  # SSE comment: keep the connection warm
                last_ka = now
            if not busy:
                sleep(0.5)
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass  # client went away
    finally:
        stop.set()
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# HTTP handler.
# ---------------------------------------------------------------------------

def make_handler(cfg):
    # Auth state shared across every request: a fresh Handler instance is created per
    # connection, but these closure vars live for the server's lifetime. The passcode
    # is resolved ONCE here from env / secrets.json / console-auth.json (see
    # resolve_auth) so all surfaces share one credential with the Pi portal.
    auth_method = resolve_auth(cfg.configs_dir)
    sessions = SessionStore()
    # Console keeps its single-window lockout (window == lockout) via the shared
    # limiter's `window=` kwarg, preserving the prior 5-fails / 5-min behavior.
    rate = RateLimiter(max_fails=RATE_LIMIT_MAX_FAILS, window=RATE_LIMIT_WINDOW_S)
    # ONE shared edge proxy client; the console talks to many gardens by request PATH
    # and passes the per-garden token per call.
    edge = EdgeClient(cfg.edge)

    def auth_enabled():
        # No passcode set anywhere => open console (localhost dev). Once a passcode
        # exists (env / secrets.json / console-auth.json), every surface but /login is
        # gated. The LAN guard applies either way.
        return auth_method.enabled

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        # -- response helpers --
        def _json(self, code, obj):
            payload = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _html(self, code, text):
            payload = text.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _bytes(self, code, content_type, body):
            body = body or b""
            self.send_response(code)
            self.send_header("Content-Type", content_type or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _read_json(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            return json.loads(raw) if raw else {}

        # -- auth guards --
        def _client_ip(self):
            return self.client_address[0] if self.client_address else ""

        def _lan_ok(self):
            """First gate on every request: reject any peer not on the local network."""
            if is_lan_addr(self._client_ip()):
                return True
            self._json(403, {"error": "forbidden — local network only"})
            return False

        def _session_token(self):
            jar = SimpleCookie(self.headers.get("Cookie", ""))
            return jar[SESSION_COOKIE].value if SESSION_COOKIE in jar else ""

        def _authed(self):
            return (not auth_enabled()) or sessions.valid(self._session_token())

        def _require_session(self):
            if self._authed():
                return True
            self._json(401, {"error": "auth required"})
            return False

        def _login(self):
            ip = self._client_ip()
            if rate.locked(ip):
                self._json(429, {"error": "too many attempts; try again later"})
                return
            if not auth_enabled():
                self._json(400, {"error": "no passcode configured"})
                return
            try:
                body = self._read_json()
            except Exception:
                self._json(400, {"error": "bad json"})
                return
            if auth_method.verify(str(body.get("passcode", ""))):
                rate.reset(ip)
                tok = sessions.mint()
                payload = json.dumps({"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Set-Cookie", f"{SESSION_COOKIE}={tok}; HttpOnly; "
                                 f"SameSite=Strict; Path=/; Max-Age={SESSION_TTL_SECONDS}")
                self.end_headers()
                self.wfile.write(payload)
            else:
                rate.record_fail(ip)
                self._json(401, {"error": "invalid passcode"})

        def _logout(self):
            sessions.drop(self._session_token())
            payload = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Set-Cookie", f"{SESSION_COOKIE}=; HttpOnly; SameSite=Strict; "
                             f"Path=/; Max-Age=0")
            self.end_headers()
            self.wfile.write(payload)

        # -- routes -----------------------------------------------------
        # Gated /api/* route tables (Phase 2), mirroring portal.py: the session gate is
        # applied ONCE in the dispatcher so it can't be forgotten when a route is added.
        # The always-open surfaces (the SPA shell at "/", favicon, shared static assets,
        # and the login/logout POSTs) stay explicit below to preserve their precedence.
        # Each value takes the handler instance + the parsed query (GET) or body (POST).
        GET_API_ROUTES = {
            "/api/console/info": lambda h, q: h._console_info(),
            "/api/console/overview": lambda h, q: h._console_overview(q.get("service_id")),
            "/api/console/state": lambda h, q: h._proxy_state(q.get("garden", "default")),
            "/api/console/stream": lambda h, q: h._stream(q),
            "/api/console/logs": lambda h, q: h._logs(q),
            "/api/console/cost": lambda h, q: h._cost(q),
            "/api/console/snapshot": lambda h, q: h._proxy_snapshot(q.get("garden", ""), q.get("device", "")),
            "/api/console/device": lambda h, q: h._proxy_device(q.get("garden", ""), q.get("device", "")),
        }
        POST_API_ROUTES = {
            "/api/console/control": lambda h, b: h._control(b.get("garden", "default"), b.get("cmd")),
            "/api/console/cost-rates": lambda h, b: h._save_rates(b),
        }

        # -- GET --
        def do_GET(self):
            if not self._lan_ok():
                return
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/":
                if auth_enabled() and not self._authed():
                    self._html(200, LOGIN_HTML)
                elif CONSOLE_HTML.exists():
                    self._html(200, CONSOLE_HTML.read_text().replace("__ASSET_VERSION__", ASSET_VERSION))
                else:
                    self._html(500, "<h1>console.html missing</h1>")
                return
            if u.path == "/favicon.ico":
                # Brand favicon (Fastly mark), served unauthenticated so it also loads
                # on the sign-in page. Sourced from the repo-root favicon.ico.
                try:
                    self._bytes(200, "image/x-icon", FAVICON_ICO.read_bytes())
                except OSError:
                    self._bytes(404, "image/x-icon", b"")
                return
            if u.path in ("/static/app.css", "/static/gp.css", "/static/gp.js"):
                # Shared UI assets, ungated like the favicon (needed by the sign-in page).
                name = u.path.rsplit("/", 1)[-1]
                ctype = "text/css; charset=utf-8" if name.endswith(".css") \
                    else "application/javascript; charset=utf-8"
                try:
                    self._bytes(200, ctype, (UI_STATIC / name).read_bytes())
                except OSError:
                    self._bytes(404, "text/plain", b"not found")
                return
            # Every /api/* GET requires a session once a passcode is configured. The gate
            # runs BEFORE the route lookup so an unknown path is still 401 (not 404) to an
            # unauthenticated caller — preserving the pre-table precedence.
            if not self._require_session():
                return
            handler = self.GET_API_ROUTES.get(u.path)
            if handler is None:
                self._json(404, {"error": "not found"})
                return
            handler(self, q)

        # -- POST --
        def do_POST(self):
            if not self._lan_ok():
                return
            u = urlparse(self.path)
            # Auth endpoints are ungated: they establish/clear the session itself.
            if u.path == "/api/console/login":
                self._login()
                return
            if u.path == "/api/console/logout":
                self._logout()
                return
            # Everything else is a management action -> require a session.
            if not self._require_session():
                return
            handler = self.POST_API_ROUTES.get(u.path)
            if handler is None:
                self._json(404, {"error": "not found"})
                return
            try:
                body = self._read_json()
            except Exception:
                self._json(400, {"error": "bad json"})
                return
            handler(self, body)

        # -- api handlers --
        def _console_info(self):
            self._json(200, {
                "mock": cfg.mock,
                "edge": cfg.edge,
                "services": list_services(cfg.configs_dir),
                "taxonomy": {
                    "kinds": sorted(taxonomy.KINDS),
                    "observer_types": sorted(taxonomy.OBSERVER_TYPES),
                    "deterrent_types": sorted(taxonomy.DETERRENT_TYPES),
                },
            })

        def _console_overview(self, sid):
            if not sid:
                self._json(400, {"error": "service_id required"})
                return
            try:
                self._json(200, read_overview(cfg.configs_dir, sid))
            except Exception as e:
                self._json(500, {"error": str(e)})

        # -- handlers --
        def _proxy_state(self, gid):
            if not cfg.edge:
                self._json(200, {"offline": True, "reason": "no edge configured"})
                return
            try:
                if gid == "default":
                    self._json(200, edge.get_json("/api/state"))
                else:
                    tok = garden_token(cfg.configs_dir, gid)
                    self._json(200, edge.get_json(f"/api/gardens/{gid}", token=tok))
            except (requests.RequestException, ValueError) as e:
                self._json(200, {"offline": True, "reason": str(e)})

        def _proxy_snapshot(self, gid, did):
            """Proxy one camera's latest JPEG, attaching the garden token server-side.
            A 404 (no snapshot yet) is passed through so the gallery shows a placeholder
            rather than erroring."""
            if not (gid and did):
                self._json(400, {"error": "garden + device required"})
                return
            if not cfg.edge:
                self._bytes(404, "text/plain", b"no edge configured")
                return
            tok = garden_token(cfg.configs_dir, gid)
            try:
                status, ctype, body = edge.get_bytes(
                    f"/api/gardens/{gid}/devices/{did}/snapshot", token=tok, timeout=5.0)
            except requests.RequestException as e:
                self._bytes(502, "text/plain", str(e).encode())
                return
            if status == 200:
                self._bytes(200, "image/jpeg", body)
            else:
                self._bytes(status, ctype, body)

        def _proxy_device(self, gid, did):
            """Combined latest event + telemetry for one device (token attached
            server-side). Best-effort: an edge hiccup yields nulls, never an error."""
            if not (gid and did):
                self._json(400, {"error": "garden + device required"})
                return
            out = {"event": None, "telemetry": None}
            if cfg.edge:
                tok = garden_token(cfg.configs_dir, gid)
                base = f"/api/gardens/{gid}/devices/{did}"
                for leaf in ("event", "telemetry"):
                    try:
                        out[leaf] = edge.get_json(f"{base}/{leaf}", token=tok)
                    except (requests.RequestException, ValueError):
                        out[leaf] = None
            self._json(200, out)

        def _control(self, gid, cmd):
            # Validate against the SSOT command vocab (off/monitor/active/stop/resume +
            # arm/disarm aliases) so the console never 400s the three-mode commands.
            if cmd not in CONTROL_COMMANDS:
                self._json(400, {"error": f"unknown cmd {cmd!r}"})
                return
            if not cfg.edge:
                self._json(503, {"error": "no edge configured"})
                return
            url, is_default = edge_control_target(cfg.edge, gid)
            tok = None if is_default else garden_token(cfg.configs_dir, gid)
            try:
                self._json(200, edge.post_json(url, {"cmd": cmd}, token=tok))
            except requests.HTTPError as e:
                code = e.response.status_code if e.response is not None else 502
                self._json(code, {"error": f"edge {code}"})
            except (requests.RequestException, ValueError) as e:
                self._json(502, {"error": str(e)})

        def _stream(self, q):
            op = q.pop("op", None)
            if op is None:
                self._json(400, {"error": "op required"})
                return
            # SSE response: no Content-Length, connection closed at end-of-stream.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            def write(frame):
                self.wfile.write(frame)
                self.wfile.flush()

            try:
                write(sse_event({"hello": True, "op": op, "mock": cfg.mock}, event="ready"))
                stream_cli(cfg, op, q, write=write)
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True

        def _logs(self, q):
            sid = q.get("service_id")
            if not sid:
                self._json(400, {"error": "service_id required"})
                return
            svc = service_config(cfg.configs_dir, sid)
            if not svc:
                self._json(404, {"error": f"no config for service {sid!r}"})
                return
            token = svc.get("fastly_api_key") or os.environ.get("FASTLY_API_KEY", "")
            # SSE response: no Content-Length, connection closed at end-of-stream.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            def write(frame):
                self.wfile.write(frame)
                self.wfile.flush()

            try:
                run_log_stream(svc, token, write)
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True

        def _cost(self, q):
            """Cost calculator: measure live usage for a service over a window and
            price it (actual-to-date + estimated monthly). Same auth/token shape as
            ``_logs``; degrades resource-by-resource so a partial Fastly/S3 outage
            still returns a renderable breakdown rather than a 500."""
            sid = q.get("service_id")
            if not sid:
                self._json(400, {"error": "service_id required"})
                return
            svc = service_config(cfg.configs_dir, sid)
            if not svc:
                self._json(404, {"error": f"no config for service {sid!r}"})
                return
            token = svc.get("fastly_api_key") or os.environ.get("FASTLY_API_KEY", "")
            # Window is a token (1h/24h/7d/30d) or a legacy bare day count; default 7d.
            win = usage_stats.parse_window(q.get("window"), default="7d")
            # Scope FOS bytes/objects to one garden via its g/<gid>/ key prefix when
            # asked (the default garden + 'all' span the whole bucket).
            garden = (q.get("garden") or "").strip()
            prefix = f"g/{garden}/" if garden and garden not in ("default", "all") else None
            rates = cost_rates.load_rates(cfg.configs_dir)
            usage = usage_stats.gather_usage(svc, token, win["token"], garden_prefix=prefix)
            self._json(200, {
                "service_id": sid,
                "service_name": svc.get("service_name"),
                "garden": garden or None,
                "window": win["token"],
                "window_label": win["label"],
                "window_days": win["days"],
                "window_hours": win["hours"],
                "usage": usage,
                "actual": cost_rates.compute_actual(usage, rates),
                "monthly": cost_rates.compute_monthly(usage, rates),
                "rates": rates,
                "mock": cfg.mock,
            })

        def _save_rates(self, body):
            """Persist edited cost rates (configs/cost-rates.json) and echo the
            merged result so the page can recompute immediately."""
            merged = cost_rates.save_rates(cfg.configs_dir, body or {})
            self._json(200, {"ok": True, "rates": merged})

    return Handler


_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", ""}


def _cmd_set_passcode(argv):
    """`python -m provision.console set-passcode` — prompt for + store the admin
    passcode (hashed, gitignored) in the console's own ``console-auth.json``. This is
    the LEGACY fallback source; on a Pi you can instead set ``GP_ADMIN_PASSCODE`` in
    ``.env`` (shared with the portal) and skip this step entirely (see ``resolve_auth``)."""
    import argparse
    import getpass
    p = argparse.ArgumentParser(prog="console set-passcode")
    p.add_argument("--configs-dir", default=None)
    args = p.parse_args(argv)
    configs_dir = pathlib.Path(args.configs_dir or orchestrator.CONFIGS_DIR)
    pw1 = getpass.getpass("New console passcode: ")
    if len(pw1) < 6:
        print("passcode too short (min 6 characters)")
        return 1
    if pw1 != getpass.getpass("Confirm passcode: "):
        print("passcodes do not match")
        return 1
    path = save_auth_record(configs_dir, make_auth_record(pw1))
    print(f"[console] passcode set -> {path} (gitignored). Restart the console to enforce it.")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "set-passcode":
        return _cmd_set_passcode(argv[1:])

    import argparse
    p = argparse.ArgumentParser(description="Fastly Garden Protector admin console (control plane)")
    p.add_argument("--listen", default="127.0.0.1:8050",
                   help="host:port (use 0.0.0.0:8050 to expose on the LAN — requires a passcode)")
    p.add_argument("--edge", default=os.environ.get("GP_BACKEND", "http://localhost:7878"),
                   help="Edge base URL for live state + control proxying")
    p.add_argument("--mock", action="store_true",
                   help="Run provisioning ops in FASTLY_MOCK_MODE (no Fastly calls)")
    p.add_argument("--configs-dir", default=None)
    p.add_argument("--env-file", default=os.environ.get("GP_ENV_FILE"),
                   help="path to .env (default <repo>/.env) — supplies GP_ADMIN_PASSCODE et al.")
    args = p.parse_args(argv)

    # Best-effort: load the Pi's .env so GP_ADMIN_PASSCODE (and friends) populate the
    # environment, exactly like the portal — a freshly-booted Pi enforces the console
    # from the same .env without the interactive `set-passcode` step. A real env var /
    # CLI flag still wins (load_env_file never overwrites an already-set var).
    load_env_file(args.env_file or str(REPO_ROOT / ".env"))

    mock = args.mock or os.environ.get("FASTLY_MOCK_MODE", "") not in ("", "0", "false")
    cfg = ConsoleConfig(configs_dir=args.configs_dir, mock=mock, edge=args.edge)
    host, _, port = args.listen.rpartition(":")

    # Safety: refuse to bind a non-loopback (LAN/public) interface without a passcode —
    # otherwise anyone on the network could manage the garden unauthenticated. The
    # passcode may come from GP_ADMIN_PASSCODE, secrets.json, or console-auth.json.
    is_loopback = host in _LOOPBACK_HOSTS
    if not is_loopback and not resolve_auth(cfg.configs_dir).enabled:
        print(f"[console] REFUSING to bind {args.listen}: no passcode set. Set "
              f"GP_ADMIN_PASSCODE in .env, or run `python -m provision.console "
              f"set-passcode`, or bind to 127.0.0.1.", flush=True)
        return 2

    server = ThreadingHTTPServer((host or "127.0.0.1", int(port)), make_handler(cfg))
    banner = "  [MOCK MODE]" if mock else ""
    print(f"[console] admin console on http://{args.listen}{banner}  edge={args.edge}", flush=True)
    auth_src = resolve_auth(cfg.configs_dir).source
    print(f"[console] admin passcode source: {auth_src}"
          + ("  (open — localhost dev only)" if auth_src == "none" else ""), flush=True)
    if not is_loopback:
        print("[console] WARNING: LAN-reachable — the passcode is the only thing guarding "
              "management. Failed logins are rate-limited per IP.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
