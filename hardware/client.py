import time
import sys
import os
import subprocess
import tempfile
import threading
import traceback
import uuid
import requests

# Observe-only telemetry layer. Best-effort: when uninitialized (e.g. unit tests)
# every call is a no-op; it NEVER raises into the safety path. Dual import so it
# works both as a script (`python3 hardware/client.py`) and as a package
# (`python3 -m hardware.client` / pytest from the repo root).
try:
    from hardware import telemetry
except ImportError:
    import telemetry

# Header NAMES are the Pi<->edge wire contract; pull them from the generated SSOT
# (contract/spec.toml -> provision/contract_gen.py) so a spec rename can't silently
# diverge the senders from the edge (TEST-002). Systemd runs with the repo root as
# the working directory, so the package import resolves; the fallback adds the repo
# root to sys.path for plain-script execution (sys.path[0] == hardware/).
try:
    from provision import contract_gen as _cg
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from provision import contract_gen as _cg

# 1. Graceful hardware fallbacks for GPIO controls (gpiozero)
try:
    from gpiozero import OutputDevice, MotionSensor
    GPIO_AVAILABLE = True
except ImportError:
    GPIO_AVAILABLE = False

# 2. Graceful library fallbacks for streaming video analysis (OpenCV)
try:
    import cv2
    OPENCV_AVAILABLE = True
except ImportError:
    OPENCV_AVAILABLE = False


# =====================================================================
# PART I: CAMERA SOURCES (CameraSource Interface)
# =====================================================================

class CameraSource:
    """Base interface for camera devices."""
    def capture_image(self) -> bytes:
        raise NotImplementedError("Subclasses must implement capture_image.")


class MockCamera(CameraSource):
    """Fallback mock camera that serves local JPG files or dummy bytes."""
    def __init__(self, output_dir="tests/fixtures"):
        self.output_dir = output_dir

    @telemetry.traced("camera", "capture.mock")
    def capture_image(self) -> bytes:
        fixture_path = os.path.join(self.output_dir, "raccoon.jpg")
        if os.path.exists(fixture_path):
            with open(fixture_path, "rb") as f:
                return f.read()
        return b"DUMMY_IMAGE_BYTES"


class RibbonCamera(CameraSource):
    """CSI ribbon-style Raspberry Pi Camera using rpicam-jpeg."""
    def __init__(self, timeout_ms=1000, width=640, height=480, quality=None):
        self.timeout_ms = timeout_ms
        self.width = width
        self.height = height
        self.quality = quality  # JPEG quality 0-100; None -> rpicam-jpeg default

    @telemetry.traced("camera", "capture.csi")
    def capture_image(self) -> bytes:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_name = tmp.name
        try:
            cmd = [
                "rpicam-jpeg",
                "-o", tmp_name,
                "-t", str(self.timeout_ms),
                "--width", str(self.width),
                "--height", str(self.height),
                "--nopreview"
            ]
            if self.quality:
                cmd += ["--quality", str(int(self.quality))]
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            with open(tmp_name, "rb") as f:
                return f.read()
        except Exception as e:
            print(f"[CAMERA-RIBBON] Error during capture: {e}")
            raise
        finally:
            if os.path.exists(tmp_name):
                os.remove(tmp_name)


class USBCamera(CameraSource):
    """Standard USB webcam capturing either via OpenCV (fast) or fswebcam (fallback)."""
    def __init__(self, device="/dev/video1", width=640, height=480, quality=None):
        self.device = device
        self.width = width
        self.height = height
        self.quality = quality  # JPEG quality 0-100; None -> encoder default

    @telemetry.traced("camera", "capture.usb")
    def capture_image(self) -> bytes:
        # Prefer OpenCV if installed since it is much faster and stays in memory
        if OPENCV_AVAILABLE:
            try:
                # Open by explicit device PATH + V4L2 backend. Index-based opens
                # (cv2.VideoCapture(1)) are brittle when a CSI camera and UVC
                # metadata nodes also occupy /dev/video* — they can land on the
                # wrong or a non-capture node. Resolve the node with
                # hardware/camera_probe.py first.
                cap = cv2.VideoCapture(self.device, cv2.CAP_V4L2)
                if cap.isOpened():
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
                    # Discard first few frames for auto-exposure stabilization
                    for _ in range(3):
                        cap.read()
                    ret, frame = cap.read()
                    cap.release()
                    if ret and frame is not None:
                        enc_params = ([int(cv2.IMWRITE_JPEG_QUALITY), int(self.quality)]
                                      if self.quality else [])
                        _, encoded_img = cv2.imencode(".jpg", frame, enc_params)
                        return encoded_img.tobytes()
                    print(f"[CAMERA-USB] Opened {self.device} but read no frame "
                          f"(metadata node / wrong device?). Falling back to fswebcam...")
                else:
                    cap.release()
                    print(f"[CAMERA-USB] OpenCV could not open {self.device} via CAP_V4L2. "
                          f"Falling back to fswebcam...")
            except Exception as e:
                print(f"[CAMERA-USB] OpenCV memory capture failed: {e}. Falling back to fswebcam...")

        # Fallback to calling fswebcam CLI utility
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_name = tmp.name
        try:
            cmd = [
                "fswebcam",
                "-d", self.device,
                "-r", f"{self.width}x{self.height}",
                "--no-banner",
            ]
            if self.quality:
                cmd += ["--jpeg", str(int(self.quality))]
            cmd.append(tmp_name)
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            with open(tmp_name, "rb") as f:
                return f.read()
        except Exception as e:
            print(f"[CAMERA-USB] fswebcam utility capture failed: {e}")
            raise
        finally:
            if os.path.exists(tmp_name):
                os.remove(tmp_name)


