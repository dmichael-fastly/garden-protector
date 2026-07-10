"""Unit tests for the admin console (provision/console.py).

Pure helpers (SSE framing, CLI arg building, overview aggregation, control target,
secret stripping) + the generic process-streaming pump. No real provisioning runs
here, so the real configs/ dir is never touched (the live mock-mode flow is a
separate smoke).
"""
import json
import sys

import pytest

from provision import console


# --- SSE framing ----------------------------------------------------------

def test_sse_event_single_line():
    out = console.sse_event({"line": "hello", "level": "info"}).decode()
    assert out == 'data: {"line": "hello", "level": "info"}\n\n'


def test_sse_event_named_event():
    out = console.sse_event({"done": True}, event="done").decode()
    assert out.startswith("event: done\n")
    assert out.endswith("\n\n")
    assert 'data: {"done": true}' in out


def test_sse_event_is_well_formed_and_escapes_newlines():
    # Compact JSON escapes embedded newlines, so a payload is always ONE data line
    # terminated by a blank line — exactly one SSE event, no premature dispatch.
    out = console.sse_event({"line": "a\nb"}).decode()
    assert out.count("data: ") == 1
    assert "\\n" in out          # the newline is escaped inside the JSON string
    assert out.endswith("\n\n")  # blank line terminates the event


# --- build_cli_args -------------------------------------------------------

def test_build_cli_args_provision():
    args = console.build_cli_args("provision", {
        "service_name": "gp-demo", "region": "us-west-2", "skip_archive": "1"})
    assert args[:3] == ["provision", "--service-name", "gp-demo"]
    assert "--region" in args and "us-west-2" in args
    assert "--skip-archive" in args


def test_build_cli_args_provision_omits_empty_optionals():
    args = console.build_cli_args("provision", {"service_name": "x"})
    assert "--domain" not in args
    assert "--skip-archive" not in args


def test_build_cli_args_register_device():
    args = console.build_cli_args("register-device", {
        "garden": "backyard", "device": "cam-front", "kind": "observer",
        "type": "camera_usb", "node": "pi-01", "name": "Front", "service_id": "S1"})
    for pair in (["--garden", "backyard"], ["--device", "cam-front"],
                 ["--kind", "observer"], ["--type", "camera_usb"],
                 ["--node", "pi-01"], ["--service-id", "S1"]):
        i = args.index(pair[0])
        assert args[i + 1] == pair[1]


def test_build_cli_args_create_garden_location_and_drops_notes():
    args = console.build_cli_args("create-garden", {
        "garden": "backyard", "name": "Backyard", "tz": "America/Los_Angeles",
        "address": "1 Main", "lat": "37.7", "lon": "-122.4",
        "notes": "should be dropped", "device": "pi-gw", "kind": "observer",
        "type": "camera_csi", "service_id": "S1"})
    assert args[:3] == ["create-garden", "--garden", "backyard"]
    for pair in (["--name", "Backyard"], ["--tz", "America/Los_Angeles"],
                 ["--address", "1 Main"], ["--lat", "37.7"], ["--lon", "-122.4"],
                 ["--device", "pi-gw"], ["--service-id", "S1"]):
        i = args.index(pair[0])
        assert args[i + 1] == pair[1]
    assert "--notes" not in args   # garden notes are Pi-local, never sent to the edge


def test_build_cli_args_update_garden_no_device_no_notes():
    args = console.build_cli_args("update-garden", {
        "garden": "backyard", "name": "Back Garden", "tz": "America/Chicago",
        "address": "2 Elm", "lat": "41.8", "lon": "-87.6",
        "notes": "should be dropped", "device": "should-be-ignored", "service_id": "S1"})
    assert args[:3] == ["update-garden", "--garden", "backyard"]
    for pair in (["--name", "Back Garden"], ["--tz", "America/Chicago"],
                 ["--address", "2 Elm"], ["--lat", "41.8"], ["--lon", "-87.6"],
                 ["--service-id", "S1"]):
        i = args.index(pair[0])
        assert args[i + 1] == pair[1]
    assert "--notes" not in args     # notes are Pi-local, never sent to the edge
    assert "--device" not in args    # update-garden never registers a device
    assert "--name" in args


