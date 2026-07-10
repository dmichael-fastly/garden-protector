#!/usr/bin/env python3
"""camera_probe.py — Identify and validate the CSI ribbon + USB cameras on the Pi.

Run this FIRST, before any end-to-end validation. Hardware bring-up should be
bottom-up: prove each camera captures a real JPEG in isolation and learn the
*actual* device nodes before trusting the full capture -> Fastly -> inference loop.

What it does (capture-only; no GPIO, no network):
  1. Enumerates CSI cameras via `rpicam-hello --list-cameras` (libcamera).
  2. Enumerates V4L2 nodes via `v4l2-ctl` and works out which /dev/videoN is the
     real USB *capture* node (a single UVC webcam often also exposes a metadata
     node that produces no frames).
  3. Captures one still from the CSI camera (rpicam-jpeg) and one from the USB
     camera (OpenCV via explicit device path + CAP_V4L2, fswebcam fallback),
     timing each.
  4. Prints a summary and the exact `--camera` / `--camera-device` flags to pass
     to client.py next.

Why this exists: the defaults baked into client.py (USB = /dev/video1,
OpenCV-by-index) are *assumptions* this script verifies. On Raspberry Pi OS
Bookworm the CLI is
`rpicam-*` (the old `libcamera-*` names were removed).

Usage:
    python3 hardware/camera_probe.py
    python3 hardware/camera_probe.py --out /tmp/probe --width 1280 --height 720
"""

import argparse
import glob
import os
import shutil
import subprocess
import sys
import time

try:
    import cv2
    OPENCV_AVAILABLE = True
except ImportError:
    OPENCV_AVAILABLE = False

# Observe-only telemetry: records probe capture timings so Step 0/1 bring-up
# numbers land in the SAME DB as live captures (probe rows carry no cid). No-op
# when uninitialized / GP_TELEMETRY=0. Dual import (script vs package).
try:
    from hardware import telemetry
except ImportError:
    import telemetry


def run(cmd, timeout=15):
    """Run a command; return (returncode, stdout, stderr). rc=127 if missing."""
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout)
        return p.returncode, p.stdout.decode(errors="replace"), p.stderr.decode(errors="replace")
    except FileNotFoundError:
        return 127, "", f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s: {' '.join(cmd)}"


def hr(title):
    print("\n" + "=" * 64)
    print(title)
    print("=" * 64)


# ---------------------------------------------------------------------------
# CSI (ribbon) camera — libcamera / rpicam-apps
# ---------------------------------------------------------------------------

def enumerate_csi():
    hr("1. CSI ribbon camera (libcamera)")
    rpicam = shutil.which("rpicam-hello") or shutil.which("libcamera-hello")
    if not rpicam:
        print("  rpicam-hello not found. On Bookworm install with:")
        print("    sudo apt update && sudo apt install -y rpicam-apps")
        return False
    if "libcamera-hello" in rpicam:
        print("  NOTE: only legacy 'libcamera-hello' found. On Bookworm the tools")
        print("        are renamed to 'rpicam-*'; consider updating the OS image.")
    rc, out, err = run([rpicam, "--list-cameras"])
    print((out or err).rstrip() or "  (no output)")
    detected = rc == 0 and ("Available cameras" in out or ":" in out and "imx" in out.lower())
    if not detected:
        print("  -> No CSI camera detected by libcamera. Check the ribbon seating,")
        print("     `dmesg | grep -i camera`, and that the cable is the right way round.")
    return detected


