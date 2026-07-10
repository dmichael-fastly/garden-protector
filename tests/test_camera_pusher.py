"""Tests for hardware/camera_pusher.py — the directly-attached-camera → edge pusher.

The pusher is the MONITORING path: it captures a JPEG per camera and POSTs it to
/api/evidence with that camera's X-Device-Id, then ignores the verdict. These tests
pin the wire contract (endpoint, raw-JPEG body, identity headers, tokenless vs
tokened) and the per-camera failure isolation, all without real cameras (MockCamera /
a file source) or a real edge (responses-mocked HTTP).
"""
import os

import pytest
import responses

from hardware import camera_pusher
from hardware.client import MockCamera, RibbonCamera, USBCamera

FIXTURE = os.path.join("tests", "fixtures", "raccoon.jpg")


# --- pure source parsing / resolution --------------------------------------

def test_parse_camera_arg_ok():
    assert camera_pusher.parse_camera_arg("cam-a=usb:/dev/video1") == ("cam-a", "usb:/dev/video1")
    assert camera_pusher.parse_camera_arg("ribbon-cam = ribbon") == ("ribbon-cam", "ribbon")


@pytest.mark.parametrize("bad", ["noequals", "=usb", "cam=", "  =  "])
def test_parse_camera_arg_rejects(bad):
    with pytest.raises(ValueError):
        camera_pusher.parse_camera_arg(bad)


def test_make_camera_types():
    assert isinstance(camera_pusher.make_camera("ribbon"), RibbonCamera)
    assert isinstance(camera_pusher.make_camera("csi"), RibbonCamera)
    assert isinstance(camera_pusher.make_camera("mock"), MockCamera)
    usb = camera_pusher.make_camera("usb")
    assert isinstance(usb, USBCamera) and usb.device == "/dev/video1"
    usb2 = camera_pusher.make_camera("usb:/dev/video3")
    assert isinstance(usb2, USBCamera) and usb2.device == "/dev/video3"
    fc = camera_pusher.make_camera("mock:" + FIXTURE)
    assert fc.capture_image() == open(FIXTURE, "rb").read()


def test_make_camera_threads_quality_and_size():
    cam = camera_pusher.make_camera("ribbon", width=320, height=240, quality=55)
    assert isinstance(cam, RibbonCamera)
    assert cam.width == 320 and cam.height == 240 and cam.quality == 55
    usb = camera_pusher.make_camera("usb:/dev/video2", width=100, height=80, quality=40)
    assert isinstance(usb, USBCamera) and usb.quality == 40


def test_make_camera_unknown():
    with pytest.raises(ValueError):
        camera_pusher.make_camera("rtsp://nope")


# --- header / wire contract -------------------------------------------------

def test_build_headers_tokened_and_tokenless():
    h = camera_pusher.build_headers("home", "cam-x", "pi-1", "tok-9", "trace-abc")
    assert h["X-Garden-Id"] == "home"
    assert h["X-Device-Id"] == "cam-x"
    assert h["X-Node-Id"] == "pi-1"
    assert h["X-Garden-Trace-Id"] == "trace-abc"
    assert h["X-Garden-Auth"] == "tok-9"
    assert h["Content-Type"] == "image/jpeg"
    assert "X-Garden-Auth" not in camera_pusher.build_headers("home", "c", "p", "", "t")
    # Multi-angle correlation headers + the alarm trigger marker are OPTIONAL: omitted by
    # default so older edges / the safety gateway are unaffected and routine frames don't alarm.
    assert "X-Capture-Ts" not in h and "X-Capture-Batch" not in h
    assert "X-Trigger" not in h


