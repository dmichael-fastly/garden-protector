#!/usr/bin/env python3
"""
camera_view.py — live camera preview for aiming the camera in the garden.

Run this ON THE PI (it's headless, so there's no local display). It starts a tiny
web server and streams the camera as MJPEG. Open the printed URL in a browser on
your phone or laptop on the same Wi-Fi/LAN and walk the camera around the garden
until the framing is right.

What you see on the stream:
  - a rule-of-thirds framing grid + center crosshair (composition aid)
  - a live timestamp and the measured frame rate
  - GREEN motion boxes drawn with the SAME logic as the real trigger
    (OpenCVMotionTrigger in client.py): GaussianBlur 21x21 -> absdiff -> thresh 25
    -> dilate x2 -> contour area >= --threshold. So "what lights up green here is
    what would wake the system" — invaluable for deciding where to point it and how
    to set --threshold for your scene.

Examples:
    # USB webcam (the camera your motion trigger uses):
    python3 hardware/camera_view.py --camera usb --device /dev/video1

    # CSI ribbon camera (8MP imx219, uses rpicam-vid under the hood):
    python3 hardware/camera_view.py --camera ribbon

    # Framing only, no motion overlay, on a custom port:
    python3 hardware/camera_view.py --camera usb --no-motion --port 8080

By default this binds 127.0.0.1 (loopback ONLY) because the preview has NO auth.
To view it from your phone/laptop, SSH-tunnel and browse localhost:
    ssh -L 8000:localhost:8000 <pi>     # then open on your machine:
    http://localhost:8000               # the live MJPEG preview
    http://localhost:8000/snapshot      # grab a single still
Pass --bind 0.0.0.0 to expose it directly on the LAN (UNAUTHENTICATED — only on a
trusted network; the URL is then http://<pi-ip>:8000 / http://raspberrypi.local:8000).

Heads up:
  - This tool has NO passcode. It defaults to loopback so it can't accidentally
    publish an open camera feed to the LAN; --bind 0.0.0.0 opts into that.
  - Only ONE process can own a V4L2 device at a time. If client.py / the
    garden-camera daemon is running on the same device you'll get a
    "could not open" / EBUSY error — stop it first (and stop THIS before they run).
  - The USB cam and the CSI cam are different hardware, so you CAN preview the CSI
    cam here while client.py watches the USB cam for motion.
  - Ctrl-C to stop.
"""

import argparse
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import cv2
    import numpy as np
except ImportError:
    sys.exit(
        "[camera_view] OpenCV is required. On the Pi:\n"
        "    sudo apt update && sudo apt install -y python3-opencv\n"
    )


# --------------------------------------------------------------------------- #
# Frame publishing: one capture thread writes the latest JPEG; HTTP handlers
# block until a new frame is available, so streams stay in lock-step with the
# camera without busy-spinning.
# --------------------------------------------------------------------------- #
class FramePublisher:
    def __init__(self):
        self._cond = threading.Condition()
        self._jpeg = None
        self._seq = 0
        self.stopped = False

    def publish(self, jpeg_bytes):
        with self._cond:
            self._jpeg = jpeg_bytes
            self._seq += 1
            self._cond.notify_all()

    def get(self, last_seq):
        """Block until a frame newer than last_seq exists; return (jpeg, seq)."""
        with self._cond:
            while self._seq == last_seq and not self.stopped:
                self._cond.wait(timeout=5)
            return self._jpeg, self._seq

    def latest(self):
        with self._cond:
            return self._jpeg

    def stop(self):
        with self._cond:
            self.stopped = True
            self._cond.notify_all()


# --------------------------------------------------------------------------- #
# Frame sources -> each yields BGR numpy frames forever.
# --------------------------------------------------------------------------- #
def usb_frames(device, width, height):
    """Yield BGR frames from a UVC/USB camera via OpenCV + V4L2."""
    cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
    if not cap.isOpened():
        raise RuntimeError(
            f"could not open USB camera '{device}' via V4L2. "
            f"Is another process (client.py) using it? Is the path right? "
            f"(try: v4l2-ctl --list-devices)"
        )
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    try:
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                time.sleep(0.05)
                continue
            yield frame
    finally:
        cap.release()


