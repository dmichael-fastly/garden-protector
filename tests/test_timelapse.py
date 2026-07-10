"""Unit tests for hardware/timelapse.py — the frame-selection logic (pure) and the
GIF encoder (Pillow, skipped if absent). The encoders are otherwise exercised end-to-end
via the portal flow in test_portal.py (with a faked encoder)."""

import io
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from hardware import timelapse as tl  # noqa: E402


def _ev(date, time, dev, action="none", species="none", key=None):
    return {"date": date, "time": time, "device": dev, "action": action,
            "species": species, "key": key or f"{date}/{time}/{dev}"}


DAYS = ["2026-06-23", "2026-06-22", "2026-06-21"]   # newest-first, like /api/archive/days
DATA = {
    "2026-06-23": [_ev("2026-06-23", "10:00:00", "cam-a"),
                   _ev("2026-06-23", "09:00:00", "cam-b", action="mitigate", species="raccoon")],
    "2026-06-22": [_ev("2026-06-22", "08:00:00", "cam-a", species="deer")],
    "2026-06-21": [_ev("2026-06-21", "07:00:00", "cam-a")],
}


def _fetch(d):
    return DATA.get(d, [])


def test_event_type_buckets():
    assert tl.event_type({"action": "mitigate", "species": "raccoon"}) == "sprayed"
    assert tl.event_type({"action": "none", "species": "deer"}) == "sighting"
    assert tl.event_type({"action": "none", "species": "none"}) == "clear"
    assert tl.event_type({"action": "none", "species": "class-1"}) == "clear"


def test_select_keys_is_chronological():
    keys, meta = tl.select_keys(DAYS, _fetch)
    assert keys == [
        "2026-06-21/07:00:00/cam-a",
        "2026-06-22/08:00:00/cam-a",
        "2026-06-23/09:00:00/cam-b",
        "2026-06-23/10:00:00/cam-a",
    ]
    assert meta["total"] == 4 and not meta["capped_days"] and not meta["capped_frames"]
    assert meta["capped_partial"] is False          # nothing truncated by default


def test_select_keys_filters_camera_and_type():
    cam, _ = tl.select_keys(DAYS, _fetch, cam="cam-a")
    assert all("cam-a" in k for k in cam) and len(cam) == 3
    sprayed, _ = tl.select_keys(DAYS, _fetch, action="sprayed")
    assert sprayed == ["2026-06-23/09:00:00/cam-b"]
    sighting, _ = tl.select_keys(DAYS, _fetch, action="sighting")
    assert sighting == ["2026-06-22/08:00:00/cam-a"]


def test_select_keys_date_range():
    keys, _ = tl.select_keys(DAYS, _fetch, date_from="2026-06-22", date_to="2026-06-23")
    assert keys[0].startswith("2026-06-22") and len(keys) == 3
    assert all(k >= "2026-06-22" for k in keys)


def test_select_keys_caps_days_keeping_newest():
    keys, meta = tl.select_keys(DAYS, _fetch, max_days=1)
    assert meta["capped_days"] is True and meta["days_used"] == 1
    assert all(k.startswith("2026-06-23") for k in keys)


def test_select_keys_caps_frames_keeping_newest():
    keys, meta = tl.select_keys(DAYS, _fetch, max_frames=2)
    assert meta["capped_frames"] is True and len(keys) == 2
    # The newest two frames are kept (chronological tail).
    assert keys == ["2026-06-23/09:00:00/cam-b", "2026-06-23/10:00:00/cam-a"]


def test_select_keys_flags_partial_when_a_day_hits_the_fetch_cap():
    # A day whose fetcher returns >= day_cap events was silently truncated by the edge,
    # so the render must be flagged partial; below the cap it must not be.
    busy = {"2026-06-23": [_ev("2026-06-23", "00:00:%02d" % i, "cam-a") for i in range(5)]}
    keys, meta = tl.select_keys(["2026-06-23"], lambda d: busy.get(d, []), day_cap=5)
    assert meta["capped_partial"] is True and len(keys) == 5
    keys, meta = tl.select_keys(["2026-06-23"], lambda d: busy.get(d, []), day_cap=6)
    assert meta["capped_partial"] is False
    # No day_cap given => never flagged (caller didn't pass a limit to detect).
    _, meta = tl.select_keys(["2026-06-23"], lambda d: busy.get(d, []))
    assert meta["capped_partial"] is False