def test_build_headers_capture_correlation():
    # When supplied, the capture time is sent as a whole-second string and the batch id
    # verbatim, so every camera in a tick shares one timeline second + one group key.
    h = camera_pusher.build_headers("home", "cam-x", "pi-1", "", "tr",
                                    capture_ts=1_609_556_645.8, batch="tick-9f2a")
    assert h["X-Capture-Ts"] == "1609556645"   # int-truncated, stringified
    assert h["X-Capture-Batch"] == "tick-9f2a"
    # An empty batch is still omitted (ungrouped), even with a capture time present.
    h2 = camera_pusher.build_headers("home", "cam-x", "pi-1", "", "tr", capture_ts=10, batch="")
    assert h2["X-Capture-Ts"] == "10" and "X-Capture-Batch" not in h2


def test_build_headers_trigger_marker():
    # The alarm trigger marker is sent verbatim only when set (a confirmed motion event);
    # omitted otherwise so routine cadence pushes stay History-only at the edge.
    h = camera_pusher.build_headers("home", "cam-x", "pi-1", "", "tr", trigger="motion 4.2% x3")
    assert h["X-Trigger"] == "motion 4.2% x3"
    assert "X-Trigger" not in camera_pusher.build_headers("home", "c", "p", "", "t", trigger="")


def test_new_batch_is_short_and_unique():
    a, b = camera_pusher.new_batch(), camera_pusher.new_batch()
    assert a != b and len(a) == 12 and a.isalnum()


# --- header SSOT conformance (TEST-002) -------------------------------------
# The Pi->edge wire headers are single-sourced in contract/spec.toml -> the
# generated provision.contract_gen.HEADER_* constants, which the Rust edge reads.
# These tests assert BOTH Python senders emit keys EQUAL to those constants (by
# reference to the constant, not a re-typed literal), so a `make gen` rename of a
# header in spec.toml can no longer silently break the wire with all tests green.

def test_camera_pusher_build_headers_use_generated_header_consts():
    from provision import contract_gen as cg

    # All optional + mandatory headers present so the full key set is exercised.
    h = camera_pusher.build_headers(
        "home", "cam-x", "pi-1", "tok-9", "trace-abc",
        capture_ts=1_609_556_645, batch="tick-9f2a", trigger="motion x3",
    )
    # Every identity/tracing/auth key the sender wrote is the GENERATED constant.
    assert cg.HEADER_GARDEN_ID in h and h[cg.HEADER_GARDEN_ID] == "home"
    assert cg.HEADER_DEVICE_ID in h and h[cg.HEADER_DEVICE_ID] == "cam-x"
    assert cg.HEADER_NODE_ID in h and h[cg.HEADER_NODE_ID] == "pi-1"
    assert cg.HEADER_TRACE_ID in h and h[cg.HEADER_TRACE_ID] == "trace-abc"
    assert cg.HEADER_AUTH in h and h[cg.HEADER_AUTH] == "tok-9"
    assert cg.HEADER_CAPTURE_TS in h and h[cg.HEADER_CAPTURE_TS] == "1609556645"
    assert cg.HEADER_CAPTURE_BATCH in h and h[cg.HEADER_CAPTURE_BATCH] == "tick-9f2a"
    assert cg.HEADER_TRIGGER in h and h[cg.HEADER_TRIGGER] == "motion x3"
    # The wire key set is EXACTLY the generated header constants (+ Content-Type) —
    # no stray hand-typed header leaks onto the contract, none is missing.
    assert set(h) == {
        "Content-Type",
        cg.HEADER_GARDEN_ID, cg.HEADER_DEVICE_ID, cg.HEADER_NODE_ID,
        cg.HEADER_TRACE_ID, cg.HEADER_AUTH, cg.HEADER_CAPTURE_TS, cg.HEADER_CAPTURE_BATCH,
        cg.HEADER_TRIGGER,
    }


