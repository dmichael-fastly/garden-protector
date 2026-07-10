"""Server-side timelapse rendering — Pi admin only.

Selects the archived JPEGs for a date range + camera + type filter and stitches them
into an animated GIF (Pillow) or an MP4 (OpenCV, when available) for download. There is
NO ffmpeg dependency: Pillow is already required (hardware/requirements.txt) and cv2 is
the same gracefully-imported dep the camera code uses (hardware/client.py), so MP4 falls
back to GIF when OpenCV (or an MP4 encoder in this OpenCV build) isn't present.

`select_keys` is PURE given its two fetcher callables, so the frame-selection logic
(filtering, chronological order, day/frame caps) is unit-tested without network/encoders.
"""

import datetime
import io
import os

# Encoders are optional + gracefully imported, exactly like hardware/client.py:28.
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - exercised only on a Pillow-less host
    Image = ImageDraw = ImageFont = None
try:
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover - exercised only on a cv2-less host
    cv2 = None
    np = None

# Per-format frame caps. GIF buffers every frame in memory (Pillow needs them all to
# write a multi-frame GIF), so it is capped tighter; MP4 streams frame-by-frame through
# cv2.VideoWriter, so it can be much longer. Days in a single render are capped too.
GIF_MAX_FRAMES = 240
MP4_MAX_FRAMES = 1800
MAX_DAYS = 31
DEFAULT_FPS = 8
DEFAULT_WIDTH = 640


def event_type(ev):
    """Bucket an archive event the same way GP.archive.type does in the browser:
    sprayed (a deterrent fired) / sighting (something detected) / clear."""
    if ev.get("action") == "mitigate":
        return "sprayed"
    sp = ev.get("species")
    if sp and sp != "none" and not str(sp).startswith("class-"):
        return "sighting"
    return "clear"


def select_keys(days, events_for_day, *, date_from=None, date_to=None, cam="", action="",
                max_days=MAX_DAYS, max_frames=MP4_MAX_FRAMES, day_cap=None):
    """Choose the archive object keys for a timelapse, oldest -> newest.

    PURE given the fetcher callables:
      - ``days``: list of "YYYY-MM-DD" that have photos (any order; /api/archive/days
        returns newest-first).
      - ``events_for_day(date)`` -> list of event dicts (date/time/action/species/device/key).

    Filters by camera + type, orders chronologically, keeps the newest ``max_days`` days
    and the newest ``max_frames`` frames. ``day_cap`` is the per-day fetch limit the caller
    used: any day returning >= that many events was silently truncated by the edge (which
    clamps ?limit to 1000 and reports no total), so the timelapse is PARTIAL for that day.
    Returns ``(keys, meta)`` where meta carries the cap flags + totals for the caller to
    surface.
    """
    in_range = sorted(d for d in days
                      if (not date_from or d >= date_from) and (not date_to or d <= date_to))
    capped_days = len(in_range) > max_days
    use_days = in_range[-max_days:] if capped_days else in_range
    events = []
    capped_partial = False
    for d in use_days:
        day_events = events_for_day(d)
        if day_cap and len(day_events) >= day_cap:
            capped_partial = True            # this day hit the edge's per-day fetch limit
        for ev in day_events:
            if cam and ev.get("device") != cam:
                continue
            if action and event_type(ev) != action:
                continue
            if ev.get("key"):
                events.append(ev)
    events.sort(key=lambda e: (e.get("date", ""), e.get("time", "")))
    capped_frames = len(events) > max_frames
    if capped_frames:
        events = events[-max_frames:]            # keep the newest max_frames
    keys = [e["key"] for e in events]
    # stamps is parallel to keys (same order) so the renderer can burn each frame's
    # capture date+time onto it — a timelapse spans days, so the day matters per frame.
    stamps = [(e.get("date", ""), e.get("time", "")) for e in events]
    return keys, {"days_used": len(use_days), "capped_days": capped_days,
                  "capped_frames": capped_frames, "capped_partial": capped_partial,
                  "total": len(keys), "stamps": stamps}