def test_build_cli_args_teardown_and_seed():
    assert console.build_cli_args("seed-registry", {"service_id": "S"}) == [
        "seed-registry", "--service-id", "S"]
    td = console.build_cli_args("teardown", {"service_id": "S", "remove_data": "yes"})
    assert td[0] == "teardown" and "--remove-data" in td


def test_build_cli_args_rotate_token_keeps_secret_off_argv():
    """The prior (current) garden token is a SECRET and must NEVER appear on argv
    (process list) or in the SSE command echo. build_cli_args carries only --garden
    / --service-id; the token travels via the child env (GP_PRIOR_GARDEN_TOKEN)."""
    args = console.build_cli_args("rotate-token", {
        "garden": "backyard", "prior_token": "SUPER-SECRET-TOK", "service_id": "S1"})
    assert args == ["rotate-token", "--garden", "backyard", "--service-id", "S1"]
    assert "--prior-token" not in args
    assert "SUPER-SECRET-TOK" not in args
    assert all("SUPER-SECRET-TOK" not in a for a in args)


def test_stream_cli_passes_prior_token_via_env_not_argv(monkeypatch):
    """stream_cli must inject the rotate-token prior token into the CHILD ENV
    (GP_PRIOR_GARDEN_TOKEN), never argv, and never echo it into the SSE stream."""
    from types import SimpleNamespace
    from provision import streaming

    captured = {}

    def fake_pump(argv, env, write):
        captured["argv"] = argv
        captured["env"] = env
        return 0

    monkeypatch.setattr(streaming, "pump_process", fake_pump)
    frames = []
    cfg = SimpleNamespace(python_exe=sys.executable, mock=True, configs_dir="/tmp")
    streaming.stream_cli(cfg, "rotate-token",
                         {"garden": "backyard", "prior_token": "SECRET-TOK", "service_id": "S1"},
                         write=lambda f: frames.append(f))

    assert captured["env"]["GP_PRIOR_GARDEN_TOKEN"] == "SECRET-TOK"
    assert "SECRET-TOK" not in " ".join(captured["argv"])
    joined = b"".join(frames).decode()
    assert "SECRET-TOK" not in joined   # never echoed into the SSE command line


def test_build_cli_args_unknown_op_raises():
    with pytest.raises(ValueError):
        console.build_cli_args("nope", {})


def test_build_cli_args_missing_required_raises():
    with pytest.raises(KeyError):
        console.build_cli_args("register-device", {"garden": "g"})  # missing device/kind/type


def test_truthy():
    assert console._truthy("1") and console._truthy("true") and console._truthy("YES")
    assert not console._truthy("0") and not console._truthy("") and not console._truthy("no")


# --- list_services / read_overview / garden_token (tmp configs) -----------

def _write(p, obj):
    p.write_text(json.dumps(obj))


def test_list_services_strips_secrets(tmp_path):
    _write(tmp_path / "SVC1.json", {
        "service_id": "SVC1", "service_name": "demo", "backend_url": "https://x",
        "cdn_url": "https://cdn", "fastly_api_key": "SECRET", "cdn_secret": "SHH"})
    _write(tmp_path / "SVC1-registry.json", {"gardens": {}, "devices": {}})  # ignored
    svcs = console.list_services(tmp_path)
    assert len(svcs) == 1
    s = svcs[0]
    assert s["service_id"] == "SVC1" and s["has_archive"] is True
    assert "fastly_api_key" not in s and "cdn_secret" not in s


def test_read_overview_aggregates_gardens_and_devices(tmp_path):
    _write(tmp_path / "SVC.json", {"service_id": "SVC", "service_name": "demo", "backend_url": "u"})
    _write(tmp_path / "SVC-registry.json", {
        "gardens": {"gardens": [
            {"garden_id": "default", "name": "Home", "tz": "UTC", "status": "active", "created_ts": 1},
            {"garden_id": "backyard", "name": "Backyard", "tz": "America/New_York", "status": "active", "created_ts": 2},
        ]},
        "devices": {"backyard": {"devices": [
            {"device_id": "cam-front", "node_id": "pi-01", "kind": "observer", "type": "camera_usb", "name": "Front", "status": "active"}]}},
    })
    ov = console.read_overview(tmp_path, "SVC")
    by = next(g for g in ov["gardens"] if g["garden_id"] == "backyard")
    assert by["device_count"] == 1 and by["devices"][0]["device_id"] == "cam-front"
    home = next(g for g in ov["gardens"] if g["garden_id"] == "default")
    assert home["device_count"] == 0


