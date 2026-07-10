#!/usr/bin/env python3
"""hardware/camera_daemon.py — single-owner camera service: live MJPEG + edge push.

A Pi camera (CSI ribbon or USB UVC) can be opened by **exactly one process at a
time**. So the live LAN video stream and the periodic edge-ML push CANNOT be two
independent processes grabbing the camera — they would fight over the device. This
daemon is that single owner:

  * It opens each camera ONCE and runs a continuous capture loop, keeping the most
    recent JPEG frame per device in memory (``FrameBuffer``).
  * It serves a genuine **MJPEG** stream (``multipart/x-mixed-replace``) per device
    on a Pi-local HTTP port, which the LAN portal proxies onto the Gadgets page.
    This is real live video (several fps) and stays on the LAN — it never goes
    through the edge.
  * On a slow cadence it pushes the latest frame of each camera to
    ``POST /api/evidence`` so the edge keeps classifying (species / action labels,
    history, the remote/away snapshot view). With ``--auto`` the cadence + photo
    quality are USER settings from the portal (default: a photo every 30 s), and the
    push loop hot-reloads them every few seconds — no restart, no ``--interval`` flag
    needed. This reuses the hardware-proven push path in :mod:`camera_pusher` and
    IGNORES the verdict — the daemon never actuates anything (monitoring path, not
    the safety path).

``--auto`` self-configures from ``configs/`` (garden id / backend / token via
:class:`pi_config.PiConfig`; the camera list from the authoritative local registry
mirror ``configs/*-registry.json``), so adding/removing a camera in the portal and
restarting the service is all it takes — no unit edits.

Examples
--------
    # Real Pi, auto-config from configs/ (what the systemd unit runs):
    python3 hardware/camera_daemon.py --auto --listen 127.0.0.1:8090

    # Explicit cameras against a local edge (mirrors camera_pusher's --camera):
    python3 hardware/camera_daemon.py --backend http://localhost:7878 --garden home \\
        --camera csi-cam=ribbon --camera usb-cam=usb:/dev/video1
"""
import argparse
import glob
import json
import os
import re
import socket
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Reuse the hardware-proven evidence-push helpers + config loader. Dual import so
# this runs both as a script (sys.path[0] == hardware/) and as a package module.
try:
    from hardware import camera_pusher as _pusher
    from hardware import pi_config as _piconfig
except ImportError:  # plain-script execution
    import camera_pusher as _pusher
    import pi_config as _piconfig

try:
    import cv2
    OPENCV_AVAILABLE = True
except Exception:  # noqa: BLE001 — cv2 is optional (USB path only)
    OPENCV_AVAILABLE = False

CAMERA_TYPES = ("camera_csi", "camera_usb")
SOI = b"\xff\xd8"   # JPEG start-of-image
EOI = b"\xff\xd9"   # JPEG end-of-image

# Daylight-only / night-motion tuning (see pi_config for the user-facing settings).
NIGHT_POLL_S = 1.0          # how often to sample frames for motion when it's dark
MOTION_COOLDOWN_S = 15.0    # min seconds between motion uploads, per camera (anti-storm)
MOTION_PIXEL_DELTA = 25     # per-pixel abs-diff (0-255) for a pixel to count as "changed"
MOTION_AREA_FRAC = 0.02     # fraction of the frame that must change to call it "motion"
DARK_MARGIN = 10            # hysteresis: brightness must clear dark_below+this to call it day

# Per-camera MOTION TRIGGER watcher — escalates CONFIRMED movement to the edge as an ALARM
# (marked with HEADER_TRIGGER). This is distinct from the night-capture-on-motion path above,
# which only fills History after dark and is never marked. The per-camera knobs (cadence,
# confirm-frames, sensitivity, cooldown, monitor-zone ROI) come from pi_config.motion_settings();
# the values below are the fixed detection-engine constants.
MOTION_AREA_MIN = 0.004     # most-sensitive changed-area threshold (sensitivity = 1.0)
MOTION_AREA_MAX = 0.08      # least-sensitive changed-area threshold (sensitivity = 0.0)
MOTION_BG_ALPHA = 0.1       # running-average background learning rate per motion sample
MOTION_WARMUP_SAMPLES = 3   # ignore the first N samples after (re)start so the background settles
MOTION_BRIGHTNESS_JUMP = 30.0  # ROI mean-luminance change (0-255) read as a light shift, not motion
MOTION_PREROLL = 6          # recent sample frames kept in memory, flushed locally when an event fires
MOTION_EVENTS_KEEP = 50     # max per-camera motion-event folders kept on local disk (count cap)
MOTION_EVENTS_MAX_AGE_S = 30 * 24 * 3600  # ...and an AGE cap so a low-traffic camera's evidence
                            # doesn't linger forever (mirrors the 30-day cloud sweep)
_DEFAULT_MOTION_DIR = "/var/lib/garden-protector/motion"
_FALLBACK_MOTION_DIR = os.path.expanduser("~/.local/state/garden-protector/motion")


def _log(msg):
    print(f"[camd] {msg}", flush=True)


# ---------------------------------------------------------------------------
# PURE helpers (device->source mapping, registry discovery, MJPEG framing).
# These have NO hardware/network side effects so they are unit-tested directly.
# ---------------------------------------------------------------------------

def parse_video_node(s):
    """Extract a ``/dev/videoN`` path from a device id or name, else ``""``.

    Accepts a literal path (``... @ /dev/video1``) or the id-encoded form the
    wizard uses (``uvcvideo-usb-camera-dev-video1`` -> ``/dev/video1``)."""
    if not s:
        return ""
    m = re.search(r"/dev/video\d+", s)
    if m:
        return m.group(0)
    m = re.search(r"video(\d+)", s)
    return f"/dev/video{m.group(1)}" if m else ""


def device_source(dev):
    """Map a registry device dict to a capture SOURCE token (parity with
    :func:`camera_pusher.make_camera`): ``camera_csi`` -> ``ribbon``;
    ``camera_usb`` -> ``usb:/dev/videoN`` (node parsed from id/name, default
    ``/dev/video1``). Raises for a non-camera type."""
    t = (dev.get("type") or "").strip()
    if t == "camera_csi":
        return "ribbon"
    if t == "camera_usb":
        node = (parse_video_node(dev.get("device_id", ""))
                or parse_video_node(dev.get("name", ""))
                or "/dev/video1")
        return f"usb:{node}"
    raise ValueError(f"device {dev.get('device_id')!r} is not a camera (type={t!r})")


