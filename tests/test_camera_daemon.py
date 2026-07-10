"""Tests for hardware/camera_daemon.py — the single-owner camera daemon.

The daemon owns each Pi camera and feeds two consumers from one capture loop: the
live LAN MJPEG stream and the periodic edge push (cadence is a hot-reloaded user
setting, default 30s). These tests pin the PURE pieces —
device→source mapping, registry discovery, the MJPEG frame splitter, and the
auto-config resolver — plus the latest-frame buffer's threading contract. No real
cameras (mock source), no real edge.
"""
import os
import threading

import pytest

from hardware import camera_daemon as cd

FIXTURE = os.path.join("tests", "fixtures", "raccoon.jpg")


# --- device -> source mapping ----------------------------------------------

def test_device_source_csi():
    assert cd.device_source({"type": "camera_csi", "device_id": "csi-camera-imx219"}) == "ribbon"


def test_device_source_usb_parses_node_from_id_and_name():
    # /dev node encoded in the id (the wizard's form)
    d = {"type": "camera_usb", "device_id": "uvcvideo-usb-camera-dev-video1"}
    assert cd.device_source(d) == "usb:/dev/video1"
    # literal path in the name wins / is found too
    d2 = {"type": "camera_usb", "device_id": "x", "name": "uvc @ /dev/video3"}
    assert cd.device_source(d2) == "usb:/dev/video3"
    # nothing parseable -> default node
    d3 = {"type": "camera_usb", "device_id": "plain-cam"}
    assert cd.device_source(d3) == "usb:/dev/video1"


def test_device_source_rejects_non_camera():
    with pytest.raises(ValueError):
        cd.device_source({"type": "motion_radar", "device_id": "r1"})


def test_parse_video_node_variants():
    assert cd.parse_video_node("/dev/video2") == "/dev/video2"
    assert cd.parse_video_node("foo-dev-video5") == "/dev/video5"
    assert cd.parse_video_node("nothing") == ""


# --- registry discovery -----------------------------------------------------

def test_discover_cameras_filters_to_active_cameras():
    mirror = {"devices": {"g1": {"devices": [
        {"device_id": "a", "type": "camera_csi"},
        {"device_id": "b", "type": "camera_usb", "name": "u @ /dev/video2"},
        {"device_id": "c", "type": "motion_radar"},            # not a camera
        {"device_id": "d", "type": "camera_usb", "status": "removed"},  # removed
    ]}}}
    assert cd.discover_cameras(mirror, "g1") == [("a", "ribbon"), ("b", "usb:/dev/video2")]


def test_discover_cameras_empty_for_unknown_garden():
    assert cd.discover_cameras({"devices": {}}, "nope") == []
    assert cd.discover_cameras({}, "g1") == []


# --- MJPEG frame splitter ---------------------------------------------------

def test_mjpeg_splitter_two_frames_and_partial():
    sp = cd.MjpegSplitter()
    frames = sp.feed(b"\xff\xd8AAA\xff\xd9\xff\xd8BBB\xff\xd9\xff\xd8CC")
    assert frames == [b"\xff\xd8AAA\xff\xd9", b"\xff\xd8BBB\xff\xd9"]
    # the trailing partial completes on the next chunk
    assert sp.feed(b"CC\xff\xd9") == [b"\xff\xd8CCCC\xff\xd9"]


def test_mjpeg_splitter_frame_split_across_chunks():
    sp = cd.MjpegSplitter()
    assert sp.feed(b"\xff\xd8AB") == []          # no EOI yet
    assert sp.feed(b"CD\xff\xd9") == [b"\xff\xd8ABCD\xff\xd9"]


def test_mjpeg_splitter_drops_leading_garbage():
    sp = cd.MjpegSplitter()
    assert sp.feed(b"junk\xff\xd8OK\xff\xd9") == [b"\xff\xd8OK\xff\xd9"]


# --- make_stream resolution -------------------------------------------------

def test_make_stream_types():
    assert isinstance(cd.make_stream("ribbon", width=640, height=480, fps=10), cd.RibbonStream)
    usb = cd.make_stream("usb:/dev/video4", width=640, height=480, fps=10)
    assert isinstance(usb, cd.USBStream) and usb.device == "/dev/video4"
    mock = cd.make_stream("mock:" + FIXTURE, width=1, height=1, fps=5)
    assert isinstance(mock, cd.MockStream)
    with pytest.raises(ValueError):
        cd.make_stream("rtsp://nope", width=1, height=1, fps=1)


