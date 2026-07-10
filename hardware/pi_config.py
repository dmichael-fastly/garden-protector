#!/usr/bin/env python3
"""hardware/pi_config.py — the Pi's local config + secret stores for the wizard.

Two files under ``configs/`` (the whole dir is gitignored — see .gitignore):

  * ``configs/pi-garden.json`` — the DETAILED, non-secret local topology: pi/network
    facts, garden details (name/address/tz/notes), node_id, the deployed Fastly
    coordinates, and the per-device list WITH transport detail (which /dev/video,
    mDNS host:port, GPIO pin…). Written PROGRESSIVELY (atomic temp-file + os.replace)
    as each wizard step completes, so a refresh resumes mid-setup.

  * ``configs/secrets.json`` — 0600, owner-only: the Fastly API token and the admin
    passcode hash. NEVER logged, NEVER sent to the browser, NEVER written into
    pi-garden.json.

This is the Pi side of the two-tier split (RFC decision #4): the EDGE only ever sees
the coarse {device_id,node_id,kind,type,name,status} + garden name/tz/location;
transport detail lives ONLY here.
"""
import json
import os
import pathlib
import re
import sys
import tempfile
import time

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_CONFIGS_DIR = _REPO_ROOT / "configs"

# Ensure the repo root is importable so `provision.auth` (the shared LAN-admin auth
# library) resolves whether pi_config is imported by the portal/tests or run as a
# script (`python3 hardware/pi_config.py`). See CHARTER: one Python service library.
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
from provision import auth  # noqa: E402

CONFIG_NAME = "pi-garden.json"
SECRETS_NAME = "secrets.json"
SCHEMA_VERSION = 1

# Capture / upload cost knobs — shared by the portal, camera_daemon and camera_pusher
# so they all agree on how often and how big the cloud photos are. These are the two
# levers that actually move the Fastly Object Storage bill: every stored object is
# billed a 30-day minimum, so the spend is roughly
#     photos_per_day  ×  bytes_per_photo  ×  max(retention_days, 30)
# i.e. uploading less often (interval_s) and smaller (quality preset) are what help.
DEFAULT_INTERVAL_S = 30
DEFAULT_QUALITY = "standard"
QUALITY_PRESETS = {            # token -> (max_width, max_height, jpeg_quality)
    "high": (1280, 720, 88),
    "standard": (640, 480, 80),
    "saver": (480, 360, 60),
}


def resolve_quality(token):
    """Map a quality preset token to ``(max_width, max_height, jpeg_quality)``;
    unknown or blank tokens fall back to the ``standard`` preset."""
    return QUALITY_PRESETS.get((token or "").strip().lower(), QUALITY_PRESETS[DEFAULT_QUALITY])


# Cadence options offered in the UI (seconds) and rough per-photo upload size per
# quality preset (bytes). The byte figures are typical per-camera averages measured
# on real hardware (CSI+USB) — standard ~20-48 KB, saver ~9-19 KB — used ONLY to give
# the Storage page a ballpark monthly-cost estimate, never for anything billed.
UPLOAD_INTERVAL_OPTIONS = [15, 30, 60, 300, 900]
APPROX_UPLOAD_BYTES = {"high": 120_000, "standard": 35_000, "saver": 14_000}

# Daylight-only capture: don't fill the cloud with dark, empty night frames. When on,
# the routine cadence photo is skipped once the scene goes dark; in "motion" night mode
# the cameras instead upload only when something MOVES (after-dark "evidence"), in
# "pause" mode they go quiet until daylight. "Dark" = the camera frame's mean luminance
# (0-255) drops below ``dark_below``. NOTE: an ordinary camera can't see in true darkness
# — night motion only helps where there's some ambient/porch light.
DEFAULT_DAYLIGHT_ONLY = True
DEFAULT_NIGHT_MODE = "motion"          # "motion" = capture on movement | "pause" = nothing
NIGHT_MODES = ("motion", "pause")
DEFAULT_DARK_BELOW = 45                 # mean-luminance (0-255) below which it's "night"

# Per-camera MOTION TRIGGER config (Pi-local; keyed by device id under "motion" in
# pi-garden.json). When ``enabled``, the camera daemon watches that camera's live feed and
# only ESCALATES a frame to the edge (as a real alarm) when it sees sustained movement —
# the per-sample frames are never uploaded. This is what lets a camera be a useful alarm
# trigger without flooding (vs. "every routine photo is an alarm"). Tuning:
#   cadence_s       how often to sample frames for motion (smaller = snappier, more CPU)
#   confirm_frames  consecutive motion samples required before firing (debounces glitches)
#   sensitivity     0.0-1.0; higher fires on smaller movement (maps to the area threshold)
#   cooldown_s      refractory seconds after a fire (anti-storm)
#   roi             normalized {x,y,w,h} "monitor zone" to watch, or None for the whole frame
MOTION_CADENCE_OPTIONS = (1, 2, 5)
DEFAULT_MOTION = {
    "enabled": False,
    "cadence_s": 1,
    "confirm_frames": 3,
    "sensitivity": 0.5,
    "cooldown_s": 30,
    "roi": None,
}