def discover_cameras(mirror, gid):
    """From a registry mirror dict, return ``[(device_id, source)]`` for every
    active camera in garden ``gid``. Non-camera and removed devices are skipped."""
    out = []
    devices = (((mirror or {}).get("devices") or {}).get(gid) or {}).get("devices", [])
    for d in devices:
        if (d.get("type") in CAMERA_TYPES
                and (d.get("status") or "active") != "removed"):
            try:
                out.append((d["device_id"], device_source(d)))
            except (ValueError, KeyError):
                continue
    return out


class MjpegSplitter:
    """Stateful splitter: ``feed(chunk)`` returns the list of complete JPEG frames
    found so far. rpicam-vid emits concatenated JPEGs (an MJPEG byte stream); this
    carves them back into individual frames on SOI/EOI boundaries. Pure + testable
    (no I/O), and tolerant of frames split across read() chunks."""
    def __init__(self, max_buffer=8 * 1024 * 1024):
        self._buf = bytearray()
        self._max = max_buffer

    def feed(self, chunk):
        self._buf.extend(chunk)
        frames = []
        while True:
            start = self._buf.find(SOI)
            if start < 0:
                break
            end = self._buf.find(EOI, start + 2)
            if end < 0:
                # Incomplete frame; drop leading garbage before the SOI to bound RAM.
                if start > 0:
                    del self._buf[:start]
                if len(self._buf) > self._max:   # runaway / not really MJPEG -> reset
                    self._buf.clear()
                break
            end += 2
            frames.append(bytes(self._buf[start:end]))
            del self._buf[:end]
        return frames


# ---------------------------------------------------------------------------
# Latest-frame buffer — one per camera. Writers (capture loop) set frames; readers
# (MJPEG streamers, the push thread) take the latest or block for the next one.
# ---------------------------------------------------------------------------

class FrameBuffer:
    def __init__(self):
        self._cond = threading.Condition()
        self._jpeg = None
        self._seq = 0
        self._ts = 0.0

    def set(self, jpeg, *, now):
        with self._cond:
            self._jpeg = jpeg
            self._seq += 1
            self._ts = now
            self._cond.notify_all()

    def latest(self):
        with self._cond:
            return self._jpeg, self._seq, self._ts

    def wait_next(self, last_seq, timeout):
        """Block until a frame newer than ``last_seq`` (or ``timeout``). Returns
        ``(jpeg, seq)``; ``seq == last_seq`` means the wait timed out."""
        with self._cond:
            if self._seq == last_seq:
                self._cond.wait(timeout)
            return self._jpeg, self._seq


# ---------------------------------------------------------------------------
# Viewer-gated capture rate — the camera is the Pi's biggest CPU sink, but the
# continuous feed is SHARED (live MJPEG + cloud snapshots + night motion), so it can
# never be fully stopped. Instead we capture at a LOW fps when nobody's watching the
# live feed (still plenty for the occasional snapshot + frame-diff) and bump to full
# fps only while a viewer is connected.
# ---------------------------------------------------------------------------

class _AnyEvent:
    """Duck-typed Event that's "set" when ANY underlying event is. Lets a capture
    source stop on EITHER the global shutdown OR a per-camera fps-change request.
    ``wait`` delegates to the first (global) event — a pending restart is caught by the
    source's own ``is_set()`` check on its next frame (≤ one frame period)."""
    def __init__(self, *evts):
        self._evts = evts

    def is_set(self):
        return any(e.is_set() for e in self._evts)

    def wait(self, timeout=None):
        return self._evts[0].wait(timeout)


class CaptureControl:
    """Per-camera fps gate. ``desired_fps()`` is the LIVE rate while ≥1 viewer is watching
    the MJPEG stream, else the IDLE rate. Crossing 0<->1 viewers sets ``restart`` so the
    capture loop rebuilds the source at the new rate. The camera is never stopped — only
    its frame rate (and thus CPU) drops — so cloud snapshots + night motion keep working."""
    def __init__(self, idle_fps, live_fps):
        self.idle_fps = max(1, int(idle_fps))
        self.live_fps = max(self.idle_fps, int(live_fps))
        self._viewers = 0
        self._lock = threading.Lock()
        self.restart = threading.Event()

    def desired_fps(self):
        with self._lock:
            return self.live_fps if self._viewers > 0 else self.idle_fps

    def viewers(self):
        with self._lock:
            return self._viewers

    def add_viewer(self):
        with self._lock:
            self._viewers += 1
            first = self._viewers == 1
        if first:
            self.restart.set()   # idle -> live: rebuild at full fps now

    def remove_viewer(self):
        with self._lock:
            self._viewers = max(0, self._viewers - 1)
            now_idle = self._viewers == 0
        if now_idle:
            self.restart.set()   # live -> idle: rebuild at the low fps


def _ribbon_req_fps(fps):
    """rpicam-vid under-delivers vs the requested ``--framerate`` (measured ~73%), so ask
    ~1.6x the target to clear it — but honour LOW targets too (the old code forced >=30,
    which pinned the CSI cam at ~22fps + ~30% CPU even when idle). PURE + unit-tested."""
    return max(2, round(int(fps) * 1.6))


# ---------------------------------------------------------------------------
# Continuous capture sources — open the device ONCE and yield frames until stop.
# ---------------------------------------------------------------------------

