#!/usr/bin/env python3
# Garden Protector - radar-trip capture + heartbeat listener (Pi side).
#
# POST /trigger  (radar trip) -> returns INSTANTLY, then in the background:
#     over ~5s grab a BURST of frames from each camera (the radar fires early, so
#     the subject usually arrives a beat later) -> save locally + push each to the
#     edge /api/evidence with X-Trigger, all sharing ONE capture-batch -> ONE alarm
#     that carries the whole sequence of frames.
#     A short VIDEO CLIP (~5s MP4, falling back to GIF) is also encoded per camera
#     from the burst frames using the existing timelapse encoder.
# POST /heartbeat -> returns instantly, forwards to edge /api/telemetry AS the
#     registered device so it shows ACTIVE in the dashboard's Node Health tile.
# Local retention: prunes ~/captures older than RETAIN_HOURS (hourly + per trip).
#
# Imports: stdlib only for networking (immune to the stray client.py shadow; survives
# git reset). The timelapse encoder (hardware/timelapse.py) is imported lazily at
# runtime via sys.path so it's optional — if unavailable, clips are skipped silently.
# Run under the venv from ~/ via `sudo systemd-run --unit=cap-listener ...`.
import json
import os
import sys
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Lazily import the timelapse encoder from the repo (available when running from the Pi
# where the repo clone is at /home/drew/garden-protector). Falls back gracefully so the
# listener works even if the path is absent (e.g. on a dev machine without the full tree).
_REPO = "/home/drew/garden-protector"
_timelapse = None


def _get_timelapse():
    global _timelapse
    if _timelapse is None:
        try:
            if _REPO not in sys.path:
                sys.path.insert(0, _REPO)
            import hardware.timelapse as tl

            _timelapse = tl
        except Exception as e:
            print("[cap] timelapse encoder unavailable: %s" % e, flush=True)
            _timelapse = False  # sentinel: don't retry
    return _timelapse if _timelapse else None


# ---- local camera daemon ----
CAMD = "http://127.0.0.1:8090"
OUT = os.path.expanduser("~/captures")
LISTEN = ("0.0.0.0", 8091)

# ---- this node's identity in the live garden (registered device) ----
GARDEN = "YOUR_GARDEN_ID"
DEVICE = "YOUR_DEVICE_ID"  # has can_trigger_alarm=true
NODE = "YOUR_NODE_ID"
TRIGGER_REASON = "radar motion"

# ---- capture-burst tuning (radar trips early -> sample across a window) ----
BURST_ROUNDS = 5  # snapshots per camera per trip
BURST_INTERVAL_S = 1.0  # spacing -> ~5s of coverage
RETAIN_HOURS = 24  # local ~/captures retention

# ---- Pi config (token read fresh so rotation is handled) ----
CFG = "/home/drew/garden-protector/configs"
SECRETS = CFG + "/secrets.json"
PIGARDEN = CFG + "/pi-garden.json"
BACKEND_FALLBACK = "https://YOUR-SERVICE-NAME.edgecompute.app"

_busy = threading.Lock()  # one burst at a time


def read_token():
    try:
        return json.load(open(SECRETS)).get("garden_token", "")
    except Exception as e:
        print("[cap] token read failed:", e, flush=True)
        return ""


def read_backend():
    try:
        return (json.load(open(PIGARDEN)).get("fastly") or {}).get(
            "backend_url"
        ) or BACKEND_FALLBACK
    except Exception:
        return BACKEND_FALLBACK


def live_cameras():
    try:
        with urllib.request.urlopen(CAMD + "/healthz", timeout=5) as r:
            return json.loads(r.read()).get("cameras", [])
    except Exception as e:
        print("[cap] healthz failed:", e, flush=True)
        return []


def snapshot(did):
    url = CAMD + "/snapshot?device=" + urllib.parse.quote(did)
    for attempt in range(2):  # retry once for daemon cold-start
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                return r.read()
        except Exception as e:
            print("[cap] snapshot %s (try %d): %s" % (did, attempt + 1, e), flush=True)
            time.sleep(0.3)
    return None


def push_alarm(jpeg, trace, ts, batch):
    """POST raw JPEG to the edge as an alarm-trigger frame. Returns verdict dict or None."""
    headers = {
        "Content-Type": "image/jpeg",
        "X-Garden-Id": GARDEN,
        "X-Device-Id": DEVICE,
        "X-Node-Id": NODE,
        "X-Garden-Trace-Id": trace,
        "X-Capture-Ts": str(int(ts)),
        "X-Capture-Batch": batch,  # shared -> whole burst = ONE alarm
        "X-Trigger": TRIGGER_REASON,  # the marker that makes it an alarm
    }
    tok = read_token()
    if tok:
        headers["X-Garden-Auth"] = tok
    req = urllib.request.Request(
        read_backend().rstrip("/") + "/api/evidence",
        data=jpeg,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        print("[cap] edge push HTTP %s: %s" % (e.code, e.read()[:120]), flush=True)
    except Exception as e:
        print("[cap] edge push failed: %s" % e, flush=True)
    return None


def forward_telemetry(body):
    """Forward the node's heartbeat JSON to edge /api/telemetry AS THE REGISTERED DEVICE,
    so it shows as an active node in the dashboard's Node Health tile."""
    headers = {
        "Content-Type": "application/json",
        "X-Garden-Id": GARDEN,
        "X-Device-Id": DEVICE,
        "X-Node-Id": NODE,
        "X-Garden-Trace-Id": os.urandom(8).hex(),
    }
    tok = read_token()
    if tok:
        headers["X-Garden-Auth"] = tok
    req = urllib.request.Request(
        read_backend().rstrip("/") + "/api/telemetry",
        data=body or b"{}",
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
            print(
                "[hb] %s telemetry ok (node active)" % time.strftime("%H:%M:%S"),
                flush=True,
            )
            return True
    except urllib.error.HTTPError as e:
        print("[hb] telemetry HTTP %s: %s" % (e.code, e.read()[:120]), flush=True)
    except Exception as e:
        print("[hb] telemetry forward failed: %s" % e, flush=True)
    return False


def prune_old():
    """Delete local captures older than RETAIN_HOURS."""
    cutoff = time.time() - RETAIN_HOURS * 3600
    n = 0
    try:
        for f in os.listdir(OUT):
            if not (f.startswith("cap_") or f.startswith("clip_")):
                continue
            p = os.path.join(OUT, f)
            try:
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)
                    n += 1
            except OSError:
                pass
    except FileNotFoundError:
        pass
    if n:
        print(
            "[cap] pruned %d local file(s) older than %dh" % (n, RETAIN_HOURS),
            flush=True,
        )


def capture_burst():
    """Two phases: a TIGHT ~5s window of snapshots (so the frames cluster around the
    moment even though pushing is slower), then push them all -> ONE batched alarm.
    Sequential push keeps the shared alarm_log KV doc race-free and naturally
    rate-limits alarm creation while the over-eager radar re-triggers."""
    if not _busy.acquire(blocking=False):
        print("[cap] burst already running, skipping", flush=True)
        return
    try:
        os.makedirs(OUT, exist_ok=True)
        prune_old()
        cams = live_cameras()
        # --- capture phase: fast in-RAM snapshots across a ~5s window ---
        frames = []  # (ts, jpeg, device_id)
        for i in range(BURST_ROUNDS):
            ts = int(time.time())
            for did in cams:
                jpeg = snapshot(did)
                if not jpeg:
                    continue
                with open(
                    os.path.join(OUT, "cap_%d_%d_%s.jpg" % (ts, i, did)), "wb"
                ) as f:
                    f.write(jpeg)  # local backup = the full sequence
                frames.append((ts, jpeg, did))
            if i < BURST_ROUNDS - 1:
                time.sleep(BURST_INTERVAL_S)
        # --- clip phase: encode one MP4 (or GIF) per camera from the burst frames ---
        tl = _get_timelapse()
        clip_ts = frames[0][0] if frames else int(time.time())
        clips = []
        if tl and frames:
            # Group frames by camera (capture_burst interleaves cameras per round)
            by_cam = {}
            for _ts, jpeg, did in frames:
                by_cam.setdefault(did, []).append(jpeg)
            for did, jpegs in by_cam.items():
                clip_path = os.path.join(OUT, "clip_%d_%s" % (clip_ts, did))
                try:
                    if tl.have_mp4():
                        clip_path += ".mp4"
                        tl.encode_mp4(jpegs, clip_path, fps=2, width=640)
                    else:
                        clip_path += ".gif"
                        tl.encode_gif(jpegs, clip_path, fps=2, width=640)
                    clips.append(os.path.basename(clip_path))
                    print(
                        "[cap] clip saved: %s (%d frames)"
                        % (os.path.basename(clip_path), len(jpegs)),
                        flush=True,
                    )
                except Exception as e:
                    print("[cap] clip encode failed (%s): %s" % (did, e), flush=True)

        # --- push phase: all frames -> ONE alarm (shared batch) ---
        batch = os.urandom(6).hex()
        pushed = 0
        for _ts, jpeg, _did in frames:
            if push_alarm(jpeg, os.urandom(8).hex(), _ts, batch) is not None:
                pushed += 1
        print(
            "[cap] %s trip -> %d frames over ~%ds, %d clip(s), pushed %d batch=%s"
            % (
                time.strftime("%H:%M:%S"),
                len(frames),
                int(BURST_ROUNDS * BURST_INTERVAL_S),
                len(clips),
                pushed,
                batch,
            ),
            flush=True,
        )
    finally:
        _busy.release()


class Handler(BaseHTTPRequestHandler):
    def _read_body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(n) if n else b""

    def _reply(self, text):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(text.encode())

    def _route(self):
        # Answer INSTANTLY, do the slow work in a background thread (so the ESP32's
        # short trigger/heartbeat timeout is always satisfied -> no ETIMEDOUT).
        if self.path.startswith("/heartbeat"):
            body = self._read_body()
            threading.Thread(
                target=forward_telemetry, args=(body,), daemon=True
            ).start()
            self._reply("hb queued\n")
        else:  # /trigger
            self._read_body()
            threading.Thread(target=capture_burst, daemon=True).start()
            self._reply("capturing\n")

    do_POST = do_GET = lambda self: self._route()

    def log_message(self, *a):
        pass


def retention_loop():
    while True:
        prune_old()
        time.sleep(3600)


if __name__ == "__main__":
    print(
        "[cap] listener up on %s:%d -> save %s (%dh retain) + alarms/telemetry to %s (device %s)"
        % (LISTEN[0], LISTEN[1], OUT, RETAIN_HOURS, read_backend(), DEVICE),
        flush=True,
    )
    threading.Thread(target=retention_loop, daemon=True).start()
    ThreadingHTTPServer(LISTEN, Handler).serve_forever()
