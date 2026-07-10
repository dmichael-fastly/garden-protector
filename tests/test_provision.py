"""Unit tests for the gp-provision control plane.

Pure logic (charset, taxonomy, registry blob building, token-name parity, the
default-tokenless guard) is tested offline; anything that would hit the Fastly
API is patched at the import site or driven through FASTLY_MOCK_MODE.
"""

import base64
import json
from unittest.mock import patch

import pytest

from provision import fastly_api, registry, taxonomy
from provision.ids import DEFAULT_GARDEN, is_valid_id


# --- id charset (parity with the Rust edge `is_valid_id`) -----------------


def test_is_valid_id_matches_rust_contract():
    assert is_valid_id("default")
    assert is_valid_id("cam-front-01")
    assert is_valid_id("a")
    assert is_valid_id("a" * 64)
    assert not is_valid_id("")
    assert not is_valid_id("a" * 65)
    assert not is_valid_id("Cam1")  # uppercase
    assert not is_valid_id("cam_1")  # underscore not in id charset
    assert not is_valid_id("../armed")  # slash (cross-tenant guard)
    assert not is_valid_id("g/1")
    assert not is_valid_id("a b")
    assert not is_valid_id("café")
    assert not is_valid_id("g1\n")  # fullmatch rejects trailing newline


# token_secret_name parity is now enforced by the spec-driven CONFORMANCE test in
# tests/test_contract.py (which loads contract/spec.toml + the generated fixture and
# checks the Python module against it; the Rust side is checked by the generated
# contract_gen::gen_tests cargo test). This replaced the old literal-string test that
# each language copied separately — a format change in one used to be silent.


# --- device taxonomy (cameras AND non-camera sensors) ---------------------


def test_taxonomy_known_observer_types_incl_motion_and_heat():
    assert taxonomy.validate_device_type("observer", "camera_usb") is True
    assert taxonomy.validate_device_type("observer", "motion_pir") is True
    assert taxonomy.validate_device_type("observer", "heat_thermal") is True
    assert taxonomy.validate_device_type("deterrent", "solenoid") is True


def test_taxonomy_custom_type_accepted_when_charset_valid():
    # "other things that plug into a Pi" — unknown but charset-valid -> accepted
    # (returns False = not a *known* type, but does NOT raise).
    assert taxonomy.validate_device_type("observer", "lidar_tof") is False


def test_taxonomy_rejects_bad_kind_and_bad_type():
    with pytest.raises(ValueError):
        taxonomy.validate_device_type("sensor", "camera_usb")  # bad kind
    with pytest.raises(ValueError):
        taxonomy.validate_device_type("observer", "Camera USB")  # bad charset


# --- registry blob building (pure) ----------------------------------------


def test_seed_default_garden_blob():
    reg = registry.upsert_garden(
        registry.empty_gardens(), DEFAULT_GARDEN, "Home", "America/New_York", ts=10
    )
    assert reg["v"] == 1
    assert [g["garden_id"] for g in reg["gardens"]] == ["default"]
    assert reg["gardens"][0]["created_ts"] == 10


def test_upsert_garden_is_idempotent_and_preserves_created_ts():
    reg = registry.upsert_garden(
        registry.empty_gardens(), "backyard", "Backyard", "UTC", ts=10
    )
    reg2 = registry.upsert_garden(reg, "backyard", "Renamed", "UTC", ts=99)
    assert len(reg2["gardens"]) == 1
    g = reg2["gardens"][0]
    assert g["name"] == "Renamed"
    assert g["created_ts"] == 10  # preserved across update
    assert reg2["updated_ts"] == 99


def test_upsert_garden_rejects_bad_id():
    with pytest.raises(ValueError):
        registry.upsert_garden(registry.empty_gardens(), "../armed", "x", "UTC", ts=1)


# --- garden location (RFC decision #4: the edge keeps coarse location) -------


def test_upsert_garden_with_location():
    reg = registry.upsert_garden(
        registry.empty_gardens(),
        "backyard",
        "Backyard",
        "UTC",
        ts=10,
        location={"address": "1 Main", "lat": 37.7, "lon": -122.4},
    )
    assert reg["gardens"][0]["location"] == {
        "address": "1 Main",
        "lat": 37.7,
        "lon": -122.4,
    }


def test_upsert_garden_omits_location_when_absent():
    reg = registry.upsert_garden(
        registry.empty_gardens(), "backyard", "Backyard", "UTC", ts=10
    )
    assert "location" not in reg["gardens"][0]  # legacy/default gardens stay coarse


def test_upsert_garden_preserves_prior_location_on_update():
    reg = registry.upsert_garden(
        registry.empty_gardens(),
        "backyard",
        "Backyard",
        "UTC",
        ts=10,
        location={"address": "1 Main"},
    )
    # Re-upsert WITHOUT a location (e.g. registering a 2nd device) must NOT drop it.
    reg2 = registry.upsert_garden(reg, "backyard", "Renamed", "UTC", ts=20)
    assert reg2["gardens"][0]["location"] == {"address": "1 Main"}


def test_create_garden_mints_token_and_writes_location():
    ctx = {
        "token": "admintok",
        "garden_state_store_id": "STATE",
        "garden_tokens_store_id": "SECRET",
        "backend_url": "https://edge",
    }
    with (
        patch.object(registry, "read_gardens", return_value=registry.empty_gardens()),
        patch.object(registry, "write_gardens") as wg,
        patch.object(registry, "mint_token", return_value="minted-tok") as mint,
    ):
        res = registry.create_garden(
            ctx,
            gid="backyard",
            name="Backyard",
            tz="America/Los_Angeles",
            ts=5,
            location={"address": "1 Main", "lat": 1.0, "lon": 2.0},
        )
    mint.assert_called_once()
    assert res["token"] == "minted-tok"
    assert res["garden"]["name"] == "Backyard"
    assert res["garden"]["location"] == {"address": "1 Main", "lat": 1.0, "lon": 2.0}
    wg.assert_called_once()


def test_update_garden_edits_entry_without_minting():
    """A post-setup metadata edit updates name/tz/location but NEVER touches the
    per-garden token (a rename must not knock the Pi offline)."""
    existing = registry.upsert_garden(
        registry.empty_gardens(),
        "backyard",
        "Backyard",
        "UTC",
        ts=1,
        location={"address": "1 Main"},
    )
    ctx = {
        "token": "t",
        "garden_state_store_id": "STATE",
        "garden_tokens_store_id": "SECRET",
    }
    with (
        patch.object(registry, "read_gardens", return_value=existing),
        patch.object(registry, "write_gardens") as wg,
        patch.object(registry, "mint_token") as mint,
    ):
        entry = registry.update_garden(
            ctx,
            gid="backyard",
            name="Back Garden",
            tz="America/Chicago",
            ts=9,
            location={"address": "2 Elm", "lat": 1.0, "lon": 2.0},
        )
    mint.assert_not_called()
    wg.assert_called_once()
    assert entry["name"] == "Back Garden" and entry["tz"] == "America/Chicago"
    assert entry["location"] == {"address": "2 Elm", "lat": 1.0, "lon": 2.0}
    assert entry["created_ts"] == 1  # preserved across the update


def test_update_garden_unknown_id_raises():
    ctx = {"token": "t", "garden_state_store_id": "STATE"}
    with (
        patch.object(registry, "read_gardens", return_value=registry.empty_gardens()),
        patch.object(registry, "write_gardens") as wg,
    ):
        with pytest.raises(ValueError):
            registry.update_garden(ctx, gid="ghost", name="Ghost", tz="UTC", ts=1)
    wg.assert_not_called()