class StreamCamera(CameraSource):
    """Captures evidence from an already-running OpenCV motion stream.

    When the motion trigger and the capture camera are the SAME USB device,
    opening a second VideoCapture would hit V4L2 streaming contention (EBUSY)
    and yield a failed/black frame. This reuses the trigger's latest frame
    instead of opening the device a second time.
    """
    def __init__(self, motion_trigger, fallback_device=None):
        self.motion_trigger = motion_trigger
        self.fallback_device = fallback_device

    @telemetry.traced("camera", "capture.stream")
    def capture_image(self) -> bytes:
        jpeg = self.motion_trigger.get_last_frame_jpeg()
        if jpeg is not None:
            return jpeg
        # Stream not active yet (e.g. one-shot --trigger): a direct open is safe.
        if self.fallback_device is not None:
            return USBCamera(device=self.fallback_device).capture_image()
        raise RuntimeError("Motion stream has no frame yet and no fallback device configured.")


# =====================================================================
# PART II: DETERRENT DEVICES (DeterrentDevice Interface)
# =====================================================================

class DeterrentDevice:
    """Base interface for relays, alarms, and other output peripherals."""
    def on(self):
        raise NotImplementedError()
    def off(self):
        raise NotImplementedError()
    @property
    def is_active(self) -> bool:
        raise NotImplementedError()


class MockDeterrent(DeterrentDevice):
    """In-memory state deterrent used for software tests and local dry-runs."""
    def __init__(self, name="Generic Deterrent"):
        self.name = name
        self._active = False

    @telemetry.traced("deterrent", "mock.on")
    def on(self):
        self._active = True
        print(f"[MOCK-DET] {self.name}: ACTIVE (HIGH)")

    @telemetry.traced("deterrent", "mock.off")
    def off(self):
        self._active = False
        print(f"[MOCK-DET] {self.name}: INACTIVE (LOW)")

    @property
    def is_active(self) -> bool:
        return self._active


class GPIDeterrent(DeterrentDevice):
    """Native Raspberry Pi GPIO output relay using gpiozero.

    Relay boards come in two polarities. A LOW-LEVEL-TRIGGER (active-low) board —
    the type specified in docs/hardware-reference.md — energizes its coil when the GPIO is driven
    LOW. Passing active_high=False makes gpiozero's logical on()/off() map to the
    physical relay correctly, and initial_value=False guarantees the relay starts
    DE-ENERGIZED (fail-closed) at construction rather than firing on boot.
    """
    def __init__(self, pin: int, active_high: bool = False):
        self.pin = pin
        self.active_high = active_high
        if GPIO_AVAILABLE:
            self.device = OutputDevice(pin, active_high=active_high, initial_value=False)
        else:
            print(f"[GPIO-DET] Warning: gpiozero not available. Falling back to Mock for pin {pin}.")
            self.device = MockDeterrent(f"GPIO Pin {pin}")

    @telemetry.traced("deterrent", "gpio.on")
    def on(self):
        self.device.on()

    @telemetry.traced("deterrent", "gpio.off")
    def off(self):
        self.device.off()

    @property
    def is_active(self) -> bool:
        if GPIO_AVAILABLE:
            return self.device.is_active
        return self.device.is_active