def test_mock_stream_yields_fixture_bytes_until_stopped():
    stop = threading.Event()
    stream = cd.make_stream("mock:" + FIXTURE, width=1, height=1, fps=1000)
    gen = stream.frames(stop)
    first = next(gen)
    assert first == open(FIXTURE, "rb").read()
    stop.set()
    # after stop the generator drains and ends
    remaining = list(gen)
    assert all(f == first for f in remaining)


# --- FrameBuffer threading contract ----------------------------------------

def test_frame_buffer_latest_and_wait_next():
    buf = cd.FrameBuffer()
    assert buf.latest() == (None, 0, 0.0)
    buf.set(b"one", now=1.0)
    jpeg, seq, ts = buf.latest()
    assert jpeg == b"one" and seq == 1 and ts == 1.0

    # wait_next returns immediately when a newer frame already exists
    got, gotseq = buf.wait_next(0, timeout=0.1)
    assert got == b"one" and gotseq == 1

    # wait_next blocks until a producer thread posts a new frame
    def producer():
        buf.set(b"two", now=2.0)
    t = threading.Timer(0.05, producer)
    t.start()
    got2, seq2 = buf.wait_next(1, timeout=2.0)
    t.join()
    assert got2 == b"two" and seq2 == 2


def test_frame_buffer_wait_next_times_out_without_new_frame():
    buf = cd.FrameBuffer()
    buf.set(b"x", now=0.0)
    got, seq = buf.wait_next(1, timeout=0.05)   # already at seq 1 -> times out
    assert seq == 1 and got == b"x"


# --- auto-config from configs/ ---------------------------------------------

def test_auto_config_resolves_garden_token_and_cameras(tmp_path):
    import json
    from hardware import pi_config

    configs = tmp_path / "configs"
    configs.mkdir()
    pc = pi_config.PiConfig(str(configs))
    pc.save_partial({
        "provisioned": True,
        "node_id": "pi-01",
        "garden": {"garden_id": "homestead"},
        "fastly": {"backend_url": "https://edge.example/"},
    })
    pc.set_secret("garden_token", "tok-xyz")
    (configs / "svc123-registry.json").write_text(json.dumps({
        "devices": {"homestead": {"devices": [
            {"device_id": "csi-camera-imx219", "type": "camera_csi"},
            {"device_id": "uvc-dev-video1", "type": "camera_usb"},
        ]}}
    }))

    garden, backend, token, node, cameras = cd.auto_config(str(configs))
    assert garden == "homestead"
    assert backend == "https://edge.example"      # to_env() strips the trailing slash
    assert token == "tok-xyz"
    assert node == "pi-01"
    assert cameras == [("csi-camera-imx219", "ribbon"), ("uvc-dev-video1", "usb:/dev/video1")]


# --- cloud-upload cost knobs (cadence + photo quality) ----------------------

def test_resolve_quality_presets_and_fallback():
    from hardware import pi_config
    assert pi_config.resolve_quality("saver") == (480, 360, 60)
    assert pi_config.resolve_quality("high") == (1280, 720, 88)
    assert pi_config.resolve_quality("bogus") == pi_config.QUALITY_PRESETS["standard"]
    assert pi_config.resolve_quality(None) == pi_config.QUALITY_PRESETS["standard"]


def test_capture_settings_defaults_and_persist(tmp_path):
    from hardware import pi_config
    pc = pi_config.PiConfig(str(tmp_path))
    d = pc.capture_settings()
    assert d["interval_s"] == 30 and d["quality"] == "standard"
    assert d["daylight_only"] is True and d["night_mode"] == "motion"   # new defaults
    pc.save_partial({"capture": {"interval_s": 120, "quality": "saver"}})
    d = pc.capture_settings()
    assert d["interval_s"] == 120 and d["quality"] == "saver"
    # a junk value on disk falls back to safe defaults
    pc.save_partial({"capture": {"interval_s": "soon", "quality": "nope"}})
    d = pc.capture_settings()
    assert d["interval_s"] == 30 and d["quality"] == "standard"


def test_encode_for_upload_falls_back_without_cv2(monkeypatch):
    raw = b"\xff\xd8not-a-real-jpeg\xff\xd9"
    monkeypatch.setattr(cd, "OPENCV_AVAILABLE", False)
    assert cd._encode_for_upload(raw, 320, 240, 60) is raw