def test_create_garden_default_is_tokenless():
    ctx = {
        "token": "t",
        "garden_state_store_id": "STATE",
        "garden_tokens_store_id": "SECRET",
    }
    with (
        patch.object(registry, "read_gardens", return_value=registry.empty_gardens()),
        patch.object(registry, "write_gardens"),
        patch.object(registry, "mint_token") as mint,
    ):
        res = registry.create_garden(
            ctx, gid=DEFAULT_GARDEN, name="Home", tz="UTC", ts=5
        )
    mint.assert_not_called()
    assert res["token"] is None


def test_register_device_returns_coarse_device_only():
    """Two-tier split: the device identity the edge gets is EXACTLY the coarse fields
    — NEVER transport/host/dev/gpio (that stays in pi-garden.json)."""
    ctx = {
        "token": "t",
        "garden_state_store_id": "STATE",
        "garden_tokens_store_id": "SECRET",
        "backend_url": "https://edge",
    }
    with (
        patch.object(registry, "read_gardens", return_value=registry.empty_gardens()),
        patch.object(registry, "write_gardens"),
        patch.object(
            registry, "read_devices", return_value=registry.empty_devices("backyard")
        ),
        patch.object(registry, "write_devices"),
        patch.object(registry, "mint_token", return_value="tok"),
    ):
        res = registry.register_device(
            ctx,
            gid="backyard",
            did="cam-front",
            node_id="pi-01",
            kind="observer",
            dev_type="camera_usb",
            name="Front",
            ts=1,
        )
    assert set(res["device"]) == {"device_id", "node_id", "kind", "type", "name"}


def test_cli_location_from_args():
    from types import SimpleNamespace
    from provision import cli

    assert cli._location_from_args(
        SimpleNamespace(address="1 Main", lat=1.0, lon=2.0)
    ) == {"address": "1 Main", "lat": 1.0, "lon": 2.0}
    assert (
        cli._location_from_args(SimpleNamespace(address=None, lat=None, lon=None))
        is None
    )
    assert cli._location_from_args(SimpleNamespace(address="", lat=3.0, lon=None)) == {
        "lat": 3.0
    }


def test_add_device_uniqueness_and_taxonomy():
    reg = registry.empty_devices("backyard")
    reg = registry.add_device(
        reg,
        device_id="cam-front",
        node_id="pi-01",
        kind="observer",
        dev_type="camera_usb",
        name="Front",
        ts=1,
    )
    reg = registry.add_device(
        reg,
        device_id="pir-1",
        node_id="pi-01",
        kind="observer",
        dev_type="motion_pir",
        name="Motion",
        ts=2,
    )
    assert [d["device_id"] for d in reg["devices"]] == ["cam-front", "pir-1"]
    # `type` key (not `dev_type`) in the serialized blob (matches the edge serde).
    assert reg["devices"][0]["type"] == "camera_usb"
    # Duplicate device_id within the garden is rejected.
    with pytest.raises(ValueError):
        registry.add_device(
            reg,
            device_id="cam-front",
            node_id="pi-01",
            kind="observer",
            dev_type="camera_csi",
            name="dup",
            ts=3,
        )


def test_add_device_alarm_role_defaults():
    # Cameras default to confirm-only (trigger off) so nothing fires alarms until opted in;
    # non-cameras default to off/off. Explicit booleans override.
    reg = registry.empty_devices("backyard")
    reg = registry.add_device(
        reg,
        device_id="cam-1",
        node_id="pi-01",
        kind="observer",
        dev_type="camera_usb",
        name="Cam",
        ts=1,
    )
    reg = registry.add_device(
        reg,
        device_id="pir-1",
        node_id="pi-01",
        kind="observer",
        dev_type="motion_pir",
        name="PIR",
        ts=2,
    )
    cam = reg["devices"][0]
    pir = reg["devices"][1]
    assert cam["can_trigger_alarm"] is False and cam["can_confirm_alarm"] is True
    assert pir["can_trigger_alarm"] is False and pir["can_confirm_alarm"] is False
    # Explicit override (e.g. a radar marked as a trigger).
    reg = registry.add_device(
        reg,
        device_id="radar-1",
        node_id="pi-01",
        kind="observer",
        dev_type="ir_break_beam",
        name="Radar",
        ts=3,
        can_trigger_alarm=True,
        can_confirm_alarm=False,
    )
    assert reg["devices"][2]["can_trigger_alarm"] is True


def test_edit_device_toggles_alarm_roles_and_preserves_when_absent():
    reg = registry.empty_devices("backyard")
    reg = registry.add_device(
        reg,
        device_id="cam-1",
        node_id="pi-01",
        kind="observer",
        dev_type="camera_usb",
        name="Cam",
        ts=1,
    )
    # Turn the camera into a trigger.
    reg = registry.edit_device(reg, device_id="cam-1", can_trigger_alarm=True, ts=2)
    assert reg["devices"][0]["can_trigger_alarm"] is True
    assert reg["devices"][0]["can_confirm_alarm"] is True  # untouched
    # An edit that omits the roles preserves them (only renames here).
    reg = registry.edit_device(reg, device_id="cam-1", name="Renamed", ts=3)
    assert reg["devices"][0]["can_trigger_alarm"] is True
    assert reg["devices"][0]["name"] == "Renamed"


# --- token minting guard --------------------------------------------------


def test_mint_token_refuses_default_garden():
    with pytest.raises(ValueError):
        registry.mint_token("SECRET-STORE", DEFAULT_GARDEN, "tok")


def test_mint_token_writes_base64_secret_under_slash_free_name():
    with (
        patch.object(fastly_api, "secret_put") as sp,
        patch.object(fastly_api, "secret_delete") as sd,
    ):
        val = registry.mint_token(
            "SECRET-STORE", "backyard", "admin-token", value="the-token"
        )
    assert val == "the-token"
    sp.assert_called_once()
    args, _ = sp.call_args
    store_id, name, value, tok = args
    assert store_id == "SECRET-STORE"
    assert name == "g.backyard.token_current"  # slash-free, parity with edge
    # Initial mint clears any stale previous slot (single valid credential).
    sd.assert_called_once()
    assert sd.call_args[0][1] == "g.backyard.token_previous"


def test_secret_put_base64_encodes_value():
    # The Fastly Secret API requires base64; verify fastly() gets it encoded.
    with patch("provision.fastly_api.fastly") as f:
        fastly_api.secret_put(
            "SID", "g.backyard.token_current", "plain-token", "admintok"
        )
    args, kwargs = f.call_args
    method, path = args[0], args[1]
    body = args[2]
    assert method == "PUT"
    assert path == "/resources/stores/secret/SID/secrets"
    assert body["name"] == "g.backyard.token_current"
    assert base64.b64decode(body["secret"]).decode() == "plain-token"


# --- register_device end-to-end (registry I/O mocked) ---------------------


def test_register_device_default_garden_is_tokenless():
    ctx = {
        "token": "admintok",
        "garden_state_store_id": "STATE",
        "garden_tokens_store_id": "SECRET",
        "backend_url": "https://edge",
    }
    with (
        patch.object(registry, "read_gardens", return_value=registry.empty_gardens()),
        patch.object(registry, "write_gardens"),
        patch.object(
            registry, "read_devices", return_value=registry.empty_devices("default")
        ),
        patch.object(registry, "write_devices"),
        patch.object(registry, "mint_token") as mint,
    ):
        res = registry.register_device(
            ctx,
            gid="default",
            did="cam-front",
            node_id="pi-01",
            kind="observer",
            dev_type="camera_usb",
            name="Front",
            ts=1,
        )
    assert res["token"] is None  # default garden never tokenized
    mint.assert_not_called()
    assert res["deploy_env"]["GP_GARDEN_TOKEN"] == ""