def ribbon_frames(width, height, fps):
    """Yield BGR frames from a CSI ribbon camera by decoding rpicam-vid MJPEG."""
    import subprocess

    cmd = [
        "rpicam-vid",
        "--codec", "mjpeg",
        "--inline",
        "--nopreview",
        "-t", "0",
        "--width", str(width),
        "--height", str(height),
        "--framerate", str(fps),
        "-o", "-",
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        raise RuntimeError(
            "rpicam-vid not found. On Pi OS Bookworm it ships with the camera "
            "stack; install with: sudo apt install -y rpicam-apps"
        )

    buf = b""
    SOI, EOI = b"\xff\xd8", b"\xff\xd9"
    try:
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                err = proc.poll()
                raise RuntimeError(
                    f"rpicam-vid ended (exit {err}). Is the CSI camera connected "
                    f"and enabled? (try: rpicam-hello --list-cameras)"
                )
            buf += chunk
            # Extract every complete JPEG sitting in the buffer.
            while True:
                start = buf.find(SOI)
                end = buf.find(EOI, start + 2)
                if start == -1 or end == -1:
                    break
                jpeg = buf[start:end + 2]
                buf = buf[end + 2:]
                frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
                if frame is not None:
                    yield frame
    finally:
        proc.terminate()


# --------------------------------------------------------------------------- #
# Overlay: framing grid + motion boxes (motion logic mirrors client.py's
# OpenCVMotionTrigger so the preview matches real trigger behavior).
# --------------------------------------------------------------------------- #
def make_overlay(min_area, motion_on):
    prev_gray = {"g": None}
    fps_state = {"t": None, "fps": 0.0}

    def draw(frame):
        h, w = frame.shape[:2]

        # --- motion (same pipeline as the real trigger) ---
        moving = False
        if motion_on:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (21, 21), 0)
            if prev_gray["g"] is not None and prev_gray["g"].shape == gray.shape:
                diff = cv2.absdiff(prev_gray["g"], gray)
                thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)[1]
                thresh = cv2.dilate(thresh, None, iterations=2)
                contours, _ = cv2.findContours(
                    thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
                )
                for c in contours:
                    if cv2.contourArea(c) < min_area:
                        continue
                    moving = True
                    x, y, cw, ch = cv2.boundingRect(c)
                    cv2.rectangle(frame, (x, y), (x + cw, y + ch), (0, 255, 0), 2)
            prev_gray["g"] = gray

        # --- rule-of-thirds grid + center crosshair ---
        for i in (1, 2):
            cv2.line(frame, (w * i // 3, 0), (w * i // 3, h), (60, 60, 60), 1)
            cv2.line(frame, (0, h * i // 3), (w, h * i // 3), (60, 60, 60), 1)
        cx, cy = w // 2, h // 2
        cv2.drawMarker(frame, (cx, cy), (0, 200, 255), cv2.MARKER_CROSS, 24, 1)

        # --- fps (exponential moving average) ---
        now = time.time()
        if fps_state["t"] is not None:
            dt = now - fps_state["t"]
            if dt > 0:
                inst = 1.0 / dt
                fps_state["fps"] = (
                    inst if fps_state["fps"] == 0 else 0.9 * fps_state["fps"] + 0.1 * inst
                )
        fps_state["t"] = now

        # --- HUD text (timestamp / size / fps / motion) ---
        label = "%s  %dx%d  %.1f fps" % (
            time.strftime("%Y-%m-%d %H:%M:%S"), w, h, fps_state["fps"],
        )
        cv2.rectangle(frame, (0, 0), (w, 24), (0, 0, 0), -1)
        cv2.putText(frame, label, (6, 17), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (255, 255, 255), 1, cv2.LINE_AA)
        if motion_on:
            tag = "MOTION" if moving else "still"
            color = (0, 255, 0) if moving else (160, 160, 160)
            cv2.putText(frame, tag, (w - 90, 17), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                        color, 2, cv2.LINE_AA)
        return frame

    return draw


# --------------------------------------------------------------------------- #
# HTTP server
# --------------------------------------------------------------------------- #
INDEX_HTML = b"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Fastly Garden Protector - camera view</title>
<style>
  body{margin:0;background:#111;color:#ccc;font:14px system-ui,sans-serif;text-align:center}
  h1{font-size:15px;font-weight:600;margin:8px}
  img{max-width:100%;height:auto;display:block;margin:0 auto;background:#000}
  a{color:#6cf}
</style></head><body>
<h1>Fastly Garden Protector &mdash; live camera view</h1>
<img src="/stream" alt="camera stream">
<p><a href="/snapshot">grab a still</a> &middot; green boxes = would trigger motion</p>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):  # keep the console quiet
        pass

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send_bytes(INDEX_HTML, "text/html")
        elif path == "/stream":
            self._stream()
        elif path == "/snapshot":
            jpeg = self.server.publisher.latest()
            if jpeg is None:
                self.send_error(503, "no frame yet")
            else:
                self._send_bytes(jpeg, "image/jpeg")
        else:
            self.send_error(404)

    def _send_bytes(self, data, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _stream(self):
        self.send_response(200)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-cache, private")
        self.send_header("Pragma", "no-cache")
        self.send_header(
            "Content-Type", "multipart/x-mixed-replace; boundary=FRAME"
        )
        self.end_headers()
        last = 0
        try:
            while True:
                jpeg, last = self.server.publisher.get(last)
                if jpeg is None:
                    continue
                self.wfile.write(b"--FRAME\r\n")
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(("Content-Length: %d\r\n\r\n" % len(jpeg)).encode())
                self.wfile.write(jpeg)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass  # client closed the tab; normal


def lan_ip():
    """Best-effort primary LAN IP (no traffic actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser(
        description="Live camera preview for aiming the camera in the garden.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--camera", choices=["usb", "ribbon"], default="usb",
                    help="USB/UVC webcam (default) or CSI ribbon camera")
    ap.add_argument("--device", default="/dev/video1",
                    help="V4L2 path for --camera usb (default: /dev/video1)")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--fps", type=int, default=15,
                    help="capture frame rate for --camera ribbon (default: 15)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--bind", default="127.0.0.1",
                    help="Address to bind (default: 127.0.0.1 — loopback only, since "
                         "this preview has NO auth). To reach it from your phone/laptop, "
                         "SSH-tunnel (ssh -L 8000:localhost:8000 pi). Pass --bind 0.0.0.0 "
                         "to expose it to the whole LAN UNAUTHENTICATED (do this only on a "
                         "trusted network, and stop it before garden-camera runs).")
    ap.add_argument("--threshold", type=int, default=2000,
                    help="min motion contour area to draw a box, matches "
                         "client.py --opencv-threshold (default: 2000)")
    ap.add_argument("--no-motion", dest="motion", action="store_false",
                    help="disable the green motion overlay (framing grid only)")
    args = ap.parse_args()

    if args.camera == "usb":
        source = usb_frames(args.device, args.width, args.height)
        src_desc = "USB %s" % args.device
    else:
        source = ribbon_frames(args.width, args.height, args.fps)
        src_desc = "CSI ribbon (rpicam-vid)"

    publisher = FramePublisher()
    overlay = make_overlay(args.threshold, args.motion)

    def capture_loop():
        try:
            for frame in source:
                frame = overlay(frame)
                ok, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                if ok:
                    publisher.publish(jpeg.tobytes())
        except Exception as e:  # surface camera errors instead of dying silently
            print("\n[camera_view] capture stopped: %s" % e)
            publisher.stop()

    t = threading.Thread(target=capture_loop, daemon=True)
    t.start()

    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    server.publisher = publisher

    ip = lan_ip()
    loopback_only = args.bind in ("127.0.0.1", "::1", "localhost")
    print("[camera_view] source : %s  (%dx%d)" % (src_desc, args.width, args.height))
    print("[camera_view] motion : %s (threshold=%d)"
          % ("on" if args.motion else "off", args.threshold))
    print("[camera_view] bind   : %s:%d%s"
          % (args.bind, args.port, "  (loopback only)" if loopback_only else "  (LAN — UNAUTHENTICATED)"))
    if loopback_only:
        print("[camera_view] loopback-only (no auth). From your phone/laptop, SSH-tunnel then browse localhost:")
        print("    ssh -L %d:localhost:%d <pi>   then   http://localhost:%d" % (args.port, args.port, args.port))
        print("    (or re-run with --bind 0.0.0.0 to expose it directly on the LAN)")
    else:
        print("[camera_view] open in a browser on your phone/laptop:")
        print("    http://%s:%d" % (ip, args.port))
        print("    http://raspberrypi.local:%d   (if mDNS resolves)" % args.port)
    print("[camera_view] Ctrl-C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[camera_view] shutting down.")
    finally:
        publisher.stop()
        server.shutdown()


if __name__ == "__main__":
    main()