class USBCommandDeterrent(DeterrentDevice):
    """USB/External-command-controlled relay activated via shell commands."""
    def __init__(self, cmd_on: str, cmd_off: str):
        self.cmd_on = cmd_on
        self.cmd_off = cmd_off
        self._active = False

    @telemetry.traced("deterrent", "usbcmd.on")
    def on(self):
        self._active = True
        print(f"[USB-CMD-DET] Running on-command: {self.cmd_on}")
        try:
            subprocess.run(self.cmd_on, shell=True, check=True)
        except subprocess.SubprocessError as e:
            print(f"[USB-CMD-DET] On-command failed: {e}")

    @telemetry.traced("deterrent", "usbcmd.off")
    def off(self):
        self._active = False
        print(f"[USB-CMD-DET] Running off-command: {self.cmd_off}")
        try:
            subprocess.run(self.cmd_off, shell=True, check=True)
        except subprocess.SubprocessError as e:
            print(f"[USB-CMD-DET] Off-command failed: {e}")

    @property
    def is_active(self) -> bool:
        return self._active


# =====================================================================
# PART III: INPUT SENSORS / TRIGGERS (TriggerSensor Interface)
# =====================================================================

class TriggerSensor:
    """Base interface for physical triggers."""
    def set_callback(self, callback):
        raise NotImplementedError()
    def start(self):
        pass
    def stop(self):
        pass


class MockTrigger(TriggerSensor):
    """In-memory mock trigger for manual and unit tests."""
    def __init__(self):
        self.callback = None

    def set_callback(self, callback):
        self.callback = callback

    def manual_trip(self):
        """Simulate a sensor trigger manually."""
        if self.callback:
            print("[MOCK-TRIGGER] Manual sensor trip activated!")
            self.callback()


class GPIOTrigger(TriggerSensor):
    """Native Raspberry Pi PIR motion sensor (e.g. HC-SR501) on a GPIO pin.

    The HC-SR501 drives its output HIGH on motion (active-high), so we use
    gpiozero's MotionSensor (purpose-built, no internal pull-up) rather than
    Button, whose pull_up=True default would invert the trip logic and fight
    the PIR's push-pull output.
    """
    def __init__(self, pin: int):
        self.pin = pin
        self.callback = None
        if GPIO_AVAILABLE:
            self.sensor = MotionSensor(pin)
        else:
            print(f"[GPIO-TRIG] Warning: gpiozero not available. Falling back to Mock Trigger on pin {pin}.")
            self.sensor = MockTrigger()

    def set_callback(self, callback):
        self.callback = callback
        if GPIO_AVAILABLE:
            self.sensor.when_motion = callback
        else:
            self.sensor.set_callback(callback)