def test_register_device_nondefault_mints_token():
    ctx = {
        "token": "admintok",
        "garden_state_store_id": "STATE",
        "garden_tokens_store_id": "SECRET",
        "backend_url": "https://edge",
    }
    with (
        patch.object(registry, "read_gardens", return_value=registry.empty_gardens()),
        patch.object(registry, "write_gardens"),
        patch.object(
            registry, "read_devices", return_value=registry.empty_devices("backyard")
        ),
        patch.object(registry, "write_devices"),
        patch.object(registry, "mint_token", return_value="minted-token") as mint,
    ):
        res = registry.register_device(
            ctx,
            gid="backyard",
            did="pir-1",
            node_id="pi-01",
            kind="observer",
            dev_type="motion_pir",
            name="Motion",
            ts=1,
        )
    mint.assert_called_once()
    # First device in a fresh garden: no existing token -> mint a brand-new one.
    assert mint.call_args.kwargs.get("value") is None
    assert res["token"] == "minted-token"
    assert res["deploy_env"]["GP_GARDEN_TOKEN"] == "minted-token"
    assert res["deploy_env"]["GP_GARDEN_ID"] == "backyard"


def test_register_device_reuses_existing_token_no_rotation():
    """Adding a SECOND device must NOT rotate the garden token (the gateway + the
    first camera share it). With ctx['existing_token'] set, register re-PUTs that
    SAME value (idempotent) rather than minting a new one."""
    ctx = {
        "token": "admintok",
        "garden_state_store_id": "STATE",
        "garden_tokens_store_id": "SECRET",
        "backend_url": "https://edge",
        "existing_token": "keep-me-tok",
    }
    with (
        patch.object(
            registry,
            "read_gardens",
            return_value=registry.upsert_garden(
                registry.empty_gardens(), "backyard", "Backyard", "UTC", 1
            ),
        ),
        patch.object(registry, "write_gardens"),
        patch.object(
            registry, "read_devices", return_value=registry.empty_devices("backyard")
        ),
        patch.object(registry, "write_devices"),
        patch.object(registry, "mint_token", return_value="keep-me-tok") as mint,
    ):
        res = registry.register_device(
            ctx,
            gid="backyard",
            did="cam-2",
            node_id="pi-01",
            kind="observer",
            dev_type="camera_usb",
            name="Cam 2",
            ts=2,
        )
    mint.assert_called_once()
    assert mint.call_args.kwargs.get("value") == "keep-me-tok"  # reused, not rotated
    assert res["deploy_env"]["GP_GARDEN_TOKEN"] == "keep-me-tok"


# --- registry mirror is the source of truth (eventual-consistency guard) --


def test_register_device_accumulates_via_local_mirror(tmp_path):
    """Regression: KV is eventually consistent, so a KV read-modify-write loses
    rapid successive registrations. The LOCAL mirror must be authoritative, so
    registering three devices in a row yields all three (NOT just the last).
    KV/secret writes are mocked; the mirror file is real (tmp)."""
    ctx = {
        "service_id": "SVC",
        "token": "admintok",
        "garden_state_store_id": "STATE",
        "garden_tokens_store_id": "SECRET",
        "backend_url": "https://edge",
        "configs_dir": str(tmp_path),
    }
    with (
        patch.object(fastly_api, "kv_put"),
        patch.object(fastly_api, "kv_get", return_value=None),
        patch.object(fastly_api, "secret_put"),
        patch.object(fastly_api, "secret_delete"),
    ):
        for did, typ in [
            ("cam-front", "camera_usb"),
            ("pir-1", "motion_pir"),
            ("heat-1", "heat_thermal"),
        ]:
            registry.register_device(
                ctx,
                gid="backyard",
                did=did,
                node_id="pi-01",
                kind="observer",
                dev_type=typ,
                name=did,
                ts=1,
            )
        devices = registry.read_devices(ctx, "backyard")["devices"]
    assert [d["device_id"] for d in devices] == ["cam-front", "pir-1", "heat-1"], (
        "all three devices must accumulate via the authoritative local mirror"
    )


def test_register_device_hydrates_from_kv_and_does_not_overwrite(tmp_path):
    """Regression (lost-update): on a host with NO local mirror, register_device must
    HYDRATE existing devices from KV before appending, or it would clobber every
    previously-registered device for the garden. Here KV already holds one device;
    registering a second must yield BOTH (not just the new one)."""
    existing_gardens = json.dumps(
        registry.upsert_garden(
            registry.empty_gardens(), "backyard", "Backyard", "UTC", 1
        )
    ).encode()
    existing_devices = json.dumps(
        registry.add_device(
            registry.empty_devices("backyard"),
            device_id="cam-front",
            node_id="pi-01",
            kind="observer",
            dev_type="camera_usb",
            name="Front",
            ts=1,
        )
    ).encode()

    def fake_kv_get(store_id, key, token):
        if key == registry.GARDENS_KEY:
            return existing_gardens
        if key == registry.devices_key("backyard"):
            return existing_devices
        return None

    ctx = {
        "service_id": "SVC",
        "token": "admintok",
        "garden_state_store_id": "STATE",
        "garden_tokens_store_id": "SECRET",
        "backend_url": "https://edge",
        "configs_dir": str(tmp_path),
    }
    with (
        patch.object(fastly_api, "kv_get", side_effect=fake_kv_get),
        patch.object(fastly_api, "kv_put"),
        patch.object(fastly_api, "secret_put"),
        patch.object(fastly_api, "secret_delete"),
    ):
        registry.register_device(
            ctx,
            gid="backyard",
            did="pir-1",
            node_id="pi-01",
            kind="observer",
            dev_type="motion_pir",
            name="Motion",
            ts=2,
        )
        devices = registry.read_devices(ctx, "backyard")["devices"]
    assert [d["device_id"] for d in devices] == ["cam-front", "pir-1"], (
        "must hydrate the existing cam-front from KV, not overwrite it with only pir-1"
    )


# --- teardown auth gate (security regression) -----------------------------


