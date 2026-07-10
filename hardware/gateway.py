#!/usr/bin/env python3
"""hardware/gateway.py — the indoor Raspberry Pi *gateway* (Tier 1 of the push model).

The decided physical build (docs/hardware-architecture.md) puts an ESP32-C3
garden node **at the garden** (radar + pump) and moves the Pi
**indoors** as a gateway. The node never talks to Fastly directly; it pushes to
this gateway over the LAN, and the gateway aggregates, applies the local fast-path
decision, and forwards to **Fastly Compute** (the edge) over the WAN. This module
implements the Tier-1 contract from docs/endpoint-contract.md:

    POST /motion      incident report (held open ~3 s) -> {spray, seconds|reason}
    POST /frame       time-lapse / liveness still       -> {interval_s, armed, maintenance}
    POST /heartbeat   liveness + environment (no image) -> {interval_s, armed, maintenance}

and the local operator/debug surface:

    GET  /healthz     liveness of the gateway itself
    GET  /state       gateway's view (armed, node liveness, latest telemetry)
    POST /maintenance {"on": true|false}  mute the spray while gardening

Design rules carried from the rest of the codebase:
  * **Fail-closed is sacred.** A timeout / non-2xx / unreadable state on the edge
    hop must always resolve to *don't spray*. Nothing here turns a deterrent ON as
    a failure mode. The pure decision core ([`local_precheck`] / [`resolve_motion`])
    makes that auditable and is unit-tested without any I/O.
  * **Local rain suppression is the authoritative fast path** (the node owns the
    gauge; the gateway mirrors it so a wet incident never even spends an edge call).
  * **Telemetry is observe-only, best-effort, never on the spray path's latency**
    (hardware/telemetry.py): handlers open a `trace_span`, edge calls `emit`.
  * **Forward-compat identity** (X-Garden-Id / X-Device-Id / X-Node-Id /
    X-Garden-Auth / X-Garden-Trace-Id) on every edge call, default "default".

Stdlib + `requests` only (matches hardware/camera_view.py's http.server pattern).
"""
import argparse
import base64
import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

# Observe-only telemetry (best-effort; no-op until init()). Dual import so this
# works as a script and as a package, exactly like hardware/client.py.
try:
    from hardware import telemetry
except ImportError:  # running as a plain script from hardware/
    import telemetry

# Header NAMES are the Pi<->edge wire contract; pull them from the generated SSOT
# (contract/spec.toml -> provision/contract_gen.py) so a spec rename can't silently
# diverge this gateway's edge headers from what the edge reads (matches the import
# style hardware/client.py uses). Systemd runs with the repo root as the working
# directory, so the package import resolves; the fallback adds the repo root to
# sys.path for plain-script execution (sys.path[0] == hardware/).
try:
    from provision import contract_gen as _cg
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from provision import contract_gen as _cg


# ---------------------------------------------------------------------------
# Tunables (env-overridable; CLI wins). Mirror the contract's cadences.
# ---------------------------------------------------------------------------

# A node is considered DOWN after this many seconds of heartbeat silence. The
# edge derives the same at 150 s; the gateway is the *closer* watchdog (~3 missed
# 60 s beats) and is what fires the node_down alert.
NODE_OFFLINE_AFTER_SECS = 150
# Rain telemetry older than this is ignored by the local rain veto (matches the
# edge's RAIN_TELEMETRY_FRESH_SECS) so one missed beat can't re-enable spraying
# mid-shower, but a long-dead gauge doesn't suppress forever.
RAIN_TELEMETRY_FRESH_SECS = 600
# Default requested burst the gateway asks the node for; the NODE still applies
# its own hard cap (min(seconds, 4)) regardless — see endpoint-contract.md.
DEFAULT_SPRAY_SECONDS = 3
# Default time-lapse interval the gateway tells the node to use.
DEFAULT_INTERVAL_S = 300

# Hard ceiling on a request body the gateway will read into memory. The LAN-facing
# routes only ever carry small JSON (telemetry + a base64 JPEG ~ tens of KB), so a
# few-MB cap is generous for a legit node yet stops a malicious/buggy LAN device
# from exhausting the Pi's RAM with an oversized (or lying Content-Length) POST. A
# Content-Length over the cap is rejected 413 WITHOUT reading; the actual read is
# also capped so a lying/absent length can't over-allocate. Override via env.
MAX_BODY_BYTES = int(os.environ.get("GP_GATEWAY_MAX_BODY_BYTES", str(4 * 1024 * 1024)))