def test_garden_token_reads_env_file(tmp_path):
    (tmp_path / "backyard-cam.env").write_text(
        "GP_GARDEN_ID=backyard\nGP_GARDEN_TOKEN=secret-tok-123\nGP_BACKEND=u\n")
    assert console.garden_token(tmp_path, "backyard") == "secret-tok-123"
    assert console.garden_token(tmp_path, "default") == ""  # default is tokenless
    assert console.garden_token(tmp_path, "nonexistent") == ""


# --- edge_control_target --------------------------------------------------

def test_edge_control_target_default_vs_garden():
    url, is_default = console.edge_control_target("http://edge:7878/", "default")
    assert url == "http://edge:7878/api/control" and is_default is True
    url, is_default = console.edge_control_target("http://edge:7878", "backyard")
    assert url == "http://edge:7878/api/gardens/backyard/control" and is_default is False


# --- pump_process (generic streamer) --------------------------------------

def test_pump_process_streams_lines_and_returns_code():
    frames = []
    code = console.pump_process(
        [sys.executable, "-c", "print('line one'); print('line two')"],
        env=None, write=frames.append)
    assert code == 0
    text = b"".join(frames).decode()
    assert "line one" in text and "line two" in text
    # each line is its own SSE data frame
    assert text.count("data: ") == 2


def test_pump_process_nonzero_exit():
    code = console.pump_process(
        [sys.executable, "-c", "import sys; print('boom'); sys.exit(3)"],
        env=None, write=lambda f: None)
    assert code == 3


def test_pump_process_classifies_error_level():
    frames = []
    console.pump_process(
        [sys.executable, "-c", "print('FAILED something')"], env=None, write=frames.append)
    assert '"level": "error"' in b"".join(frames).decode()


def test_strip_glyphs_removes_emoji_keeps_text():
    # Streamed deploy/provision lines ship glyph-free now (the browser renders a leveled
    # SVG icon instead); the leading emoji still encodes the level for classification.
    from provision import streaming
    assert streaming.strip_glyphs("\U0001F511 Checking your account…") == "Checking your account…"
    assert streaming.strip_glyphs("✓ Done") == "Done"
    assert streaming.strip_glyphs("⚠️ heads up") == "heads up"
    assert streaming.strip_glyphs("plain line, no glyph") == "plain line, no glyph"


# --- LAN admin auth: passcode KDF -----------------------------------------

def test_passcode_roundtrip_and_mismatch():
    rec = console.make_auth_record("hunter2-garden")
    assert rec["algo"] == "scrypt" and rec["salt"] and rec["hash"]
    assert console.verify_passcode("hunter2-garden", rec)
    assert not console.verify_passcode("wrong-passcode", rec)


def test_verify_passcode_rejects_bad_records():
    assert not console.verify_passcode("x", {})
    assert not console.verify_passcode("x", {"algo": "md5", "salt": "00", "hash": "00"})
    assert not console.verify_passcode("x", {"algo": "scrypt", "salt": "zz", "hash": "qq"})  # bad hex


def test_save_then_load_auth_record(tmp_path):
    assert console.load_auth_record(tmp_path) is None
    rec = console.make_auth_record("pass-the-tomatoes")
    path = console.save_auth_record(tmp_path, rec)
    assert path.name == console.AUTH_FILE_NAME
    loaded = console.load_auth_record(tmp_path)
    assert console.verify_passcode("pass-the-tomatoes", loaded)


# --- LAN guard ------------------------------------------------------------