class RibbonStream:
    """CSI camera as a continuous MJPEG stream via ``rpicam-vid --codec mjpeg``."""
    def __init__(self, *, width, height, fps):
        self.width, self.height, self.fps = width, height, fps

    def frames(self, stop_evt):
        import subprocess
        # rpicam-vid under-delivers vs the requested --framerate (and `--inline` is an
        # H264-only flag that hurts MJPEG), so request ABOVE the target (~1.6x) — but
        # HONOUR low targets so an idle camera actually drops to a low rate (the old code
        # forced >=30, pinning the CSI cam at ~22fps/~30% CPU even with nobody watching).
        req_fps = _ribbon_req_fps(self.fps)
        cmd = [
            "rpicam-vid", "-t", "0", "--codec", "mjpeg", "--nopreview",
            "--framerate", str(req_fps), "--quality", "70",
            "--width", str(self.width), "--height", str(self.height), "-o", "-",
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        splitter = MjpegSplitter()
        try:
            while not stop_evt.is_set():
                # read1() returns as soon as data is available (don't block for a full
                # buffer), so the pipe drains promptly and rpicam doesn't stall/drop.
                chunk = proc.stdout.read1(65536)
                if not chunk:
                    break
                for frame in splitter.feed(chunk):
                    yield frame
        finally:
            # kill AND reap: a bare kill() leaves a <defunct> zombie and — critically — the
            # CSI device isn't released until the process is fully gone, so an immediate
            # rebuild (fps change) would race a busy camera and churn. wait() blocks until
            # it exits (device freed) so the next rpicam-vid opens cleanly on the first try.
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass


class USBStream:
    """USB/UVC camera as a continuous stream via OpenCV ``VideoCapture`` kept open."""
    def __init__(self, *, device, width, height, fps):
        self.device, self.width, self.height, self.fps = device, width, height, fps

    def frames(self, stop_evt):
        if not OPENCV_AVAILABLE:
            raise RuntimeError("OpenCV (cv2) is required for the USB live stream")
        cap = cv2.VideoCapture(self.device, cv2.CAP_V4L2)
        if not cap.isOpened():
            cap.release()
            raise RuntimeError(f"could not open USB camera {self.device}")
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        period = 1.0 / max(1, self.fps)
        try:
            while not stop_evt.is_set():
                t0 = time.time()
                ok, frame = cap.read()
                if not ok or frame is None:
                    stop_evt.wait(0.1)
                    continue
                ok2, enc = cv2.imencode(".jpg", frame)
                if ok2:
                    yield enc.tobytes()
                slack = period - (time.time() - t0)
                if slack > 0:
                    stop_evt.wait(slack)
        finally:
            cap.release()


class MockStream:
    """A fixed JPEG served on a timer — for tests / no-hardware smoke runs."""
    def __init__(self, *, path, fps):
        self.path, self.fps = path, fps

    def frames(self, stop_evt):
        with open(self.path, "rb") as f:
            data = f.read()
        period = 1.0 / max(1, self.fps)
        while not stop_evt.is_set():
            yield data
            stop_evt.wait(period)


def make_stream(source, *, width, height, fps, mock_fixture="tests/fixtures/raccoon.jpg"):
    """Resolve a SOURCE token (``ribbon`` | ``usb[:/dev/videoN]`` | ``mock[:path]``)
    to a continuous stream — the streaming counterpart of
    :func:`camera_pusher.make_camera`."""
    src = (source or "").strip()
    if src in ("ribbon", "csi"):
        return RibbonStream(width=width, height=height, fps=fps)
    if src == "usb" or src.startswith("usb:"):
        device = src.split(":", 1)[1] if ":" in src else "/dev/video1"
        return USBStream(device=device, width=width, height=height, fps=fps)
    if src == "mock":
        return MockStream(path=mock_fixture, fps=fps)
    if src.startswith("mock:"):
        return MockStream(path=src.split(":", 1)[1], fps=fps)
    raise ValueError(f"unknown camera source {source!r}")


# ---------------------------------------------------------------------------
# Worker loops — capture (per camera) + the slow edge push (all cameras).
# ---------------------------------------------------------------------------

def _capture_loop(name, make, control, buf, stop_evt, clock=time.time):
    """Own one camera: pump its frames into ``buf`` forever, at ``control``'s current fps.
    ``make(fps)`` builds a fresh source for the given rate. When the viewer count crosses
    0<->1 (``control.restart``) the source is rebuilt at the new rate; on any source error
    it's reopened after a short backoff, so a transient glitch never permanently kills the
    feed. NOTE: ``make`` takes fps so the rate can change without reopening code paths."""
    while not stop_evt.is_set():
        fps = control.desired_fps()
        control.restart.clear()
        stopper = _AnyEvent(stop_evt, control.restart)   # stop on shutdown OR fps change
        try:
            stream = make(fps)
            got = 0
            for jpeg in stream.frames(stopper):
                buf.set(jpeg, now=clock())
                got += 1
                if stopper.is_set():
                    break
            if not stop_evt.is_set() and not control.restart.is_set():
                _log(f"{name}: stream ended after {got} frame(s); reopening in 2s")
        except Exception as e:  # noqa: BLE001 — monitoring path, never fatal
            _log(f"{name}: capture error -> {e}; retrying in 2s")
        if control.restart.is_set():
            stop_evt.wait(0.3)   # fps change: brief settle for device release, then rebuild
            continue
        stop_evt.wait(2.0)


def _encode_for_upload(jpeg, max_w, max_h, quality):
    """Re-encode a captured frame down to ``<= (max_w, max_h)`` at ``quality`` JPEG so
    the copy stored in the cloud is small — the live LAN stream keeps its full size.
    Smaller bytes-per-photo is a direct Fastly-storage cost lever (30-day minimum).
    If OpenCV is unavailable or anything fails, return the frame unchanged so uploads
    never break (graceful fallback on a CSI-only Pi without cv2)."""
    if not OPENCV_AVAILABLE:
        return jpeg
    try:
        import numpy as np
        arr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if arr is None:
            return jpeg
        h, w = arr.shape[:2]
        scale = min(1.0, max_w / w, max_h / h)
        if scale < 1.0:
            arr = cv2.resize(arr, (max(1, int(w * scale)), max(1, int(h * scale))),
                             interpolation=cv2.INTER_AREA)
        ok, enc = cv2.imencode(".jpg", arr, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
        return enc.tobytes() if ok else jpeg
    except Exception:  # noqa: BLE001 — never break the upload on a resize hiccup
        return jpeg


def _decode_gray_small(jpeg, width=320):
    """Decode a JPEG to a small, blurred GRAYSCALE ndarray for cheap brightness +
    motion analysis (downscaled so it's fast and robust to sensor noise). Returns None
    when OpenCV is unavailable or the frame can't be decoded — callers treat None as
    "can't measure" (which keeps daylight-only inert rather than wrongly going dark)."""
    if not OPENCV_AVAILABLE:
        return None
    try:
        import numpy as np
        arr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_GRAYSCALE)
        if arr is None:
            return None
        h, w = arr.shape[:2]
        if w > width:
            arr = cv2.resize(arr, (width, max(1, int(h * width / w))), interpolation=cv2.INTER_AREA)
        return cv2.GaussianBlur(arr, (5, 5), 0)
    except Exception:  # noqa: BLE001
        return None


def _brightness(gray):
    """Mean luminance (0-255) of a grayscale ndarray, or None if not measurable."""
    if gray is None:
        return None
    import numpy as np
    return float(np.mean(gray))


def _motion_score(prev, cur):
    """Fraction (0.0-1.0) of pixels that changed by more than ``MOTION_PIXEL_DELTA``
    between two grayscale frames. 0.0 when either frame is missing or shapes differ —
    so the first night frame (no reference yet) never counts as motion. Pure numpy
    (no cv2), so it's unit-testable without OpenCV."""
    if prev is None or cur is None or getattr(prev, "shape", None) != getattr(cur, "shape", None):
        return 0.0
    import numpy as np
    diff = np.abs(prev.astype(np.int16) - cur.astype(np.int16))
    changed = int(np.count_nonzero(diff > MOTION_PIXEL_DELTA))
    return changed / float(diff.size or 1)


# --- motion-trigger detection engine (pure numpy; unit-testable without OpenCV) -----------

def _crop_roi(gray, roi):
    """Crop a grayscale ndarray to a normalized monitor-zone ROI ``{x,y,w,h}`` (each 0-1).
    Returns the WHOLE frame when ``roi`` is None/empty or the crop would be degenerate, so
    motion is only ever measured inside the region the admin drew. Pure numpy slicing."""
    if gray is None or not roi:
        return gray
    h, w = gray.shape[:2]
    x0, y0 = int(round(roi["x"] * w)), int(round(roi["y"] * h))
    x1 = int(round((roi["x"] + roi["w"]) * w))
    y1 = int(round((roi["y"] + roi["h"]) * h))
    x0, y0 = max(0, min(x0, w - 1)), max(0, min(y0, h - 1))
    x1, y1 = max(x0 + 1, min(x1, w)), max(y0 + 1, min(y1, h))
    sub = gray[y0:y1, x0:x1]
    return sub if getattr(sub, "size", 0) else gray


def _motion_vs_bg(bg, cur):
    """Fraction (0.0-1.0) of pixels that differ from the running-average background ``bg`` by
    more than ``MOTION_PIXEL_DELTA``. 0.0 when either is missing or shapes differ (so the first
    sample, with no background yet, never counts as motion). A background model (vs a raw
    prev-frame diff) means a subject that pauses doesn't vanish and slow light drift is absorbed.
    Pure numpy."""
    if bg is None or cur is None or getattr(bg, "shape", None) != getattr(cur, "shape", None):
        return 0.0
    import numpy as np
    diff = np.abs(bg.astype(np.float32) - cur.astype(np.float32))
    changed = int(np.count_nonzero(diff > MOTION_PIXEL_DELTA))
    return changed / float(diff.size or 1)


def _blend_bg(bg, cur, alpha):
    """Exponential-moving-average background update: ``bg = (1-alpha)*bg + alpha*cur``. Seeds with
    ``cur`` on first use or a shape change. Returns a float32 ndarray. Pure numpy."""
    import numpy as np
    c = cur.astype(np.float32)
    if bg is None or getattr(bg, "shape", None) != getattr(c, "shape", None):
        return c.copy()
    return (1.0 - alpha) * bg + alpha * c


def _area_threshold(sensitivity):
    """Map a 0-1 sensitivity to the minimum changed-area fraction that counts as motion: higher
    sensitivity -> lower threshold (fires on smaller movement). Monotonic, clamped. Pure."""
    s = min(max(float(sensitivity), 0.0), 1.0)
    return MOTION_AREA_MAX - s * (MOTION_AREA_MAX - MOTION_AREA_MIN)


def _resolve_motion_dir():
    """First writable base dir for the local motion ring buffer (env override wins), or None if
    none is writable — in which case event frames just aren't persisted (the trigger still fires)."""
    env = os.environ.get("GP_MOTION_DIR")
    for base in ([env] if env else [_DEFAULT_MOTION_DIR, _FALLBACK_MOTION_DIR]):
        try:
            os.makedirs(base, exist_ok=True)
            if os.access(base, os.W_OK):
                return base
        except Exception:  # noqa: BLE001 — try the next candidate
            continue
    return None


def _flush_motion_event(base, did, samples, ts):
    """Persist the in-memory pre-roll of a CONFIRMED motion event to a local, per-camera, capped
    folder. These frames are NEVER uploaded — the cloud only ever sees the single confirmed frame
    (as the alarm evidence). Best-effort; never raises (a storage hiccup must not break the watcher)."""
    if not base or not samples:
        return
    try:
        slug = re.sub(r"[^A-Za-z0-9_.-]", "_", did) or "cam"
        cam_dir = os.path.join(base, slug)
        ev_dir = os.path.join(cam_dir, str(int(ts * 1000)))
        os.makedirs(ev_dir, exist_ok=True)
        for i, (_t, jpeg) in enumerate(samples):
            with open(os.path.join(ev_dir, f"{i:03d}.jpg"), "wb") as f:
                f.write(jpeg)
        events = sorted(d for d in glob.glob(os.path.join(cam_dir, "*")) if os.path.isdir(d))
        # Count cap: drop the oldest folders past MOTION_EVENTS_KEEP.
        stale = events[:-MOTION_EVENTS_KEEP] if len(events) > MOTION_EVENTS_KEEP else []
        # Age cap: also drop folders older than MOTION_EVENTS_MAX_AGE_S (the folder name is the
        # event's epoch-ms). Folders with an unparseable name are left alone (best-effort).
        cutoff = ts - MOTION_EVENTS_MAX_AGE_S
        for d in events:
            try:
                if int(os.path.basename(d)) / 1000 < cutoff:
                    stale.append(d)
            except ValueError:
                continue
        for old in set(stale):
            for fp in glob.glob(os.path.join(old, "*")):
                try:
                    os.unlink(fp)
                except OSError:
                    pass
            try:
                os.rmdir(old)
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        pass


def _push_one(cfg, did, jpeg, max_w, max_h, jpeg_q, *, reason="", capture_ts=None, batch="", trigger=""):
    """Shrink + POST one camera frame to the edge; log the verdict (never act on it).
    ``capture_ts``/``batch`` (optional) correlate this frame with the other cameras pushed
    in the same tick — see :func:`camera_pusher.build_headers`. ``trigger`` (optional) marks
    this push as a genuine alarm trigger EVENT (e.g. confirmed motion) so the edge records an
    alarm; routine cadence pushes leave it empty and stay History-only."""
    payload = _encode_for_upload(jpeg, max_w, max_h, jpeg_q)
    headers = _pusher.build_headers(cfg.garden, did, cfg.node_id, cfg.token, _pusher.new_trace(),
                                    capture_ts=capture_ts, batch=batch, trigger=trigger)
    reply = _pusher.push_evidence(cfg.backend, payload, headers, cfg.timeout)
    tag = f" [{reason}]" if reason else ""
    _log(f"{did}: edge push ({len(payload)//1024} KB){tag} -> "
         f"action={reply.get('action', '?')} species={reply.get('species', '?')}")
    return reply


# --- cross-restart push schedule -------------------------------------------
# The edge-push cadence is anchored to the WALL-CLOCK time of the last real push, persisted
# to a tiny JSON file, so a daemon restart (every deploy does `git reset --hard` + restart)
# resumes the schedule instead of resetting its timer to process-start. Without this a burst
# of deploys, each killing the daemon before its next push lands, can drop a long run of
# captures (observed: a ~99-min gap during a deploy storm). Mirrors telemetry's
# primary->fallback path convention; best-effort everywhere (a missing/bad file just means
# "due now", never a crash or an indefinitely suppressed capture).
_DEFAULT_PUSH_STATE = "/var/lib/garden-protector/camera_push_state.json"
_FALLBACK_PUSH_STATE = os.path.expanduser("~/.local/state/garden-protector/camera_push_state.json")


def _resolve_push_state_path():
    """First writable candidate for the push-schedule anchor (env override wins), or None
    if none is writable — in which case the schedule stays in-memory (pre-existing behavior)."""
    env = os.environ.get("GP_CAMERA_PUSH_STATE")
    for path in ([env] if env else [_DEFAULT_PUSH_STATE, _FALLBACK_PUSH_STATE]):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if os.access(os.path.dirname(path), os.W_OK):
                return path
        except Exception:  # noqa: BLE001 — try the next candidate
            continue
    return None


def _load_last_push(path, now):
    """Persisted last-push epoch so the cadence survives a restart. Returns 0.0 (== due now)
    on any problem, and ignores non-positive or FUTURE values so a stale file or a clock that
    jumped back can never suppress capture indefinitely."""
    if not path:
        return 0.0
    try:
        with open(path) as f:
            ts = float(json.load(f).get("last_push", 0.0))
        return ts if 0.0 < ts <= now else 0.0
    except Exception:  # noqa: BLE001 — missing/corrupt -> behave as if never pushed
        return 0.0


def _save_last_push(path, ts):
    """Persist the last-push epoch atomically (tmp + replace); best-effort — a failed write
    must never break the capture loop, it just means the next restart resumes a bit early."""
    if not path:
        return
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"last_push": ts}, f)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        pass