def test_push_loop_hot_reads_capture_settings(monkeypatch):
    # The push loop must re-read cadence/quality from PiConfig each tick (so a portal
    # save applies with no restart) and shrink the frame to that quality before POST.
    import threading
    import time as _t
    pushed = []
    monkeypatch.setattr(cd._pusher, "push_evidence",
                        lambda backend, jpeg, headers, timeout: (pushed.append(jpeg), {"action": "none", "species": "x"})[1])
    seen_quality = []
    monkeypatch.setattr(cd, "_encode_for_upload",
                        lambda jpeg, w, h, q: seen_quality.append((w, h, q)) or jpeg)

    class FakeBuf:
        def latest(self):
            return (b"\xff\xd8\xff\xd9", 1, 0.0)

    class FakePC:
        def __init__(self):
            self.reads = 0
        def capture_settings(self):
            self.reads += 1
            return {"interval_s": 1, "quality": "saver"}

    cfg = cd._pusher.PusherConfig(backend="http://edge", garden="g", token="t")
    pc = FakePC()
    stop = threading.Event()
    th = threading.Thread(target=cd._push_loop, args=(cfg, {"cam": FakeBuf()}, stop),
                          kwargs={"pc": pc}, daemon=True)
    th.start()
    _t.sleep(0.25)
    stop.set()
    th.join(timeout=2)
    assert pc.reads >= 1, "loop should hot-read capture_settings"
    assert pushed, "loop should push at least once"
    # quality token 'saver' resolved to its (w,h,q) and applied to the upload re-encode
    assert (480, 360, 60) in seen_quality


def test_main_auto_uses_saved_interval_and_enables_hot_reload(tmp_path, monkeypatch):
    # The systemd unit no longer passes --interval (it was a misleading dead "12"):
    # with --auto the cadence/quality come from the saved capture settings, and a
    # PiConfig is handed to the push loop so it can hot-reload on change.
    from hardware import pi_config
    pc = pi_config.PiConfig(str(tmp_path))
    pc.save_partial({"capture": {"interval_s": 300, "quality": "saver"}})
    monkeypatch.setattr(cd, "auto_config",
                        lambda d: ("g", "http://e", "t", "n", [("c", "ribbon")]))
    seen = {}
    monkeypatch.setattr(cd, "run", lambda cfg, cameras, **kw: seen.update(
        interval=cfg.interval, quality=kw.get("upload_quality"), pc=kw.get("pc")))
    cd.main(["--auto", "--configs-dir", str(tmp_path)])
    assert seen["interval"] == 300.0          # saved setting, not a CLI default
    assert seen["quality"] == "saver"
    assert seen["pc"] is not None             # push loop can hot-read on change


def test_main_explicit_camera_falls_back_to_default_interval(tmp_path, monkeypatch):
    # The --camera dev path has no hot-reload; with no --interval it uses the shared
    # default (30s), never a magic literal baked into the daemon.
    from hardware import pi_config
    seen = {}
    monkeypatch.setattr(cd, "run", lambda cfg, cameras, **kw: seen.update(
        interval=cfg.interval, pc=kw.get("pc")))
    cd.main(["--camera", "c=mock:" + FIXTURE])
    assert seen["interval"] == float(pi_config.DEFAULT_INTERVAL_S)
    assert seen["pc"] is None


# --- daylight-only / night-motion -------------------------------------------

def test_motion_score_pure():
    np = pytest.importorskip("numpy")
    a = np.zeros((10, 10), dtype=np.uint8)
    assert cd._motion_score(a, a.copy()) == 0.0          # identical frames
    assert cd._motion_score(None, a) == 0.0              # no reference yet
    b = a.copy(); b[:5, :] = 255                         # half the pixels jump
    assert cd._motion_score(a, b) == pytest.approx(0.5, abs=0.01)
    assert cd._motion_score(a, np.zeros((4, 4), np.uint8)) == 0.0   # shape mismatch


def test_brightness_pure():
    np = pytest.importorskip("numpy")
    assert cd._brightness(None) is None
    assert cd._brightness(np.full((4, 4), 200, np.uint8)) == pytest.approx(200.0)


def _run_push_loop_briefly(monkeypatch, settings, *, brightness, motion, dur=0.25, poll=None,
                           state_path=None, buf=None):
    """Drive _push_loop with controlled brightness + motion (image analysis stubbed, so
    no real frames/cv2 needed). Returns the list of push 'reasons' ('routine' or
    'night-motion …'). ``state_path`` enables the cross-restart schedule anchor; ``buf``
    overrides the (frame-ready) camera buffer."""
    import threading
    import time as _t
    pushes = []
    if poll is not None:
        monkeypatch.setattr(cd, "NIGHT_POLL_S", poll)
    monkeypatch.setattr(cd, "_push_one",
                        lambda cfg, did, jpeg, w, h, q, reason="", capture_ts=None, batch="":
                        pushes.append(reason or "routine"))
    monkeypatch.setattr(cd, "_decode_gray_small", lambda jpeg, width=320: "G" if jpeg else None)
    monkeypatch.setattr(cd, "_brightness", lambda g: brightness if g is not None else None)
    monkeypatch.setattr(cd, "_motion_score", lambda prev, cur: motion)

    class FakeBuf:
        def latest(self):
            return (b"\xff\xd8\xff\xd9", 1, 0.0)

    class FakePC:
        def capture_settings(self):
            return settings

    cfg = cd._pusher.PusherConfig(backend="http://edge", garden="g", token="t")
    stop = threading.Event()
    th = threading.Thread(target=cd._push_loop, args=(cfg, {"cam": buf if buf is not None else FakeBuf()}, stop),
                          kwargs={"pc": FakePC(), "state_path": state_path}, daemon=True)
    th.start()
    _t.sleep(dur)
    stop.set()
    th.join(timeout=2)
    return pushes