def test_client_request_headers_use_generated_header_consts():
    from provision import contract_gen as cg
    from hardware.client import GardenProtectorClient

    c = GardenProtectorClient(
        backend_url="http://unused",
        garden_id="backyard", device_id="pi-01", node_id="node-a",
        garden_token="s3cr3t",
    )
    c._active_cid = "deadbeefdeadbeef"
    h = c._request_headers({"Content-Type": "image/jpeg"})

    assert h[cg.HEADER_TRACE_ID] == "deadbeefdeadbeef"
    assert h[cg.HEADER_GARDEN_ID] == "backyard"
    assert h[cg.HEADER_DEVICE_ID] == "pi-01"
    assert h[cg.HEADER_NODE_ID] == "node-a"
    assert h[cg.HEADER_AUTH] == "s3cr3t"
    # The client's safety-path headers are exactly the generated identity/trace/auth
    # constants (the pusher-only capture-correlation headers are NOT on this path),
    # plus whatever `extra` the caller supplies.
    assert set(h) == {
        "Content-Type",
        cg.HEADER_TRACE_ID, cg.HEADER_GARDEN_ID, cg.HEADER_DEVICE_ID,
        cg.HEADER_NODE_ID, cg.HEADER_AUTH,
    }


@responses.activate
def test_capture_and_push_posts_raw_jpeg_with_identity_headers():
    responses.add(responses.POST, "http://edge/api/evidence",
                  json={"action": "mitigate", "species": "red fox", "confidence": 0.9}, status=200)
    cfg = camera_pusher.PusherConfig(backend="http://edge", garden="home", node_id="pi-7", token="")
    reply = camera_pusher.capture_and_push(cfg, "cam-usb", camera_pusher.make_camera("mock"))
    assert reply["action"] == "mitigate" and reply["species"] == "red fox"

    req = responses.calls[0].request
    assert req.url == "http://edge/api/evidence"
    assert req.method == "POST"
    assert req.headers["X-Device-Id"] == "cam-usb"
    assert req.headers["X-Garden-Id"] == "home"
    assert req.headers["X-Node-Id"] == "pi-7"
    assert req.headers["Content-Type"] == "image/jpeg"
    assert "X-Garden-Auth" not in req.headers  # tokenless garden sends no auth
    assert req.headers["X-Garden-Trace-Id"]    # a trace id is always present
    assert req.body == open(FIXTURE, "rb").read()  # raw bytes, not base64/multipart


@responses.activate
def test_token_is_sent_when_present():
    responses.add(responses.POST, "http://edge/api/evidence", json={"action": "none"}, status=200)
    cfg = camera_pusher.PusherConfig(backend="http://edge", garden="backyard", token="secret-tok")
    camera_pusher.capture_and_push(cfg, "cam-1", camera_pusher.make_camera("mock"))
    assert responses.calls[0].request.headers["X-Garden-Auth"] == "secret-tok"


# --- multi-camera orchestration + isolation --------------------------------

@responses.activate
def test_run_once_pushes_each_camera_as_its_own_device():
    responses.add(responses.POST, "http://edge/api/evidence",
                  json={"action": "none", "species": "raccoon"}, status=200)
    cfg = camera_pusher.PusherConfig(backend="http://edge", garden="home", token="t")
    cams = [("cam-a", camera_pusher.make_camera("mock")),
            ("cam-b", camera_pusher.make_camera("mock:" + FIXTURE))]
    camera_pusher.run(cfg, cams, once=True)

    assert len(responses.calls) == 2
    seen = {c.request.headers["X-Device-Id"] for c in responses.calls}
    assert seen == {"cam-a", "cam-b"}
    for c in responses.calls:
        assert c.request.headers["X-Garden-Id"] == "home"
        assert c.request.headers["X-Garden-Auth"] == "t"


@responses.activate
def test_one_camera_failure_does_not_stop_the_others():
    responses.add(responses.POST, "http://edge/api/evidence", json={"action": "none"}, status=200)
    cfg = camera_pusher.PusherConfig(backend="http://edge", garden="home")
    cams = [("cam-good", camera_pusher.make_camera("mock")),
            ("cam-bad", camera_pusher.make_camera("mock:/no/such/file.jpg"))]
    # Must not raise even though cam-bad's capture throws FileNotFoundError.
    camera_pusher.run(cfg, cams, once=True)
    devs = [c.request.headers["X-Device-Id"] for c in responses.calls]
    assert devs == ["cam-good"]  # the good camera still pushed