def test_teardown_order_stops_writes_then_empties_kv_and_bucket(monkeypatch):
    """Regression: teardown left non-empty KV stores (Fastly 409) and couldn't delete the
    FOS bucket with the scoped key while the service was still writing. Correct order:
    delete the Compute service (stop writes) -> revoke the scoped FOS key -> empty+delete
    the bucket with a fresh admin key -> delete that admin key -> empty+delete KV stores."""
    from provision import client, fos_setup, orchestrator

    order, calls = [], {"kv_empty": [], "bucket": None}

    def fake_fastly(method, path, *a, **k):
        if method == "DELETE" and path == "/service/SVC":
            order.append("svc")
        if method == "DELETE" and path.startswith("/resources/stores/"):
            order.append("store:" + path.rsplit("/", 1)[1])
        return {}

    calls["config_empty"] = []
    monkeypatch.setattr(client, "fastly", fake_fastly)
    monkeypatch.setattr(fastly_api, "get_active_version", lambda sid, tok: 1)
    monkeypatch.setattr(
        fastly_api, "kv_empty", lambda sid, tok: calls["kv_empty"].append(sid) or 2
    )
    monkeypatch.setattr(
        fastly_api,
        "config_empty",
        lambda sid, tok: calls["config_empty"].append(sid) or 3,
    )
    monkeypatch.setattr(
        fos_setup,
        "ensure_fos_access_key",
        lambda desc, tok, **kw: {"access_key": "AKADMIN", "secret_key": "SK"},
    )
    # Bounded key-propagation poll replaced the fixed sleep — stub it to "ready".
    monkeypatch.setattr(fos_setup, "wait_for_fos_key", lambda *a, **k: True)
    monkeypatch.setattr(
        fos_setup,
        "delete_fos_bucket",
        lambda name, region, ak, sk: (
            order.append("bucket"),
            calls.update(bucket=(name, ak)),
        ),
    )
    monkeypatch.setattr(
        fos_setup,
        "delete_fos_access_key",
        lambda kid, tok: order.append("delkey:" + kid),
    )
    monkeypatch.setattr(orchestrator.time, "sleep", lambda *_: None)

    state = {
        "service_id": "SVC",
        "cdn_service_id": None,
        "garden_state_store_id": "KV1",
        "garden_models_store_id": "KV2",
        "garden_tokens_store_id": "SEC",
        "fos_bucket": "B",
        "fos_region": "us-east-1",
        "garden_config_store_id": "CFG1",
        "fos_config_store_id": "CFG2",
        "fos_access_key_id": "SCOPEDKEY",
        "fos_secret_access_key": "x",
    }
    list(
        orchestrator.perform_teardown(
            state, "tok", {"remove_bucket": True, "remove_stores": True}
        )
    )

    assert calls["kv_empty"] == ["KV1", "KV2"]  # KV emptied before delete
    assert calls["config_empty"] == ["CFG1", "CFG2"]  # config emptied before delete
    assert calls["bucket"] == ("B", "AKADMIN")  # bucket via ADMIN key, not scoped
    # ORDER: service (writes) gone -> scoped key revoked -> bucket emptied/deleted -> admin key removed
    assert (
        order.index("svc")
        < order.index("delkey:SCOPEDKEY")
        < order.index("bucket")
        < order.index("delkey:AKADMIN")
    )
    # EVERY store type deleted after the bucket — KV, secret, AND config (no orphans)
    for store in ("KV1", "KV2", "SEC", "CFG1", "CFG2"):
        assert order.index("bucket") < order.index("store:" + store), (
            f"{store} not torn down"
        )


def test_wait_for_fos_key_polls_until_authenticated(monkeypatch):
    """A freshly-minted FOS key takes seconds to propagate; wait_for_fos_key retries
    on auth-not-ready errors and returns True as soon as head_bucket authenticates
    (replacing the old fixed time.sleep(12) in rollback teardown)."""
    from provision import fos_setup

    class _ClientErr(Exception):
        def __init__(self, code):
            super().__init__(code)
            self.response = {"Error": {"Code": code}}

    calls = {"n": 0}

    class FakeS3:
        def head_bucket(self, Bucket):
            calls["n"] += 1
            if calls["n"] < 3:
                raise _ClientErr("InvalidAccessKeyId")  # not propagated yet
            return {}  # now authenticates

    import time as _t

    monkeypatch.setattr(fos_setup, "_s3_client", lambda *a, **k: FakeS3())
    monkeypatch.setattr(_t, "sleep", lambda *_: None)
    ok = fos_setup.wait_for_fos_key(
        "B", "us-east-1", "AK", "SK", timeout=30, interval=0
    )
    assert ok is True
    assert calls["n"] == 3  # retried twice, then succeeded


def test_wait_for_fos_key_returns_true_on_nosuchbucket(monkeypatch):
    """A real NoSuchBucket/404 still proves the key AUTHENTICATED (bucket just gone),
    so the poll returns True immediately rather than spinning to timeout."""
    from provision import fos_setup

    class _ClientErr(Exception):
        def __init__(self, code):
            super().__init__(code)
            self.response = {"Error": {"Code": code}}

    class FakeS3:
        def head_bucket(self, Bucket):
            raise _ClientErr("NoSuchBucket")

    monkeypatch.setattr(fos_setup, "_s3_client", lambda *a, **k: FakeS3())
    assert (
        fos_setup.wait_for_fos_key("B", "us-east-1", "AK", "SK", timeout=5, interval=0)
        is True
    )


def test_wait_for_fos_key_times_out_when_never_propagates(monkeypatch):
    """If the key never authenticates within the timeout, return False (the caller
    proceeds and lets the real delete surface any failure)."""
    import time as _time
    from provision import fos_setup

    class _ClientErr(Exception):
        def __init__(self, code):
            super().__init__(code)
            self.response = {"Error": {"Code": code}}

    class FakeS3:
        def head_bucket(self, Bucket):
            raise _ClientErr("AccessDenied")

    # Advance a fake monotonic clock past the deadline on the 2nd check.
    clock = {"t": 0.0}
    monkeypatch.setattr(fos_setup, "_s3_client", lambda *a, **k: FakeS3())
    monkeypatch.setattr(_time, "monotonic", lambda: clock["t"])

    def fake_sleep(_):
        clock["t"] += 10.0

    monkeypatch.setattr(_time, "sleep", fake_sleep)
    assert (
        fos_setup.wait_for_fos_key("B", "us-east-1", "AK", "SK", timeout=5, interval=1)
        is False
    )


def test_config_empty_lists_then_deletes_every_item(monkeypatch):
    """config_empty must enumerate items and DELETE each so the store can be removed
    (Fastly 409s on a non-empty config store, same as KV)."""
    deleted = []

    def fake_fastly(method, path, *a, **k):
        if method == "GET" and path == "/resources/stores/config/CFG/items":
            return {
                "data": [
                    {"item_key": "cdn_host"},
                    {"item_key": "bucket"},
                    {"item_key": "region"},
                ]
            }
        if method == "DELETE":
            deleted.append(path)
            return {}
        raise AssertionError(f"unexpected {method} {path}")

    monkeypatch.setattr(fastly_api, "fastly", fake_fastly)
    n = fastly_api.config_empty("CFG", "tok")
    assert n == 3
    assert deleted == [
        "/resources/stores/config/CFG/item/cdn_host",
        "/resources/stores/config/CFG/item/bucket",
        "/resources/stores/config/CFG/item/region",
    ]


@pytest.mark.security_regression
def test_teardown_requires_global_token_no_fallback():
    from provision import orchestrator

    # No token at all -> reject (never falls back to a stored key).
    with pytest.raises(PermissionError):
        orchestrator.require_global_token(None)
    # A non-global token -> reject.
    with patch.object(fastly_api, "token_scope", return_value={"scope": "purge_all"}):
        with pytest.raises(PermissionError):
            orchestrator.require_global_token("weak-token")
    # A global token -> accepted.
    with patch.object(fastly_api, "token_scope", return_value={"scope": "global"}):
        assert orchestrator.require_global_token("good-token") == "good-token"


# --- find-or-create service pagination (CLOUD-001) ------------------------


def test_find_service_by_name_pages_past_the_first_page(monkeypatch):
    """Regression (CLOUD-001): GET /service is paginated. A service that only appears
    on page 2 must be FOUND, or provision() would create a duplicate service+bucket."""
    pages = {
        1: [{"id": f"S{i}", "name": f"svc-{i}"} for i in range(100)],  # full page
        2: [{"id": "TARGET", "name": "the-one"}],  # short page -> last
    }
    seen = []

    def fake_fastly(method, path, *a, **k):
        assert method == "GET" and path.startswith("/service?")
        import urllib.parse as up

        q = up.parse_qs(up.urlsplit(path).query)
        page = int(q["page"][0])
        assert q["per_page"][0] == "100"
        seen.append(page)
        return pages.get(page, [])

    monkeypatch.setattr(fastly_api, "fastly", fake_fastly)
    found = fastly_api.find_service_by_name("the-one", "tok")
    assert found == {"id": "TARGET", "name": "the-one"}
    assert seen == [1, 2]  # walked to page 2