def normalize_roi(roi):
    """Validate a normalized region-of-interest rectangle. Returns ``{x,y,w,h}`` with each
    value in 0.0-1.0 (w,h > 0; w,h clamped so the rect stays inside the frame), or None for
    "whole frame" when the input is missing/invalid. Normalized (not pixels) so it survives
    a resolution change."""
    if not isinstance(roi, dict):
        return None
    try:
        x, y = float(roi.get("x")), float(roi.get("y"))
        w, h = float(roi.get("w")), float(roi.get("h"))
    except (TypeError, ValueError):
        return None
    x = min(max(x, 0.0), 1.0)
    y = min(max(y, 0.0), 1.0)
    w = min(max(w, 0.0), 1.0 - x)
    h = min(max(h, 0.0), 1.0 - y)
    if w <= 0.0 or h <= 0.0:
        return None
    return {"x": round(x, 4), "y": round(y, 4), "w": round(w, 4), "h": round(h, 4)}


def normalize_motion(raw):
    """Validate a per-camera motion-trigger config dict, filling safe defaults (see
    DEFAULT_MOTION). Pure — used by both the read path and the portal setter."""
    m = raw if isinstance(raw, dict) else {}
    en, cad, cf = m.get("enabled"), m.get("cadence_s"), m.get("confirm_frames")
    sens, cd = m.get("sensitivity"), m.get("cooldown_s")
    return {
        "enabled": en if isinstance(en, bool) else DEFAULT_MOTION["enabled"],
        "cadence_s": (int(cad) if isinstance(cad, (int, float)) and not isinstance(cad, bool)
                      and int(cad) in MOTION_CADENCE_OPTIONS else DEFAULT_MOTION["cadence_s"]),
        "confirm_frames": (int(cf) if isinstance(cf, (int, float)) and not isinstance(cf, bool)
                           and 1 <= cf <= 10 else DEFAULT_MOTION["confirm_frames"]),
        "sensitivity": (round(float(sens), 3) if isinstance(sens, (int, float))
                        and not isinstance(sens, bool) and 0.0 <= sens <= 1.0
                        else DEFAULT_MOTION["sensitivity"]),
        "cooldown_s": (int(cd) if isinstance(cd, (int, float)) and not isinstance(cd, bool)
                       and cd >= 1 else DEFAULT_MOTION["cooldown_s"]),
        "roi": normalize_roi(m.get("roi")),
    }


# ---------------------------------------------------------------------------
# Pure helpers (atomic write, deep-merge, slug). The scrypt passcode KDF +
# {algo,salt,hash} record live in the shared `provision.auth` module; PiConfig's
# passcode methods below delegate to it.
# ---------------------------------------------------------------------------

