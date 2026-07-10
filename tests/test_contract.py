"""Spec-driven CONFORMANCE test for the generated cross-language contract.

Replaces the old literal-string parity test (where each language hard-coded the same
expected strings, so a format change in one was silent). This loads the SINGLE source
— contract/spec.toml — plus the generated fixture, and asserts the generated Python
module (provision/contract_gen.py) agrees. The Rust side (contract_gen.rs) is checked
against the same spec by the generated `contract_gen::gen_tests` cargo test, so BOTH
languages are verified against one source. `make gen-check` (in make ci) then
guarantees the checked-in generated files haven't drifted from contract/spec.toml.
"""
import json
import pathlib
import tomllib

from provision import contract_gen as cg
from provision import ids

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = tomllib.loads((ROOT / "contract" / "spec.toml").read_text())
FIXTURE = json.loads((ROOT / "tests" / "contract_fixture.json").read_text())


def test_generated_consts_match_spec():
    assert cg.DEFAULT_GARDEN == SPEC["identity"]["default_garden"]
    assert cg.ID_MIN_LEN == SPEC["identity"]["id_min_len"]
    assert cg.ID_MAX_LEN == SPEC["identity"]["id_max_len"]
    assert cg.TOKEN_SLOT_CURRENT == SPEC["token"]["slot_current"]
    assert cg.TOKEN_SLOT_PREVIOUS == SPEC["token"]["slot_previous"]
    assert cg.HEADER_GARDEN_ID == SPEC["headers"]["garden_id"]
    assert cg.HEADER_DEVICE_ID == SPEC["headers"]["device_id"]
    assert cg.HEADER_NODE_ID == SPEC["headers"]["node_id"]
    assert cg.HEADER_TRACE_ID == SPEC["headers"]["trace_id"]
    assert cg.HEADER_AUTH == SPEC["headers"]["auth"]
    assert cg.HEADER_CAPTURE_TS == SPEC["headers"]["capture_ts"]
    assert cg.HEADER_CAPTURE_BATCH == SPEC["headers"]["capture_batch"]
    assert cg.HEADER_TRIGGER == SPEC["headers"]["trigger"]
    assert cg.NODE_OFFLINE_AFTER_SECS == SPEC["thresholds"]["node_offline_after_secs"]
    assert cg.RAIN_TELEMETRY_FRESH_SECS == SPEC["thresholds"]["rain_telemetry_fresh_secs"]
    assert cg.MITIGATE_THRESHOLD == SPEC["thresholds"]["mitigate_threshold"]
    assert cg.SECONDS_PER_DAY == SPEC["archive"]["seconds_per_day"]
    assert list(cg.CONTROL_COMMANDS) == SPEC["control"]["commands"]


def test_generated_fns_match_fixture():
    exp = FIXTURE["expected"]
    for arg, want in exp["key"].items():
        gid, name = arg.split("|")
        assert cg.key(gid, name) == want, arg
    for arg, want in exp["dev_key"].items():
        gid, did, name = arg.split("|")
        assert cg.dev_key(gid, did, name) == want, arg
    for arg, want in exp["token_secret_name"].items():
        gid, slot = arg.split("|")
        assert cg.token_secret_name(gid, slot) == want, arg
    for s, want in exp["is_valid_id"].items():
        assert cg.is_valid_id(s) is want, repr(s)


def test_generated_ui_consts_match_spec():
    assert cg.ASSET_VERSION == SPEC["ui"]["asset_version"]
    spec_nav = [(n["id"], n["href"], n["label"], bool(n["viewer_ok"]), n.get("group", ""))
                for n in SPEC["ui"]["nav"]]
    assert [tuple(x) for x in cg.NAV] == spec_nav


def test_render_nav_matches_fixture():
    # The Python render_nav must produce the same bytes the fixture/cargo tests pin, so
    # the edge (Rust) and Pi (Python) nav can never diverge.
    for arg, want in FIXTURE["expected"]["render_nav"].items():
        active, view_only = arg.split("|")
        assert cg.render_nav(active, view_only == "true") == want, arg


def test_render_nav_omits_admin_links_when_view_only():
    # The security boundary: admin destinations are OMITTED (not just hidden) for viewers.
    viewer = cg.render_nav("nav-dashboard", True)
    for admin in ("nav-devices", "nav-settings", "nav-costs", "nav-logs", "nav-storage"):
        assert admin not in viewer, admin
    for ok in ("nav-dashboard", "nav-history", "nav-timelapse"):
        assert ok in viewer, ok
    # The admin tier (view_only=False) shows every link, active marked.
    admin_nav = cg.render_nav("nav-costs", False)
    assert 'id="nav-costs" class="active"' in admin_nav
    assert "nav-storage" in admin_nav


def test_ids_reexports_agree_with_generated():
    # The back-compat shim (provision.ids) re-exports the generated values verbatim.
    assert ids.DEFAULT_GARDEN == cg.DEFAULT_GARDEN
    assert ids.is_valid_id is cg.is_valid_id
    assert ids.token_secret_name is cg.token_secret_name
    assert ids.TOKEN_SLOT_CURRENT == cg.TOKEN_SLOT_CURRENT
    assert ids.TOKEN_SLOT_PREVIOUS == cg.TOKEN_SLOT_PREVIOUS


def test_token_secret_name_is_slash_free():
    # Fastly Secret-Store names forbid "/", so the encoding must stay slash-free.
    assert "/" not in cg.token_secret_name("g1", cg.TOKEN_SLOT_CURRENT)
    assert cg.token_secret_name("backyard", cg.TOKEN_SLOT_CURRENT) == "g.backyard.token_current"