_DL = {"interval_s": 1, "quality": "saver", "daylight_only": True,
       "night_mode": "motion", "dark_below": 45}


def test_push_loop_daylight_does_routine(monkeypatch):
    pushes = _run_push_loop_briefly(monkeypatch, _DL, brightness=150, motion=0.0)
    assert pushes and all(r == "routine" for r in pushes)


def test_push_loop_dark_no_motion_is_quiet(monkeypatch):
    pushes = _run_push_loop_briefly(monkeypatch, _DL, brightness=10, motion=0.0)
    assert pushes == []                       # dark + nothing moving -> no upload


def test_push_loop_dark_motion_uploads_then_cools_down(monkeypatch):
    # Tick fast so several samples land inside the window; the per-camera cooldown must
    # still hold it to a single upload despite continuous "motion".
    pushes = _run_push_loop_briefly(monkeypatch, _DL, brightness=10, motion=0.9, poll=0.03)
    assert pushes == ["night-motion 90.0%"]


def test_push_loop_dark_pause_mode_ignores_motion(monkeypatch):
    s = {**_DL, "night_mode": "pause"}
    pushes = _run_push_loop_briefly(monkeypatch, s, brightness=10, motion=0.9, poll=0.03)
    assert pushes == []                       # pause = quiet until daylight


def test_push_loop_daylight_only_off_shoots_in_the_dark(monkeypatch):
    s = {**_DL, "daylight_only": False}
    pushes = _run_push_loop_briefly(monkeypatch, s, brightness=5, motion=0.0)
    assert pushes and all(r == "routine" for r in pushes)   # feature off -> routine regardless


def test_push_loop_daylight_stamps_one_batch_and_capture_ts(monkeypatch):
    # Every camera pushed in ONE daylight tick must carry the SAME batch id + capture_ts so
    # the edge files them under a single timeline moment (the multi-angle correlation goal).
    import threading
    import time as _t
    calls = []
    monkeypatch.setattr(cd, "_push_one",
                        lambda cfg, did, jpeg, w, h, q, reason="", capture_ts=None, batch="":
                        calls.append({"did": did, "capture_ts": capture_ts, "batch": batch}))

    class FakeBuf:
        def latest(self):
            return (b"\xff\xd8\xff\xd9", 1, 0.0)

    class FakePC:
        def capture_settings(self):
            return {"interval_s": 1, "quality": "saver", "daylight_only": False}

    cfg = cd._pusher.PusherConfig(backend="http://edge", garden="g", token="t")
    stop = threading.Event()
    buffers = {"cam-a": FakeBuf(), "cam-b": FakeBuf()}
    th = threading.Thread(target=cd._push_loop, args=(cfg, buffers, stop),
                          kwargs={"pc": FakePC()}, daemon=True)
    th.start(); _t.sleep(0.12); stop.set(); th.join(timeout=2)

    assert len(calls) >= 2
    tick = calls[:2]                                  # interval=1s -> only the first tick fires
    assert {c["did"] for c in tick} == {"cam-a", "cam-b"}
    assert tick[0]["batch"] and tick[0]["batch"] == tick[1]["batch"]        # one shared, non-empty batch
    assert tick[0]["capture_ts"] == tick[1]["capture_ts"]                   # one shared capture second
    assert isinstance(tick[0]["capture_ts"], int)


# --- motion-trigger detection engine (pure) ---------------------------------

def test_area_threshold_monotonic_and_bounded():
    ts = [cd._area_threshold(s) for s in (0.0, 0.25, 0.5, 0.75, 1.0)]
    assert all(ts[i] > ts[i + 1] for i in range(len(ts) - 1))   # higher sensitivity -> lower threshold
    assert ts[-1] == pytest.approx(cd.MOTION_AREA_MIN)
    assert ts[0] == pytest.approx(cd.MOTION_AREA_MAX)
    assert cd._area_threshold(5.0) == pytest.approx(cd.MOTION_AREA_MIN)   # clamps out-of-range