def _push_loop(cfg, buffers, stop_evt, *, upload_quality=None, pc=None, clock=time.time, state_path=None):
    """Push each camera's latest frame to the edge for classification/history. Reuses
    :mod:`camera_pusher`; verdict is logged, never acted on. The pushed frame is shrunk
    to the upload-quality preset first (the live LAN stream is untouched).

    Cadence + quality (and the daylight-only knobs) are re-read from ``pc`` (a
    :class:`pi_config.PiConfig`) every tick when given, so portal changes take effect
    within a few seconds — no restart, no feed blip. Falls back to the static
    ``cfg.interval`` / ``upload_quality`` when ``pc`` is None (explicit-camera mode).

    Daylight-only behaviour (when enabled in capture settings): while the scene is bright
    we push on the normal cadence; once it goes dark we either go quiet ("pause") or
    switch to motion-triggered capture ("motion") — uploading a frame only when it
    differs enough from the previous sample, with a per-camera cooldown. This keeps the
    cloud free of dark, empty night frames while still catching after-dark movement. The
    detection/deterrent path (gateway -> edge) is separate and unaffected by this.

    Motion-trigger watcher: independently, any camera the admin enabled as a motion trigger
    (pi_config.motion_settings) is watched day AND night — a running-average background +
    sensitivity + N-consecutive-frames + cooldown over its monitor-zone ROI. On a confirmed
    detection it escalates ONE marked frame to the edge (which becomes an alarm); the per-sample
    frames are never uploaded (a local pre-roll is kept on disk). This is layered ON TOP of the
    routine capture above, so a trigger camera still feeds History on its normal cadence."""
    last_push = _load_last_push(state_path, clock())   # resume the cadence across restarts, don't reset it
    night = False               # current day/night state (hysteresis-damped)
    ref_gray = {}               # did -> last sampled grayscale frame (night-capture motion reference)
    last_motion = {}            # did -> clock() of this camera's last night-capture motion upload
    # Motion-trigger watcher state (per camera), distinct from the night-capture path above.
    mt_bg = {}                  # did -> running-average background (float ndarray) of the ROI
    mt_streak = {}              # did -> consecutive motion-sample count (the confirm debounce)
    mt_warm = {}                # did -> remaining warmup samples before arming
    mt_last_sample = {}         # did -> clock() of this camera's last motion sample (per-cadence)
    mt_last_fire = {}           # did -> clock() of this camera's last escalation (cooldown)
    mt_last_mean = {}           # did -> previous ROI mean luminance (brightness-jump rejection)
    mt_preroll = {}             # did -> deque[(ts, jpeg)] recent samples, flushed locally on a fire
    motion_dir = _resolve_motion_dir()
    while not stop_evt.is_set():
        interval, quality = cfg.interval, upload_quality
        daylight_only = False
        night_mode, dark_below = _piconfig.DEFAULT_NIGHT_MODE, float(_piconfig.DEFAULT_DARK_BELOW)
        if pc is not None:
            try:
                s = pc.capture_settings()
                interval, quality = float(s["interval_s"]), s["quality"]
                daylight_only = bool(s.get("daylight_only"))
                night_mode = s.get("night_mode") or night_mode
                dark_below = float(s.get("dark_below", dark_below))
            except Exception:  # noqa: BLE001 — keep pushing on a transient bad config read
                pass
        max_w, max_h, jpeg_q = _piconfig.resolve_quality(quality)
        tick = min(5.0, max(0.5, interval))

        # Per-camera motion-trigger config (read fresh each tick so portal edits hot-reload).
        # Only cameras the admin enabled are watched; for the rest this is zero overhead.
        motion_cfg = {}
        if pc is not None:
            for did in buffers:
                try:
                    m = pc.motion_settings(did)
                except Exception:  # noqa: BLE001 — a bad config read must not stop capture
                    m = None
                if m and m.get("enabled"):
                    motion_cfg[did] = m
        # Drop watcher state for cameras no longer enabled (admin toggled the trigger off, or the
        # camera left the registry). This re-arms warmup + discards the stale background, so a later
        # re-enable settles fresh instead of firing a spurious alarm against a pre-disable scene; it
        # also releases the per-camera ndarray/deque for removed cameras.
        for did in set(mt_bg) - set(motion_cfg):
            for st in (mt_bg, mt_streak, mt_warm, mt_last_sample, mt_last_fire, mt_last_mean, mt_preroll):
                st.pop(did, None)
        if motion_cfg:  # sample at least as fast as the snappiest enabled motion cadence
            tick = min(tick, float(min(m["cadence_s"] for m in motion_cfg.values())))
            tick = max(0.5, tick)

        # Day/night decision from the brightest camera, with hysteresis so dusk/dawn
        # don't flap. Only measured when daylight-only is on (zero overhead when off).
        grays = {}
        if daylight_only:
            grays = {did: _decode_gray_small(buf.latest()[0]) for did, buf in buffers.items()}
            brts = [b for b in (_brightness(g) for g in grays.values()) if b is not None]
            if brts:  # only change state when we can actually measure the scene
                bright = max(brts)
                if night and bright >= dark_below + DARK_MARGIN:
                    night = False
                    _log(f"daylight resumed (brightness {bright:.0f}) — routine capture on")
                elif not night and bright < dark_below:
                    night = True
                    _log(f"dark (brightness {bright:.0f}) — routine paused; night mode = {night_mode}")
        else:
            night = False

        if not night:
            # Daylight (or feature off): the normal cadence push. Stamp the WHOLE tick with
            # ONE capture second + ONE batch id so every camera lands on the same timeline
            # moment and groups as one multi-angle set. Without this the edge times each
            # frame at its own receive instant, which drifts seconds apart because the pushes
            # below are sequential AND block on the edge's per-frame inference round-trip.
            if clock() - last_push >= interval:
                batch_ts = int(clock())
                batch_id = _pusher.new_batch()
                had_frame = False
                for did, buf in buffers.items():
                    jpeg, _seq, _ts = buf.latest()
                    if not jpeg:
                        continue
                    had_frame = True
                    try:
                        _push_one(cfg, did, jpeg, max_w, max_h, jpeg_q,
                                  capture_ts=batch_ts, batch=batch_id)
                    except Exception as e:  # noqa: BLE001
                        _log(f"{did}: edge push FAILED -> {e}")
                # Advance the schedule only once a frame was actually available — a tick that
                # fires while the cameras are still warming up (common right after a restart)
                # must NOT consume the whole interval, or the first real push slips ~15 min.
                if had_frame:
                    last_push = clock()
                    _save_last_push(state_path, last_push)
            ref_gray.clear()  # forget motion refs so the first dark frame isn't "motion vs a daytime frame"
        elif night_mode == "motion":
            # Dark + motion mode: upload only when the frame changes (evidence), with a
            # per-camera cooldown so a lingering subject doesn't upload every second. A camera
            # that's a motion TRIGGER is skipped here — the watcher below already covers its
            # after-dark movement (and as an alarm, not just History).
            for did, buf in buffers.items():
                if did in motion_cfg:
                    continue
                jpeg, _seq, _ts = buf.latest()
                if not jpeg:
                    continue
                cur = grays.get(did)
                score = _motion_score(ref_gray.get(did), cur)
                ref_gray[did] = cur
                if score >= MOTION_AREA_FRAC and clock() - last_motion.get(did, 0.0) >= MOTION_COOLDOWN_S:
                    try:
                        # Night motion is an independent per-camera detection, not a
                        # synchronized set, so it stays ungrouped (no batch) — but we still
                        # send the frame's real capture time for an accurate timeline stamp.
                        _push_one(cfg, did, jpeg, max_w, max_h, jpeg_q,
                                  reason=f"night-motion {score*100:.1f}%",
                                  capture_ts=int(_ts) if _ts else None)
                        last_motion[did] = clock()
                    except Exception as e:  # noqa: BLE001
                        _log(f"{did}: motion push FAILED -> {e}")
            tick = min(tick, NIGHT_POLL_S)
        # else: dark + "pause" mode -> stay quiet until daylight returns.

        # Motion-trigger watcher — runs day AND night for cameras enabled as triggers, layered
        # on top of the routine capture above. Each enabled camera samples at its own cadence,
        # builds a running-average background over its monitor-zone ROI, and only escalates a
        # MARKED frame (-> alarm) after `confirm_frames` consecutive motion samples, respecting a
        # cooldown. Light-jump (clouds / lights toggling) is rejected; the per-sample frames stay
        # local (a pre-roll is flushed to disk on a fire), never uploaded.
        now = clock()
        for did, m in motion_cfg.items():
            if now - mt_last_sample.get(did, 0.0) < m["cadence_s"]:
                continue                              # not due for a sample yet (per-camera cadence)
            buf = buffers.get(did)
            if buf is None:
                continue
            jpeg, _seq, _ts = buf.latest()
            cur_full = grays.get(did)
            if cur_full is None:                      # decode if the daylight gate didn't already
                cur_full = _decode_gray_small(jpeg)
            if not jpeg or cur_full is None:
                continue
            mt_last_sample[did] = now
            roi = _crop_roi(cur_full, m["roi"])
            mean = _brightness(roi)
            prev_mean = mt_last_mean.get(did)
            mt_last_mean[did] = mean
            light_jump = (prev_mean is not None and mean is not None
                          and abs(mean - prev_mean) > MOTION_BRIGHTNESS_JUMP)
            mt_preroll.setdefault(did, deque(maxlen=MOTION_PREROLL)).append((now, jpeg))
            score = _motion_vs_bg(mt_bg.get(did), roi)
            # Fold this sample into the background; fast-adapt on a light jump so it re-settles.
            mt_bg[did] = _blend_bg(mt_bg.get(did), roi, 0.5 if light_jump else MOTION_BG_ALPHA)
            warm = mt_warm.get(did, MOTION_WARMUP_SAMPLES)
            if warm > 0:                              # let the background settle before arming
                mt_warm[did] = warm - 1
                mt_streak[did] = 0
                continue
            is_motion = (not light_jump) and score >= _area_threshold(m["sensitivity"])
            mt_streak[did] = mt_streak.get(did, 0) + 1 if is_motion else 0
            if (mt_streak.get(did, 0) >= m["confirm_frames"]
                    and now - mt_last_fire.get(did, 0.0) >= m["cooldown_s"]):
                reason = f"motion {score * 100:.1f}% x{m['confirm_frames']}"
                try:
                    _push_one(cfg, did, jpeg, max_w, max_h, jpeg_q,
                              reason=reason, trigger=reason,
                              capture_ts=int(_ts) if _ts else int(now))
                    mt_last_fire[did] = now
                    mt_streak[did] = 0
                    _flush_motion_event(motion_dir, did, list(mt_preroll.get(did, ())), now)
                except Exception as e:  # noqa: BLE001
                    _log(f"{did}: motion-trigger push FAILED -> {e}")

        # Tick <=5s so a portal settings change is picked up promptly even when the
        # interval is long; sample faster while watching for motion.
        stop_evt.wait(tick)