class BodyTooLarge(Exception):
    """Raised by the handler's body reader when a request exceeds MAX_BODY_BYTES,
    so do_POST can answer 413 (Payload Too Large) instead of the generic 400."""


def now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# PURE DECISION CORE (no I/O, no clock reads — `now_ms` is passed in). This is
# the whole fail-closed spray decision, isolated so it can be exhaustively
# unit-tested. The HTTP handlers below are a thin shell over these.
# ---------------------------------------------------------------------------

# Schedule / environment veto reasons, in order of authority (highest first). A
# match here means the gateway can answer the node WITHOUT an edge round-trip.
def local_precheck(armed, override_stop, maintenance, irrigation, rain_active):
    """Local, pre-edge veto. Returns a reason string to withhold the spray, or
    ``None`` to proceed to the edge classification.

    Order encodes authority: an emergency STOP outranks a maintenance mute, which
    outranks the irrigation window, which outranks rain. All of these are *safe*
    withholds — none can ever turn a spray on."""
    if not armed:
        return "disarmed"
    if override_stop:
        return "stop"
    if maintenance:
        return "maintenance"
    if irrigation:
        return "irrigation"
    if rain_active:
        return "rain"
    return None


def resolve_motion(precheck_reason, edge_ok, edge_action, edge_reason, spray_seconds):
    """Final spray decision for a /motion incident. FAIL-CLOSED by construction:
    every non-``mitigate`` branch returns ``spray:false``.

    * ``precheck_reason`` set       -> withheld locally (never hit the edge).
    * ``edge_ok`` false             -> edge timed out / errored -> stand down.
    * edge ``action == "mitigate"`` -> spray (node still caps the seconds).
    * edge ``action == "none"``     -> withheld; surface the edge's reason
      (e.g. "rain"/"human") or a generic "veto"."""
    if precheck_reason is not None:
        return {"spray": False, "reason": precheck_reason}
    if not edge_ok:
        return {"spray": False, "reason": "edge_unreachable"}
    if edge_action == "mitigate":
        return {"spray": True, "seconds": spray_seconds}
    return {"spray": False, "reason": edge_reason or "veto"}


def rain_active(telemetry_blob, now, fresh_secs=RAIN_TELEMETRY_FRESH_SECS):
    """True iff the freshest local telemetry says it is actively raining AND the
    reading is fresh. Mirrors the edge's `rain_should_suppress` freshness rule so
    the local fast path and the edge backstop agree. Stale/absent -> False (worst
    case a harmless spray in the rain)."""
    if not isinstance(telemetry_blob, dict):
        return False
    if not telemetry_blob.get("raining"):
        return False
    ts = telemetry_blob.get("last_local_ms")
    if not isinstance(ts, (int, float)):
        return False
    if now < ts:
        return True  # clock skew -> treat as fresh
    return (now - ts) / 1000.0 <= fresh_secs


def node_is_down(last_seen_ms, now, offline_after_secs=NODE_OFFLINE_AFTER_SECS):
    """Liveness derivation: a node never seen, or silent past the window, is DOWN."""
    if not isinstance(last_seen_ms, (int, float)):
        return True
    if now < last_seen_ms:
        return False  # future stamp (skew) -> just seen
    return (now - last_seen_ms) / 1000.0 > offline_after_secs


def in_irrigation_window(windows, now_local_minutes):
    """True if the current local time-of-day (minutes since midnight) falls in any
    configured ``[start, end)`` irrigation-suppression window. Windows are pairs of
    minutes; a window that wraps midnight (start > end) is supported."""
    for start, end in windows or ():
        if start <= end:
            if start <= now_local_minutes < end:
                return True
        else:  # wraps midnight, e.g. 23:30 -> 00:30
            if now_local_minutes >= start or now_local_minutes < end:
                return True
    return False