def test_crop_roi_and_background_confine_motion():
    np = pytest.importorskip("numpy")
    frame0 = np.zeros((10, 10), np.uint8)
    frame1 = frame0.copy(); frame1[0:3, 0:3] = 255          # movement only in the top-left
    tl = {"x": 0.0, "y": 0.0, "w": 0.3, "h": 0.3}
    br = {"x": 0.7, "y": 0.7, "w": 0.3, "h": 0.3}
    # Watching the top-left zone -> the change registers as motion.
    bg_tl = cd._blend_bg(None, cd._crop_roi(frame0, tl), 1.0)
    assert cd._motion_vs_bg(bg_tl, cd._crop_roi(frame1, tl)) > 0.5
    # Watching the bottom-right zone -> the same change is ignored (outside the monitor zone).
    bg_br = cd._blend_bg(None, cd._crop_roi(frame0, br), 1.0)
    assert cd._motion_vs_bg(bg_br, cd._crop_roi(frame1, br)) == 0.0
    # No ROI -> whole frame is watched.
    assert cd._crop_roi(frame0, None).shape == frame0.shape


def test_motion_vs_bg_needs_a_background():
    np = pytest.importorskip("numpy")
    g = np.zeros((8, 8), np.uint8)
    assert cd._motion_vs_bg(None, g) == 0.0                  # first sample, no reference yet
    assert cd._motion_vs_bg(g.astype("float32"), np.full((8, 8), 255, np.uint8)) == 1.0


def test_flush_motion_event_writes_and_caps(tmp_path, monkeypatch):
    monkeypatch.setattr(cd, "MOTION_EVENTS_KEEP", 2)
    base = str(tmp_path)
    for i in range(4):                                       # four events; only the newest 2 survive
        cd._flush_motion_event(base, "cam-a", [(0.0, b"\xff\xd8frame" + bytes([i]))], ts=1000 + i)
    cam = tmp_path / "cam-a"
    events = sorted(p.name for p in cam.iterdir())
    assert len(events) == 2
    frames = list((cam / events[-1]).iterdir())
    assert frames and frames[0].read_bytes().startswith(b"\xff\xd8")


def test_flush_motion_event_drops_folders_past_age_bound(tmp_path, monkeypatch):
    # The count cap alone lets a low-traffic camera hoard ancient evidence forever; the AGE cap
    # must also reap folders older than MOTION_EVENTS_MAX_AGE_S (folder name = event epoch-ms),
    # while keeping recent ones — and unparseable folder names are left untouched.
    monkeypatch.setattr(cd, "MOTION_EVENTS_KEEP", 100)          # high count cap -> isolate the AGE cap
    monkeypatch.setattr(cd, "MOTION_EVENTS_MAX_AGE_S", 10)      # 10s window for the test
    base = str(tmp_path)
    now = 1_000_000.0
    cam = tmp_path / "cam-a"
    cam.mkdir()
    # Two pre-existing folders: one well outside the window, one inside it.
    old = cam / str(int((now - 100) * 1000)); old.mkdir(); (old / "000.jpg").write_bytes(b"\xff\xd8old")
    recent = cam / str(int((now - 1) * 1000)); recent.mkdir(); (recent / "000.jpg").write_bytes(b"\xff\xd8new")
    junk = cam / "not-a-timestamp"; junk.mkdir(); (junk / "x").write_bytes(b"keep")
    # Flushing a fresh event at `now` should reap the stale folder and leave the rest.
    cd._flush_motion_event(base, "cam-a", [(0.0, b"\xff\xd8frame")], ts=now)
    names = {p.name for p in cam.iterdir()}
    assert old.name not in names                                # too old -> reaped
    assert recent.name in names                                # within the window -> kept
    assert junk.name in names                                  # unparseable -> left alone
    assert str(int(now * 1000)) in names                       # the new event was written


# --- motion-trigger watcher (through _push_loop) -----------------------------