def test_find_service_by_name_stops_on_short_first_page(monkeypatch):
    """A short (<per_page) first page is the last page — no extra requests, returns
    None when absent."""
    calls = []

    def fake_fastly(method, path, *a, **k):
        calls.append(path)
        return [{"id": "S1", "name": "other"}]  # 1 item < per_page=100 -> last page

    monkeypatch.setattr(fastly_api, "fastly", fake_fastly)
    assert fastly_api.find_service_by_name("absent", "tok") is None
    assert len(calls) == 1  # did not page further


def test_provision_reuses_service_found_on_page_two_no_duplicate(monkeypatch, tmp_path):
    """Regression (CLOUD-001) — the BEHAVIORAL invariant, end to end: when the target
    service only appears on PAGE 2 of GET /service, provision() must take the REUSE
    branch and NEVER call create_service. The unpaginated bug would miss it and stand
    up a DUPLICATE Compute service + a fresh FOS bucket + a new scoped key + new stores
    (cost + a second live credential set). `find_service_by_name` is left REAL here so
    the actual pagination drives the find-or-create decision."""
    pages = {
        1: [
            {"id": f"S{i}", "name": f"other-{i}"} for i in range(100)
        ],  # full page, no match
        2: [{"id": "REUSED-SVC", "name": "svc"}],  # target on page 2
    }

    def fake_fastly(method, path, *a, **k):
        assert method == "GET" and path.startswith("/service?")
        import urllib.parse as up

        page = int(up.parse_qs(up.urlsplit(path).query)["page"][0])
        return pages.get(page, [])

    # The transport under find_service_by_name is REAL pagination; everything else stubbed.
    monkeypatch.setattr(fastly_api, "fastly", fake_fastly)
    rec = _run_provision_recording(
        monkeypatch,
        tmp_path,
        cdn_url="https://svc-cdn.global.ssl.fastly.net",
        find_service=fastly_api.find_service_by_name,  # REAL find-or-create lookup
        service_name="svc",
    )
    # The duplicate-create path was NOT taken — the page-2 service was reused.
    assert rec["created"] == [], (
        "create_service was called -> duplicate service provisioned"
    )


# --- KV transport fidelity (CLOUD-003) ------------------------------------


def test_kv_get_returns_byte_exact_unparsed_value(monkeypatch):
    """Regression (CLOUD-003): kv_get must return the value BYTE-EXACT (no json.loads
    -> re-json.dumps round-trip that reorders keys / corrupts binary). garden_models
    holds raw ONNX bytes."""
    raw_bytes = b"\x00\x01ONNX\xff\xfe not-json"

    def fake_raw_get(path, *, token, **k):
        assert path == "/resources/stores/kv/MODELS/keys/mobilenet_v2.onnx"
        return raw_bytes

    monkeypatch.setattr(fastly_api, "fastly_raw_get", fake_raw_get)
    out = fastly_api.kv_get("MODELS", "mobilenet_v2.onnx", "tok")
    assert out == raw_bytes  # byte-identical, not re-serialized


def test_kv_get_returns_none_on_404(monkeypatch):
    monkeypatch.setattr(
        fastly_api,
        "fastly_raw_get",
        lambda path, *, token, **k: (_ for _ in ()).throw(
            RuntimeError("HTTP 404 GET ...")
        ),
    )
    assert fastly_api.kv_get("S", "absent", "tok") is None


def test_kv_get_preserves_json_key_order(monkeypatch):
    """A JSON registry blob round-trips byte-for-byte — keys not reordered, whitespace
    not reformatted (the old fastly() path re-dumped and could reorder)."""
    blob = b'{"z":1,"a":2,"m":[3,2,1]}'
    monkeypatch.setattr(fastly_api, "fastly_raw_get", lambda path, *, token, **k: blob)
    assert fastly_api.kv_get("S", "index/gardens", "tok") == blob


# --- display-name sanitization (CLOUD-007) --------------------------------


def test_sanitize_display_name_strips_control_chars_and_bounds_length():
    from provision import cli

    s = cli._sanitize_display_name
    # CR/LF/NUL and other control bytes are dropped (header-injection defense)
    assert s("Back\r\nyard\x00") == "Backyard"
    assert "\n" not in s("a\nb") and "\r" not in s("a\rb")
    # C1 control range dropped too
    assert s("x\x85y") == "xy"
    # tabs normalize to a space, surrounding whitespace trimmed
    assert s("  hi\tthere  ") == "hi there"
    # length-bounded
    assert len(s("z" * 500)) == 80
    # ordinary unicode (accents, emoji) is preserved — only control chars are stripped
    assert s("Café 🌱") == "Café 🌱"
    assert s("") == ""


# --- CDN read-signing VCL (Step-3 evidence-archive read-back regression) ---


def test_cdn_vcl_read_signing_security_invariants():
    """Guards the evidence-archive read-back security model (each invariant maps to
    a live bug the first end-to-end deploy surfaced):

      - the public `/img/<key>` prefix is stripped to the bucket-relative object key
        the edge writes (else the SigV4 GET resolves `/<bucket>/img/<key>` -> 404);
      - `bereq.http.host` is set to the FOS bucket-region host before signing (else
        the SigV4 host mismatches the origin AND the shielded fetch loops back into
        this service -> 503 "Same machine same service");
      - the edge<->shield auth handoff uses the unspoofable `X-Edge-CDN-Auth` marker,
        NOT the client-spoofable `Fastly-FF`, and
        any client-supplied marker is stripped before the gate trusts it;
      - the query allow-list is RESTRICTIVE — no S3 LIST params — so the one shared
        read secret can't enumerate other gardens' keys (cross-tenant leak);
      - the rate-limiter's ratecounter/penaltybox are declared (undeclared -> the
        whole CDN service fails to compile/activate);
      - load_vcl substitutes both placeholder secrets (no literal leftover).
    """
    import re
    from provision.vcl import load_vcl

    cdn_secret = "unit-test-cdn-secret"
    vcl = load_vcl(cdn_secret)

    # /img/ vanity prefix is normalized to the real object key before signing
    assert 'regsub(req.url, "^/img/", "/")' in vcl
    # Host is set on the ORIGIN via the fos_origin backend's native override_host,
    # NOT in VCL (the VCL never mutates bereq.http.host). The SigV4 canonical request
    # signs the computed var.fosHost value — which must equal override_host — because
    # override_host is applied after miss_pass, so bereq.http.host is still the CDN
    # domain at signing time (signing it -> SignatureDoesNotMatch).
    assert "set bereq.http.host =" not in vcl  # the VCL never mutates the Host header
    assert '"host:" var.fosHost' in vcl
    assert 'var.fosHost = var.fosRegion ".object.fastlystorage.app"' in vcl
    # edge<->shield handoff uses the baked secret marker, never spoofable Fastly-FF
    assert "X-Edge-CDN-Auth" in vcl
    assert "req.http.Fastly-FF" not in vcl
    assert "unset req.http.X-Edge-CDN-Auth" in vcl
    # restrictive query allow-list: single-object params only, no LIST enumeration
    m = re.search(r'querystring\.filter_except\(req\.url,\s*"([^"]*)"\)', vcl)
    assert m, "filter_except allow-list not found"
    allowed = m.group(1)
    assert (
        "list-type" not in allowed
        and "prefix" not in allowed
        and "delimiter" not in allowed
    )
    assert "versionId" in allowed  # single-object GET params are still allowed
    # Edge Rate Limiting objects are declared (undeclared -> activation hard-fails)
    assert "ratecounter auth_fail_rc" in vcl and "penaltybox auth_fail_pb" in vcl
    # CONSTANT-LENGTH read-gate compare: both the presented key AND the expected
    # secret are SHA-256 hashed before the `!=`, so the byte compare is over a fixed
    # 64-hex-char digest (no prefix/length timing oracle). The penaltybox stays.
    assert 'digest.hash_sha256(subfield(req.url.qs, "key", "&"))' in vcl
    assert "digest.hash_sha256(req.http.x-fastly-key)" in vcl
    assert 'digest.hash_sha256(table.lookup(cdn_auth, "secret"' in vcl
    # the RAW secret is never compared directly anymore (would short-circuit byte-wise)
    assert 'subfield(req.url.qs, "key", "&") != table.lookup(cdn_auth' not in vcl
    # both placeholders substituted; the gate fallback resolves to the real secret
    assert "__CDN_SECRET__" not in vcl and "__SHIELD_SECRET__" not in vcl
    assert cdn_secret in vcl
    # Immutable archive objects: FOS sets no Cache-Control, so the CDN adds a long one
    # (30 days + immutable) and caches at the Fastly edge for the same duration.
    assert 'set beresp.http.Cache-Control = "public, max-age=2592000, immutable"' in vcl
    assert "set beresp.ttl = 2592000s" in vcl