def minutes_of_day(epoch_secs, tz_offset_minutes=0):
    """Local minutes-since-midnight for an epoch time + fixed UTC offset (pure)."""
    local = int(epoch_secs) + tz_offset_minutes * 60
    return (local % 86400) // 60


# ---------------------------------------------------------------------------
# GATEWAY STATE — the live, mutable view. All mutation goes through a lock; the
# pure core above reads snapshots of it.
# ---------------------------------------------------------------------------

class GatewayState:
    """Thread-safe holder for the gateway's operating view: arm-state synced from
    the edge, the local maintenance mute, and the latest node telemetry/liveness."""

    def __init__(self, *, spray_seconds=DEFAULT_SPRAY_SECONDS, interval_s=DEFAULT_INTERVAL_S,
                 irrigation_windows=None, tz_offset_minutes=0):
        self._lock = threading.Lock()
        # Synced from the edge GET /api/state (authoritative arm/override). Start
        # fail-closed (disarmed) until the first successful sync.
        self.armed = False
        self.override_stop = False
        # Local-only mute applied on the next heartbeat (gardening). Not on the edge.
        self.maintenance = False
        # Latest node telemetry blob (folded from /heartbeat and /motion), stamped
        # with the gateway's own `last_local_ms` receipt clock.
        self.latest_telemetry = {}
        self.last_seen_ms = None
        self.node_down = True  # until first beat
        # Config.
        self.spray_seconds = spray_seconds
        self.interval_s = interval_s
        self.irrigation_windows = irrigation_windows or []
        self.tz_offset_minutes = tz_offset_minutes
        # Counters for the debug surface.
        self.motions = 0
        self.sprays = 0
        self.frames = 0
        self.heartbeats = 0

    def snapshot(self):
        with self._lock:
            return {
                "armed": self.armed,
                "override_stop": self.override_stop,
                "maintenance": self.maintenance,
                "node_down": self.node_down,
                "last_seen_ms": self.last_seen_ms,
                "latest_telemetry": dict(self.latest_telemetry),
                "interval_s": self.interval_s,
                "counters": {
                    "motions": self.motions, "sprays": self.sprays,
                    "frames": self.frames, "heartbeats": self.heartbeats,
                },
            }

    def set_arm_state(self, armed, override_stop):
        with self._lock:
            self.armed = bool(armed)
            self.override_stop = bool(override_stop)

    def set_maintenance(self, on):
        with self._lock:
            self.maintenance = bool(on)

    def fold_telemetry(self, blob, now):
        """Merge a node-reported telemetry blob into the live view, stamping the
        gateway receipt clock and refreshing liveness."""
        if not isinstance(blob, dict):
            blob = {"raw": blob}
        with self._lock:
            self.latest_telemetry.update(blob)
            self.latest_telemetry["last_local_ms"] = now
            self.last_seen_ms = now
            self.node_down = False

    def operating_params(self):
        """The {interval_s, armed, maintenance} block returned to the node. The
        node sees ``armed`` as the *effective* armed (armed AND not stopped)."""
        with self._lock:
            return {
                "interval_s": self.interval_s,
                "armed": self.armed and not self.override_stop,
                "maintenance": self.maintenance,
            }

    def motion_inputs(self, now):
        """Snapshot of everything `local_precheck` needs for a /motion decision."""
        with self._lock:
            irrigation = in_irrigation_window(
                self.irrigation_windows, minutes_of_day(now / 1000.0, self.tz_offset_minutes)
            )
            return {
                "armed": self.armed,
                "override_stop": self.override_stop,
                "maintenance": self.maintenance,
                "irrigation": irrigation,
                "rain_active": rain_active(self.latest_telemetry, now),
                "spray_seconds": self.spray_seconds,
            }


# ---------------------------------------------------------------------------
# EDGE CLIENT — the WAN hop to Fastly Compute. All identity headers + the
# per-incident trace id live here. Fail-closed: callers treat any exception as
# "edge not ok".
# ---------------------------------------------------------------------------

