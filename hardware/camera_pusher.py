#!/usr/bin/env python3
"""hardware/camera_pusher.py — push stills from directly-attached Pi cameras to the edge.

The Pi is the hub. Cameras wired **directly to the Pi** (CSI ribbon / USB UVC) are
each a distinct EDGE DEVICE. This captures from each on a cadence and POSTs the JPEG
to ``POST /api/evidence`` with that camera's ``X-Device-Id``, so the edge stores a
per-device ``latest_image`` that the admin console's camera gallery renders.

This is the **monitoring** path, not the safety path:

  * No radar is required — a timer drives capture (the time-lapse / latest-still mode
    of the contract). When you later add radar (on the Pi or an ESP32-C3), the
    fail-closed spray decision stays on the gateway/node, exactly as today.
  * The edge classifies each frame as a side effect, so the gallery also shows a
    species/action label per camera — but this process **IGNORES the verdict**. It
    never actuates anything; nothing here can turn a deterrent on.

Per-device snapshots only separate on the edge for a **non-default** garden
(``g/<gid>/dev/<did>/…``); the ``default`` garden shares one slot (last-writer-wins),
so register a named garden (e.g. ``home``) before running >1 camera.

Examples
--------
    # Two real cameras on the Pi -> a local Viceroy edge on the Mac:
    python3 hardware/camera_pusher.py --backend http://192.168.1.50:7878 --garden home \\
        --camera cam-ribbon=ribbon --camera cam-usb=usb:/dev/video1 --interval 30

    # Offline smoke (no hardware) against the fake/real edge, one round then exit:
    python3 hardware/camera_pusher.py --backend http://localhost:7878 --garden home \\
        --camera cam-a=mock --camera cam-b=mock:tests/fixtures/empty_garden.jpg --once

The token (for a provisioned garden) comes from ``--token`` or ``$GP_GARDEN_TOKEN`` —
never the command-line if you can help it. The deploy-env minted by the console
(``configs/<gid>-<did>.env``) carries ``GP_GARDEN_TOKEN`` / ``GP_GARDEN_ID`` /
``GP_BACKEND``; ``source`` it on the Pi.
"""
import argparse
import os
import socket
import sys
import threading
import time
import uuid

import requests

# Reuse the existing, hardware-proven camera capture classes. Dual import so this
# runs both as a script (`python3 hardware/camera_pusher.py`) and as a package
# (`python3 -m hardware.camera_pusher`). We import the `client` MODULE (not bare
# names) and never do a top-level `import client` from the repo root, to avoid the
# stale root-level client.py shadowing trap.
try:
    from hardware import client as _client
    from hardware import pi_config as _piconfig
except ImportError:  # plain-script execution (sys.path[0] == hardware/)
    import client as _client
    import pi_config as _piconfig

# The header NAMES are part of the Pi<->edge wire contract and live in the SSOT
# (contract/spec.toml -> generated provision/contract_gen.py). Importing the
# generated constants — instead of re-typing the literals here — is what keeps a
# spec rename from silently breaking the wire (TEST-002). The systemd units run
# with WorkingDirectory=<repo root>, so `from provision import …` resolves when
# launched as a package; the fallback puts the repo root on sys.path for the
# plain-script case (sys.path[0] == hardware/, repo root NOT on the path).
try:
    from provision import contract_gen as _cg
except ImportError:  # plain-script execution: add repo root (parent of hardware/)
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from provision import contract_gen as _cg


# ---------------------------------------------------------------------------
# Camera source resolution (pure) — maps a `--camera` SOURCE token to a capturer.
# ---------------------------------------------------------------------------

class _FileCamera(_client.CameraSource):
    """A mock camera that returns the bytes of a specific JPEG file each capture."""
    def __init__(self, path):
        self.path = path

    def capture_image(self) -> bytes:
        with open(self.path, "rb") as f:
            return f.read()