def test_cap_for_format():
    assert tl.cap_for("gif") == tl.GIF_MAX_FRAMES
    assert tl.cap_for("mp4") == tl.MP4_MAX_FRAMES


def test_select_keys_returns_stamps_parallel_to_keys():
    keys, meta = tl.select_keys(DAYS, _fetch)
    # one (date, time) per key, same chronological order — the renderer burns these on.
    assert meta["stamps"] == [
        ("2026-06-21", "07:00:00"),
        ("2026-06-22", "08:00:00"),
        ("2026-06-23", "09:00:00"),
        ("2026-06-23", "10:00:00"),
    ]
    assert len(meta["stamps"]) == len(keys)


def test_stamp_label_formats_utc_and_falls_back():
    # to_local=False keeps it deterministic regardless of the host zone.
    assert tl.stamp_label("2026-06-21", "09:05:03", to_local=False) == "Jun 21 · 9:05:03 AM"
    assert tl.stamp_label("2026-06-21", "13:00:00", to_local=False) == "Jun 21 · 1:00:00 PM"
    # unparseable -> raw passthrough, never raises
    assert tl.stamp_label("not-a-date", "x") == "not-a-date x"
    assert tl.stamp_label("", "") == ""


def test_encode_gif_accepts_labeled_frames(tmp_path):
    Image = pytest.importorskip("PIL.Image", reason="Pillow not installed")
    frames = []
    for i, color in enumerate(((200, 30, 30), (30, 200, 30), (30, 30, 200))):
        buf = io.BytesIO()
        Image.new("RGB", (64, 48), color).save(buf, format="JPEG")
        frames.append((buf.getvalue(), "Jun 2%d · 9:00:00 AM" % i))   # (bytes, label) pairs
    out = tmp_path / "tl.gif"
    path = tl.encode_gif(iter(frames), str(out), fps=4, width=48)
    assert path == str(out) and out.exists() and out.stat().st_size > 0
    with Image.open(str(out)) as im:
        assert getattr(im, "is_animated", False) and im.n_frames == 3


def test_encode_gif_with_pillow(tmp_path):
    Image = pytest.importorskip("PIL.Image", reason="Pillow not installed")
    # Build a few tiny JPEG frames in-memory and stitch them into a GIF.
    frames = []
    for color in ((200, 30, 30), (30, 200, 30), (30, 30, 200)):
        buf = io.BytesIO()
        Image.new("RGB", (64, 48), color).save(buf, format="JPEG")
        frames.append(buf.getvalue())
    seen = []
    out = tmp_path / "tl.gif"
    path = tl.encode_gif(iter(frames), str(out), fps=4, width=48, on_frame=seen.append)
    assert path == str(out) and out.exists() and out.stat().st_size > 0
    assert seen == [1, 2, 3]                       # progress callback fired per frame
    with Image.open(str(out)) as im:
        assert getattr(im, "is_animated", False) and im.n_frames == 3
        assert im.size[0] == 48                     # downscaled to the requested width


def test_encode_gif_rejects_more_than_the_frame_cap(tmp_path):
    Image = pytest.importorskip("PIL.Image", reason="Pillow not installed")
    buf = io.BytesIO(); Image.new("RGB", (8, 6), (1, 1, 1)).save(buf, format="JPEG")
    one = buf.getvalue()
    frames = (one for _ in range(tl.GIF_MAX_FRAMES + 1))   # one over the in-RAM cap
    with pytest.raises(RuntimeError, match="too many frames"):
        tl.encode_gif(frames, str(tmp_path / "big.gif"), fps=4, width=8)


def test_encode_dispatch_falls_back_to_gif_without_cv2(tmp_path, monkeypatch):
    pytest.importorskip("PIL.Image", reason="Pillow not installed")
    monkeypatch.setattr(tl, "have_mp4", lambda: False)
    from PIL import Image
    buf = io.BytesIO(); Image.new("RGB", (32, 24), (10, 10, 10)).save(buf, format="JPEG")
    out = tl.encode([buf.getvalue()], str(tmp_path / "tl.mp4"), fmt="mp4", fps=2, width=32)
    assert out.endswith(".gif") and os.path.exists(out)    # graceful fallback to GIF
