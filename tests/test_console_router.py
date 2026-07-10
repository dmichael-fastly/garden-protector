"""Phase 2 router precedence for the admin console (provision/console.py).

The do_GET/do_POST dispatchers were converted from if/elif chains to data-driven
route tables with the session gate applied ONCE. These tests pin the precedence the
conversion had to preserve:
  * static assets + favicon stay ungated even with a passcode set (the sign-in page
    needs them);
  * the session gate runs BEFORE the route lookup, so an UNKNOWN /api path is a 401
    (auth required) to an unauthenticated caller — NOT a 404;
  * an unknown path becomes a 404 once authenticated;
  * the login/logout POSTs are reachable without a session.
"""
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

import requests

from provision import console


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


def test_static_and_favicon_ungated_even_with_passcode(tmp_path, monkeypatch):
    monkeypatch.setenv("FASTLY_MOCK_MODE", "1")
    console.save_auth_record(tmp_path, console.make_auth_record("hunter2"))
    with _server(tmp_path) as url:
        # No session cookie -> the sign-in page's own assets must still load.
        assert requests.get(url + "/static/gp.css", timeout=5).status_code == 200
        assert requests.get(url + "/static/gp.js", timeout=5).status_code == 200
        assert requests.get(url + "/favicon.ico", timeout=5).status_code == 200


def test_unknown_api_path_is_401_before_route_lookup_when_unauthed(tmp_path, monkeypatch):
    # Session gate runs BEFORE the route table, so an unknown /api path is a 401
    # (auth required), NOT a 404 — matching the pre-Phase-2 precedence.
    monkeypatch.setenv("FASTLY_MOCK_MODE", "1")
    console.save_auth_record(tmp_path, console.make_auth_record("hunter2"))
    with _server(tmp_path) as url:
        assert requests.get(url + "/api/console/nope", timeout=5).status_code == 401


def test_unknown_api_path_is_404_when_authed(tmp_path, monkeypatch):
    # With no passcode configured the caller is implicitly authed, so an unknown
    # /api path falls through the route table to a 404.
    monkeypatch.setenv("FASTLY_MOCK_MODE", "1")
    with _server(tmp_path) as url:
        assert requests.get(url + "/api/console/nope", timeout=5).status_code == 404


def test_login_and_logout_are_ungated(tmp_path, monkeypatch):
    monkeypatch.setenv("FASTLY_MOCK_MODE", "1")
    console.save_auth_record(tmp_path, console.make_auth_record("hunter2"))
    with _server(tmp_path) as url:
        # Logout needs no session (idempotent clear) -> 200, never the gate's 401.
        assert requests.post(url + "/api/console/logout", timeout=5).status_code == 200
        # Login is reachable without a session; a wrong passcode is a 401 from the
        # login handler ("invalid passcode"), distinct from the gate ("auth required").
        r = requests.post(url + "/api/console/login", json={"passcode": "wrong"}, timeout=5)
        assert r.status_code == 401 and r.json()["error"] == "invalid passcode"