@telemetry.traced("camera", "probe.csi")
def capture_csi(outdir, width, height):
    rpicam = shutil.which("rpicam-jpeg") or shutil.which("libcamera-jpeg")
    if not rpicam:
        print("  rpicam-jpeg not found; skipping CSI capture.")
        return None
    path = os.path.join(outdir, "csi_sample.jpg")
    # -t is the AE/AWB warm-up window before the frame is saved; keep it small
    # for a fair benchmark but non-zero so exposure can settle.
    cmd = [rpicam, "-o", path, "-t", "800", "--width", str(width),
           "--height", str(height), "--nopreview"]
    t0 = time.time()
    rc, out, err = run(cmd, timeout=20)
    dt = time.time() - t0
    if rc == 0 and os.path.exists(path) and os.path.getsize(path) > 0:
        print(f"  CSI capture OK  -> {path}  ({os.path.getsize(path)} bytes, {dt*1000:.0f} ms incl. ~800ms warm-up)")
        return path
    print(f"  CSI capture FAILED (rc={rc}): {(err or out).strip()[:300]}")
    return None


# ---------------------------------------------------------------------------
# USB (UVC) camera — V4L2
# ---------------------------------------------------------------------------

def v4l2_node_info(dev):
    """Return (driver, bus_info, is_capture) for a /dev/videoN node."""
    driver, bus = "", ""
    rc, out, _ = run(["v4l2-ctl", "-d", dev, "--info"])
    if rc == 0:
        for line in out.splitlines():
            s = line.strip()
            if s.lower().startswith("driver name"):
                driver = s.split(":", 1)[-1].strip()
            elif s.lower().startswith("bus info"):
                bus = s.split(":", 1)[-1].strip()
    rc, out, _ = run(["v4l2-ctl", "-d", dev, "--list-formats-ext"])
    # A real capture node enumerates pixel formats / sizes; a metadata node does not.
    is_capture = rc == 0 and ("Size:" in out or "'YUYV'" in out or "'MJPG'" in out or "[0]:" in out)
    return driver, bus, is_capture


def enumerate_usb():
    hr("2. USB webcam (V4L2)")
    nodes = sorted(glob.glob("/dev/video*"))
    if not nodes:
        print("  No /dev/video* nodes present.")
        return []
    if shutil.which("v4l2-ctl"):
        rc, out, _ = run(["v4l2-ctl", "--list-devices"])
        if rc == 0:
            print(out.rstrip())
    else:
        print("  v4l2-ctl not found (sudo apt install -y v4l-utils). Falling back to raw node scan.")

    print("\n  Per-node analysis (driver / bus / capture-capable):")
    usb_capture = []
    for dev in nodes:
        if shutil.which("v4l2-ctl"):
            driver, bus, is_capture = v4l2_node_info(dev)
        else:
            driver, bus, is_capture = "?", "?", True  # unknown; let capture test decide
        is_usb = "usb" in bus.lower() or driver.lower() == "uvcvideo"
        kind = "USB" if is_usb else ("CSI/platform" if driver else "?")
        cap = "capture" if is_capture else "metadata/non-capture"
        print(f"    {dev:14s} {kind:13s} driver={driver or '?':10s} {cap}")
        if is_usb and is_capture:
            usb_capture.append(dev)
    if not usb_capture:
        print("\n  -> Could not positively identify a USB capture node via v4l2-ctl.")
        print("     The capture test below will probe each node directly.")
    else:
        print(f"\n  -> Likely USB capture node(s): {', '.join(usb_capture)}")
    return usb_capture or nodes


@telemetry.traced("camera", "probe.usb.opencv")
def capture_usb_opencv(dev, outdir, width, height):
    if not OPENCV_AVAILABLE:
        return None, "opencv not installed"
    # Open by explicit device PATH + V4L2 backend (robust); index-based open is
    # brittle when several cameras / metadata nodes are present.
    cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
    if not cap.isOpened():
        return None, "VideoCapture could not open device"
    try:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        for _ in range(5):           # discard frames for auto-exposure
            cap.read()
        t0 = time.time()
        ok, frame = cap.read()
        dt = time.time() - t0
        if not ok or frame is None:
            return None, "opened but read() returned no frame (metadata node?)"
        enc_ok, buf = cv2.imencode(".jpg", frame)
        if not enc_ok:
            return None, "imencode failed"
        path = os.path.join(outdir, "usb_sample.jpg")
        with open(path, "wb") as f:
            f.write(buf.tobytes())
        return (path, dt), None
    finally:
        cap.release()