def _run_watcher(monkeypatch, motion_cfg, *, vs_bg, brightness=100.0, dur=1.3):
    """Drive _push_loop with one motion-enabled camera, the image analysis stubbed so the
    'motion score' is forced. Returns the pushes that carried a trigger marker (the alarms)."""
    import time as _t
    pushes = []
    monkeypatch.setattr(cd, "_push_one",
                        lambda cfg, did, jpeg, w, h, q, reason="", capture_ts=None, batch="", trigger="":
                        pushes.append({"reason": reason, "trigger": trigger, "did": did}))
    monkeypatch.setattr(cd, "_decode_gray_small", lambda jpeg, width=320: "G" if jpeg else None)
    monkeypatch.setattr(cd, "_crop_roi", lambda gray, roi: gray)
    monkeypatch.setattr(cd, "_brightness", lambda g: brightness if g is not None else None)
    monkeypatch.setattr(cd, "_blend_bg", lambda bg, cur, a: cur)
    monkeypatch.setattr(cd, "_motion_vs_bg", lambda bg, cur: vs_bg)
    monkeypatch.setattr(cd, "MOTION_WARMUP_SAMPLES", 0)
    monkeypatch.setattr(cd, "_flush_motion_event", lambda *a, **k: None)

    class FakeBuf:
        def latest(self):
            return (b"\xff\xd8\xff\xd9", 1, 0.0)

    class FakePC:
        def capture_settings(self):        # daylight off + long interval: routine won't interfere
            return {"interval_s": 3600, "quality": "saver", "daylight_only": False}

        def motion_settings(self, did):
            return motion_cfg

    cfg = cd._pusher.PusherConfig(backend="http://edge", garden="g", token="t")
    stop = threading.Event()
    th = threading.Thread(target=cd._push_loop, args=(cfg, {"cam": FakeBuf()}, stop),
                          kwargs={"pc": FakePC()}, daemon=True)
    th.start(); _t.sleep(dur); stop.set(); th.join(timeout=2)
    return [p for p in pushes if p["trigger"]]            # only the marked (alarm) pushes


_MCFG = {"enabled": True, "cadence_s": 0.05, "confirm_frames": 2,
         "sensitivity": 0.5, "cooldown_s": 100, "roi": None}


def test_motion_trigger_fires_marked_after_confirm(monkeypatch):
    fires = _run_watcher(monkeypatch, _MCFG, vs_bg=1.0)
    # Sustained motion -> exactly one MARKED escalation (cooldown holds the rest), with a
    # "motion …" reason that doubles as the trigger marker the edge turns into an alarm.
    assert len(fires) == 1
    assert fires[0]["trigger"] == fires[0]["reason"]
    assert fires[0]["reason"].startswith("motion ")


def test_motion_trigger_quiet_without_motion(monkeypatch):
    assert _run_watcher(monkeypatch, _MCFG, vs_bg=0.0) == []     # nothing moving -> no alarm


def test_disable_clears_watcher_state_so_reenable_starts_fresh(monkeypatch):
    # Toggling a camera's motion trigger off must drop ALL its watcher state, so a later re-enable
    # re-arms warmup and discards the pre-disable background (otherwise the first post-re-enable
    # sample fires a spurious alarm against the stale scene). We observe two things on re-enable:
    #   * _blend_bg is called with bg=None (the stale background was cleared), and
    #   * warmup is re-armed, so there is NO fire on the very first re-enabled sample.
    import time as _t
    fires = []
    blend_bgs = []
    monkeypatch.setattr(cd, "_push_one",
                        lambda cfg, did, jpeg, w, h, q, reason="", capture_ts=None, batch="", trigger="":
                        fires.append(reason) if trigger else None)
    monkeypatch.setattr(cd, "_decode_gray_small", lambda jpeg, width=320: "G" if jpeg else None)
    monkeypatch.setattr(cd, "_crop_roi", lambda gray, roi: gray)
    monkeypatch.setattr(cd, "_brightness", lambda g: 100.0 if g is not None else None)
    monkeypatch.setattr(cd, "_blend_bg", lambda bg, cur, a: (blend_bgs.append(bg), "BG")[1])
    monkeypatch.setattr(cd, "_motion_vs_bg", lambda bg, cur: 1.0)   # always "motion"
    monkeypatch.setattr(cd, "_flush_motion_event", lambda *a, **k: None)
    # A 1-sample warmup so re-arming is observable (a fresh camera must settle before it fires).
    monkeypatch.setattr(cd, "MOTION_WARMUP_SAMPLES", 1)

    on = {"enabled": True, "cadence_s": 0.0, "confirm_frames": 1,
          "sensitivity": 0.5, "cooldown_s": 0.0, "roi": None}
    off = {**on, "enabled": False}
    # The watcher's tick floor is ~0.5s, and motion_settings() is read once per tick. Drive the
    # phases off that per-tick read: enabled (warm up + fire) -> disabled (state cleared) ->
    # re-enabled (warmup re-armed, fresh background).
    phases = {"i": 0}

    class FakeBuf:
        def latest(self):
            return (b"\xff\xd8\xff\xd9", 1, 0.0)

    class FakePC:
        # Short interval keeps the loop's tick floor low even while motion is DISABLED (an empty
        # motion_cfg would otherwise let the tick relax to ~5s and stall the test); the routine
        # daylight pushes it produces are unmarked, so they don't pollute `fires`.
        def capture_settings(self):
            return {"interval_s": 1, "quality": "saver", "daylight_only": False}

        def motion_settings(self, did):
            phases["i"] += 1
            if phases["i"] <= 2:
                return on            # tick 1 warms up, tick 2 fires
            if phases["i"] == 3:
                return off           # tick 3: disabled -> watcher state dropped
            return on                # tick 4+: re-enabled -> warmup re-armed, bg cleared

    cfg = cd._pusher.PusherConfig(backend="http://edge", garden="g", token="t")
    stop = threading.Event()
    th = threading.Thread(target=cd._push_loop, args=(cfg, {"cam": FakeBuf()}, stop),
                          kwargs={"pc": FakePC()}, daemon=True)
    th.start(); _t.sleep(4.0); stop.set(); th.join(timeout=3)

    assert fires, "an enabled camera with sustained motion should have fired at least once"
    # The fix's fingerprint: after the disable cleared mt_bg, the first re-enabled sample blends
    # against a None (fresh) background — the stale pre-disable scene was discarded.
    assert None in blend_bgs[1:], "re-enable must blend against a cleared (None) background"
    # And it should have blended against a real background while continuously enabled in phase 1
    # (proves we're not just always-None) — i.e. there is at least one non-None bg too.
    assert any(bg is not None for bg in blend_bgs)