def stamp_label(date, time, *, to_local=True):
    """Human caption for a frame, e.g. "Jun 21 · 9:05:03 PM".

    Archive stamps are UTC; with ``to_local`` (the default) the label is converted to the
    host's local zone — the Pi renders the export and sits at the garden, so garden-local
    time is what a viewer expects. The browser player labels in the VIEWER's chosen zone,
    so a downloaded file and the live player can differ when the viewer overrode their zone.
    Falls back to the raw "date time" on any parse failure (never raises into a render).
    """
    raw = ((date or "") + (" " + time if time else "")).strip()
    try:
        dt = datetime.datetime.strptime(
            (date or "") + "T" + (time or "00:00:00"), "%Y-%m-%dT%H:%M:%S"
        ).replace(tzinfo=datetime.timezone.utc)
        if to_local:
            dt = dt.astimezone()
        # %-d / %-I aren't portable; strip the leading zeros by hand instead.
        day = str(dt.day)
        clock = dt.strftime("%I:%M:%S %p").lstrip("0")
        return dt.strftime("%b ") + day + " · " + clock
    except (ValueError, TypeError):
        return raw


def _frame_and_label(item):
    """Accept a frame as either raw JPEG bytes or a ``(bytes, label)`` pair, so the
    encoders stamp a per-frame caption when given one and stay backward-compatible with
    the raw-bytes callers (and the existing tests)."""
    if isinstance(item, (bytes, bytearray)):
        return bytes(item), None
    b, label = item                      # (bytes, label) pair
    return bytes(b), label


def _font(size):
    """A bold-ish TrueType font at ``size`` px, falling back to Pillow's bitmap default
    when no TrueType face is installed (so a bare Pi still stamps, just smaller)."""
    if ImageFont is None:
        return None
    for name in ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf", "Arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except (OSError, IOError):
            continue
    try:
        return ImageFont.load_default(size)      # Pillow >= 10.1 takes a size
    except TypeError:                            # pragma: no cover - old Pillow
        return ImageFont.load_default()