class OpenCVMotionTrigger(TriggerSensor):
    """Webcam streaming video analyzer that runs real-time frame differencing."""
    def __init__(self, device="/dev/video1", threshold=2000, cooldown_seconds=10):
        self.device = device
        self.threshold = threshold  # minimum pixel difference contour area to count as motion
        self.cooldown_seconds = cooldown_seconds
        self.callback = None
        self.running = False
        self.thread = None
        self._last_frame = None
        self._frame_lock = threading.Lock()

    def set_callback(self, callback):
        self.callback = callback

    def get_last_frame_jpeg(self):
        """Return the most recent streamed frame as JPEG bytes (or None).

        Lets evidence capture reuse the already-open stream instead of a second
        open() on the same V4L2 node, which would fail with EBUSY.
        """
        if not OPENCV_AVAILABLE:
            return None
        with self._frame_lock:
            frame = None if self._last_frame is None else self._last_frame.copy()
        if frame is None:
            return None
        ok, buf = cv2.imencode(".jpg", frame)
        return buf.tobytes() if ok else None

    def _safe_callback(self):
        try:
            if self.callback:
                self.callback()
        except Exception:
            print("[OPENCV-TRIG] Mitigation callback raised an exception:")
            traceback.print_exc()

    def start(self):
        if not OPENCV_AVAILABLE:
            print("[OPENCV-TRIG] Error: OpenCV is not installed. Run 'pip install opencv-python-headless'.")
            return
        self.running = True
        self.thread = threading.Thread(target=self._monitor_stream, daemon=True)
        self.thread.start()
        print(f"[OPENCV-TRIG] Background streaming motion analysis started on {self.device}.")

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=1.0)
            print("[OPENCV-TRIG] Background streaming motion analysis stopped.")

    def _monitor_stream(self):
        # Open by explicit device path + V4L2 backend (robust vs. index guessing).
        cap = cv2.VideoCapture(self.device, cv2.CAP_V4L2)
        if not cap.isOpened():
            print(f"[OPENCV-TRIG] Error: Could not open camera device {self.device} via CAP_V4L2.")
            return
        print(f"[OPENCV-TRIG] Streaming from {self.device}.")

        ret, frame1 = cap.read()
        if not ret:
            print("[OPENCV-TRIG] Error: Failed to read first frame from stream.")
            cap.release()
            return

        # Preprocess first frame
        gray1 = cv2.cvtColor(frame1, cv2.COLOR_BGR2GRAY)
        gray1 = cv2.GaussianBlur(gray1, (21, 21), 0)
        last_trigger_time = 0

        while self.running:
            ret, frame2 = cap.read()
            if not ret:
                break

            # Stash the latest frame so StreamCamera can grab evidence without
            # opening a second handle on the same device.
            with self._frame_lock:
                self._last_frame = frame2

            gray2 = cv2.cvtColor(frame2, cv2.COLOR_BGR2GRAY)
            gray2 = cv2.GaussianBlur(gray2, (21, 21), 0)

            # Compute difference between frames
            diff = cv2.absdiff(gray1, gray2)
            thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)[1]
            thresh = cv2.dilate(thresh, None, iterations=2)

            contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            motion_detected = False

            for contour in contours:
                if cv2.contourArea(contour) < self.threshold:
                    continue
                motion_detected = True
                break

            if motion_detected:
                now = time.time()
                if now - last_trigger_time > self.cooldown_seconds:
                    print(f"\n[OPENCV-TRIG] >>> Motion detected on camera stream ({self.device})! <<<")
                    last_trigger_time = now
                    if self.callback:
                        # Invoke trigger callback asynchronously (exceptions logged).
                        threading.Thread(target=self._safe_callback, daemon=True).start()

            gray1 = gray2
            time.sleep(0.15)  # Throttle polling to save Pi CPU cycles

        cap.release()


# =====================================================================
# PART IV: THE CLIENT CONTROLLER (GardenProtectorClient)
# =====================================================================