def make_camera(source, *, width=640, height=480, quality=None):
    """Resolve a SOURCE token to a CameraSource.

    ``ribbon``/``csi``       -> RibbonCamera (rpicam-jpeg)
    ``usb`` / ``usb:/dev/N`` -> USBCamera (OpenCV CAP_V4L2 -> fswebcam fallback)
    ``mock``                 -> MockCamera (tests/fixtures/raccoon.jpg)
    ``mock:/path.jpg``       -> that exact file

    ``quality`` (0-100) sets the JPEG quality of captured stills; ``None`` keeps the
    encoder default. Pair with ``width``/``height`` to trim cloud-storage bytes.
    """
    src = (source or "").strip()
    if src in ("ribbon", "csi"):
        return _client.RibbonCamera(width=width, height=height, quality=quality)
    if src == "usb" or src.startswith("usb:"):
        device = src.split(":", 1)[1] if ":" in src else "/dev/video1"
        return _client.USBCamera(device=device, width=width, height=height, quality=quality)
    if src == "mock":
        return _client.MockCamera()
    if src.startswith("mock:"):
        return _FileCamera(src.split(":", 1)[1])
    raise ValueError(
        f"unknown camera source {source!r} "
        "(want ribbon | csi | usb[:/dev/videoN] | mock[:/path.jpg])"
    )


def parse_camera_arg(arg):
    """Parse a ``DEVICE_ID=SOURCE`` token into ``(device_id, source)``."""
    if "=" not in arg:
        raise ValueError(f"--camera wants DEVICE_ID=SOURCE, got {arg!r}")
    dev, src = arg.split("=", 1)
    dev, src = dev.strip(), src.strip()
    if not dev or not src:
        raise ValueError(f"--camera wants DEVICE_ID=SOURCE, got {arg!r}")
    return dev, src


# ---------------------------------------------------------------------------
# Edge push (small, testable) — raw JPEG body, identity headers, ignore verdict.
# ---------------------------------------------------------------------------

def new_trace():
    return uuid.uuid4().hex[:16]


def new_batch():
    """A short id SHARED by every camera pushed in one capture tick, so the edge can group
    the angles as one multi-angle set (carried as X-Capture-Batch). 12 hex chars is plenty
    to avoid collisions between adjacent ticks."""
    return uuid.uuid4().hex[:12]


def build_headers(garden, device, node, token, trace, *, capture_ts=None, batch="", trigger=""):
    """The identity + tracing headers for a per-camera evidence POST (pure).

    ``capture_ts`` (unix seconds) and ``batch`` are OPTIONAL multi-angle correlation hints
    (header names mirror contract/spec.toml [headers]). The edge stamps the archive object
    key with ``capture_ts`` instead of its own receive time — so every camera in a tick
    shares ONE timeline second — and embeds ``batch`` so the UI groups the angles. Both are
    OMITTED when None/"", and the edge then falls back to its own clock + ungrouped, so an
    older Pi (or the safety gateway) is unaffected.

    ``trigger`` is the OPTIONAL alarm trigger marker: set it (to a short human reason like
    "motion 4.2% x3") ONLY on a push that represents a genuine trigger EVENT, e.g. a camera's
    confirmed motion. The edge creates an alarm only when the device can trigger AND this
    marker is present, so routine cadence frames (no marker) stay History-only."""
    h = {
        "Content-Type": "image/jpeg",
        _cg.HEADER_GARDEN_ID: garden,
        _cg.HEADER_DEVICE_ID: device,
        _cg.HEADER_NODE_ID: node,
        _cg.HEADER_TRACE_ID: trace,
    }
    if token:
        h[_cg.HEADER_AUTH] = token
    if capture_ts is not None:
        h[_cg.HEADER_CAPTURE_TS] = str(int(capture_ts))
    if batch:
        h[_cg.HEADER_CAPTURE_BATCH] = batch
    if trigger:
        h[_cg.HEADER_TRIGGER] = trigger
    return h