@telemetry.traced("camera", "probe.usb.fswebcam")
def capture_usb_fswebcam(dev, outdir, width, height):
    if not shutil.which("fswebcam"):
        return None, "fswebcam not installed"
    path = os.path.join(outdir, "usb_sample.jpg")
    t0 = time.time()
    rc, out, err = run(["fswebcam", "-d", dev, "-r", f"{width}x{height}",
                        "--no-banner", "-S", "5", path], timeout=20)
    dt = time.time() - t0
    if rc == 0 and os.path.exists(path) and os.path.getsize(path) > 0:
        return (path, dt), None
    return None, (err or out).strip()[:200]


def capture_usb(candidates, outdir, width, height):
    print("\n  Capture test (OpenCV path+CAP_V4L2, then fswebcam fallback):")
    for dev in candidates:
        res, errmsg = capture_usb_opencv(dev, outdir, width, height)
        if res:
            path, dt = res
            print(f"    {dev}: OpenCV OK -> {path} ({os.path.getsize(path)} bytes, {dt*1000:.0f} ms read)")
            return dev, "usb (opencv)"
        print(f"    {dev}: OpenCV no -> {errmsg}")
        res, errmsg = capture_usb_fswebcam(dev, outdir, width, height)
        if res:
            path, dt = res
            print(f"    {dev}: fswebcam OK -> {path} ({os.path.getsize(path)} bytes, {dt*1000:.0f} ms)")
            return dev, "usb (fswebcam fallback)"
        print(f"    {dev}: fswebcam no -> {errmsg}")
    return None, None


def main():
    ap = argparse.ArgumentParser(description="Probe and validate Pi CSI + USB cameras.")
    ap.add_argument("--out", default="camera_probe_out", help="output dir for sample JPEGs")
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    # Observe-only; no-op if GP_TELEMETRY=0. atexit (registered by init) flushes
    # even if the probe raises; the explicit shutdown() at the end is for clarity.
    telemetry.init()

    if sys.platform == "darwin":
        print("WARNING: running on macOS, not the Pi. This script is meant to run ON the Pi")
        print("         (CSI/V4L2 tooling won't be present here).\n")

    csi_ok = enumerate_csi()
    csi_path = capture_csi(args.out, args.width, args.height) if csi_ok else None

    usb_candidates = enumerate_usb()
    usb_dev, usb_via = capture_usb(usb_candidates, args.out, args.width, args.height) if usb_candidates else (None, None)

    hr("3. Summary & recommended next commands")
    print(f"  CSI ribbon camera : {'OK' if csi_path else 'NOT WORKING'}")
    print(f"  USB webcam        : {'OK on ' + usb_dev + ' via ' + usb_via if usb_dev else 'NOT WORKING'}")
    print(f"  Sample images     : {os.path.abspath(args.out)}/  (open them to eyeball quality)")
    print()
    if csi_path:
        print("  Validate CSI end-to-end (set BACKEND to your dev machine IP:7878):")
        print("    python3 hardware/client.py --trigger --camera ribbon \\")
        print("        --backend http://<BACKEND>:7878 --mitigation-seconds 5")
    if usb_dev:
        print(f"  Validate USB end-to-end (note the resolved device node {usb_dev}):")
        print(f"    python3 hardware/client.py --trigger --camera usb --camera-device {usb_dev} \\")
        print("        --backend http://<BACKEND>:7878 --mitigation-seconds 5")
    if not csi_path and not usb_dev:
        print("  Neither camera captured. Fix camera detection before running client.py.")
    print()
    print("  Reminder: with the current backend, 'action: mitigate' does NOT prove the")
    print("  ML distinguished a critter from an empty scene (no softmax/label map yet).")
    print("  See the validation plan doc for what each test actually proves.")

    telemetry.shutdown()


if __name__ == "__main__":
    main()
