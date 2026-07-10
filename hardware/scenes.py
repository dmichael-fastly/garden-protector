#!/usr/bin/env python3
"""hardware/scenes.py — synthetic garden "scenes" for the simulator + a local
deterministic classifier the fake edge uses.

The stock MobileNet V2 on the edge has no `raccoon` synset and won't recognize a
hand-drawn animal, so a simulation that wants to exercise the **spray** path can't
rely on the real model. Instead each scene is rendered with a distinctive
*dominant colour*; the local fake edge (tests/fake_edge.py) averages the image and
maps it back to a verdict. This keeps three things true at once:

  * the **real** edge stays honest (a synthetic scene classifies as `none` there —
    exactly what the validation runbook documents),
  * the **fake** edge is fully deterministic for tests + offline demos, and
  * the dashboard shows visibly different snapshots per event (nice for the demo).

The scene -> verdict table mirrors the edge's decision semantics:
presumption-of-critter with a **human veto** (the one case that must fail safe).

Requires Pillow (already a sim dependency). Pure data + a tiny renderer.
"""
from io import BytesIO

# Anchor colour (RGB) + the verdict the fake edge returns for each scene. The
# `action`/`species`/`confidence` shape matches the real /api/evidence response.
SCENES = {
    "raccoon": {
        "rgb": (70, 70, 78),
        "verdict": {"action": "mitigate", "species": "raccoon", "confidence": 0.93},
    },
    "fox": {
        "rgb": (200, 110, 40),
        "verdict": {"action": "mitigate", "species": "red fox", "confidence": 0.88},
    },
    "cat": {
        "rgb": (150, 120, 92),
        "verdict": {"action": "mitigate", "species": "tabby cat", "confidence": 0.71},
    },
    # A person in frame is the safety-critical veto: never spray a human.
    "human": {
        "rgb": (40, 90, 180),
        "verdict": {"action": "none", "species": "person", "confidence": 0.96, "reason": "human"},
    },
    # Confidently empty / foliage-only ("wind") scene -> stand down.
    "empty": {
        "rgb": (45, 135, 60),
        "verdict": {"action": "none", "species": "empty", "confidence": 0.50, "reason": "empty"},
    },
}

SCENE_NAMES = list(SCENES)


def verdict_for_scene(scene):
    """The canonical /api/evidence verdict for a named scene."""
    return dict(SCENES[scene]["verdict"])


def render_scene(scene, *, width=224, height=224, seed=0):
    """Render a JPEG for ``scene``: a dominant-colour wash + a few shapes so the
    snapshot reads as a real-ish frame while keeping the mean colour near the
    anchor (so `classify_jpeg` is stable across JPEG quantisation)."""
    from PIL import Image, ImageDraw  # local import: only the sim needs Pillow

    base = SCENES[scene]["rgb"]
    img = Image.new("RGB", (width, height), base)
    draw = ImageDraw.Draw(img)

    # Deterministic pseudo-noise so frames differ run-to-run without numpy/random
    # state leaking between tests.
    def jitter(v, amt, salt):
        return max(0, min(255, v + ((seed * 73 + salt * 31) % (2 * amt + 1)) - amt))

    # A vignette of slightly varied blocks (foliage texture).
    step = 32
    for y in range(0, height, step):
        for x in range(0, width, step):
            c = tuple(jitter(base[i], 14, x + y + i) for i in range(3))
            draw.rectangle([x, y, x + step - 2, y + step - 2], fill=c)

    # A subject blob centre-frame, tinted toward the anchor so the mean holds.
    cx, cy = width // 2, height // 2
    draw.ellipse([cx - 40, cy - 30, cx + 40, cy + 40],
                 fill=tuple(min(255, c + 20) for c in base))
    if scene in ("raccoon", "fox", "cat"):
        # two "eyes" — pure flair
        draw.ellipse([cx - 22, cy - 12, cx - 8, cy + 2], fill=(245, 245, 220))
        draw.ellipse([cx + 8, cy - 12, cx + 22, cy + 2], fill=(245, 245, 220))

    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def classify_jpeg(jpeg_bytes):
    """Deterministically map a JPEG back to a scene verdict by nearest anchor
    colour. Used by the fake edge so the gateway/sim loop is fully testable
    offline. Returns a verdict dict (defaults to the `empty` stand-down on any
    decode failure — fail-safe)."""
    try:
        from PIL import Image
        img = Image.open(BytesIO(jpeg_bytes)).convert("RGB")
        # Collapsing to a single pixel yields the mean colour without the
        # deprecated getdata() iteration.
        mean = img.resize((1, 1)).getpixel((0, 0))
    except Exception:
        return verdict_for_scene("empty")

    best, best_d = "empty", float("inf")
    for name, spec in SCENES.items():
        a = spec["rgb"]
        d = sum((mean[i] - a[i]) ** 2 for i in range(3))
        if d < best_d:
            best, best_d = name, d
    return verdict_for_scene(best)