# --- CDN-first archive read wiring (SSRF guard + provisioning) -------------


def test_safe_cdn_host_allows_only_https_fastly_hosts():
    f = fastly_api.safe_cdn_host
    # default provisioned CDN URL -> bare host
    assert f("https://svc-cdn.global.ssl.fastly.net") == "svc-cdn.global.ssl.fastly.net"
    assert f("https://x.fastlystorage.app") == "x.fastlystorage.app"
    # case-insensitive host
    assert f("https://SVC-CDN.GLOBAL.SSL.FASTLY.NET") == "svc-cdn.global.ssl.fastly.net"
    # rejected: http (no TLS), non-Fastly host, bare IP / localhost / link-local
    # (cloud-metadata SSRF), and empty.
    assert f("http://svc-cdn.global.ssl.fastly.net") is None
    assert f("https://evil.example.com") is None
    assert f("https://169.254.169.254") is None
    assert f("https://localhost") is None
    assert f("https://fastly.net.evil.com") is None  # suffix must be a real boundary
    assert f("") is None
    assert f("not a url") is None
    # CLOUD-004: the allowlist is now SHAPE-specific, not a bare `.fastly.net` suffix.
    # A bare `*.fastly.net` (e.g. an attacker-registered map hostname on shared Fastly
    # space) MUST be rejected — only the generated `-cdn.global.ssl.fastly.net` shape,
    # `.fastlystorage.app`, and `.edgecompute.app` pass.
    assert f("https://evil.fastly.net") is None
    assert f("https://evil.global.ssl.fastly.net") is None  # no -cdn. infix
    assert f("https://svc.edgecompute.app") == "svc.edgecompute.app"
    assert f("https://attacker-cdn.global.ssl.fastly.net.evil.com") is None


def _run_provision_recording(
    monkeypatch,
    tmp_path,
    *,
    cdn_url,
    skip_archive=False,
    find_service=None,
    service_name="svc",
    ensure_cdn_service=None,
    ensure_fos_access_key=None,
    saved_config=None,
):
    """Drive orchestrator.provision with every external Fastly/FOS call stubbed,
    recording the config items, secrets, and backends written. `safe_cdn_host` is
    left REAL so the test exercises the actual SSRF-guarded wiring decision.

    `find_service` overrides the find-or-create lookup (default: always None ->
    create branch). Pass a real `find_service_by_name` (e.g. paging a multi-page
    GET /service) to exercise the REUSE branch and assert no duplicate is created;
    `rec["created"]` records any create_service call so the test can assert it never
    happened.

    `ensure_cdn_service` / `ensure_fos_access_key` override the default stubs — pass
    the REAL `fastly_api.ensure_cdn_service` (with `find_service` wired) to assert the
    reprovision does NOT stand up a duplicate CDN service, or a reuse-shaped FOS key
    stub (no `secret_key`) to assert the orchestrator never writes an empty secret.
    `saved_config` captures the final `save_config(out)` payload into the given dict
    (under key 'out') so a test can assert the persisted config."""
    from provision import orchestrator, fos_setup, registry

    rec = {
        "config": [],
        "secrets": [],
        "backends": [],
        "created": [],
        "cdn_created": [],
    }

    pkg = tmp_path / "pkg.tar.gz"
    pkg.write_bytes(b"x")
    cfg = {
        "token": "tok",
        "service_name": service_name,
        "region": "us-east-1",
        "bucket": "gp-svc-images",
        "cdn_url": cdn_url,
        "cdn_secret": "the-cdn-secret",
        "package_path": str(pkg),
        "model_path": str(tmp_path / "absent.onnx"),
        "skip_archive": skip_archive,
    }

    stubs = {
        "find_service_by_name": find_service or (lambda name, tok: None),
        "create_service": lambda name, t, tok: (
            rec["created"].append(name) or {"id": "SVC"}
        ),
        "latest_version": lambda sid, tok: 1,
        "get_active_version": lambda sid, tok: 1,
        "clone_version": lambda sid, v, tok: v + 1,
        "add_domain": lambda *a, **k: None,
        "ensure_kv_store": lambda name, tok: "KV",
        "ensure_secret_store": lambda name, tok: "SEC",
        "ensure_config_store": lambda name, tok: "CFG",
        "link_resource": lambda *a, **k: None,
        "kv_put": lambda *a, **k: None,
        "deploy_package": lambda *a, **k: None,
        "validate_version": lambda *a, **k: None,
        "activate_version": lambda *a, **k: None,
        "ensure_cdn_service": ensure_cdn_service
        or (lambda *a, **k: {"cdn_service_id": "CDN", "cdn_url": cdn_url}),
        "config_item": lambda store, key, val, tok: rec["config"].append((key, val)),
        "secret_put": lambda store, name, val, tok: rec["secrets"].append((name, val)),
        "add_backend": lambda sid, ver, payload, tok: rec["backends"].append(
            payload["name"]
        ),
    }
    for name, fn in stubs.items():
        monkeypatch.setattr(fastly_api, name, fn)
    monkeypatch.setattr(orchestrator, "preflight", lambda tok: None)
    monkeypatch.setattr(orchestrator, "save_state", lambda st: None)
    if saved_config is not None:
        monkeypatch.setattr(
            orchestrator, "save_config", lambda out: saved_config.update(out=out)
        )
    else:
        monkeypatch.setattr(orchestrator, "save_config", lambda out: None)
    monkeypatch.setattr(orchestrator, "clear_state", lambda: None)
    monkeypatch.setattr(
        fos_setup,
        "ensure_fos_access_key",
        ensure_fos_access_key
        or (lambda desc, tok, **kw: {"access_key": "AK", "secret_key": "SK"}),
    )
    monkeypatch.setattr(fos_setup, "ensure_fos_bucket", lambda *a, **k: None)
    monkeypatch.setattr(fos_setup, "delete_fos_access_key", lambda *a, **k: None)
    monkeypatch.setattr(
        fos_setup,
        "region_endpoint",
        lambda region: f"{region}.object.fastlystorage.app",
    )
    monkeypatch.setattr(registry, "seed_registry", lambda *a, **k: None)
    # Track CDN service creates separately so a reprovision regression test can assert
    # the REAL ensure_cdn_service never stood up a second VCL delivery service.
    real_create = fastly_api.create_service

    def _tracking_create(name, t, tok):
        if t == "vcl":
            rec["cdn_created"].append(name)
        return real_create(name, t, tok)

    monkeypatch.setattr(fastly_api, "create_service", _tracking_create)

    orchestrator.provision(cfg, ts=1)
    return rec