def test_motion_trigger_disabled_is_inert(monkeypatch):
    assert _run_watcher(monkeypatch, {**_MCFG, "enabled": False}, vs_bg=1.0) == []


def test_push_loop_night_motion_has_no_batch(monkeypatch):
    # Night motion is an independent per-camera detection, so it must stay UNGROUPED (no
    # batch) while still sending the frame's real capture time for an accurate timeline.
    import threading
    import time as _t
    calls = []
    monkeypatch.setattr(cd, "NIGHT_POLL_S", 0.03)
    monkeypatch.setattr(cd, "_push_one",
                        lambda cfg, did, jpeg, w, h, q, reason="", capture_ts=None, batch="":
                        calls.append({"reason": reason, "capture_ts": capture_ts, "batch": batch}))
    monkeypatch.setattr(cd, "_decode_gray_small", lambda jpeg, width=320: "G" if jpeg else None)
    monkeypatch.setattr(cd, "_brightness", lambda g: 10 if g is not None else None)
    monkeypatch.setattr(cd, "_motion_score", lambda prev, cur: 0.9)

    class FakeBuf:
        def latest(self):
            return (b"\xff\xd8\xff\xd9", 1, 1_700_000_000.0)

    class FakePC:
        def capture_settings(self):
            return _DL

    cfg = cd._pusher.PusherConfig(backend="http://edge", garden="g", token="t")
    stop = threading.Event()
    th = threading.Thread(target=cd._push_loop, args=(cfg, {"cam": FakeBuf()}, stop),
                          kwargs={"pc": FakePC()}, daemon=True)
    th.start(); _t.sleep(0.2); stop.set(); th.join(timeout=2)

    assert calls and all(c["batch"] == "" for c in calls)         # ungrouped
    assert calls[0]["capture_ts"] == 1_700_000_000                # frame's real time, int
    assert calls[0]["reason"].startswith("night-motion")


# --- cross-restart push schedule --------------------------------------------

def test_push_state_roundtrip_and_sanitization(tmp_path):
    p = str(tmp_path / "camera_push_state.json")
    assert cd._load_last_push(p, 1000.0) == 0.0       # missing file -> "due now"
    cd._save_last_push(p, 950.0)
    assert cd._load_last_push(p, 1000.0) == 950.0     # round-trips
    assert cd._load_last_push(p, 900.0) == 0.0        # future value (clock jumped back) ignored
    with open(p, "w") as f:
        f.write("{ not json")
    assert cd._load_last_push(p, 1000.0) == 0.0       # corrupt -> "due now"
    # None path is a no-op both ways (in-memory schedule; pre-existing behavior)
    assert cd._load_last_push(None, 1000.0) == 0.0
    cd._save_last_push(None, 5.0)                      # must not raise


def test_push_loop_resumes_schedule_after_restart(monkeypatch, tmp_path):
    # Simulate a restart that happened right after a push: a FRESH anchor means we are NOT
    # yet due, so the loop must stay quiet for the rest of the interval instead of pushing
    # again from process-start (the deploy-storm gap fix).
    import time as _t
    p = str(tmp_path / "s.json")
    cd._save_last_push(p, _t.time())                   # "we just pushed" then restarted
    pushes = _run_push_loop_briefly(monkeypatch, _DL, brightness=150, motion=0.0,
                                    dur=0.3, state_path=p)
    assert pushes == []                                # within the persisted interval -> no push