class EdgeClient:
    def __init__(self, base_url, *, garden_id="default", device_id="default",
                 node_id="default", token="", timeout=3.0):
        self.base_url = base_url.rstrip("/")
        self.garden_id = garden_id
        self.device_id = device_id
        self.node_id = node_id
        self.token = token
        self.timeout = timeout

    def _headers(self, trace_id, extra=None):
        # Header keys come from the generated contract constants (not literals) so a
        # `make gen` rename in spec.toml can't silently diverge this gateway from the
        # edge — see hardware/client.py._request_headers for the same pattern.
        h = {
            _cg.HEADER_TRACE_ID: trace_id or "",
            _cg.HEADER_GARDEN_ID: self.garden_id,
            _cg.HEADER_DEVICE_ID: self.device_id,
            _cg.HEADER_NODE_ID: self.node_id,
        }
        if self.token:
            h[_cg.HEADER_AUTH] = self.token
        if extra:
            h.update(extra)
        return h

    def post_evidence(self, jpeg_bytes, trace_id):
        """Forward the incident JPEG to /api/evidence as a raw image body (the
        contract the edge decodes). Returns the parsed verdict dict or raises."""
        t0 = time.perf_counter()
        outcome = "ok"
        try:
            r = requests.post(
                f"{self.base_url}/api/evidence",
                data=jpeg_bytes,
                headers=self._headers(trace_id, {"Content-Type": "image/jpeg"}),
                timeout=self.timeout + 27.0,  # evidence carries the generous 30 s budget
            )
            r.raise_for_status()
            return r.json()
        except Exception:
            outcome = "error"
            raise
        finally:
            telemetry.emit("http", "POST /api/evidence", cid=trace_id,
                           dur_ms=(time.perf_counter() - t0) * 1000.0, outcome=outcome)

    def post_telemetry(self, blob, trace_id):
        t0 = time.perf_counter()
        outcome = "ok"
        try:
            r = requests.post(
                f"{self.base_url}/api/telemetry",
                json=blob,
                headers=self._headers(trace_id, {"Content-Type": "application/json"}),
                timeout=self.timeout,
            )
            r.raise_for_status()
            return r.json() if r.content else {}
        except Exception:
            outcome = "error"
            raise
        finally:
            telemetry.emit("http", "POST /api/telemetry", cid=trace_id,
                           dur_ms=(time.perf_counter() - t0) * 1000.0, outcome=outcome)

    def get_state(self, trace_id):
        r = requests.get(
            f"{self.base_url}/api/state",
            headers=self._headers(trace_id),
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()

    def post_alert(self, event, node_id, last_seen_ms, detail=None, trace_id=None):
        """Notify the edge of a liveness transition so it can dispatch an SMS (the
        SMS credentials stay at the edge). Best-effort: callers swallow errors."""
        trace_id = trace_id or uuid.uuid4().hex[:16]
        body = {"event": event, "node_id": node_id, "last_seen_ms": last_seen_ms}
        if detail:
            body["detail"] = detail
        r = requests.post(
            f"{self.base_url}/api/alert", json=body,
            headers=self._headers(trace_id, {"Content-Type": "application/json"}),
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json() if r.content else {}


# ---------------------------------------------------------------------------
# ALERTING — a DOWN node fails *closed* (no spray) so loss of protection is
# silent. The production path is edge -> Twilio (a `node_down` SMS); that needs
# edge credentials, so here the alerter is pluggable: it logs, and optionally
# POSTs to a configured webhook (which a Twilio function / the edge can own).
# ---------------------------------------------------------------------------

class Alerter:
    def __init__(self, webhook_url=None, timeout=2.0):
        self.webhook_url = webhook_url
        self.timeout = timeout

    def node_down(self, node_id, last_seen_ms):
        msg = (f"[gateway] ALERT node_down node_id={node_id} "
               f"last_seen_ms={last_seen_ms} -> protection is now SILENT (fail-closed: no spray)")
        print(msg, flush=True)
        telemetry.emit("alert", "node_down", args={"node_id": node_id, "last_seen_ms": last_seen_ms},
                       outcome="warn", detail="node went DOWN")
        if self.webhook_url:
            try:
                requests.post(self.webhook_url, json={
                    "event": "node_down", "node_id": node_id, "last_seen_ms": last_seen_ms,
                }, timeout=self.timeout)
            except Exception as e:  # best-effort; an unreachable alerter must not crash the gateway
                print(f"[gateway] node_down webhook failed: {e}", flush=True)


# ---------------------------------------------------------------------------
# BACKGROUND WORKERS — arm-state sync + liveness watchdog.
# ---------------------------------------------------------------------------

class Gateway:
    """Wires state + edge client + alerter together and owns the HTTP server and
    background threads."""

    def __init__(self, state, edge, alerter, *, sync_interval=5.0, watchdog_interval=10.0):
        self.state = state
        self.edge = edge
        self.alerter = alerter
        self.sync_interval = sync_interval
        self.watchdog_interval = watchdog_interval
        self._stop = threading.Event()
        self._threads = []

    # -- /motion handling (the safety path) --------------------------------
    def handle_motion(self, body):
        """Apply the local precheck, round-trip the edge if needed, and return the
        spray directive. Mints a per-incident 16-hex trace id, like the Pi client."""
        trace_id = uuid.uuid4().hex[:16]
        telemetry.set_cid(trace_id)
        with self.state._lock:
            self.state.motions += 1

        # Fold any telemetry carried with the incident (e.g. a fresh rain reading)
        # so the local rain veto sees it before deciding.
        now = now_ms()
        if isinstance(body.get("telemetry"), dict):
            self.state.fold_telemetry(body["telemetry"], now)

        inputs = self.state.motion_inputs(now)
        precheck = local_precheck(inputs["armed"], inputs["override_stop"],
                                  inputs["maintenance"], inputs["irrigation"],
                                  inputs["rain_active"])

        edge_ok, edge_action, edge_reason = True, "none", None
        if precheck is None:
            # Only spend an edge classification when nothing local already vetoed.
            jpeg = _extract_jpeg(body)
            if jpeg is None:
                edge_ok, edge_reason = False, "no_image"
            else:
                try:
                    verdict = self.edge.post_evidence(jpeg, trace_id)
                    edge_action = verdict.get("action", "none")
                    edge_reason = verdict.get("reason")
                except Exception as e:
                    edge_ok = False
                    print(f"[gateway] /motion edge call failed: {e} -> fail-closed (no spray)", flush=True)

        decision = resolve_motion(precheck, edge_ok, edge_action, edge_reason,
                                  inputs["spray_seconds"])
        if decision.get("spray"):
            with self.state._lock:
                self.state.sprays += 1
        telemetry.emit("motion", "decide", cid=trace_id, args={
            "precheck": precheck, "edge_ok": edge_ok, "edge_action": edge_action,
            "spray": decision.get("spray"),
        }, detail=decision.get("reason"))
        telemetry.set_cid(None)
        return decision

    # -- /heartbeat + /frame handling (liveness path) ----------------------
    def handle_heartbeat(self, body):
        now = now_ms()
        self.state.fold_telemetry(body, now)
        with self.state._lock:
            self.state.heartbeats += 1
        trace_id = uuid.uuid4().hex[:16]
        # Forward the environment up to the edge so the dashboard health tiles +
        # the edge rain backstop see it. Best-effort: a telemetry hiccup must not
        # fail the node's heartbeat.
        try:
            self.edge.post_telemetry(_telemetry_for_edge(body), trace_id)
        except Exception as e:
            print(f"[gateway] heartbeat -> edge telemetry forward failed: {e}", flush=True)
        return self.state.operating_params()

    def handle_frame(self, body):
        now = now_ms()
        # A frame doubles as a liveness beat; fold the light telemetry it carries.
        light = {k: body[k] for k in ("battery_voltage", "rssi") if k in body}
        self.state.fold_telemetry(light, now)
        with self.state._lock:
            self.state.frames += 1
        # Time-lapse frames are archived locally, not classified (no incident).
        jpeg = _extract_jpeg(body)
        if jpeg is not None and self._archive_dir:
            _archive_frame(self._archive_dir, jpeg, now)
        return self.state.operating_params()

    _archive_dir = None

    # -- background loops (each is a thin retry wrapper over a single-shot op,
    #    so the single-shot ops can be unit-tested without threads/sleeps) ----
    def sync_once(self):
        """Pull the authoritative arm/override flags from the edge into local state.
        On an edge error, HOLD the last known arm-state (the /motion path is
        independently fail-closed on its own edge call)."""
        try:
            st = self.edge.get_state(uuid.uuid4().hex[:16])
            self.state.set_arm_state(st.get("armed", False), st.get("override_stop", False))
            return True
        except Exception as e:
            telemetry.emit("sync", "arm_state", outcome="error", detail=str(e))
            return False

    def check_liveness_once(self):
        """Re-derive node liveness and fire a node_down alert exactly once on the
        ONLINE->DOWN transition. Returns the current `down` boolean."""
        snap = self.state.snapshot()
        down = node_is_down(snap["last_seen_ms"], now_ms())
        with self.state._lock:
            was_down = self.state.node_down
            self.state.node_down = down
        if down and not was_down:
            self.alerter.node_down(self.edge.node_id, snap["last_seen_ms"])
            # Also notify the edge so it can SMS (creds stay at the edge). Best-effort:
            # an unreachable/unconfigured edge alert must never crash the watchdog.
            try:
                secs = None
                if isinstance(snap["last_seen_ms"], (int, float)):
                    secs = int((now_ms() - snap["last_seen_ms"]) / 1000)
                detail = f"last seen {secs}s ago" if secs is not None else "never seen"
                self.edge.post_alert("node_down", self.edge.node_id, snap["last_seen_ms"], detail=detail)
            except Exception as e:
                print(f"[gateway] edge node_down notify failed: {e} (best-effort)", flush=True)
        return down

    def _sync_loop(self):
        while not self._stop.wait(self.sync_interval):
            self.sync_once()

    def _watchdog_loop(self):
        while not self._stop.wait(self.watchdog_interval):
            self.check_liveness_once()

    def start_background(self):
        for target in (self._sync_loop, self._watchdog_loop):
            t = threading.Thread(target=target, name=f"gw-{target.__name__}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop.set()


# ---------------------------------------------------------------------------
# Body helpers (pure-ish).
# ---------------------------------------------------------------------------

# Internal bookkeeping keys we add to telemetry that should NOT be forwarded to
# the edge (the edge stamps its own last_seen_ms).
_INTERNAL_TELEMETRY_KEYS = {"last_local_ms"}


def _telemetry_for_edge(body):
    """Strip the node's own envelope fields the edge doesn't want, keep the
    environment + optional-peripheral readings verbatim (the edge stores any JSON)."""
    drop = {"ts_ms", "jpeg", "jpeg_b64", "bed"} | _INTERNAL_TELEMETRY_KEYS
    return {k: v for k, v in body.items() if k not in drop}


def _extract_jpeg(body):
    """Pull JPEG bytes from a parsed JSON body: base64 in ``jpeg_b64`` (what the
    node simulator sends). Returns bytes or None."""
    b64 = body.get("jpeg_b64")
    if isinstance(b64, str) and b64:
        try:
            return base64.b64decode(b64)
        except Exception:
            return None
    return None


# Ring-buffer cap for the optional, debug-only local frame archive (PI-003). The
# archive is unset in every deploy unit (opt-in via --archive-dir / $GP_FRAME_ARCHIVE),
# but if an operator turns it on we must not let a node fill the Pi's disk: keep at
# most this many newest frames, pruning the oldest after each write. Override with
# $GP_FRAME_ARCHIVE_MAX. The edge owns the real archive; this is a local debug aid only.
FRAME_ARCHIVE_MAX = int(os.environ.get("GP_FRAME_ARCHIVE_MAX", "500"))


def _archive_frame(archive_dir, jpeg, ts_ms, max_frames=FRAME_ARCHIVE_MAX):
    """Write one frame into the local debug archive, then enforce a count cap so the
    directory is a bounded ring buffer (never grows without limit). Best-effort: any
    I/O error is logged and swallowed — the archive is a debug aid, never on a safety
    path (the spray decision is on /motion, which never archives)."""
    try:
        os.makedirs(archive_dir, exist_ok=True)
        path = os.path.join(archive_dir, f"frame-{ts_ms}.jpg")
        with open(path, "wb") as f:
            f.write(jpeg)
        _prune_frame_archive(archive_dir, max_frames)
    except Exception as e:
        print(f"[gateway] frame archive failed: {e}", flush=True)


def _prune_frame_archive(archive_dir, max_frames):
    """Keep only the ``max_frames`` newest ``frame-*.jpg`` files; delete the rest.
    Frame names embed a monotonic ms timestamp, so lexical sort == chronological."""
    if max_frames <= 0:
        return
    frames = sorted(f for f in os.listdir(archive_dir)
                    if f.startswith("frame-") and f.endswith(".jpg"))
    for stale in frames[:-max_frames]:
        try:
            os.remove(os.path.join(archive_dir, stale))
        except OSError:
            pass


# ---------------------------------------------------------------------------
# HTTP SERVER — thin shell over the Gateway object.
# ---------------------------------------------------------------------------

def make_handler(gw):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quiet; telemetry covers observability
            pass

        def _send_json(self, code, obj):
            payload = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _read_json(self):
            # Body-size cap (Pi gateway hardening): a malicious/buggy LAN device must
            # not be able to exhaust the Pi's RAM via an oversized POST.
            #   1. A declared Content-Length over the cap is rejected WITHOUT reading.
            #   2. Otherwise read EXACTLY the declared length (already <= cap, so the
            #      allocation is bounded). We must never read past Content-Length: on a
            #      real blocking socket rfile.read(n) waits for n bytes, so over-reading
            #      would hang a keep-alive connection until the client times out.
            #   3. No (or zero) Content-Length -> no body to read (don't block).
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length > MAX_BODY_BYTES:
                raise BodyTooLarge(length)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            if not raw:
                return {}
            return json.loads(raw)

        def do_GET(self):
            if self.path.split("?")[0] == "/healthz":
                self._send_json(200, {"ok": True, "ts_ms": now_ms()})
            elif self.path.split("?")[0] == "/state":
                self._send_json(200, gw.state.snapshot())
            else:
                self._send_json(404, {"error": "not found"})

        def do_POST(self):
            route = self.path.split("?")[0]
            try:
                body = self._read_json()
            except BodyTooLarge:
                self._send_json(413, {"error": "payload too large"})
                return
            except Exception:
                self._send_json(400, {"error": "bad json"})
                return
            try:
                if route == "/motion":
                    with telemetry.trace_span("gateway", "POST /motion"):
                        self._send_json(200, gw.handle_motion(body))
                elif route == "/heartbeat":
                    with telemetry.trace_span("gateway", "POST /heartbeat"):
                        self._send_json(200, gw.handle_heartbeat(body))
                elif route == "/frame":
                    with telemetry.trace_span("gateway", "POST /frame"):
                        self._send_json(200, gw.handle_frame(body))
                elif route == "/maintenance":
                    gw.state.set_maintenance(bool(body.get("on")))
                    self._send_json(200, gw.state.operating_params())
                else:
                    self._send_json(404, {"error": "not found"})
            except Exception as e:
                # Even an unexpected server error must not become a spray: /motion
                # already returned fail-closed inside handle_motion; here we only
                # reach this for truly unexpected faults. Answer 500 (the node
                # treats any non-spray reply as no-spray).
                self._send_json(500, {"error": str(e)})

    return Handler


def build_gateway(args):
    state = GatewayState(
        spray_seconds=args.spray_seconds,
        interval_s=args.interval_s,
        irrigation_windows=_parse_windows(args.irrigation_window),
        tz_offset_minutes=args.tz_offset_minutes,
    )
    edge = EdgeClient(args.edge, garden_id=args.garden_id, device_id=args.device_id,
                      node_id=args.node_id, token=args.garden_token, timeout=args.edge_timeout)
    alerter = Alerter(webhook_url=args.alert_webhook)
    gw = Gateway(state, edge, alerter)
    gw._archive_dir = args.archive_dir
    return gw


def _parse_windows(specs):
    """Parse ``HH:MM-HH:MM`` strings into (start_min, end_min) pairs."""
    windows = []
    for spec in specs or ():
        try:
            a, b = spec.split("-")
            sh, sm = (int(x) for x in a.split(":"))
            eh, em = (int(x) for x in b.split(":"))
            windows.append((sh * 60 + sm, eh * 60 + em))
        except Exception:
            print(f"[gateway] ignoring bad --irrigation-window '{spec}'", flush=True)
    return windows


def _apply_pi_config(args):
    """When the Pi is provisioned, pi-garden.json + secrets.json (configs/) are the
    SOURCE OF TRUTH for the edge URL, garden id and token — exactly as the portal
    (reload_identity) and the camera daemon (--auto) treat them. Overlay them so a
    stale ``.env`` (e.g. a leftover ``GP_BACKEND`` pointing at an old service, or
    ``GP_GARDEN_ID=default``) can't silently route the node's heartbeat to the wrong
    garden — which makes the real dashboard show the node as offline. The Pi's
    node_id is adopted too; device_id is left as-is (to_env's pick is a camera)."""
    try:
        from hardware import pi_config as _pc
    except ImportError:  # plain-script execution
        import pi_config as _pc
    try:
        pc = _pc.PiConfig(args.configs_dir)
        if not pc.is_provisioned():
            return
        env = pc.to_env()
    except Exception as e:  # noqa: BLE001 — never block startup on config read
        print(f"[gateway] pi_config overlay skipped: {e}", flush=True)
        return
    if env.get("GP_BACKEND"):
        args.edge = env["GP_BACKEND"].rstrip("/")
    if env.get("GP_GARDEN_ID"):
        args.garden_id = env["GP_GARDEN_ID"]
    if env.get("GP_NODE_ID"):
        args.node_id = env["GP_NODE_ID"]
    if env.get("GP_GARDEN_TOKEN"):
        args.garden_token = env["GP_GARDEN_TOKEN"]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fastly Garden Protector indoor Pi gateway (Tier 1)")
    parser.add_argument("--listen", default="0.0.0.0:8088", help="LAN bind addr (default 0.0.0.0:8088)")
    parser.add_argument("--edge", default=os.environ.get("GP_BACKEND", "http://localhost:7878"),
                        help="Fastly Compute edge base URL (env GP_BACKEND)")
    parser.add_argument("--garden-id", default=os.environ.get("GP_GARDEN_ID", "default"))
    parser.add_argument("--device-id", default=os.environ.get("GP_DEVICE_ID", "default"))
    parser.add_argument("--node-id", default=os.environ.get("GP_NODE_ID", "default"))
    parser.add_argument("--garden-token", default=os.environ.get("GP_GARDEN_TOKEN", ""))
    parser.add_argument("--edge-timeout", type=float, default=3.0)
    parser.add_argument("--spray-seconds", type=int, default=DEFAULT_SPRAY_SECONDS)
    parser.add_argument("--interval-s", type=int, default=DEFAULT_INTERVAL_S)
    parser.add_argument("--irrigation-window", action="append",
                        help="Suppress sprays in this local window, HH:MM-HH:MM (repeatable)")
    parser.add_argument("--tz-offset-minutes", type=int, default=0,
                        help="Fixed UTC offset for the irrigation-window clock (e.g. -240 for EDT)")
    parser.add_argument("--alert-webhook", default=os.environ.get("GP_ALERT_WEBHOOK"),
                        help="Optional URL POSTed on a node_down transition")
    parser.add_argument("--archive-dir", default=os.environ.get("GP_FRAME_ARCHIVE"),
                        help="DEBUG-ONLY: dir to archive time-lapse frames locally "
                             "(bounded ring buffer, newest GP_FRAME_ARCHIVE_MAX frames; "
                             "the edge owns the real archive). Unset in all deploy units.")
    parser.add_argument("--configs-dir", default=os.environ.get("GP_CONFIGS_DIR", "configs"),
                        help="pi-garden.json/secrets.json dir; when provisioned it is the "
                             "source of truth for edge/garden/token (overrides a stale .env)")
    args = parser.parse_args(argv)
    _apply_pi_config(args)

    telemetry.init(garden_id=args.garden_id, device_id=args.device_id, node_id=args.node_id)
    gw = build_gateway(args)
    gw.start_background()

    host, _, port = args.listen.rpartition(":")
    server = ThreadingHTTPServer((host or "0.0.0.0", int(port)), make_handler(gw))
    print(f"[gateway] listening on {args.listen} -> edge {args.edge} "
          f"(garden={args.garden_id} device={args.device_id})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        gw.stop()
        server.shutdown()
        telemetry.shutdown()


if __name__ == "__main__":
    main()