def test_provision_wires_cdn_read_path(monkeypatch, tmp_path):
    """A normal (archive-enabled) provision points the edge at the CDN: cdn_host in
    fos_config, the read secret in the Secret Store, and a cdn_read backend — alongside
    the existing direct-FOS write wiring."""
    rec = _run_provision_recording(
        monkeypatch, tmp_path, cdn_url="https://svc-cdn.global.ssl.fastly.net"
    )
    assert ("cdn_host", "svc-cdn.global.ssl.fastly.net") in rec["config"]
    assert ("fos.cdn_secret", "the-cdn-secret") in rec["secrets"]
    assert "cdn_read" in rec["backends"]
    assert "fos_archive" in rec["backends"]  # direct-FOS path still wired (fallback)
    assert ("fos.secret_key", "SK") in rec["secrets"]


def test_provision_skip_archive_wires_nothing(monkeypatch, tmp_path):
    rec = _run_provision_recording(
        monkeypatch,
        tmp_path,
        cdn_url="https://svc-cdn.global.ssl.fastly.net",
        skip_archive=True,
    )
    assert rec["backends"] == []
    assert all(k != "cdn_host" for k, _ in rec["config"])
    assert all(n != "fos.cdn_secret" for n, _ in rec["secrets"])


def test_provision_rejects_non_fastly_cdn_url_but_keeps_fos(monkeypatch, tmp_path):
    """An SSRF-failing cdn_url skips ONLY the CDN read wiring; the direct-FOS write
    path stays intact so the archive still works (uncached)."""
    rec = _run_provision_recording(
        monkeypatch, tmp_path, cdn_url="http://169.254.169.254"
    )
    assert "cdn_read" not in rec["backends"]
    assert "fos_archive" in rec["backends"]
    assert all(k != "cdn_host" for k, _ in rec["config"])
    assert all(n != "fos.cdn_secret" for n, _ in rec["secrets"])


# --- reprovision is idempotent (CLOUD HIGH-severity audit) ----------------


def test_reprovision_reuses_cdn_service_no_duplicate(monkeypatch, tmp_path):
    """Regression (CLOUD HIGH-1): a reprovision must NOT stand up a SECOND CDN
    read-signing VCL service. `ensure_cdn_service` was an UNCONDITIONAL create, so a
    same-named CDN delivery service accumulated every run (billable, each a live FOS
    read gate) and teardown only ever recorded the LAST one. Here the CDN service
    already exists by name and the cdn_secret is recorded, so the REAL ensure_cdn_service
    must take the find-or-create REUSE branch and never call create_service for a vcl
    type."""
    existing = {
        "svc": {"id": "SVC", "name": "svc"},  # reused Compute service
        "Garden Protector CDN SVC": {
            "id": "EXISTING-CDN",
            "name": "Garden Protector CDN SVC",
        },
    }

    def fake_find_service(name, tok):
        return existing.get(name)

    rec = _run_provision_recording(
        monkeypatch,
        tmp_path,
        cdn_url="https://svc-cdn.global.ssl.fastly.net",
        find_service=fake_find_service,
        ensure_cdn_service=fastly_api.ensure_cdn_service,  # REAL find-or-create
    )
    # No SECOND CDN VCL service was created — the existing one was reused.
    assert rec["cdn_created"] == [], (
        "ensure_cdn_service created a duplicate CDN VCL service"
    )
    # The reused Compute service wasn't re-created either.
    assert rec["created"] == []


def test_reprovision_reuses_cdn_service_records_existing_id(monkeypatch, tmp_path):
    """The REUSE branch of ensure_cdn_service returns the EXISTING service id (not a
    fresh one) using the recorded cdn_url, so teardown/state stay pointed at the live
    read gate."""
    with (
        patch.object(
            fastly_api,
            "find_service_by_name",
            return_value={"id": "EXISTING-CDN", "name": "Garden Protector CDN SVC"},
        ),
        patch.object(fastly_api, "create_service") as create,
    ):
        out = fastly_api.ensure_cdn_service(
            {
                "service_id": "SVC",
                "fos_region": "us-east-1",
                "fos_bucket": "b",
                "cdn_url": "https://svc-cdn.global.ssl.fastly.net",
                "cdn_secret": "shh",
            },
            "AK",
            "SK",
            "tok",
        )
    create.assert_not_called()
    assert out == {
        "cdn_service_id": "EXISTING-CDN",
        "cdn_url": "https://svc-cdn.global.ssl.fastly.net",
    }


def test_ensure_cdn_service_refuses_reuse_without_recorded_secret(monkeypatch):
    """A same-named CDN service whose write-only read-gate secret we DON'T hold cannot
    be safely reused (the secret is unrecoverable) — ensure_cdn_service must raise rather
    than silently collide or create a broken duplicate."""
    with (
        patch.object(
            fastly_api,
            "find_service_by_name",
            return_value={"id": "EXISTING-CDN", "name": "Garden Protector CDN SVC"},
        ),
        patch.object(fastly_api, "create_service") as create,
    ):
        with pytest.raises(RuntimeError, match="cdn_secret"):
            fastly_api.ensure_cdn_service(
                {
                    "service_id": "SVC",
                    "fos_region": "us-east-1",
                    "fos_bucket": "b",
                    "cdn_url": "https://svc-cdn.global.ssl.fastly.net",
                    "cdn_secret": "",
                },
                "AK",
                "SK",
                "tok",
            )
    create.assert_not_called()


def test_reprovision_reused_fos_key_does_not_write_empty_secret(monkeypatch, tmp_path):
    """Regression (CLOUD HIGH-2): ensure_fos_access_key is find-or-create by DESCRIPTION,
    and the FOS LIST/GET access-key endpoint never echoes the secret_key. On a reprovision
    the scoped key comes back with NO secret_key. The orchestrator must NOT pass that
    empty secret onward — doing so wrote an empty `fos.secret_key` into the Secret Store
    (breaking archive SigV4 signing) or crashed on `.encode()` of None. With the key
    reused, the existing Secret Store value is still valid, so fos.secret_key must be left
    untouched, and the CDN service must be REUSED (never re-wired with an empty secret)."""

    def reuse_shaped_fos_key(desc, tok, **kw):
        # The admin temp key still carries a secret (it's freshly minted each run); only
        # the persistent scoped object key takes the GET branch with no secret_key.
        if "admin-temp" in desc:
            return {"access_key": "ADMINAK", "secret_key": "ADMINSK"}
        return {"access_key": "AK-EXISTING"}  # reused -> NO secret_key

    existing = {
        "svc": {"id": "SVC", "name": "svc"},
        "Garden Protector CDN SVC": {
            "id": "EXISTING-CDN",
            "name": "Garden Protector CDN SVC",
        },
    }
    saved = {}
    rec = _run_provision_recording(
        monkeypatch,
        tmp_path,
        cdn_url="https://svc-cdn.global.ssl.fastly.net",
        find_service=lambda name, tok: existing.get(name),
        ensure_cdn_service=fastly_api.ensure_cdn_service,  # REAL — must take REUSE branch
        ensure_fos_access_key=reuse_shaped_fos_key,
        saved_config=saved,
    )
    # The empty/None secret was NEVER written to the Secret Store.
    assert all(name != "fos.secret_key" for name, _ in rec["secrets"]), (
        "an empty fos.secret_key was written on reused key"
    )
    assert all(val for _, val in rec["secrets"]), (
        "a None/empty secret value was written"
    )
    # The CDN read gate's shared secret (from cfg, not the FOS key) is still wired.
    assert ("fos.cdn_secret", "the-cdn-secret") in rec["secrets"]
    # No duplicate CDN VCL service was created — the existing one was reused.
    assert rec["cdn_created"] == []
    # The persisted config still carries the existing CDN service id.
    assert saved["out"]["cdn_service_id"] == "EXISTING-CDN"


