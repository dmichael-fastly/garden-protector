#!/usr/bin/env python3
"""tests/fake_edge.py — a deterministic stand-in for the Fastly Compute edge.

Implements the Tier-2 contract (docs/endpoint-contract.md) that the gateway and
dashboard talk to, but classifies images by their synthetic scene colour
(hardware/scenes.classify_jpeg) instead of running MobileNet. That makes the whole
node -> gateway -> edge loop fully deterministic for tests and offline demos, while
the *real* edge stays the honest ML path.

It reproduces the behaviours the rest of the system depends on:
  * `/api/evidence` -> classify, apply the **rain veto** (raining + fresh -> none,
    reason "rain"), publish latest event + snapshot.
  * `/api/telemetry` -> stamp `last_seen_ms`, store the blob.
  * `/api/state` / `/api/status` -> armed/override/continue + node liveness block.
  * `/api/control` -> arm/disarm/stop/resume.
  * `/api/snapshot` -> latest JPEG.

Usable as a library (`FakeEdge(...).start()` returns a base URL) or a script.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from hardware import scenes
except ImportError:
    import sys
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from hardware import scenes

# Thresholds come from the GENERATED cross-language contract (contract/spec.toml ->
# provision/contract_gen.py) so this test double can't silently drift from the real
# edge's liveness/rain-veto windows. (repo root is on sys.path: pytest's rootdir, or
# the ImportError fallback above when run as a script.)
from provision.contract_gen import (  # noqa: E402
    NODE_OFFLINE_AFTER_SECS,
    RAIN_TELEMETRY_FRESH_SECS,
)


def _now_ms():
    return int(time.time() * 1000)


def derive_mode(armed, override_stop):
    """Mirror of the edge's derive_mode: armed=false -> off; armed+held -> monitor;
    armed+free -> active."""
    if not armed:
        return "off"
    return "monitor" if override_stop else "active"


def apply_control(cmd, armed, override_stop):
    """Mirror of the edge's apply_control MODE-setting truth table. Returns the new
    (armed, override_stop), or None for stop/resume (which mutate abort_cid, handled
    separately) and for truly unknown commands."""
    return {
        "off": (False, override_stop),
        "disarm": (False, override_stop),  # alias of off
        "monitor": (True, True),
        "active": (True, False),
        "arm": (True, False),  # alias of active
    }.get(cmd)


def rain_should_suppress(action, telemetry, now):
    if action != "mitigate" or not isinstance(telemetry, dict):
        return False
    if not telemetry.get("raining"):
        return False
    ts = telemetry.get("last_seen_ms")
    if not isinstance(ts, (int, float)):
        return False
    if now < ts:
        return True
    return (now - ts) / 1000.0 <= RAIN_TELEMETRY_FRESH_SECS


class FakeEdge:
    def __init__(self, host="127.0.0.1", port=0, *, armed=True):
        self.host = host
        self.port = port
        self._lock = threading.Lock()
        self.armed = armed
        self.override_stop = False
        # Per-event smart-Stop state (mirror of the edge's per-garden KV fields).
        self.abort_cid = ""
        self.last_mitigate_cid = ""
        self.latest_event = None
        self.latest_telemetry = None
        self.latest_image = None
        self._server = None
        self._thread = None
        self.requests = []  # (method, path) audit trail for assertions
        self.raw_requests = []  # (method, full-path-with-query) — for proxy query-forwarding asserts
        # Archive/maintenance stand-ins (the portal proxies these read endpoints + the
        # prune POST; the fake just echoes canned data + records the forwarded query).
        self.archive_days = []
        self.archive_events = []
        self.cameras = []
        self.prune_result = {"deleted": 0, "days_pruned": 0, "failed": 0, "remaining": False, "cutoff": None}
        self.wipe_result = {"deleted": 0, "days_wiped": 0, "failed": 0, "remaining": False}
        # Alarm stand-ins (the portal proxies the tag/delete/prune/wipe POSTs + the /api/alarms
        # read + the per-key /api/alarm GET). The fake records a tag per id + echoes canned alarm
        # data, enough to exercise the portal's proxy + admin + retention plumbing.
        self.alarm_tags = {}  # id -> label
        self.alarms_result = {"threshold_pct": 30, "min_labels": 3, "can_manage": True,
                              "recommendations": [], "alarms": []}
        self.alarm_prune_result = {"ok": True, "deleted": 0, "kept": 0}
        self.alarm_wipe_result = {"ok": True, "deleted": 0}

    # -- request handlers (pure-ish; hold the lock) -----------------------
    def on_evidence(self, jpeg, cid=""):
        verdict = scenes.classify_jpeg(jpeg)
        now = _now_ms()
        with self._lock:
            tel = self.latest_telemetry
            if rain_should_suppress(verdict.get("action"), tel, now):
                verdict["action"] = "none"
                verdict["reason"] = "rain"
            self.latest_image = jpeg
            self.latest_event = {
                "species": verdict.get("species"),
                "confidence": verdict.get("confidence"),
                "action": verdict.get("action"),
                "reason": verdict.get("reason"),
                "ts": now,
            }
            # On a new mitigate decision, stamp last_mitigate_cid + auto-clear abort_cid
            # (mirror of record_mitigate_cid). The cid is the request trace id if sent.
            if verdict.get("action") == "mitigate":
                self.last_mitigate_cid = cid or ""
                self.abort_cid = ""
        return {k: verdict[k] for k in ("action", "species", "confidence") if k in verdict} | (
            {"reason": verdict["reason"]} if verdict.get("reason") else {})

    def on_telemetry(self, blob):
        now = _now_ms()
        if not isinstance(blob, dict):
            blob = {"raw": blob}
        blob = dict(blob)
        blob["last_seen_ms"] = now
        with self._lock:
            self.latest_telemetry = blob
        return {"ok": True}

    def state(self):
        now = _now_ms()
        with self._lock:
            tel = self.latest_telemetry
            last_seen = tel.get("last_seen_ms") if isinstance(tel, dict) else None
            online = last_seen is not None and (now - last_seen) / 1000.0 <= NODE_OFFLINE_AFTER_SECS
            since = int((now - last_seen) / 1000.0) if last_seen else None
            return {
                "mode": derive_mode(self.armed, self.override_stop),
                "armed": self.armed,
                "override_stop": self.override_stop,
                "continue_mitigation": self.armed and not self.override_stop,
                "latest_event": self.latest_event,
                "node": {
                    "online": bool(online),
                    "last_seen_ms": last_seen,
                    "seconds_since": since,
                    "telemetry": tel,
                },
            }

    def control(self, cmd):
        with self._lock:
            # stop/resume mutate abort_cid (the per-event smart Stop), not the mode tuple.
            if cmd == "stop":
                self.abort_cid = self.last_mitigate_cid  # one-shot abort of the live spray
                return self.state()
            if cmd == "resume":
                self.abort_cid = ""
                return self.state()
            res = apply_control(cmd, self.armed, self.override_stop)
            if res is None:
                return None
            self.armed, self.override_stop = res
        return self.state()

    # -- server lifecycle -------------------------------------------------
    def start(self):
        edge = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _json(self, code, obj):
                payload = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def _send_image(self, img):
                if img is None:
                    self._json(404, {"error": "no snapshot"})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(img)))
                self.end_headers()
                self.wfile.write(img)

            def do_GET(self):
                path = self.path.split("?")[0]
                edge.requests.append(("GET", path))
                edge.raw_requests.append(("GET", self.path))
                if path in ("/api/state",):
                    self._json(200, edge.state())
                elif path == "/api/status":
                    # Mirror the edge heartbeat: the fail-closed floor ANDed with the
                    # per-event abort. event_cid comes from the live event's trace header
                    # (empty -> never matches a non-empty abort_cid).
                    event_cid = self.headers.get("X-Garden-Trace-Id") or ""
                    with edge._lock:
                        floor = edge.armed and not edge.override_stop
                        aborted = bool(edge.abort_cid) and event_cid == edge.abort_cid
                    self._json(200, {"continue_mitigation": floor and not aborted})
                elif path == "/api/snapshot":
                    with edge._lock:
                        img = edge.latest_image
                    self._send_image(img)
                elif path == "/api/cameras":
                    self._json(200, {"cameras": edge.cameras})
                elif path == "/api/gadget":
                    with edge._lock:
                        self._json(200, {"event": edge.latest_event, "telemetry": edge.latest_telemetry})
                elif path == "/api/archive/days":
                    self._json(200, {"days": edge.archive_days})
                elif path == "/api/archive/image":
                    with edge._lock:
                        img = edge.latest_image
                    self._send_image(img)
                elif path == "/api/archive":
                    self._json(200, {"events": edge.archive_events})
                elif path == "/api/alarms":
                    self._json(200, edge.alarms_result)
                elif path == "/api/alarm":
                    from urllib.parse import urlparse, parse_qs
                    key = (parse_qs(urlparse(self.path).query).get("key") or [""])[0]
                    self._json(200, {"alarm": ({"id": key, "tag": edge.alarm_tags.get(key)} if key else None)})
                # Per-device garden routes (non-default gardens). This single-slot fake
                # ignores gid/did and serves the one latest image/event/telemetry it has
                # — enough to exercise the portal's per-device proxy + auth plumbing.
                elif path.startswith("/api/gardens/") and path.endswith("/snapshot"):
                    with edge._lock:
                        img = edge.latest_image
                    self._send_image(img)
                elif path.startswith("/api/gardens/") and path.endswith("/event"):
                    with edge._lock:
                        ev = edge.latest_event
                    self._json(200, ev or {})
                elif path.startswith("/api/gardens/") and path.endswith("/telemetry"):
                    with edge._lock:
                        tel = edge.latest_telemetry
                    self._json(200, tel or {})
                else:
                    self._json(404, {"error": "not found"})

            def do_POST(self):
                path = self.path.split("?")[0]
                edge.requests.append(("POST", path))
                edge.raw_requests.append(("POST", self.path))
                raw = self._body()
                if path == "/api/archive/prune":
                    self._json(200, edge.prune_result)
                    return
                if path == "/api/archive/wipe":
                    self._json(200, edge.wipe_result)
                    return
                if path == "/api/alarm-tag":
                    try:
                        body = json.loads(raw) if raw else {}
                    except Exception:
                        self._json(400, {"error": "bad json"})
                        return
                    edge.alarm_tags[body.get("id", "")] = body.get("label")
                    self._json(200, {"ok": True, "label": body.get("label"), "edited": False})
                    return
                if path == "/api/alarm/delete":
                    try:
                        body = json.loads(raw) if raw else {}
                    except Exception:
                        self._json(400, {"error": "bad json"})
                        return
                    deleted = edge.alarm_tags.pop(body.get("id", ""), None) is not None
                    self._json(200, {"ok": True, "deleted": deleted})
                    return
                if path == "/api/alarms/prune":
                    self._json(200, edge.alarm_prune_result)
                    return
                if path == "/api/alarms/wipe":
                    self._json(200, edge.alarm_wipe_result)
                    return
                if path == "/api/evidence":
                    cid = self.headers.get("X-Garden-Trace-Id") or ""
                    self._json(200, edge.on_evidence(raw, cid))
                elif path == "/api/telemetry":
                    try:
                        blob = json.loads(raw) if raw else {}
                    except Exception:
                        self._json(400, {"error": "bad json"})
                        return
                    self._json(200, edge.on_telemetry(blob))
                elif path == "/api/control":
                    try:
                        cmd = json.loads(raw).get("cmd")
                    except Exception:
                        self._json(400, {"error": "bad json"})
                        return
                    res = edge.control(cmd)
                    self._json(200 if res else 400, res or {"error": "unknown cmd"})
                elif path == "/api/alert":
                    # No Twilio in the fake edge -> mirror the real edge's
                    # not-configured (still 200, so the alert is "received").
                    try:
                        ev = json.loads(raw).get("event") if raw else None
                    except Exception:
                        ev = None
                    self._json(200, {"dispatched": False, "reason": "not_configured", "event": ev})
                else:
                    self._json(404, {"error": "not found"})

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self.port = self._server.server_address[1]
        # poll_interval=0.02 (vs the 0.5s default) so stop()'s shutdown() returns
        # promptly instead of blocking up to half a second per test teardown.
        self._thread = threading.Thread(
            target=self._server.serve_forever, args=(0.02,), daemon=True)
        self._thread.start()
        return self.base_url

    @property
    def base_url(self):
        return f"http://{self.host}:{self.port}"

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Deterministic fake Fastly Garden Protector edge")
    ap.add_argument("--port", type=int, default=7878)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()
    edge = FakeEdge(host=args.host, port=args.port)
    url = edge.start()
    print(f"[fake-edge] listening on {url}", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        edge.stop()