def test_is_lan_addr_allows_local_rejects_public():
    for ip in ("127.0.0.1", "::1", "10.0.0.5", "192.168.1.20", "172.16.3.4",
               "169.254.10.10", "fe80::1"):
        assert console.is_lan_addr(ip), ip
    for ip in ("8.8.8.8", "1.1.1.1", "9.9.9.9", "2606:4700:4700::1111", "not-an-ip", ""):
        assert not console.is_lan_addr(ip), ip


def test_is_lan_addr_global_ipv6_accepted_only_same_link():
    # Home LANs number hosts with global IPv6 (GUA) out of the ISP prefix. A GUA
    # client is LAN iff it shares the /64 of the server address the connection
    # arrived on (same /64 == same link).
    server = "2601:280:4685:4ce0:7d5d:e03f:d075:481e"
    same_link = "2601:280:4685:4ce0:ca9:7e8:38f1:8056"
    other_link = "2601:280:4685:9999:ca9:7e8:38f1:8056"
    far_off = "2606:4700:4700::1111"
    assert console.is_lan_addr(same_link, server)
    assert not console.is_lan_addr(other_link, server)
    assert not console.is_lan_addr(far_off, server)
    # Fail closed without the server hint, and don't widen the gate when the server
    # address isn't itself a global IPv6 (IPv4-mapped / loopback).
    assert not console.is_lan_addr(same_link)
    assert not console.is_lan_addr(same_link, "10.0.0.5")
    assert not console.is_lan_addr(same_link, "::1")
    # IPv4-mapped client still unwraps to a private address regardless of server hint.
    assert console.is_lan_addr("::ffff:10.0.0.5", server)


# --- sessions (injected clock) --------------------------------------------

def test_session_store_mint_valid_expire():
    t = {"now": 1000.0}
    s = console.SessionStore(ttl=100, clock=lambda: t["now"])
    tok = s.mint()
    assert s.valid(tok)
    assert not s.valid("nope") and not s.valid("")
    t["now"] = 1101.0                       # past expiry
    assert not s.valid(tok)
    tok2 = s.mint()
    assert s.valid(tok2)
    s.drop(tok2)
    assert not s.valid(tok2)


# --- rate limiter (injected clock) ----------------------------------------

def test_rate_limiter_lockout_reset_and_window():
    t = {"now": 0.0}
    rl = console.RateLimiter(max_fails=3, window=60, clock=lambda: t["now"])
    ip = "192.168.1.50"
    assert not rl.locked(ip)
    for _ in range(3):
        rl.record_fail(ip)
    assert rl.locked(ip)
    rl.reset(ip)                            # a successful login clears the penalty
    assert not rl.locked(ip)
    for _ in range(3):
        rl.record_fail(ip)
    assert rl.locked(ip)
    t["now"] = 61.0                         # window elapsed -> auto-reset
    assert not rl.locked(ip)


# --- log tail: gzip parse + FOS new-line diffing --------------------------

def test_parse_log_object_gzip_and_plain():
    import gzip
    raw = b"[GP] one\n\n[GP] two\n"
    assert console.parse_log_object(gzip.compress(raw)) == ["[GP] one", "[GP] two"]
    assert console.parse_log_object(raw) == ["[GP] one", "[GP] two"]  # plain body tolerated


def test_fos_log_source_backfill_then_incremental():
    class _FakeFos(console.FosLogSource):
        def __init__(self, rounds, lines):
            self._rounds, self._lines, self._i = rounds, lines, 0
            self._seen, self._primed = set(), False
            self._backfill_objects, self._max_backfill_lines = 2, 200

        def _list_keys(self):
            ks = self._rounds[min(self._i, len(self._rounds) - 1)]
            self._i += 1
            return list(ks)

        def _read_lines(self, key):
            return list(self._lines.get(key, []))

    rounds = [["k1", "k2", "k3"], ["k1", "k2", "k3", "k4"], ["k1", "k2", "k3", "k4"]]
    lines = {"k1": ["a"], "k2": ["b"], "k3": ["c"], "k4": ["d1", "d2"]}
    f = _FakeFos(rounds, lines)
    assert f.poll() == ["b", "c"]      # first poll backfills only the last 2 objects (k1 skipped)
    assert f.poll() == ["d1", "d2"]    # second poll emits only the new object
    assert f.poll() == []              # nothing new -> empty