def _atomic_write(path, text, *, mode):
    """Write ``text`` to ``path`` atomically (temp file in the same dir + os.replace)
    and chmod it to ``mode``. A reader never sees a torn file."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _deep_merge(base, patch):
    """Recursively merge ``patch`` into ``base`` (dicts merge; everything else,
    including lists, is replaced)."""
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def slugify_garden_id(name):
    """Turn a garden display name into a registry-safe id (charset [a-z0-9-], 1-64),
    matching provision/ids.is_valid_id."""
    s = re.sub(r"[^a-z0-9-]+", "-", (name or "").strip().lower())
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:64] or "garden"


# ---------------------------------------------------------------------------
# PiConfig — the stateful store object the portal holds.
# ---------------------------------------------------------------------------

class PiConfig:
    """Read/write access to pi-garden.json (progressive, non-secret) and
    secrets.json (0600). All methods read through to disk so a separate process
    (e.g. gp-provision) writing the registry is reflected on the next call."""

    def __init__(self, configs_dir=None, *, clock=time.time):
        self.dir = pathlib.Path(configs_dir or DEFAULT_CONFIGS_DIR)
        self.config_path = self.dir / CONFIG_NAME
        self.secrets_path = self.dir / SECRETS_NAME
        self._clock = clock

    # -- non-secret config --------------------------------------------------
    @staticmethod
    def _skeleton():
        return {"v": SCHEMA_VERSION, "provisioned": False, "step": "detect",
                "pi": {}, "network": {}, "garden": {}, "node_id": "pi-01",
                "fastly": {}, "devices": [], "created_ts": 0, "updated_ts": 0}

    def load(self):
        """Current pi-garden.json, or a fresh skeleton if absent/corrupt."""
        try:
            data = json.loads(self.config_path.read_text())
            return data if isinstance(data, dict) else self._skeleton()
        except (OSError, ValueError):
            return self._skeleton()

    def save_partial(self, patch):
        """Deep-merge ``patch`` into pi-garden.json and atomically rewrite it.
        Stamps created_ts (first write) + updated_ts. Returns the merged config."""
        cfg = self.load()
        _deep_merge(cfg, patch)
        now = int(self._clock())
        if not cfg.get("created_ts"):
            cfg["created_ts"] = now
        cfg["updated_ts"] = now
        cfg.setdefault("v", SCHEMA_VERSION)
        _atomic_write(self.config_path, json.dumps(cfg, indent=2), mode=0o644)
        return cfg

    def capture_settings(self):
        """Per-garden cloud-upload knobs (non-secret) with safe defaults: how often to
        upload a photo (``interval_s``, seconds) and the photo-quality preset token.
        Stored under the ``capture`` key in pi-garden.json so they survive restarts.
        These are the Fastly-storage cost levers (fewer + smaller photos = less billed);
        the daemon/pusher resolve the quality token via :func:`resolve_quality`."""
        c = self.load().get("capture") or {}
        iv, q = c.get("interval_s"), c.get("quality")
        nm, db, dl = c.get("night_mode"), c.get("dark_below"), c.get("daylight_only")
        return {
            "interval_s": iv if isinstance(iv, int) and iv >= 1 else DEFAULT_INTERVAL_S,
            "quality": q if q in QUALITY_PRESETS else DEFAULT_QUALITY,
            "daylight_only": dl if isinstance(dl, bool) else DEFAULT_DAYLIGHT_ONLY,
            "night_mode": nm if nm in NIGHT_MODES else DEFAULT_NIGHT_MODE,
            "dark_below": int(db) if isinstance(db, (int, float)) and 0 <= db <= 255 else DEFAULT_DARK_BELOW,
        }

    def motion_settings(self, device_id):
        """Per-camera motion-trigger config (Pi-local) with safe defaults, read fresh each call
        so the daemon picks up portal edits without a restart. Stored under
        ``motion.<device_id>`` in pi-garden.json; motion is OFF by default (a camera only
        becomes a motion trigger when an admin enables it). See :data:`DEFAULT_MOTION`."""
        m = (self.load().get("motion") or {}).get(device_id) or {}
        return normalize_motion(m)

    def is_provisioned(self):
        return self.load().get("provisioned") is True

    def step(self):
        return self.load().get("step")

    # -- secrets (0600) -----------------------------------------------------
    def _load_secrets(self):
        try:
            data = json.loads(self.secrets_path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get_secret(self, key):
        """Raw secret value (str for tokens, dict for the passcode record), or None.
        Server-side only — never serialize the returned value to the browser."""
        return self._load_secrets().get(key)

    def set_secret(self, key, value):
        """Set one secret and atomically rewrite secrets.json at 0600."""
        s = self._load_secrets()
        s[key] = value
        _atomic_write(self.secrets_path, json.dumps(s), mode=0o600)

    # -- admin passcode (hashed in secrets.json) ----------------------------
    def passcode_record(self):
        rec = self.get_secret("admin_passcode_hash")
        return rec if isinstance(rec, dict) else None

    def has_passcode(self):
        return self.passcode_record() is not None

    def set_passcode(self, passcode):
        self.set_secret("admin_passcode_hash", auth.make_auth_record(passcode))

    def verify_passcode(self, passcode):
        rec = self.passcode_record()
        return auth.verify_passcode(passcode, rec) if rec else False

    # -- env rendering ------------------------------------------------------
    def to_env(self):
        """Render the GP_* env the portal/gateway/client read, from pi-garden.json
        (+ the garden token from secrets.json). pi-garden.json stays the source of
        truth; this keeps existing .env-based code working. Empty until provisioned."""
        cfg = self.load()
        g = cfg.get("garden") or {}
        fa = cfg.get("fastly") or {}
        env = {}
        if g.get("garden_id"):
            env["GP_GARDEN_ID"] = g["garden_id"]
        if cfg.get("node_id"):
            env["GP_NODE_ID"] = cfg["node_id"]
        if fa.get("backend_url"):
            env["GP_BACKEND"] = fa["backend_url"]
        tok = self.get_secret("garden_token")
        if isinstance(tok, str) and tok:
            env["GP_GARDEN_TOKEN"] = tok
        # The Pi gateway is device #1; surface its id if recorded.
        prim = next((d for d in (cfg.get("devices") or []) if d.get("enabled")), None)
        if prim and prim.get("device_id"):
            env["GP_DEVICE_ID"] = prim["device_id"]
        return env


if __name__ == "__main__":
    pc = PiConfig()
    print(json.dumps({"provisioned": pc.is_provisioned(), "step": pc.step(),
                      "has_passcode": pc.has_passcode(), "env_keys": sorted(pc.to_env())},
                     indent=2))