def test_push_loop_pushes_when_anchor_is_stale_and_repersists(monkeypatch, tmp_path):
    import time as _t
    p = str(tmp_path / "s.json")
    cd._save_last_push(p, 1.0)                          # ancient anchor -> due now
    pushes = _run_push_loop_briefly(monkeypatch, _DL, brightness=150, motion=0.0,
                                    dur=0.25, state_path=p)
    assert pushes == ["routine"]                       # stale anchor -> pushes immediately
    assert cd._load_last_push(p, _t.time()) > 1.0      # ...and re-anchored to ~now


def test_push_loop_empty_buffer_does_not_consume_interval(monkeypatch, tmp_path):
    # A "due" tick that fires while the camera is still warming up (no frame yet) must NOT
    # advance the schedule — otherwise the first real push slips a whole interval.
    p = str(tmp_path / "s.json")

    class EmptyBuf:
        def latest(self):
            return (None, 0, 0.0)

    pushes = _run_push_loop_briefly(monkeypatch, _DL, brightness=150, motion=0.0,
                                    dur=0.25, state_path=p, buf=EmptyBuf())
    assert pushes == []                                # nothing to push
    assert cd._load_last_push(p, 1e12) == 0.0          # schedule NOT advanced (no anchor written)


@pytest.mark.skipif(not cd.OPENCV_AVAILABLE, reason="OpenCV not installed")
def test_encode_for_upload_shrinks_oversized_frame():
    import cv2
    import numpy as np
    big = np.zeros((720, 1280, 3), dtype=np.uint8)
    ok, enc = cv2.imencode(".jpg", big, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
    assert ok
    out = cd._encode_for_upload(enc.tobytes(), 480, 360, 60)
    dec = cv2.imdecode(np.frombuffer(out, np.uint8), cv2.IMREAD_COLOR)
    assert dec.shape[1] <= 480 and dec.shape[0] <= 360
    assert len(out) <= len(enc.tobytes())


# --- viewer-gated capture fps (CPU saver) ----------------------------------

def test_ribbon_req_fps_honours_low_and_overshoots():
    # The old code forced >=30 (pinning the CSI cam high even when idle); now low targets
    # stay low while live targets get ~1.6x headroom to clear rpicam's under-delivery.
    assert cd._ribbon_req_fps(2) == 3       # idle -> genuinely low
    assert cd._ribbon_req_fps(10) == 16     # live -> overshoot the 10 target
    assert cd._ribbon_req_fps(1) == 2       # floor
    assert cd._ribbon_req_fps(2) < 30       # regression: not pinned to 30


def test_capture_control_viewer_gating():
    c = cd.CaptureControl(idle_fps=2, live_fps=10)
    assert c.desired_fps() == 2 and c.viewers() == 0   # idle by default
    c.add_viewer()
    assert c.desired_fps() == 10 and c.viewers() == 1   # first viewer -> live
    assert c.restart.is_set()                           # ...and a rebuild was signalled
    c.restart.clear()
    c.add_viewer()                                       # second viewer: still live, no churn
    assert c.desired_fps() == 10 and not c.restart.is_set()
    c.remove_viewer()
    assert c.desired_fps() == 10 and not c.restart.is_set()  # one viewer left -> still live
    c.remove_viewer()
    assert c.desired_fps() == 2 and c.viewers() == 0    # last viewer gone -> idle
    assert c.restart.is_set()                           # ...rebuild at the low rate
    c.remove_viewer()                                    # underflow is clamped
    assert c.viewers() == 0


def test_any_event_is_set_on_either():
    a, b = threading.Event(), threading.Event()
    ev = cd._AnyEvent(a, b)
    assert not ev.is_set()
    b.set()
    assert ev.is_set()


def test_capture_loop_switches_fps_when_a_viewer_connects():
    import time
    fps_calls = []

    def make(fps):
        fps_calls.append(fps)
        return cd.MockStream(path=FIXTURE, fps=max(1, fps))

    control = cd.CaptureControl(idle_fps=2, live_fps=10)
    buf = cd.FrameBuffer()
    stop = threading.Event()
    t = threading.Thread(target=cd._capture_loop, args=("cam", make, control, buf, stop), daemon=True)
    t.start()
    try:
        deadline = time.time() + 2
        while not fps_calls and time.time() < deadline:
            time.sleep(0.02)
        assert fps_calls and fps_calls[0] == 2          # starts at the idle rate
        control.add_viewer()                            # a viewer connects...
        deadline = time.time() + 2
        while len(fps_calls) < 2 and time.time() < deadline:
            time.sleep(0.02)
        assert 10 in fps_calls                          # ...so the source is rebuilt at live fps
    finally:
        stop.set()
        t.join(timeout=2)