def test_reprovision_reused_fos_key_uses_recorded_cdn_when_present(
    monkeypatch, tmp_path
):
    """On a reused FOS key (no secret), the orchestrator must NOT call ensure_cdn_service
    with an empty secret. If a CDN service id is already recorded in the stub config it
    reuses that directly (no find/create at all)."""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "SVC.json").write_text(
        json.dumps(
            {
                "service_id": "SVC",
                "cdn_service_id": "RECORDED-CDN",
                "fos_secret_access_key": "PRIOR-SECRET",
            }
        )
    )
    monkeypatch.setenv("GP_CONFIGS_DIR", str(cfg_dir))
    # Re-import config dir resolution by patching the module constant directly.
    from provision import orchestrator

    monkeypatch.setattr(orchestrator, "CONFIGS_DIR", cfg_dir)

    cdn_calls = []

    def boom_cdn(*a, **k):
        cdn_calls.append(a)
        raise AssertionError(
            "ensure_cdn_service must NOT be called with a reused (secret-less) key"
        )

    def reuse_shaped_fos_key(desc, tok, **kw):
        if "admin-temp" in desc:
            return {"access_key": "ADMINAK", "secret_key": "ADMINSK"}
        return {"access_key": "AK-EXISTING"}

    saved = {}
    rec = _run_provision_recording(
        monkeypatch,
        tmp_path,
        cdn_url="https://svc-cdn.global.ssl.fastly.net",
        find_service=lambda name, tok: (
            {"id": "SVC", "name": "svc"} if name == "svc" else None
        ),
        ensure_cdn_service=boom_cdn,
        ensure_fos_access_key=reuse_shaped_fos_key,
        saved_config=saved,
    )
    assert cdn_calls == []  # ensure_cdn_service untouched
    assert saved["out"]["cdn_service_id"] == "RECORDED-CDN"  # reused the recorded id
    # The prior recorded secret is carried forward (not blanked) in the config.
    assert saved["out"]["fos_secret_access_key"] == "PRIOR-SECRET"
    assert all(name != "fos.secret_key" for name, _ in rec["secrets"])


# --- device deletion and editing tests -------------------------------------


def test_delete_device_pure():
    reg = registry.empty_devices("backyard")
    reg = registry.add_device(
        reg,
        device_id="cam-1",
        node_id="pi-01",
        kind="observer",
        dev_type="camera_usb",
        name="Cam",
        ts=10,
    )
    reg = registry.add_device(
        reg,
        device_id="pir-1",
        node_id="pi-01",
        kind="observer",
        dev_type="motion_pir",
        name="Pir",
        ts=10,
    )

    # Delete existing device
    reg_del = registry.delete_device(reg, device_id="cam-1", ts=20)
    assert len(reg_del["devices"]) == 1
    assert reg_del["devices"][0]["device_id"] == "pir-1"
    assert reg_del["updated_ts"] == 20

    # Deleting missing device raises ValueError
    import pytest

    with pytest.raises(ValueError, match="not found"):
        registry.delete_device(reg, device_id="ghost", ts=20)


def test_edit_device_pure():
    reg = registry.empty_devices("backyard")
    reg = registry.add_device(
        reg,
        device_id="cam-1",
        node_id="pi-01",
        kind="observer",
        dev_type="camera_usb",
        name="Cam",
        ts=10,
    )

    # Edit display name and node_id
    reg_edit = registry.edit_device(
        reg, device_id="cam-1", name="Front Gate Cam", node_id="pi-02", ts=20
    )
    dev = reg_edit["devices"][0]
    assert dev["name"] == "Front Gate Cam"
    assert dev["node_id"] == "pi-02"
    assert dev["kind"] == "observer"
    assert dev["type"] == "camera_usb"

    # Edit taxonomy kind/type
    reg_tax = registry.edit_device(
        reg, device_id="cam-1", kind="deterrent", dev_type="solenoid", ts=20
    )
    dev2 = reg_tax["devices"][0]
    assert dev2["kind"] == "deterrent"
    assert dev2["type"] == "solenoid"

    # Invalid node_id raises ValueError
    import pytest

    with pytest.raises(ValueError, match="invalid node_id"):
        registry.edit_device(reg, device_id="cam-1", node_id="invalid/id", ts=20)

    # Invalid taxonomy raises ValueError
    with pytest.raises(ValueError, match="invalid device type"):
        registry.edit_device(reg, device_id="cam-1", dev_type="invalid/type", ts=20)


def test_unregister_device_e2e(tmp_path):
    ctx = {
        "service_id": "SVC",
        "token": "admintok",
        "garden_state_store_id": "STATE",
        "garden_tokens_store_id": "SECRET",
        "backend_url": "https://edge",
        "configs_dir": str(tmp_path),
    }

    # Seed local mirror with two devices
    reg = registry.empty_devices("backyard")
    reg = registry.add_device(
        reg,
        device_id="cam-1",
        node_id="pi-01",
        kind="observer",
        dev_type="camera_usb",
        name="Cam",
        ts=10,
    )
    reg = registry.add_device(
        reg,
        device_id="pir-1",
        node_id="pi-01",
        kind="observer",
        dev_type="motion_pir",
        name="Pir",
        ts=10,
    )

    mirror_data = {"gardens": registry.empty_gardens(), "devices": {"backyard": reg}}
    mirror_path = tmp_path / "SVC-registry.json"
    mirror_path.write_text(json.dumps(mirror_data))

    with (
        patch.object(fastly_api, "kv_put") as kv_put,
        patch.object(fastly_api, "secret_delete") as sec_del,
    ):
        # 1. Unregister first device (garden still has pir-1 remaining, so no token deletion)
        res = registry.unregister_device(ctx, gid="backyard", did="cam-1", ts=20)
        assert res["device_id"] == "cam-1"
        assert res["deleted_token"] is False
        kv_put.assert_called_once()  # wrote back backyard devices

        # 2. Unregister last device (this deletes garden token as well)
        kv_put.reset_mock()
        res2 = registry.unregister_device(ctx, gid="backyard", did="pir-1", ts=30)
        assert res2["device_id"] == "pir-1"
        assert res2["deleted_token"] is True
        assert sec_del.call_count == 2  # current + previous tokens deleted


def test_unregister_last_device_surfaces_token_delete_failure(tmp_path):
    """Regression (CLOUD-010): if the LAST device leaves a garden but the Secret Store
    delete FAILS, the garden token is still a valid edge credential. unregister_device
    must NOT swallow this — it raises TokenStillLiveError so the operator knows the
    token is live and retries (the device is already out of the registry)."""
    ctx = {
        "service_id": "SVC",
        "token": "admintok",
        "garden_state_store_id": "STATE",
        "garden_tokens_store_id": "SECRET",
        "backend_url": "https://edge",
        "configs_dir": str(tmp_path),
    }
    reg = registry.add_device(
        registry.empty_devices("backyard"),
        device_id="cam-1",
        node_id="pi-01",
        kind="observer",
        dev_type="camera_usb",
        name="Cam",
        ts=10,
    )
    (tmp_path / "SVC-registry.json").write_text(
        json.dumps({"gardens": registry.empty_gardens(), "devices": {"backyard": reg}})
    )

    with (
        patch.object(fastly_api, "kv_put"),
        patch.object(
            fastly_api,
            "secret_delete",
            side_effect=RuntimeError("HTTP 500 secret delete"),
        ),
    ):
        with pytest.raises(registry.TokenStillLiveError) as ei:
            registry.unregister_device(ctx, gid="backyard", did="cam-1", ts=20)
    # the error names the garden + the still-live secret slots, so the operator can act
    assert ei.value.gid == "backyard"
    assert "g.backyard.token_current" in ei.value.secret_names
    assert "g.backyard.token_previous" in ei.value.secret_names
