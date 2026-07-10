"""Unit tests for hardware/pi_config.py — the Pi's local config + secret stores.

Asserts the invariants the wizard relies on: progressive atomic writes, secrets
file is 0600 and the Fastly token NEVER lands in pi-garden.json, scrypt passcode
hashing (plaintext never stored), and to_env() rendering.
"""
import os
import stat

from hardware import pi_config as pc
from provision import auth


def _cfg(tmp_path):
    return pc.PiConfig(tmp_path)


def test_skeleton_when_absent(tmp_path):
    c = _cfg(tmp_path)
    cfg = c.load()
    assert cfg["v"] == pc.SCHEMA_VERSION
    assert cfg["provisioned"] is False
    assert c.is_provisioned() is False
    assert c.step() == "detect"


def test_save_partial_is_progressive_and_atomic(tmp_path):
    c = _cfg(tmp_path)
    c.save_partial({"garden": {"name": "Backyard"}, "step": "garden-details"})
    c.save_partial({"garden": {"address": "123 Main"}})   # accumulates, doesn't clobber
    cfg = c.load()
    assert cfg["garden"] == {"name": "Backyard", "address": "123 Main"}
    assert cfg["step"] == "garden-details"
    assert cfg["created_ts"] > 0 and cfg["updated_ts"] >= cfg["created_ts"]
    # atomic write leaves no torn temp files behind
    assert [p.name for p in tmp_path.iterdir()] == ["pi-garden.json"]


def test_provisioned_flag(tmp_path):
    c = _cfg(tmp_path)
    assert c.is_provisioned() is False
    c.save_partial({"provisioned": True})
    assert c.is_provisioned() is True


def test_secrets_file_is_0600_and_token_never_in_config(tmp_path):
    c = _cfg(tmp_path)
    c.save_partial({"garden": {"name": "Backyard"}})
    c.set_secret("fastly_api_token", "SECRET-TOKEN-xyz")
    assert stat.S_IMODE(os.stat(c.secrets_path).st_mode) == 0o600
    assert c.get_secret("fastly_api_token") == "SECRET-TOKEN-xyz"
    # the token must NEVER land in the non-secret config file
    assert "SECRET-TOKEN-xyz" not in c.config_path.read_text()


def test_passcode_set_verify_and_hash_only(tmp_path):
    c = _cfg(tmp_path)
    assert c.has_passcode() is False
    c.set_passcode("pass-the-tomatoes")
    assert c.has_passcode() is True
    assert c.verify_passcode("pass-the-tomatoes") is True
    assert c.verify_passcode("wrong") is False
    assert "pass-the-tomatoes" not in c.secrets_path.read_text()  # plaintext never stored
    assert stat.S_IMODE(os.stat(c.secrets_path).st_mode) == 0o600
    rec = c.passcode_record()
    assert rec["algo"] == "scrypt" and rec["salt"] and rec["hash"]


def test_passcode_kdf_works_at_production_cost(tmp_path, monkeypatch):
    """conftest lowers _SCRYPT_N for speed; pin the real production cost here once
    so the memory-hard parameters (n=2**14, maxmem) stay exercised end-to-end — a
    regression that hardcoded a weak n or an insufficient maxmem would be caught.
    The KDF lives in provision.auth (Phase 1), so pin the cost there."""
    monkeypatch.setattr(auth, "_SCRYPT_N", 2 ** 14)
    c = _cfg(tmp_path)
    c.set_passcode("pass-the-tomatoes")
    assert c.verify_passcode("pass-the-tomatoes") is True
    assert c.verify_passcode("wrong") is False