def push_evidence(backend, jpeg, headers, timeout=30.0):
    """POST a raw JPEG to {backend}/api/evidence; return the parsed verdict dict."""
    url = backend.rstrip("/") + "/api/evidence"
    r = requests.post(url, data=jpeg, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json() if r.content else {}


class PusherConfig:
    def __init__(self, *, backend, garden="default", node_id="pi-01", token="",
                 interval=30.0, timeout=30.0):
        self.backend = backend.rstrip("/")
        self.garden = garden
        self.node_id = node_id
        self.token = token
        self.interval = interval
        self.timeout = timeout


def capture_and_push(cfg, device, cam):
    """Capture one frame from ``cam`` and push it as ``device``. Returns the edge
    verdict dict (logged, not acted on). Raises on capture/HTTP failure so the
    caller can isolate per-camera errors."""
    jpeg = cam.capture_image()
    headers = build_headers(cfg.garden, device, cfg.node_id, cfg.token, new_trace())
    reply = push_evidence(cfg.backend, jpeg, headers, cfg.timeout)
    return reply


def _log(msg):
    print(f"[pusher] {msg}", flush=True)


def _run_camera_loop(cfg, device, cam, stop_evt, *, once=False):
    """One camera's capture/push loop, fully isolated: any error is logged and the
    loop continues (or, in --once mode, returns) so a single bad camera never takes
    the others down."""
    while not stop_evt.is_set():
        t0 = time.time()
        try:
            reply = capture_and_push(cfg, device, cam)
            action = reply.get("action", "?")
            species = reply.get("species", "?")
            reason = reply.get("reason")
            tail = f" reason={reason}" if reason else ""
            _log(f"{device}: pushed -> action={action} species={species}{tail} "
                 f"({(time.time() - t0) * 1000:.0f} ms)")
        except Exception as e:  # noqa: BLE001 — monitoring path, never fatal
            _log(f"{device}: push FAILED -> {e} (will retry next cycle)")
        if once:
            return
        stop_evt.wait(cfg.interval)


def run(cfg, cameras, *, once=False):
    """Drive ``cameras`` (a list of (device_id, CameraSource)). One thread each;
    blocks until Ctrl-C (or returns after one round when ``once``)."""
    if cfg.garden == "default" and len(cameras) > 1:
        _log("WARNING: garden 'default' shares ONE snapshot slot (last-writer-wins); "
             "register a named garden so each camera shows separately.")
    stop_evt = threading.Event()
    threads = []
    for device, cam in cameras:
        t = threading.Thread(target=_run_camera_loop, args=(cfg, device, cam, stop_evt),
                             kwargs={"once": once}, name=f"cam-{device}", daemon=True)
        t.start()
        threads.append(t)
    try:
        if once:
            for t in threads:
                t.join()
        else:
            while any(t.is_alive() for t in threads):
                time.sleep(0.2)
    except KeyboardInterrupt:
        _log("shutting down…")
    finally:
        stop_evt.set()
        for t in threads:
            t.join(timeout=2.0)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Push directly-attached Pi camera stills to the edge.")
    ap.add_argument("--backend", default=os.environ.get("GP_BACKEND", "http://localhost:7878"),
                    help="Edge base URL (Viceroy or deployed Compute)")
    ap.add_argument("--garden", default=os.environ.get("GP_GARDEN_ID", "default"),
                    help="Garden id (use a NON-default garden for >1 camera)")
    ap.add_argument("--token", default=os.environ.get("GP_GARDEN_TOKEN", ""),
                    help="Per-garden bearer token (or set GP_GARDEN_TOKEN)")
    ap.add_argument("--node-id", default=os.environ.get("GP_NODE_ID", socket.gethostname() or "pi-01"),
                    help="Physical node id (logging only)")
    ap.add_argument("--camera", action="append", default=[], metavar="DEVICE_ID=SOURCE",
                    help="Repeatable. SOURCE = ribbon | usb[:/dev/videoN] | mock[:/path.jpg]")
    ap.add_argument("--interval", type=float, default=30.0, help="Seconds between captures per camera")
    ap.add_argument("--timeout", type=float, default=30.0, help="Edge POST timeout (s)")
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--quality", choices=sorted(_piconfig.QUALITY_PRESETS), default=None,
                    help="Photo-quality preset; overrides --width/--height with the preset size")
    ap.add_argument("--once", action="store_true", help="Capture one round from each camera, then exit")
    args = ap.parse_args(argv)

    if not args.camera:
        ap.error("at least one --camera DEVICE_ID=SOURCE is required")

    width, height, jpeg_q = args.width, args.height, None
    if args.quality:
        width, height, jpeg_q = _piconfig.resolve_quality(args.quality)

    try:
        specs = [parse_camera_arg(c) for c in args.camera]
        cameras = [(dev, make_camera(src, width=width, height=height, quality=jpeg_q))
                   for dev, src in specs]
    except ValueError as e:
        ap.error(str(e))

    cfg = PusherConfig(backend=args.backend, garden=args.garden, node_id=args.node_id,
                       token=args.token, interval=args.interval, timeout=args.timeout)
    auth = "with token" if cfg.token else "tokenless"
    _log(f"edge={cfg.backend} garden={cfg.garden} node={cfg.node_id} ({auth}); "
         f"{len(cameras)} camera(s): {', '.join(d for d, _ in cameras)}; "
         f"interval={cfg.interval}s" + ("  [--once]" if args.once else ""))
    run(cfg, cameras, once=args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
