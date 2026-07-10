"""Console admin-passcode SOURCE resolution (provision/console.py `resolve_auth`).

The console used to read its passcode ONLY from its own ``console-auth.json`` (set via
``console set-passcode``). It now resolves, highest precedence first:

  1. ``GP_ADMIN_PASSCODE`` (env / .env — shared with the Pi portal; lets a fresh boot
     enforce the console with no interactive step),
  2. ``configs/secrets.json`` ``admin_passcode_hash`` (the shared hashed record),
  3. ``configs/console-auth.json`` (legacy fallback).

These tests pin that precedence + the verify behaviour for each source, plus the
LAN-bind refusal honouring all three, and an end-to-end env-passcode login.
"""
import json
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

import requests

from provision import auth, console


def _write_secrets(configs_dir, passcode):
    """Write a configs/secrets.json with a shared admin_passcode_hash record, exactly
    as the Pi portal/pi_config would."""
    p = configs_dir / console.SECRETS_FILE_NAME
    p.write_text(json.dumps({
        console.SECRETS_PASSCODE_KEY: auth.make_auth_record(passcode),
        "fastly_api_key": "unrelated-secret",  # proves we read only the passcode key
    }))
    return p


# ---------------------------------------------------------------------------
# resolve_auth precedence + verify
# ---------------------------------------------------------------------------

def test_no_source_means_disabled(tmp_path):
    m = console.resolve_auth(tmp_path)
    assert m.enabled is False
    assert m.source == "none"
    assert m.verify("anything") is False


def test_console_auth_json_is_the_fallback(tmp_path):
    console.save_auth_record(tmp_path, console.make_auth_record("filepass"))
    m = console.resolve_auth(tmp_path)
    assert m.enabled is True
    assert m.source == "console-auth.json"
    assert m.verify("filepass") is True
    assert m.verify("nope") is False


def test_secrets_json_outranks_console_auth_json(tmp_path):
    console.save_auth_record(tmp_path, console.make_auth_record("filepass"))
    _write_secrets(tmp_path, "secretpass")
    m = console.resolve_auth(tmp_path)
    assert m.source == "secrets.json"
    assert m.verify("secretpass") is True
    assert m.verify("filepass") is False  # the lower-precedence record is NOT consulted


def test_env_outranks_everything(tmp_path, monkeypatch):
    console.save_auth_record(tmp_path, console.make_auth_record("filepass"))
    _write_secrets(tmp_path, "secretpass")
    monkeypatch.setenv("GP_ADMIN_PASSCODE", "envpass")
    m = console.resolve_auth(tmp_path)
    assert m.source == "env"
    assert m.verify("envpass") is True
    assert m.verify("secretpass") is False
    assert m.verify("filepass") is False


def test_empty_env_passcode_is_ignored(tmp_path, monkeypatch):
    # An empty GP_ADMIN_PASSCODE must NOT count as "configured" (it would otherwise
    # accept an empty submission) — fall through to the next source.
    console.save_auth_record(tmp_path, console.make_auth_record("filepass"))
    monkeypatch.setenv("GP_ADMIN_PASSCODE", "")
    m = console.resolve_auth(tmp_path)
    assert m.source == "console-auth.json"
    assert m.verify("") is False
    assert m.verify("filepass") is True


def test_malformed_secrets_json_falls_through(tmp_path):
    (tmp_path / console.SECRETS_FILE_NAME).write_text("{ not json")
    console.save_auth_record(tmp_path, console.make_auth_record("filepass"))
    m = console.resolve_auth(tmp_path)
    assert m.source == "console-auth.json"
    assert m.verify("filepass") is True


def test_secrets_json_without_passcode_key_falls_through(tmp_path):
    (tmp_path / console.SECRETS_FILE_NAME).write_text(json.dumps({"fastly_api_key": "x"}))
    m = console.resolve_auth(tmp_path)
    assert m.enabled is False
    assert m.source == "none"


# ---------------------------------------------------------------------------
# LAN-bind refusal honours every source
# ---------------------------------------------------------------------------

def test_lan_bind_refused_when_no_passcode_anywhere(tmp_path):
    # --env-file points at a nonexistent file so the real repo .env can't leak a
    # passcode into this test; the autouse fixture clears GP_ADMIN_PASSCODE.
    rc = console.main(["--listen", "0.0.0.0:0", "--configs-dir", str(tmp_path),
                       "--env-file", str(tmp_path / "nope.env")])
    assert rc == 2


def test_lan_bind_refusal_logic_passes_with_env(tmp_path, monkeypatch):
    # When a passcode IS resolvable, the refusal branch is skipped. We assert the
    # decision (resolve_auth(...).enabled) rather than letting main() serve_forever.
    monkeypatch.setenv("GP_ADMIN_PASSCODE", "envpass")
    assert console.resolve_auth(tmp_path).enabled is True


# ---------------------------------------------------------------------------
# End-to-end: login with the env-sourced passcode
# ---------------------------------------------------------------------------

@contextmanager
def _server(configs_dir):
    cfg = console.ConsoleConfig(configs_dir=configs_dir, mock=True)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), console.make_handler(cfg))
    t = threading.Thread(target=srv.serve_forever, args=(0.02,), daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def test_login_with_env_passcode_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("FASTLY_MOCK_MODE", "1")
    monkeypatch.setenv("GP_ADMIN_PASSCODE", "envpass")  # read at make_handler time
    with _server(tmp_path) as url:
        # Wrong passcode -> 401 from the login handler (not the gate).
        assert requests.post(url + "/api/console/login",
                             json={"passcode": "wrong"}, timeout=5).status_code == 401
        # Correct env passcode -> 200 + session cookie, then a gated route works.
        s = requests.Session()
        r = s.post(url + "/api/console/login", json={"passcode": "envpass"}, timeout=5)
        assert r.status_code == 200
        assert "gp_session" in r.cookies
        assert s.get(url + "/api/console/info", timeout=5).status_code == 200