# ---------------------------------------------------------------------------
# Local HTTP server — MJPEG stream + snapshot + healthz (Pi-local; portal proxies).
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # silence per-request stderr spam
        pass

    def _device(self):
        q = self.path.split("?", 1)[1] if "?" in self.path else ""
        from urllib.parse import parse_qs
        return (parse_qs(q).get("device") or [""])[0].strip()

    def do_GET(self):
        route = self.path.split("?", 1)[0]
        if route == "/healthz":
            self._healthz()
        elif route == "/snapshot":
            self._snapshot()
        elif route == "/stream":
            self._stream()
        else:
            self.send_error(404, "not found")

    def _healthz(self):
        body = json.dumps({"ok": True, "cameras": sorted(self.server.buffers)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _snapshot(self):
        buf = self.server.buffers.get(self._device())
        jpeg = buf.latest()[0] if buf else None
        if not jpeg:
            self.send_error(404, "no frame yet")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(jpeg)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(jpeg)

    def _stream(self):
        buf = self.server.buffers.get(self._device())
        if not buf:
            self.send_error(404, "unknown device")
            return
        boundary = "frame"
        self.send_response(200)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-store, private")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Type",
                         f"multipart/x-mixed-replace; boundary={boundary}")
        self.end_headers()
        # Tell this camera a viewer is watching -> it captures at full fps until we leave.
        control = self.server.controls.get(self._device())
        if control:
            control.add_viewer()
        last_seq = -1
        min_period = 1.0 / max(1, self.server.stream_fps)
        try:
            while not self.server.stop_evt.is_set():
                t0 = time.time()
                jpeg, seq = buf.wait_next(last_seq, timeout=5.0)
                if jpeg is None or seq == last_seq:
                    continue
                last_seq = seq
                head = (f"--{boundary}\r\nContent-Type: image/jpeg\r\n"
                        f"Content-Length: {len(jpeg)}\r\n\r\n").encode()
                self.wfile.write(head)
                self.wfile.write(jpeg)
                self.wfile.write(b"\r\n")
                slack = min_period - (time.time() - t0)
                if slack > 0:
                    self.server.stop_evt.wait(slack)
        except (BrokenPipeError, ConnectionResetError):
            pass  # viewer navigated away — normal
        finally:
            if control:
                control.remove_viewer()   # last viewer gone -> drop back to idle fps


def serve(listen, buffers, stop_evt, *, stream_fps=10, controls=None):
    host, _, port = listen.partition(":")
    httpd = ThreadingHTTPServer((host or "127.0.0.1", int(port or 8090)), _Handler)
    httpd.daemon_threads = True
    httpd.buffers = buffers
    httpd.stop_evt = stop_evt
    httpd.stream_fps = stream_fps
    httpd.controls = controls or {}   # per-device CaptureControl for viewer-gated fps
    t = threading.Thread(target=httpd.serve_forever, name="camd-http", daemon=True)
    t.start()
    return httpd


# ---------------------------------------------------------------------------
# Auto-config — read garden/token/backend + the camera list from configs/.
# ---------------------------------------------------------------------------

def load_registry_mirror(configs_dir):
    """Load the authoritative local registry mirror (``configs/*-registry.json``).
    Returns ``{}`` if none is present (un-provisioned Pi)."""
    for p in sorted(glob.glob(os.path.join(configs_dir, "*-registry.json"))):
        try:
            return json.loads(open(p, encoding="utf-8").read())
        except (OSError, ValueError):
            continue
    return {}


def auto_config(configs_dir):
    """Return ``(garden, backend, token, node_id, cameras)`` resolved from
    ``configs/`` — the same sources the portal/gateway read. ``cameras`` is
    ``[(device_id, source)]``."""
    pc = _piconfig.PiConfig(configs_dir)
    env = pc.to_env()
    garden = env.get("GP_GARDEN_ID", "")
    backend = env.get("GP_BACKEND", "").rstrip("/")
    token = env.get("GP_GARDEN_TOKEN") or (pc.get_secret("garden_token") or "")
    node = env.get("GP_NODE_ID") or socket.gethostname() or "pi-01"
    cameras = discover_cameras(load_registry_mirror(configs_dir), garden) if garden else []
    return garden, backend, token, node, cameras


# ---------------------------------------------------------------------------
# Orchestration + CLI.
# ---------------------------------------------------------------------------

def run(cfg, cameras, *, listen, width, height, capture_fps, stream_fps,
        idle_fps=2, upload_quality=None, pc=None):
    """Start a capture thread per camera + one HTTP server + the edge-push loop.
    Blocks until SIGINT. ``cameras`` is ``[(device_id, source)]``. ``upload_quality``
    is the initial photo-quality preset; ``pc`` (a PiConfig) lets the push loop hot-read
    cadence/quality so portal changes apply without a restart. Each camera captures at
    ``idle_fps`` until a live viewer connects, then at ``max(capture_fps, stream_fps)``."""
    stop_evt = threading.Event()
    buffers = {}
    controls = {}
    threads = []
    live_fps = max(capture_fps, stream_fps)
    for did, source in cameras:
        try:
            make_stream(source, width=width, height=height, fps=capture_fps)  # validate source
        except ValueError as e:
            _log(f"{did}: skipped -> {e}")
            continue
        buf = FrameBuffer()
        buffers[did] = buf
        control = CaptureControl(idle_fps=idle_fps, live_fps=live_fps)
        controls[did] = control
        # Bind `source` per-iteration; build a fresh source at whatever fps the loop wants.
        make = (lambda src: (lambda fps: make_stream(src, width=width, height=height, fps=fps)))(source)
        t = threading.Thread(target=_capture_loop, args=(did, make, control, buf, stop_evt),
                             name=f"cap-{did}", daemon=True)
        t.start()
        threads.append(t)

    if not buffers:
        _log("no cameras to capture — idling (add a camera in the portal, then "
             "restart). Serving /healthz only.")

    serve(listen, buffers, stop_evt, stream_fps=stream_fps, controls=controls)
    _log(f"MJPEG server on http://{listen} (devices: {', '.join(buffers) or 'none'}); "
         f"capture idle={idle_fps}fps, live={live_fps}fps (full rate only while watching)")

    push_thread = None
    if buffers and cfg.token and cfg.backend:
        state_path = _resolve_push_state_path()
        push_thread = threading.Thread(
            target=_push_loop, args=(cfg, buffers, stop_evt),
            kwargs={"upload_quality": upload_quality, "pc": pc, "state_path": state_path},
            name="camd-push", daemon=True)
        push_thread.start()
        live = " (live-reloads on change)" if pc is not None else ""
        anchor = f"; schedule anchored at {state_path}" if state_path else "; schedule in-memory (no writable state path)"
        _log(f"edge push every {cfg.interval}s at {upload_quality or _piconfig.DEFAULT_QUALITY} "
             f"quality{live} -> {cfg.backend} (garden={cfg.garden}){anchor}")
    else:
        _log("edge push DISABLED (no token/backend or no cameras) — live stream only")

    try:
        while not stop_evt.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        _log("shutting down…")
    finally:
        stop_evt.set()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Single-owner Pi camera daemon: live MJPEG + edge push.")
    ap.add_argument("--auto", action="store_true",
                    help="Self-configure garden/token/backend + cameras from configs/")
    ap.add_argument("--configs-dir", default=os.environ.get("GP_CONFIGS_DIR", "configs"),
                    help="Where pi-garden.json / secrets.json / *-registry.json live")
    ap.add_argument("--backend", default=os.environ.get("GP_BACKEND", "http://localhost:7878"))
    ap.add_argument("--garden", default=os.environ.get("GP_GARDEN_ID", "default"))
    ap.add_argument("--token", default=os.environ.get("GP_GARDEN_TOKEN", ""))
    ap.add_argument("--node-id", default=os.environ.get("GP_NODE_ID", socket.gethostname() or "pi-01"))
    ap.add_argument("--camera", action="append", default=[], metavar="DEVICE_ID=SOURCE",
                    help="Repeatable (ignored with --auto). SOURCE = ribbon | usb[:/dev/videoN] | mock[:/path]")
    ap.add_argument("--listen", default=os.environ.get("GP_CAMD_LISTEN", "127.0.0.1:8090"))
    ap.add_argument("--interval", type=float, default=None,
                    help="Seconds between edge pushes per camera. With --auto the saved "
                         "portal setting governs (hot-reloaded); this is only the initial "
                         "value / the cadence for the explicit --camera dev path "
                         f"(default {_piconfig.DEFAULT_INTERVAL_S}s).")
    ap.add_argument("--timeout", type=float, default=30.0, help="Edge POST timeout (s)")
    ap.add_argument("--capture-fps", type=int, default=10, help="Camera capture frame rate WHILE a viewer watches the live feed")
    ap.add_argument("--idle-fps", type=int, default=2,
                    help="Capture frame rate when nobody's watching the live feed (still feeds "
                         "cloud snapshots + night motion). Lower = less CPU. Default 2.")
    ap.add_argument("--stream-fps", type=int, default=10, help="Max MJPEG frame rate served to viewers")
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--quality", choices=sorted(_piconfig.QUALITY_PRESETS), default=None,
                    help="Cloud-upload photo-quality preset (overrides the saved setting)")
    args = ap.parse_args(argv)

    # Cloud-upload cadence + photo quality are user settings the portal writes to
    # pi-garden.json. With --auto they govern: this just seeds the initial value, and
    # _push_loop hot-reloads both every few seconds (so the systemd unit needs no
    # --interval flag). An explicit --interval/--quality only seeds a different start
    # (CLI > saved setting > default). Without --auto (explicit --camera dev path,
    # no hot-reload) the flag is the live cadence, defaulting to the shared default.
    interval, upload_quality, pc = args.interval, args.quality, None
    if args.auto:
        garden, backend, token, node, cameras = auto_config(args.configs_dir)
        if not cameras:
            _log(f"--auto: no cameras registered for garden {garden!r} in "
                 f"{args.configs_dir} (or Pi not provisioned). Idling.")
        pc = _piconfig.PiConfig(args.configs_dir)   # hot-read cadence/quality on change
        cap = pc.capture_settings()
        if interval is None:
            interval = float(cap["interval_s"])
        if upload_quality is None:
            upload_quality = cap["quality"]
    else:
        garden, backend, token, node = args.garden, args.backend, args.token, args.node_id
        try:
            cameras = [_pusher.parse_camera_arg(c) for c in args.camera]
        except ValueError as e:
            ap.error(str(e))
        if not cameras:
            ap.error("at least one --camera DEVICE_ID=SOURCE is required (or use --auto)")
        if interval is None:
            interval = float(_piconfig.DEFAULT_INTERVAL_S)

    cfg = _pusher.PusherConfig(backend=backend, garden=garden, node_id=node,
                               token=token, interval=interval, timeout=args.timeout)
    auth = "with token" if token else "tokenless"
    _log(f"start: garden={garden} edge={backend} ({auth}); "
         f"{len(cameras)} camera(s): {', '.join(d for d, _ in cameras) or 'none'}")
    run(cfg, cameras, listen=args.listen, width=args.width, height=args.height,
        capture_fps=args.capture_fps, stream_fps=args.stream_fps, idle_fps=args.idle_fps,
        upload_quality=upload_quality, pc=pc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