class GardenProtectorClient:
    def __init__(self, backend_url="http://localhost:7878",
                 camera_source=None,
                 sprinkler_device=None,
                 strobe_device=None,
                 trigger_sensor=None,
                 garden_id="default",
                 device_id="default",
                 node_id="default",
                 garden_token=""):
        self.backend_url = backend_url
        self.state = "IDLE"
        self.max_mitigation_seconds = 60
        self.poll_interval = 3.0
        self.http_timeout = 3.0
        self._state_lock = threading.Lock()

        # Forward-compat tenancy identity (default "default" until multi-garden
        # ships). Sent on every backend call so single->multi needs no migration.
        self.garden_id = garden_id
        self.device_id = device_id
        self.node_id = node_id
        # Optional per-garden deploy-time auth token (env GP_GARDEN_TOKEN). Plumbed
        # now, ENFORCED only at Step 3; the "default" garden stays tokenless. Empty
        # => no X-Garden-Auth header is sent (don't transmit an empty credential).
        self.garden_token = garden_token
        # Correlation id for the in-flight critter event; minted in
        # trigger_mitigation, attached to BOTH the evidence POST and that event's
        # heartbeats so a trip greps out end-to-end across both tiers.
        self._active_cid = None

        # Injectable Hardware Abstraction Layer
        self.camera = camera_source or MockCamera()
        self.sprinkler = sprinkler_device or MockDeterrent("Sprinkler")
        self.strobe = strobe_device or MockDeterrent("Strobe")
        self.pir_sensor = trigger_sensor or MockTrigger()

        # Link sensor callback
        self.pir_sensor.set_callback(self.trigger_mitigation)

    def _request_headers(self, extra=None):
        """Headers for every backend call: the per-event trace id + forward-compat
        identity. Observe-only correlation — never affects the safety decision."""
        headers = {
            _cg.HEADER_TRACE_ID: self._active_cid or "",
            _cg.HEADER_GARDEN_ID: self.garden_id,
            _cg.HEADER_DEVICE_ID: self.device_id,
            _cg.HEADER_NODE_ID: self.node_id,
        }
        # Per-garden auth token, only when configured (default garden is tokenless).
        # The edge does not enforce it yet (Step 3); sending it now is forward-compat.
        if self.garden_token:
            headers[_cg.HEADER_AUTH] = self.garden_token
        if extra:
            headers.update(extra)
        return headers

    def disarm_all(self):
        """Safely shuts off all physical deterrent hardware. Guaranteed fail-closed.

        Wrapped in a telemetry span (the most safety-critical action) so a disarm
        is one greppable row alongside the per-relay rows. trace_span is
        non-blocking and re-raises unchanged, so the fail-closed behavior is intact.
        """
        with telemetry.trace_span("deterrent", "disarm_all", cid=self._active_cid):
            self.sprinkler.off()
            self.strobe.off()
            # Verify the logical state actually went inactive. If a relay still reports
            # active after off(), the board polarity is likely misconfigured (a
            # low-level-trigger board needs active_high=False / --relay-active-low).
            for name, dev in (("sprinkler", self.sprinkler), ("strobe", self.strobe)):
                try:
                    if dev.is_active:
                        print(f"[CLIENT] WARNING: {name} still reports ACTIVE after off() — "
                              f"check relay polarity (--relay-active-low/--relay-active-high)!")
                except Exception:
                    pass
            print("[CLIENT] Fail-closed: All deterrent devices disarmed.")

    def _set_state(self, new):
        """Transition the FSM and record it (best-effort, observe-only). Replaces
        the bare ``self.state = …`` assignments so every transition is telemetered."""
        old = self.state
        self.state = new
        telemetry.emit("fsm", "transition", cid=self._active_cid, args={"from": old, "to": new})

    def trigger_mitigation(self):
        """Transition from IDLE to TRIGGERED when sensor trips."""
        with self._state_lock:
            if self.state != "IDLE":
                return
            # Mint ONE 16-hex correlation id for this critter event, inside the lock
            # so it is set before any telemetry. Stored on the instance (not a
            # thread-local) so start_mitigation's heartbeat loop sends the SAME id
            # even when the OpenCV trigger runs this on a per-event daemon thread.
            # The IDLE re-entry guard above keeps exactly one event live.
            self._active_cid = uuid.uuid4().hex[:16]
            self.state = "TRIGGERED"

        # Bind the id to this thread's telemetry context (for @traced HAL calls) and
        # record the trigger + the IDLE->TRIGGERED transition (emits are non-blocking).
        telemetry.set_cid(self._active_cid)
        telemetry.emit("trigger", "motion", cid=self._active_cid)
        telemetry.emit("fsm", "transition", cid=self._active_cid, args={"from": "IDLE", "to": "TRIGGERED"})

        print(f"\n[CLIENT] Sensor tripped! Transitioning to TRIGGERED. (trace={self._active_cid})")
        
        # 1. Capture image
        print("[CLIENT] Capturing image evidence...")
        try:
            image_bytes = self.camera.capture_image()
        except Exception as e:
            print(f"[CLIENT] Camera hardware capture failed: {e}. Aborting trigger.")
            self.disarm_all()
            self._set_state("COOLDOWN")
            self.cooldown()
            return
        
        # 2. POST evidence to Fastly Compute edge
        try:
            print(f"[CLIENT] Uploading raw binary evidence to Fastly edge ({self.backend_url}/api/evidence)...")
            _post_t0 = time.perf_counter()
            response = requests.post(
                f"{self.backend_url}/api/evidence",
                data=image_bytes,
                headers=self._request_headers({"Content-Type": "image/jpeg"}),
                timeout=30.0
            )
            _post_ms = (time.perf_counter() - _post_t0) * 1000.0

            if response.status_code == 200:
                payload = response.json()
                action = payload.get("action", "none")
                # Emit AFTER the safety decision (action) is known, before acting.
                telemetry.emit("http", "POST /api/evidence", cid=self._active_cid,
                               dur_ms=_post_ms, outcome="ok", detail=f"status=200 action={action}")
                print(f"[CLIENT] Received action command from Fastly edge: {action}")

                if action == "mitigate":
                    self.start_mitigation()
                else:
                    print("[CLIENT] Edge decided no mitigation needed.")
                    self._set_state("COOLDOWN")
                    self.cooldown()
            else:
                print(f"[CLIENT] HTTP Error response from Fastly edge: {response.status_code}. Fail-closed.")
                self.disarm_all()
                # Emit after disarm so telemetry never delays the fail-closed action.
                telemetry.emit("http", "POST /api/evidence", cid=self._active_cid,
                               dur_ms=_post_ms, outcome="error", detail=f"status={response.status_code}")
                self._set_state("COOLDOWN")
                self.cooldown()

        except requests.exceptions.RequestException as e:
            print(f"[CLIENT] Connection failure during evidence upload to Fastly: {e}")
            self.disarm_all()
            telemetry.emit("http", "POST /api/evidence", cid=self._active_cid,
                           outcome="error", detail=f"exception={type(e).__name__}")
            self._set_state("COOLDOWN")
            self.cooldown()
        except Exception as e:
            # Defensive: never let an unexpected error (e.g. malformed JSON body)
            # leave the device in an armed/unknown state.
            print(f"[CLIENT] Unexpected error during evidence handling: {e}")
            traceback.print_exc()
            self.disarm_all()
            telemetry.emit("http", "POST /api/evidence", cid=self._active_cid,
                           outcome="error", detail=f"exception={type(e).__name__}")
            self._set_state("COOLDOWN")
            self.cooldown()

    def start_mitigation(self):
        """Activate physical relays and poll status loop."""
        self._set_state("MITIGATING")
        print("[CLIENT] Activating physical deterrent relays...")
        self.sprinkler.on()
        self.strobe.on()

        start_time = time.time()

        # Keep-alive status loop
        while self.state == "MITIGATING":
            elapsed = time.time() - start_time
            if elapsed >= self.max_mitigation_seconds:
                print(f"[CLIENT] Watchdog LIMIT REACHED ({self.max_mitigation_seconds}s). Force disarming.")
                self.disarm_all()
                self._set_state("COOLDOWN")
                break

            # Perform keep-alive heartbeat check
            try:
                print(f"[CLIENT] Fetching heartbeat status from Fastly ({self.backend_url}/api/status)...")
                _hb_t0 = time.perf_counter()
                response = requests.get(
                    f"{self.backend_url}/api/status",
                    headers=self._request_headers(),
                    timeout=self.http_timeout
                )
                _hb_ms = (time.perf_counter() - _hb_t0) * 1000.0

                if response.status_code == 200:
                    payload = response.json()
                    continue_mitigation = payload.get("continue_mitigation", False)
                    print(f"[CLIENT] Heartbeat result: continue_mitigation={continue_mitigation}")

                    if not continue_mitigation:
                        print("[CLIENT] Fastly edge ordered mitigation to STOP.")
                        self.disarm_all()
                        # Emit after disarm so telemetry never delays the safety action.
                        telemetry.emit("http", "GET /api/status", cid=self._active_cid,
                                       dur_ms=_hb_ms, outcome="ok", detail="continue=false")
                        self._set_state("COOLDOWN")
                        break
                    else:
                        telemetry.emit("http", "GET /api/status", cid=self._active_cid,
                                       dur_ms=_hb_ms, outcome="ok", detail="continue=true")
                else:
                    print(f"[CLIENT] Heartbeat HTTP error {response.status_code}. Fail-closed.")
                    self.disarm_all()
                    telemetry.emit("http", "GET /api/status", cid=self._active_cid,
                                   dur_ms=_hb_ms, outcome="error", detail=f"status={response.status_code}")
                    self._set_state("COOLDOWN")
                    break

            except requests.exceptions.RequestException as e:
                print(f"[CLIENT] Heartbeat network exception: {e}. Fail-closed immediately.")
                self.disarm_all()
                telemetry.emit("http", "GET /api/status", cid=self._active_cid,
                               outcome="error", detail=f"exception={type(e).__name__}")
                self._set_state("COOLDOWN")
                break
            except Exception as e:
                # Any other error mid-mitigation must still fail closed.
                print(f"[CLIENT] Unexpected heartbeat error: {e}. Fail-closed immediately.")
                traceback.print_exc()
                self.disarm_all()
                telemetry.emit("http", "GET /api/status", cid=self._active_cid,
                               outcome="error", detail=f"exception={type(e).__name__}")
                self._set_state("COOLDOWN")
                break

            time.sleep(self.poll_interval)

        self.cooldown()

    def cooldown(self):
        print("[CLIENT] Entering COOLDOWN state for 5 seconds...")
        time.sleep(5)
        self._set_state("IDLE")
        print("[CLIENT] Ready and listening for next trigger.")