def _stamp_image(im, text):
    """Draw ``text`` in the lower-left of a PIL image over a translucent plate (matches
    the player's bottom overlay). Best-effort: any drawing failure leaves the frame as-is."""
    if not text or ImageDraw is None:
        return
    try:
        draw = ImageDraw.Draw(im, "RGBA")
        size = max(11, int(round(im.height * 0.055)))
        font = _font(size)
        pad = max(3, size // 3)
        try:
            box = draw.textbbox((0, 0), text, font=font)
        except (AttributeError, TypeError):       # pragma: no cover - very old Pillow
            w = int(draw.textlength(text, font=font)); box = (0, 0, w, size)
        tw, th = box[2] - box[0], box[3] - box[1]
        top = im.height - th - 2 * pad
        draw.rectangle([0, top - pad, tw + 2 * pad, im.height], fill=(0, 0, 0, 150))
        draw.text((pad - box[0], top - box[1]), text, fill=(255, 255, 255, 255), font=font)
    except Exception:                             # pragma: no cover - never fail a render over a caption
        pass


def _stamp_frame(frame, text):
    """Draw ``text`` in the lower-left of an OpenCV BGR frame over a filled plate."""
    if not text or cv2 is None:
        return
    try:
        h, w = frame.shape[:2]
        scale = max(0.4, h / 720.0)
        thickness = max(1, int(round(scale * 2)))
        font = cv2.FONT_HERSHEY_SIMPLEX
        (tw, th), base = cv2.getTextSize(text, font, scale, thickness)
        pad = max(4, int(round(scale * 8)))
        cv2.rectangle(frame, (0, h - th - base - 2 * pad), (tw + 2 * pad, h), (0, 0, 0), -1)
        cv2.putText(frame, text, (pad, h - base - pad), font, scale,
                    (255, 255, 255), thickness, cv2.LINE_AA)
    except Exception:                             # pragma: no cover - never fail a render over a caption
        pass


def have_mp4():
    """True when an MP4 encoder is usable (OpenCV present)."""
    return cv2 is not None and np is not None


def _even(n):
    n = int(round(n))
    return n - (n % 2)               # H.264 wants even dimensions


def encode_gif(frames_bytes, out_path, *, fps=DEFAULT_FPS, width=DEFAULT_WIDTH, on_frame=None):
    """Stitch an iterable of JPEG byte strings into an animated GIF at ``out_path``."""
    if Image is None:
        raise RuntimeError("Pillow is not installed")
    imgs = []
    for i, item in enumerate(frames_bytes):
        b, label = _frame_and_label(item)
        try:
            im = Image.open(io.BytesIO(b)).convert("RGB")
        except Exception:
            continue                 # skip an unreadable frame rather than failing the whole render
        # width>0 guard: a negative width is truthy and would slip past `im.width > width`,
        # then resize to a negative dimension and raise an opaque Pillow error.
        if width and width > 0 and im.width > width:
            im = im.resize((int(width), max(1, round(im.height * width / im.width))))
        _stamp_image(im, label)      # stamp AFTER resize so the caption size is consistent
        imgs.append(im)
        if len(imgs) > GIF_MAX_FRAMES:
            # Hard backstop: a GIF holds every frame in RAM (~221 MB at the 240-frame /
            # 640px cap), so refuse the instant we exceed it rather than OOM the Pi.
            raise RuntimeError(
                "too many frames for a GIF (%d > %d) — narrow the date range, pick one"
                " camera, or export MP4" % (len(imgs), GIF_MAX_FRAMES))
        if on_frame:
            on_frame(len(imgs))
    if not imgs:
        raise RuntimeError("no frames to encode")
    duration = max(1, int(round(1000.0 / max(1, fps))))
    imgs[0].save(out_path, save_all=True, append_images=imgs[1:], duration=duration,
                 loop=0, optimize=True, disposal=2)
    return out_path


def encode_mp4(frames_bytes, out_path, *, fps=DEFAULT_FPS, width=DEFAULT_WIDTH, on_frame=None):
    """Stitch an iterable of JPEG byte strings into an MP4 at ``out_path`` (H.264 when the
    OpenCV build supports it, else MPEG-4). Streams frame-by-frame (bounded memory)."""
    if not have_mp4():
        raise RuntimeError("OpenCV is not installed")
    writer = None
    size = None
    count = 0
    for item in frames_bytes:
        b, label = _frame_and_label(item)
        frame = cv2.imdecode(np.frombuffer(b, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            continue
        h, w = frame.shape[:2]
        if width and w > width:
            frame = cv2.resize(frame, (_even(width), _even(h * width / w)))
        else:
            frame = cv2.resize(frame, (_even(w), _even(h)))
        _stamp_frame(frame, label)   # stamp AFTER resize so the caption size is consistent
        if writer is None:
            size = (frame.shape[1], frame.shape[0])
            writer = cv2.VideoWriter(out_path, cv2.VideoWriter_fourcc(*"avc1"), float(max(1, fps)), size)
            if not writer.isOpened():     # this OpenCV build lacks H.264 -> MPEG-4 Part 2
                writer = cv2.VideoWriter(out_path, cv2.VideoWriter_fourcc(*"mp4v"), float(max(1, fps)), size)
            if not writer.isOpened():
                raise RuntimeError("no MP4 encoder available in this OpenCV build")
        elif (frame.shape[1], frame.shape[0]) != size:
            frame = cv2.resize(frame, size)
        writer.write(frame)
        count += 1
        if on_frame:
            on_frame(count)
    if writer is not None:
        writer.release()
    if not count:
        raise RuntimeError("no frames to encode")
    return out_path


def encode(frames_bytes, out_path, *, fmt="gif", fps=DEFAULT_FPS, width=DEFAULT_WIDTH, on_frame=None):
    """Dispatch to the GIF or MP4 encoder. ``fmt`` of "mp4" falls back to GIF (with a
    ".gif" path) when OpenCV isn't available, so a Pi without cv2 still exports."""
    if fmt == "mp4" and have_mp4():
        return encode_mp4(frames_bytes, out_path, fps=fps, width=width, on_frame=on_frame)
    if fmt == "mp4":
        out_path = os.path.splitext(out_path)[0] + ".gif"   # graceful fallback
    return encode_gif(frames_bytes, out_path, fps=fps, width=width, on_frame=on_frame)


def cap_for(fmt):
    """The frame cap appropriate for the output format."""
    return GIF_MAX_FRAMES if fmt == "gif" else MP4_MAX_FRAMES