def test_to_env_renders_gp_vars(tmp_path):
    c = _cfg(tmp_path)
    c.save_partial({
        "garden": {"garden_id": "backyard", "name": "Backyard"},
        "node_id": "pi-01",
        "fastly": {"backend_url": "https://svc.edgecompute.app"},
        "devices": [{"device_id": "pi-gw", "enabled": True}],
    })
    c.set_secret("garden_token", "tok-123")
    env = c.to_env()
    assert env["GP_GARDEN_ID"] == "backyard"
    assert env["GP_NODE_ID"] == "pi-01"
    assert env["GP_BACKEND"] == "https://svc.edgecompute.app"
    assert env["GP_GARDEN_TOKEN"] == "tok-123"
    assert env["GP_DEVICE_ID"] == "pi-gw"


def test_to_env_leaks_nothing_until_provisioned(tmp_path):
    # Only the harmless default node id; no garden/backend/token before setup.
    env = _cfg(tmp_path).to_env()
    assert env == {"GP_NODE_ID": "pi-01"}
    assert "GP_GARDEN_ID" not in env and "GP_BACKEND" not in env and "GP_GARDEN_TOKEN" not in env


def test_slugify_garden_id():
    assert pc.slugify_garden_id("Back Yard!") == "back-yard"
    assert pc.slugify_garden_id("  The Front Garden  ") == "the-front-garden"
    assert pc.slugify_garden_id("") == "garden"
    assert pc.slugify_garden_id("a" * 100) == "a" * 64


# --- per-camera motion-trigger config ---------------------------------------

def test_normalize_motion_defaults_and_clamps():
    d = pc.normalize_motion({})
    assert d == pc.DEFAULT_MOTION
    assert d["enabled"] is False and d["roi"] is None
    good = pc.normalize_motion({"enabled": True, "cadence_s": 5, "confirm_frames": 4,
                                "sensitivity": 0.7, "cooldown_s": 20})
    assert good == {"enabled": True, "cadence_s": 5, "confirm_frames": 4,
                    "sensitivity": 0.7, "cooldown_s": 20, "roi": None}
    # Out-of-range / wrong-set values fall back to defaults (cadence 3 ∉ {1,2,5}; frames > 10).
    bad = pc.normalize_motion({"cadence_s": 3, "confirm_frames": 99, "sensitivity": 2.0,
                               "cooldown_s": 0, "enabled": "yes"})
    assert bad["cadence_s"] == 1 and bad["confirm_frames"] == 3
    assert bad["sensitivity"] == 0.5 and bad["cooldown_s"] == 30 and bad["enabled"] is False
    # bool is not accepted as an int for the numeric fields (True must not become cadence 1).
    assert pc.normalize_motion({"cadence_s": True})["cadence_s"] == 1


def test_normalize_roi_clamps_and_rejects():
    assert pc.normalize_roi(None) is None
    assert pc.normalize_roi({"x": "a"}) is None
    assert pc.normalize_roi({"x": 0.1, "y": 0.1, "w": 0, "h": 0.5}) is None      # zero area
    r = pc.normalize_roi({"x": 0.1, "y": 0.2, "w": 0.5, "h": 0.5})
    assert r == {"x": 0.1, "y": 0.2, "w": 0.5, "h": 0.5}
    # w is clamped so the rect can't spill past the frame edge (x=0.8 -> w<=0.2).
    assert pc.normalize_roi({"x": 0.8, "y": 0.0, "w": 0.5, "h": 0.5})["w"] == 0.2


def test_motion_settings_reads_per_device_and_survives_restart(tmp_path):
    c = _cfg(tmp_path)
    assert c.motion_settings("cam-a") == pc.DEFAULT_MOTION          # unset -> defaults (off)
    c.save_partial({"motion": {"cam-a": pc.normalize_motion(
        {"enabled": True, "cadence_s": 2, "roi": {"x": 0, "y": 0, "w": 0.5, "h": 0.5}})}})
    again = _cfg(tmp_path).motion_settings("cam-a")                 # fresh instance = re-read disk
    assert again["enabled"] is True and again["cadence_s"] == 2 and again["roi"]["w"] == 0.5
    assert _cfg(tmp_path).motion_settings("cam-b") == pc.DEFAULT_MOTION   # other cameras unaffected