# =====================================================================
# PART V: CMD-LINE UTILITY ENTRYPOINT
# =====================================================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Fastly Garden Protector Edge-Native IoT Client")
    
    # Generic Backend configuration
    parser.add_argument("--backend", default="http://localhost:7878",
                        help="Fastly Compute local server or production URL")

    # Forward-compat tenancy identity (default "default" until multi-garden ships).
    # Sent as X-Garden-Id / X-Device-Id / X-Node-Id on every backend call.
    parser.add_argument("--garden-id", default=os.environ.get("GP_GARDEN_ID", "default"),
                        help="Garden tenant id (env GP_GARDEN_ID; default 'default')")
    parser.add_argument("--device-id", default=os.environ.get("GP_DEVICE_ID", "default"),
                        help="Device id for this Pi/observer (env GP_DEVICE_ID; default 'default')")
    parser.add_argument("--node-id", default=os.environ.get("GP_NODE_ID", "default"),
                        help="Controller node id (env GP_NODE_ID; default 'default')")
    parser.add_argument("--garden-token", default=os.environ.get("GP_GARDEN_TOKEN", ""),
                        help="Optional per-garden auth token (env GP_GARDEN_TOKEN; default "
                             "empty/tokenless). Sent as X-Garden-Auth; not enforced by the "
                             "edge until Step 3.")
    
    # Camera selection
    parser.add_argument("--camera", choices=["mock", "ribbon", "usb"], default="mock", 
                        help="Camera interface source (default: mock)")
    parser.add_argument("--camera-device", default="/dev/video1", 
                        help="System file path for USB camera device node (default: /dev/video1)")
    
    # Sprinkler relay selection
    parser.add_argument("--sprinkler-type", choices=["mock", "gpio", "usb_command"], default="mock", 
                        help="Sprinkler control relay interface (default: mock)")
    parser.add_argument("--sprinkler-pin", type=int, default=17, 
                        help="GPIO pin for sprinkler relay (default: 17)")
    parser.add_argument("--sprinkler-cmd-on", default="echo 'sprinkler ON'", 
                        help="Shell command to run to turn sprinkler relay ON (for usb_command)")
    parser.add_argument("--sprinkler-cmd-off", default="echo 'sprinkler OFF'", 
                        help="Shell command to run to turn sprinkler relay OFF (for usb_command)")
    
    # Strobe light relay selection
    parser.add_argument("--strobe-type", choices=["mock", "gpio", "usb_command"], default="mock", 
                        help="Strobe light control relay interface (default: mock)")
    parser.add_argument("--strobe-pin", type=int, default=27, 
                        help="GPIO pin for strobe relay (default: 27)")
    parser.add_argument("--strobe-cmd-on", default="echo 'strobe ON'", 
                        help="Shell command to run to turn strobe light ON (for usb_command)")
    parser.add_argument("--strobe-cmd-off", default="echo 'strobe OFF'", 
                        help="Shell command to run to turn strobe light OFF (for usb_command)")
    parser.add_argument("--relay-active-high", action="store_true",
                        help="Treat GPIO relays as ACTIVE-HIGH. Default is active-low "
                             "(low-level-trigger board, per docs/hardware-reference.md) so relays are "
                             "de-energized at boot and on disarm.")
    
    # Sensor/Trigger selection
    parser.add_argument("--pir-type", choices=["mock", "gpio", "opencv_stream"], default="mock", 
                        help="PIR motion sensor trigger interface (default: mock)")
    parser.add_argument("--pir-pin", type=int, default=4, 
                        help="GPIO pin for physical PIR sensor (default: 4)")
    parser.add_argument("--opencv-threshold", type=int, default=2000, 
                        help="Pixel contour area threshold for OpenCV streaming motion detection (default: 2000)")
    
    # Execution modes
    parser.add_argument("--trigger", action="store_true", 
                        help="Immediately execute a single manual mock sensor trip and exit")
    parser.add_argument("--monitor", action="store_true", 
                        help="Start persistent background monitoring loop and run indefinitely")
    parser.add_argument("--mitigation-seconds", type=int, default=15,
                        help="Watchdog limit in seconds for deterrent activation before force disarming (default: 15)")
    parser.add_argument("--telemetry-db", default=None,
                        help="Telemetry SQLite DB path (env GP_TELEMETRY_DB; default "
                             "/var/lib/garden-protector/telemetry.db, falls back to "
                             "~/.local/state). Set GP_TELEMETRY=0 to disable telemetry.")

    args = parser.parse_args()

    # Start the observe-only telemetry layer (no-op if GP_TELEMETRY=0). Best-effort
    # and non-blocking; shutdown() is wired into BOTH run modes below + atexit so a
    # one-shot --trigger flushes its rows before the process exits.
    telemetry.init(db_path=args.telemetry_db, garden_id=args.garden_id,
                   device_id=args.device_id, node_id=args.node_id)

    # 1. Instantiate Camera Source
    if args.camera == "ribbon":
        cam = RibbonCamera()
    elif args.camera == "usb":
        cam = USBCamera(device=args.camera_device)
    else:
        cam = MockCamera()

    # 2. Instantiate Sprinkler Relay
    if args.sprinkler_type == "gpio":
        sprinkler = GPIDeterrent(pin=args.sprinkler_pin, active_high=args.relay_active_high)
    elif args.sprinkler_type == "usb_command":
        sprinkler = USBCommandDeterrent(cmd_on=args.sprinkler_cmd_on, cmd_off=args.sprinkler_cmd_off)
    else:
        sprinkler = MockDeterrent("Sprinkler")

    # 3. Instantiate Strobe Relay
    if args.strobe_type == "gpio":
        strobe = GPIDeterrent(pin=args.strobe_pin, active_high=args.relay_active_high)
    elif args.strobe_type == "usb_command":
        strobe = USBCommandDeterrent(cmd_on=args.strobe_cmd_on, cmd_off=args.strobe_cmd_off)
    else:
        strobe = MockDeterrent("Strobe")

    # 4. Instantiate PIR/Input Sensor
    if args.pir_type == "gpio":
        sensor = GPIOTrigger(pin=args.pir_pin)
    elif args.pir_type == "opencv_stream":
        sensor = OpenCVMotionTrigger(device=args.camera_device, threshold=args.opencv_threshold)
    else:
        sensor = MockTrigger()

    # Avoid V4L2 contention: if the OpenCV motion stream and the USB capture target
    # the same device, capture evidence from the stream's frames instead of opening
    # a second handle on the same node (which would fail with EBUSY / black frame).
    if isinstance(sensor, OpenCVMotionTrigger) and isinstance(cam, USBCamera) and cam.device == sensor.device:
        print(f"[CLIENT] Note: motion stream and capture share {cam.device}; "
              f"capturing evidence from the motion stream to avoid V4L2 contention.")
        cam = StreamCamera(sensor, fallback_device=cam.device)

    # 5. Assemble Client Controller
    client = GardenProtectorClient(
        backend_url=args.backend,
        camera_source=cam,
        sprinkler_device=sprinkler,
        strobe_device=strobe,
        trigger_sensor=sensor,
        garden_id=args.garden_id,
        device_id=args.device_id,
        node_id=args.node_id,
        garden_token=args.garden_token
    )
    client.max_mitigation_seconds = args.mitigation_seconds

    # 6. Execute Run Mode
    if args.trigger:
        # If it is a MockTrigger, manually trip it. Otherwise, trigger it via the State Machine directly.
        try:
            if isinstance(sensor, MockTrigger):
                sensor.manual_trip()
            else:
                client.trigger_mitigation()
        finally:
            # One-shot path has no other cleanup; flush telemetry before exit.
            telemetry.shutdown()
    elif args.monitor:
        # Start persistent monitor mode
        print(f"[CLIENT] Starting Fastly Garden Protector client in persistent monitoring mode...")
        print(f"[CLIENT] Configurations:")
        print(f"  - Edge Backend: {args.backend}")
        print(f"  - Camera Source: {args.camera} ({args.camera_device if args.camera == 'usb' else 'CSI Bus'})")
        print(f"  - Sprinkler Device: {args.sprinkler_type} (Pin: {args.sprinkler_pin if args.sprinkler_type == 'gpio' else 'N/A'})")
        print(f"  - Strobe Device: {args.strobe_type} (Pin: {args.strobe_pin if args.strobe_type == 'gpio' else 'N/A'})")
        print(f"  - Trigger Sensor: {args.pir_type} (Pin/Dev: {args.pir_pin if args.pir_type == 'gpio' else args.camera_device if args.pir_type == 'opencv_stream' else 'Mock'})")
        
        # Start the background thread for streaming sensors if needed
        sensor.start()

        try:
            while True:
                # Keep main thread alive. For mock trigger, allow manual terminal inputs.
                if isinstance(sensor, MockTrigger):
                    inp = input("\n[MONITOR] Enter 't' to manually trip mock sensor, or 'q' to exit: ").strip().lower()
                    if inp == 't':
                        sensor.manual_trip()
                    elif inp == 'q':
                        break
                else:
                    time.sleep(1)
        except KeyboardInterrupt:
            print("\n[CLIENT] Shutdown signal received.")
        finally:
            sensor.stop()
            client.disarm_all()
            telemetry.shutdown()  # flush after the final disarm
            print("[CLIENT] Closed successfully.")
    else:
        print("[CLIENT] Please specify execution mode: --trigger or --monitor.")
        parser.print_help()
