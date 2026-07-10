"""Unit + integration tests for the Pi LAN admin portal (hardware/portal.py).

Two layers, mirroring tests/test_gateway.py + tests/test_sim_e2e.py:
  * the PURE cores (session mint/verify, passcode compare, the rate-limit
    decision, .env parse, dashboard flag injection) — no I/O, clock passed in;
  * the HTTP shell driven against the deterministic FakeEdge, proving the auth
    gate, the edge proxy, and per-IP brute-force lockout end to end.
"""
import threading
import urllib.parse
from http.server import ThreadingHTTPServer

import pytest
import requests

from hardware import portal as pt
from tests.fake_edge import FakeEdge


# ===========================================================================
# PURE CORE
# ===========================================================================

# --- passcode compare ------------------------------------------------------

def test_check_passcode():
    assert pt.check_passcode("hunter2", "hunter2") is True
    assert pt.check_passcode("nope", "hunter2") is False
    assert pt.check_passcode("", "hunter2") is False
    assert pt.check_passcode("anything", "") is False   # unconfigured never matches
    assert pt.check_passcode(None, "hunter2") is False


# --- session mint / verify -------------------------------------------------

def test_session_roundtrip_and_expiry():
    secret = b"s3cr3t"
    tok = pt.mint_session("admin", now=1000, ttl=100, secret=secret)
    assert pt.verify_session(tok, now=1050, secret=secret) == "admin"
    assert pt.verify_session(tok, now=1099, secret=secret) == "admin"
    # exp is now+ttl=1100; at/after that -> expired
    assert pt.verify_session(tok, now=1100, secret=secret) is None
    assert pt.verify_session(tok, now=9999, secret=secret) is None


def test_session_rejects_tamper_and_wrong_secret():
    secret = b"s3cr3t"
    tok = pt.mint_session("admin", now=1000, ttl=100, secret=secret)
    assert pt.verify_session(tok, now=1050, secret=b"other") is None
    # flip the last char of the MAC
    bad = tok[:-1] + ("0" if tok[-1] != "0" else "1")
    assert pt.verify_session(bad, now=1050, secret=secret) is None
    # garbage / shapes
    assert pt.verify_session("", now=1050, secret=secret) is None
    assert pt.verify_session("no-dot", now=1050, secret=secret) is None
    assert pt.verify_session("a.b.c", now=1050, secret=secret) is None


def test_derive_secret_stable_and_explicit_wins():
    a = pt.derive_secret(None, "pass1")
    b = pt.derive_secret(None, "pass1")
    c = pt.derive_secret(None, "pass2")
    assert a == b and a != c                      # stable per passcode
    assert pt.derive_secret("explicit", "pass1") == b"explicit"


# --- rate-limit decision ---------------------------------------------------

def test_rate_decision_allows_under_threshold():
    assert pt.rate_decision([], now=1000, max_fails=3, window_s=60, lockout_s=300) == (True, 0)
    assert pt.rate_decision([990, 995], now=1000, max_fails=3, window_s=60, lockout_s=300) == (True, 0)


def test_rate_decision_locks_after_threshold():
    hist = [960, 980, 1000]  # 3 fails, last at now
    allowed, retry = pt.rate_decision(hist, now=1000, max_fails=3, window_s=60, lockout_s=300)
    assert allowed is False
    assert retry == 300  # last + lockout - now = 1000 + 300 - 1000


def test_rate_decision_unlocks_after_lockout_and_window():
    # 3 fails but all older than the window -> not recent -> allowed again
    hist = [100, 120, 140]
    assert pt.rate_decision(hist, now=1000, max_fails=3, window_s=60, lockout_s=300) == (True, 0)
    # within window, partway through lockout -> still locked, retry decays
    hist2 = [940, 960, 980]
    allowed, retry = pt.rate_decision(hist2, now=1000, max_fails=3, window_s=60, lockout_s=300)
    assert allowed is False and retry == 280  # 980 + 300 - 1000


def test_rate_limiter_shell():
    rl = pt.RateLimiter(max_fails=2, window_s=60, lockout_s=120)
    assert rl.allowed("1.2.3.4", now=1000) == (True, 0)
    rl.record_failure("1.2.3.4", now=1000)
    rl.record_failure("1.2.3.4", now=1001)
    allowed, retry = rl.allowed("1.2.3.4", now=1002)
    assert allowed is False and retry > 0
    # a different IP is unaffected
    assert rl.allowed("9.9.9.9", now=1002) == (True, 0)
    # success clears the offending IP
    rl.record_success("1.2.3.4")
    assert rl.allowed("1.2.3.4", now=1003) == (True, 0)


# --- .env loader -----------------------------------------------------------

def test_load_env_file(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text(
        "# a comment\n"
        "\n"
        "GP_ADMIN_PASSCODE=hunter2\n"
        'GP_BACKEND="https://edge.example"\n'
        "export GP_GARDEN_ID=g1\n"
        "EMPTY=\n"
        "GP_PREEXIST=fromfile\n"
    )
    monkeypatch.setenv("GP_PREEXIST", "fromenv")
    for k in ("GP_ADMIN_PASSCODE", "GP_BACKEND", "GP_GARDEN_ID", "EMPTY"):
        monkeypatch.delenv(k, raising=False)

    applied = pt.load_env_file(str(p))
    assert applied["GP_ADMIN_PASSCODE"] == "hunter2"
    assert applied["GP_BACKEND"] == "https://edge.example"   # quotes stripped
    assert applied["GP_GARDEN_ID"] == "g1"                   # export prefix handled
    assert applied["EMPTY"] == ""
    assert "GP_PREEXIST" not in applied                      # never overrides a set var
    import os
    assert os.environ["GP_PREEXIST"] == "fromenv"


def test_load_env_file_missing_is_noop():
    assert pt.load_env_file("/no/such/file.env") == {}


# --- dashboard flag injection ----------------------------------------------

def test_render_dashboard_injects_flag():
    html = "<html><head><title>x</title></head><body>hi</body></html>"
    admin = pt.render_dashboard(html, view_only=False)
    viewer = pt.render_dashboard(html, view_only=True)
    assert "window.GP_VIEW_ONLY=false;" in admin
    assert "window.GP_VIEW_ONLY=true;" in viewer
    # injected before </head>
    assert admin.index("window.GP_VIEW_ONLY") < admin.index("</head>")


def test_render_dashboard_without_head_prepends():
    out = pt.render_dashboard("<body>hi</body>", view_only=True)
    assert out.startswith("<script>window.GP_VIEW_ONLY=true;</script>")


# --- shared header partial (single source of truth) ------------------------

import os  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read_repo(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_render_header_bakes_name_and_marks_active():
    tmpl = _read_repo("hardware/portal_header.html")
    out = pt.render_header(tmpl, "nav-costs", "Backyard <Patch> & Co")
    assert 'id="nav-costs" class="active"' in out          # current page marked
    assert 'id="nav-dashboard" class="active"' not in out  # only the active one
    assert "Backyard &lt;Patch&gt; &amp; Co" in out        # baked + HTML-escaped
    assert "__GARDEN_NAME__" not in out                    # placeholder consumed


def test_render_header_includes_status_pills():
    # The shared header carries the single operational-mode pill (OFF/MONITOR/ACTIVE, with
    # SPRAYING + HELD·rain layered on ACTIVE), driven by GP.subscribeState in gp.js. It is a
    # STATUS-ONLY badge — never a clickable toggle. It appears on every sentinel page; the
    # labeled controls live on the dashboard.
    out = pt.render_header(_read_repo("hardware/portal_header.html"), "nav-costs", "X")
    assert 'id="portal-status"' in out
    assert 'id="pill-mode"' in out
    # The pill markup must NOT carry any button/toggle affordance (status-only). gp.js no
    # longer adds these client-side either (see renderStatusPills/initStatusPills).
    assert 'role="button"' not in out
    assert "aria-pressed" not in out
    # The pill renders with plain status classes only — no "clickable" affordance class.
    import re
    pill = re.search(r'id="pill-mode"[^>]*class="([^"]*)"', out)
    if pill is None:
        pill = re.search(r'class="([^"]*)"[^>]*id="pill-mode"', out)
    assert pill is not None, "pill-mode span not found"
    assert "clickable" not in pill.group(1)
    assert "gp-pill" in pill.group(1) and "mode" in pill.group(1)


def test_render_header_empty_template_is_blank():
    assert pt.render_header("", "nav-logs", "X") == ""


def test_inject_header_splices_at_marker_and_noops_without():
    page = "<header>\n  <!--PORTAL_HEADER-->\n  <div class='head-right'></div>\n</header>"
    out = pt.inject_header(page, "<nav>HDR</nav>")
    assert "<nav>HDR</nav>" in out and pt.PORTAL_HEADER_MARKER not in out
    # No marker -> unchanged (protects stub-HTML serve tests + any non-portal page).
    assert pt.inject_header("<html>STUB</html>", "<nav>HDR</nav>") == "<html>STUB</html>"


def test_every_portal_page_has_exactly_one_header_sentinel():
    for rel in ("hardware/devices.html", "hardware/settings.html", "hardware/costs.html",
                "hardware/logs.html", "hardware/storage.html", "backend/src/dashboard.html"):
        assert _read_repo(rel).count(pt.PORTAL_HEADER_MARKER) == 1, rel


def test_costs_page_now_shows_garden_name():
    # Regression: costs.html previously had NO garden-name slot at all; after the
    # shared header it does, with the name baked in.
    tmpl = _read_repo("hardware/portal_header.html")
    page = _read_repo("hardware/costs.html")
    out = pt.inject_header(page, pt.render_header(tmpl, "nav-costs", "Tomato Town"))
    assert out.count('id="garden-name"') == 1
    assert "Tomato Town" in out
    assert out.count("<header>") == 1


def test_garden_name_ttl_cache():
    class _PC:
        def __init__(self):
            self.calls = 0

        def load(self):
            self.calls += 1
            return {"garden": {"name": "Rose Bed"}}

    pc = _PC()
    p = pt.Portal(pi_config=pc)
    assert p.garden_name(now=1000) == "Rose Bed"
    assert p.garden_name(now=1030) == "Rose Bed"   # within 60s TTL -> served from cache
    assert pc.calls == 1
    assert p.garden_name(now=1100) == "Rose Bed"   # past TTL -> re-read
    assert pc.calls == 2


def test_garden_name_failsoft_when_unavailable():
    assert pt.Portal(pi_config=None).garden_name(now=1) == ""


# --- Portal.try_login ------------------------------------------------------

def test_portal_try_login_states():
    portal = _portal(edge="http://unused", max_fails=2, window_s=60, lockout_s=300)
    status, tok = portal.try_login("1.1.1.1", "hunter2", now=1000)
    assert status == "ok" and portal.role_for(tok, now=1000) == "admin"
    status, _ = portal.try_login("2.2.2.2", "wrong", now=1000)
    assert status == "bad"
    # second wrong from the same IP trips the lockout on the third try
    portal.try_login("2.2.2.2", "wrong", now=1001)
    status, retry = portal.try_login("2.2.2.2", "wrong", now=1002)
    assert status == "locked" and retry > 0


# ===========================================================================
# HTTP INTEGRATION  (portal server -> FakeEdge)
# ===========================================================================

def _portal(*, edge, **kw):
    return pt.Portal(
        admin_passcode="hunter2",
        session_secret=b"test-secret",
        edge=edge,
        dashboard_html="<html><head></head><body><div id='admin-controls'>CTL</div></body></html>",
        garden_id=kw.get("garden_id", "default"),
        garden_token=kw.get("garden_token", ""),
        rate_limiter=pt.RateLimiter(
            max_fails=kw.get("max_fails", 5),
            window_s=kw.get("window_s", 60),
            lockout_s=kw.get("lockout_s", 300),
        ),
    )


class _Harness:
    def __init__(self, **kw):
        self.edge = FakeEdge(armed=True)
        self.edge_url = self.edge.start()
        self.portal = _portal(edge=self.edge_url, **kw)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), pt.make_handler(self.portal))
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._t = threading.Thread(
            target=self.server.serve_forever, args=(0.02,), daemon=True)
        self._t.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.edge.stop()


@pytest.fixture
def h():
    harness = _Harness()
    yield harness
    harness.close()


def test_healthz_is_open(h):
    r = requests.get(h.url + "/healthz", timeout=5)
    assert r.status_code == 200 and r.json()["ok"] is True


def test_root_unauthed_serves_login(h):
    r = requests.get(h.url + "/", timeout=5)
    assert r.status_code == 200
    assert "Passcode" in r.text and "Sign in" in r.text


def test_static_assets_served_ungated(h):
    # The shared UI assets must load WITHOUT auth (the login page itself needs
    # them) and carry a content-hash ETag + no-cache so the browser always
    # revalidates (no stale copy after a deploy) but pays only a 304 when
    # unchanged. Regression: _serve_static passed extra_headers as a dict, but
    # _send iterates pairs (`for k, v in`), so a non-empty extra_headers crashed
    # mid-response -> ERR_EMPTY_RESPONSE and the whole UI rendered unstyled.
    for path, ctype in (("/static/gp.css", "text/css"),
                        ("/static/gp.js", "javascript")):
        r = requests.get(h.url + path, timeout=5)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
        assert r.content, f"{path} returned an empty body"
        assert ctype in r.headers.get("Content-Type", "")
        assert r.headers.get("Cache-Control") == "no-cache"
        etag = r.headers.get("ETag")
        assert etag, f"{path} missing ETag"
        # Re-request with the ETag -> 304 Not Modified, no body re-sent.
        r2 = requests.get(h.url + path, headers={"If-None-Match": etag}, timeout=5)
        assert r2.status_code == 304, f"{path} revalidation -> {r2.status_code}"
        assert not r2.content
        assert r2.headers.get("ETag") == etag


def test_api_requires_auth(h):
    assert requests.get(h.url + "/api/state", timeout=5).status_code == 401
    assert requests.get(h.url + "/api/snapshot", timeout=5).status_code == 401
    assert requests.post(h.url + "/api/control", json={"cmd": "arm"}, timeout=5).status_code == 401


def test_admin_route_table_gates_and_404s(h):
    # Unauthed API routes answer a JSON 401 (their JS handles it); the gate is applied
    # once in the dispatcher so it can't be forgotten on a route.
    for p in ("/api/logs", "/api/maintenance", "/api/gadget/snapshot"):
        assert requests.get(h.url + p, timeout=5).status_code == 401, f"GET {p}"
    for p in ("/api/cost-rates", "/api/settings/passcode", "/api/settings/token",
              "/api/settings/viewer-pass", "/api/maintenance/run-now", "/api/maintenance/wipe-all"):
        assert requests.post(h.url + p, json={}, timeout=5).status_code == 401, f"POST {p}"
    # ...and an unknown route falls through to 404 (the dispatch else), not 401.
    assert requests.get(h.url + "/api/nope", timeout=5).status_code == 404
    assert requests.post(h.url + "/api/nope", json={}, timeout=5).status_code == 404


def test_admin_pages_fall_back_to_login_not_401(h):
    # The full HTML admin pages render the LOGIN page (200) when unauthed — NOT a raw
    # {"error":"unauthorized"} JSON 401 — so landing on /storage signed out shows the form.
    for p in ("/admin", "/history", "/timelapse", "/devices", "/settings", "/costs", "/logs", "/storage"):
        r = requests.get(h.url + p, timeout=5)
        assert r.status_code == 200 and "Passcode" in r.text, p
        assert "unauthorized" not in r.text, p


def test_tabled_admin_route_reachable_after_login(h):
    # A method-backed tabled route (/devices) passes the gate once authed: unauthed it
    # is 401 (see above), authed it reaches its handler (status != 401). The handler may
    # then 404 on its own in the harness (no device backend) — that's not the gate.
    s = requests.Session()
    r = s.post(h.url + "/login", data={"passcode": "hunter2"},
               allow_redirects=False, timeout=5)
    assert r.status_code == 303
    assert s.get(h.url + "/devices", timeout=5).status_code != 401


def test_login_wrong_passcode_401(h):
    r = requests.post(h.url + "/login", data={"passcode": "nope"},
                      allow_redirects=False, timeout=5)
    assert r.status_code == 401
    assert "Incorrect" in r.text


def test_login_then_dashboard_and_proxy(h):
    s = requests.Session()
    # correct login -> 303 redirect + Set-Cookie
    r = s.post(h.url + "/login", data={"passcode": "hunter2"},
               allow_redirects=False, timeout=5)
    assert r.status_code == 303
    assert pt.Portal.SESSION_COOKIE in r.cookies

    # the dashboard is now served (view_only=false -> controls present)
    r = s.get(h.url + "/", timeout=5)
    assert r.status_code == 200
    assert "window.GP_VIEW_ONLY=false;" in r.text
    assert "admin-controls" in r.text

    # /api/state is proxied to the edge
    r = s.get(h.url + "/api/state", timeout=5)
    assert r.status_code == 200
    assert r.json()["armed"] is True

    # /api/control is proxied and actually flips edge state
    r = s.post(h.url + "/api/control", json={"cmd": "disarm"}, timeout=5)
    assert r.status_code == 200 and r.json()["armed"] is False
    assert ("POST", "/api/control") in h.edge.requests


def test_login_json_api(h):
    s = requests.Session()
    r = s.post(h.url + "/login", json={"passcode": "hunter2"}, timeout=5)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert s.get(h.url + "/api/state", timeout=5).status_code == 200


def _admin_session(harness):
    s = requests.Session()
    s.post(harness.url + "/login", data={"passcode": "hunter2"},
           allow_redirects=False, timeout=5)
    return s


def test_gadget_views_require_admin(h):
    assert requests.get(h.url + "/api/gadget/snapshot?device=cam", timeout=5).status_code == 401
    assert requests.get(h.url + "/api/gadget/status?device=cam", timeout=5).status_code == 401


def test_gadget_snapshot_requires_device_param(h):
    s = _admin_session(h)
    assert s.get(h.url + "/api/gadget/snapshot", timeout=5).status_code == 400
    assert s.get(h.url + "/api/gadget/status", timeout=5).status_code == 400


def test_gadget_snapshot_default_garden_falls_back_to_snapshot(h):
    # The legacy "default" garden keeps a single shared latest image, so the per-gadget
    # proxy must fall back to /api/snapshot (not the per-device garden path).
    h.edge.latest_image = b"\xff\xd8DEFAULTJPEG\xff\xd9"
    s = _admin_session(h)
    r = s.get(h.url + "/api/gadget/snapshot?device=cam-front", timeout=5)
    assert r.status_code == 200
    assert r.headers["Content-Type"].startswith("image/jpeg")
    assert r.content == b"\xff\xd8DEFAULTJPEG\xff\xd9"
    assert ("GET", "/api/snapshot") in h.edge.requests


def test_gadget_snapshot_and_status_per_device_in_named_garden():
    harness = _Harness(garden_id="g1", garden_token="tok-1")
    try:
        harness.edge.latest_image = b"\xff\xd8CAMJPEG\xff\xd9"
        harness.edge.latest_event = {"species": "raccoon", "action": "mitigate",
                                     "confidence": 0.9, "reason": None, "ts": 123}
        harness.edge.latest_telemetry = {"raining": False, "last_seen_ms": 456}
        s = _admin_session(harness)

        # snapshot proxies the PER-DEVICE garden path (not /api/snapshot)
        r = s.get(harness.url + "/api/gadget/snapshot?device=cam-front", timeout=5)
        assert r.status_code == 200 and r.content == b"\xff\xd8CAMJPEG\xff\xd9"
        assert ("GET", "/api/gardens/g1/devices/cam-front/snapshot") in harness.edge.requests

        # status combines the per-device event + telemetry into one blob
        st = s.get(harness.url + "/api/gadget/status?device=cam-front", timeout=5).json()
        assert st["event"]["species"] == "raccoon"
        assert st["telemetry"]["last_seen_ms"] == 456
        assert ("GET", "/api/gardens/g1/devices/cam-front/event") in harness.edge.requests
        assert ("GET", "/api/gardens/g1/devices/cam-front/telemetry") in harness.edge.requests
    finally:
        harness.close()


def test_brute_force_lockout():
    harness = _Harness(max_fails=3, window_s=60, lockout_s=60)
    try:
        url = harness.url + "/login"
        for _ in range(3):
            assert requests.post(url, data={"passcode": "x"},
                                 allow_redirects=False, timeout=5).status_code == 401
        # 4th attempt (even with the RIGHT passcode) is locked out by IP
        r = requests.post(url, data={"passcode": "hunter2"},
                          allow_redirects=False, timeout=5)
        assert r.status_code == 429
        assert "Retry-After" in r.headers
    finally:
        harness.close()


# ===========================================================================
# FIRST-RUN GATE + BOOTSTRAP MODE (wizard)
# ===========================================================================

from hardware import pi_config as pc  # noqa: E402

WIZARD_MARK = "GP-WIZARD-SPA"
WIZARD_HTML = f"<html><head></head><body>{WIZARD_MARK}</body></html>"


def _wizard_portal(tmp_path, *, admin_passcode="", provisioned=False, set_passcode=None):
    cfg = pc.PiConfig(tmp_path)
    if provisioned:
        cfg.save_partial({"provisioned": True, "garden": {"garden_id": "backyard"}})
    if set_passcode:
        cfg.set_passcode(set_passcode)
    return pt.Portal(
        admin_passcode=admin_passcode,
        edge="http://unused",
        dashboard_html="<html><head></head><body><div id='admin-controls'>CTL</div></body></html>",
        wizard_html=WIZARD_HTML,
        pi_config=cfg,
    )


class _Serve:
    """Minimal HTTP harness (no FakeEdge) for the wizard routes."""

    def __init__(self, portal):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), pt.make_handler(portal))
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._t = threading.Thread(
            target=self.server.serve_forever, args=(0.02,), daemon=True)
        self._t.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_bootstrap_unprovisioned_serves_wizard_and_403s_others(tmp_path):
    srv = _Serve(_wizard_portal(tmp_path))   # no passcode + not provisioned -> bootstrap
    try:
        r = requests.get(srv.url + "/", timeout=5)
        assert r.status_code == 200 and WIZARD_MARK in r.text
        # non-wizard routes are locked down (403) during bootstrap
        assert requests.get(srv.url + "/api/state", timeout=5).status_code == 403
        assert requests.get(srv.url + "/login", timeout=5).status_code == 403
        assert requests.post(srv.url + "/api/control", json={"cmd": "arm"}, timeout=5).status_code == 403
        # healthz stays open; wizard API is open (LAN) in bootstrap
        assert requests.get(srv.url + "/healthz", timeout=5).status_code == 200
        assert requests.get(srv.url + "/api/wizard/state", timeout=5).status_code == 200
    finally:
        srv.close()


def test_wizard_state_reports_bootstrap(tmp_path):
    srv = _Serve(_wizard_portal(tmp_path))
    try:
        st = requests.get(srv.url + "/api/wizard/state", timeout=5).json()
        assert st["bootstrap"] is True and st["provisioned"] is False
        assert st["has_passcode"] is False and st["has_token"] is False
    finally:
        srv.close()


def test_wizard_passcode_then_login_then_config_then_token(tmp_path):
    srv = _Serve(_wizard_portal(tmp_path))
    try:
        s = requests.Session()
        # set passcode (bootstrap is open) -> does NOT mint a session: the operator
        # must sign in with the passcode they just chose (the page reloads to login).
        r = s.post(srv.url + "/api/wizard/passcode", json={"passcode": "tomatoes1"}, timeout=5)
        assert r.status_code == 200 and r.json()["ok"] is True
        assert pt.Portal.SESSION_COOKIE not in r.cookies
        # we're now OUT of bootstrap; without a session the wizard API is locked,
        # and GET / serves the login prompt (not the wizard).
        assert s.post(srv.url + "/api/wizard/config", json={"name": "X"}, timeout=5).status_code == 401
        r = s.get(srv.url + "/", timeout=5)
        assert "Sign in" in r.text and WIZARD_MARK not in r.text
        # sign in with the chosen passcode -> the session carries us through the rest
        assert s.post(srv.url + "/login", json={"passcode": "tomatoes1"}, timeout=5).status_code == 200
        r = s.post(srv.url + "/api/wizard/config",
                   json={"name": "Back Yard", "address": "1 Main", "tz": "America/Los_Angeles"},
                   timeout=5)
        assert r.status_code == 200 and r.json()["garden_id"] == "back-yard"
        # store the Fastly token (never echoed back)
        r = s.post(srv.url + "/api/wizard/token", json={"token": "FASTLY-SECRET"}, timeout=5)
        assert r.status_code == 200 and "FASTLY-SECRET" not in r.text
        # the token landed in secrets.json (0600), NOT pi-garden.json
        c = pc.PiConfig(tmp_path)
        assert c.get_secret("fastly_api_token") == "FASTLY-SECRET"
        assert "FASTLY-SECRET" not in c.config_path.read_text()
        assert c.load()["garden"]["name"] == "Back Yard"
    finally:
        srv.close()


class _FakeResp:
    """Minimal stand-in for a requests.Response in geocode tests."""

    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self._payload


def test_wizard_geocode_proxies_nominatim(tmp_path, monkeypatch):
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured.update(url=url, params=params, headers=headers)
        return _FakeResp([{"lat": "39.7817", "lon": "-89.6501",
                           "display_name": "Springfield, IL, USA"}])

    monkeypatch.setattr(pt.requests, "get", fake_get)   # patches outbound Nominatim only
    srv = _Serve(_wizard_portal(tmp_path))              # bootstrap -> wizard API open on LAN
    try:
        r = requests.post(srv.url + "/api/wizard/geocode",
                          json={"street": "1 Main St", "city": "Springfield",
                                "state": "IL", "country": "US"}, timeout=5)
        assert r.status_code == 200
        j = r.json()
        assert j["ok"] is True and j["lat"] == 39.7817 and j["lon"] == -89.6501
        assert "nominatim.openstreetmap.org" in captured["url"]
        assert captured["params"]["country"] == "US"        # structured query passed through
        assert "User-Agent" in captured["headers"]          # Nominatim policy
    finally:
        srv.close()


def test_wizard_geocode_no_match_is_404(tmp_path, monkeypatch):
    monkeypatch.setattr(pt.requests, "get", lambda *a, **k: _FakeResp([]))
    srv = _Serve(_wizard_portal(tmp_path))
    try:
        r = requests.post(srv.url + "/api/wizard/geocode",
                          json={"q": "zzz nowhere at all"}, timeout=5)
        assert r.status_code == 404
    finally:
        srv.close()


def test_wizard_geocode_requires_an_address(tmp_path):
    srv = _Serve(_wizard_portal(tmp_path))
    try:
        r = requests.post(srv.url + "/api/wizard/geocode", json={}, timeout=5)
        assert r.status_code == 400
    finally:
        srv.close()


def test_wizard_config_persists_structured_address(tmp_path):
    srv = _Serve(_wizard_portal(tmp_path))   # bootstrap -> wizard API open on LAN
    try:
        r = requests.post(srv.url + "/api/wizard/config", json={
            "name": "Back Yard", "country": "GB",
            "address_fields": {"line1": "10 Downing St", "city": "London", "postcode": "SW1A 2AA"},
            "address": "10 Downing St, London, SW1A 2AA, United Kingdom",
            "lat": 51.5034, "lon": -0.1276, "tz": "Europe/London",
        }, timeout=5)
        assert r.status_code == 200
        g = pc.PiConfig(tmp_path).load()["garden"]
        assert g["country"] == "GB"
        assert g["address_fields"]["postcode"] == "SW1A 2AA"
        assert g["address"].startswith("10 Downing St")
        # resume echoes the structured fields so the wizard re-renders the same form
        st = requests.get(srv.url + "/api/wizard/state", timeout=5).json()
        assert st["garden"]["country"] == "GB"
        assert st["garden"]["address_fields"]["city"] == "London"
    finally:
        srv.close()


def test_unprovisioned_with_passcode_serves_login_not_wizard(tmp_path):
    # passcode set but not provisioned and not signed in -> GET / is the login prompt
    # (this is the page the wizard reloads into right after the passcode step).
    srv = _Serve(_wizard_portal(tmp_path, set_passcode="tomatoes1"))
    try:
        r = requests.get(srv.url + "/", timeout=5)
        assert r.status_code == 200 and "Sign in" in r.text and WIZARD_MARK not in r.text
        # the login page exposes the SSH-based passcode recovery
        assert "Forgot your passcode?" in r.text and "secrets.json" in r.text
        # signing in then GET / resumes the wizard
        s = requests.Session()
        assert s.post(srv.url + "/login", json={"passcode": "tomatoes1"}, timeout=5).status_code == 200
        assert WIZARD_MARK in s.get(srv.url + "/", timeout=5).text
    finally:
        srv.close()


def test_wizard_config_requires_session_once_passcode_set(tmp_path):
    # passcode set but NO session cookie -> wizard endpoints require auth
    srv = _Serve(_wizard_portal(tmp_path, set_passcode="tomatoes1"))
    try:
        r = requests.post(srv.url + "/api/wizard/config", json={"name": "X"}, timeout=5)
        assert r.status_code == 401
    finally:
        srv.close()


def test_wizard_detect_persists_pi_and_network(tmp_path):
    srv = _Serve(_wizard_portal(tmp_path))
    try:
        r = requests.get(srv.url + "/api/wizard/detect", timeout=10)
        assert r.status_code == 200
        assert set(r.json()) >= {"pi", "network", "cameras"}
        c = pc.PiConfig(tmp_path)
        # detect persists pi/network but must NOT advance the step (so a refresh
        # doesn't skip the Detect confirmation) — it stays at the skeleton default.
        assert c.step() == "detect"
        assert "network" in c.load()
    finally:
        srv.close()


def test_provisioned_with_passcode_is_normal_mode(tmp_path):
    srv = _Serve(_wizard_portal(tmp_path, provisioned=True, set_passcode="tomatoes1"))
    try:
        # GET / -> login page (provisioned, not the wizard)
        r = requests.get(srv.url + "/", timeout=5)
        assert r.status_code == 200 and "Sign in" in r.text and WIZARD_MARK not in r.text
        # normal-mode API requires auth (401, NOT a bootstrap 403)
        assert requests.get(srv.url + "/api/state", timeout=5).status_code == 401
        # wizard endpoints now require a session too
        assert requests.get(srv.url + "/api/wizard/state", timeout=5).status_code == 401
    finally:
        srv.close()


def test_login_against_hashed_passcode(tmp_path):
    srv = _Serve(_wizard_portal(tmp_path, provisioned=True, set_passcode="tomatoes1"))
    try:
        s = requests.Session()
        r = s.post(srv.url + "/login", json={"passcode": "tomatoes1"}, timeout=5)
        assert r.status_code == 200 and r.json()["ok"] is True
        # the session minted from the hashed-passcode-derived secret verifies
        assert s.get(srv.url + "/api/wizard/state", timeout=5).status_code == 200
    finally:
        srv.close()


def test_wizard_provision_stream_deploys_and_persists(tmp_path, monkeypatch):
    """Deploy SSE: with a STUBBED gp-provision (no Fastly), the portal runs
    provision -> create-garden, then persists service coords + garden token +
    provisioned, redacts the token, and adopts the new edge identity live."""
    from provision import streaming
    store = pc.PiConfig(tmp_path)
    store.set_passcode("tomatoes1")
    store.set_secret("fastly_api_token", "FTOK-secret")
    store.save_partial({"garden": {"garden_id": "backyard", "name": "Backyard", "tz": "UTC",
                                   "address": "1 Main", "lat": 1.0, "lon": 2.0}, "step": "deploy"})

    def fake_pump(argv, env, write):
        import pathlib
        import json as _json
        cd = pathlib.Path(env["GP_CONFIGS_DIR"])
        assert env.get("FASTLY_API_KEY") == "FTOK-secret"      # token via ENV …
        assert "FTOK-secret" not in " ".join(argv)             # … never argv
        write(streaming.sse_event({"line": "…working", "level": "info"}))
        if "provision" in argv:
            sn = argv[argv.index("--service-name") + 1]
            (cd / "SVC1.json").write_text(_json.dumps({
                "service_id": "SVC1", "service_name": sn,
                "backend_url": "https://svc1.edgecompute.app", "cdn_url": "https://cdn1",
                "region": "us-east-1"}))
            return 0
        if "create-garden" in argv:
            gid = argv[argv.index("--garden") + 1]
            (cd / f"{gid}-garden.env").write_text(
                f"GP_GARDEN_ID={gid}\nGP_GARDEN_TOKEN=gtok-xyz\nGP_BACKEND=https://svc1.edgecompute.app\n")
            return 0
        return 1
    monkeypatch.setattr(streaming, "pump_process", fake_pump)

    portal = pt.Portal(admin_passcode="", edge="http://unused",
                       dashboard_html="<html><head></head><body>D</body></html>",
                       wizard_html=WIZARD_HTML, pi_config=store)
    srv = _Serve(portal)
    try:
        s = requests.Session()
        assert s.post(srv.url + "/login", json={"passcode": "tomatoes1"}, timeout=5).status_code == 200
        body = s.get(srv.url + "/api/wizard/provision/stream", timeout=15).text
        assert "event: done" in body and '"ok": true' in body and '"redirect": "/devices"' in body
        assert "FTOK-secret" not in body                       # token redacted from the stream
    finally:
        srv.close()

    assert store.is_provisioned() is True
    assert store.load()["fastly"]["service_id"] == "SVC1"
    assert store.load()["fastly"]["backend_url"] == "https://svc1.edgecompute.app"
    assert store.get_secret("garden_token") == "gtok-xyz"
    # portal adopted the freshly-provisioned edge + token WITHOUT a restart
    assert portal.edge == "https://svc1.edgecompute.app"
    assert portal.garden_token == "gtok-xyz"


def test_wizard_devices_scan(tmp_path, monkeypatch):
    from hardware import discovery
    monkeypatch.setattr(discovery, "scan",
                        lambda **k: {"cameras": [{"type": "camera_usb"}],
                                     "nodes": [{"node_id": "node-7", "found_by": "mdns"}]})
    srv = _Serve(_wizard_portal(tmp_path, provisioned=True, set_passcode="tomatoes1"))
    try:
        s = requests.Session()
        s.post(srv.url + "/login", json={"passcode": "tomatoes1"}, timeout=5)
        d = s.get(srv.url + "/api/wizard/devices/scan", timeout=5).json()
        assert d["cameras"][0]["type"] == "camera_usb"
        assert d["nodes"][0]["node_id"] == "node-7"
    finally:
        srv.close()


def test_wizard_provision_stream_fails_without_token(tmp_path):
    store = pc.PiConfig(tmp_path)
    store.set_passcode("tomatoes1")
    store.save_partial({"garden": {"garden_id": "backyard", "name": "Backyard"}, "step": "deploy"})
    portal = pt.Portal(admin_passcode="", edge="http://unused",
                       dashboard_html="<html><head></head><body>D</body></html>",
                       wizard_html=WIZARD_HTML, pi_config=store)
    srv = _Serve(portal)
    try:
        s = requests.Session()
        s.post(srv.url + "/login", json={"passcode": "tomatoes1"}, timeout=5)
        body = s.get(srv.url + "/api/wizard/provision/stream", timeout=10).text
        assert '"ok": false' in body and "token" in body.lower()
        assert store.is_provisioned() is False
    finally:
        srv.close()


def test_wizard_register_stream_enforces_two_tier_split(tmp_path, monkeypatch):
    """THE split invariant: only coarse identity reaches the edge (gp-provision argv);
    the full transport + scan_meta land ONLY in pi-garden.json."""
    from provision import streaming
    store = pc.PiConfig(tmp_path)
    store.set_passcode("tomatoes1")
    store.set_secret("fastly_api_token", "FTOK")
    store.save_partial({"provisioned": True, "node_id": "pi-01",
                        "garden": {"garden_id": "backyard", "name": "Backyard"},
                        "fastly": {"service_id": "SVC1"}})
    captured = {}

    def fake_pump(argv, env, write):
        captured["argv"] = argv
        write(streaming.sse_event({"line": "registering…", "level": "info"}))
        return 0
    monkeypatch.setattr(streaming, "pump_process", fake_pump)

    portal = pt.Portal(admin_passcode="", edge="http://unused",
                       dashboard_html="<html><head></head><body>D</body></html>",
                       wizard_html=WIZARD_HTML, pi_config=store)
    srv = _Serve(portal)
    try:
        s = requests.Session()
        s.post(srv.url + "/login", json={"passcode": "tomatoes1"}, timeout=5)
        payload = {"device_id": "front-cam", "name": "Front bed camera",
                   "kind": "observer", "type": "camera_usb", "node_id": "pi-01",
                   "transport": {"kind": "usb", "dev": "/dev/video1", "bus": "usb-x", "driver": "uvcvideo"},
                   "scan_meta": {"found_by": "camera_probe"}}
        body = s.post(srv.url + "/api/wizard/devices/register/stream", json=payload, timeout=10).text
        assert '"ok": true' in body
    finally:
        srv.close()

    # EDGE (CLI argv) got ONLY coarse fields — NO transport/dev/bus/driver.
    argv = captured["argv"]
    joined = " ".join(argv)
    assert "register-device" in argv and "front-cam" in argv and "camera_usb" in argv
    for leaked in ("/dev/video1", "uvcvideo", "usb-x", "transport", "scan_meta", "found_by"):
        assert leaked not in joined, f"{leaked!r} leaked to the edge argv"

    # FULL transport + scan_meta persisted to pi-garden.json ONLY.
    dev = store.load()["devices"][0]
    assert dev["device_id"] == "front-cam" and dev["enabled"] is True
    assert dev["transport"] == {"kind": "usb", "dev": "/dev/video1", "bus": "usb-x", "driver": "uvcvideo"}
    assert dev["scan_meta"]["found_by"] == "camera_probe"


def test_devices_page_served_when_provisioned(tmp_path):
    portal = _wizard_portal(tmp_path, provisioned=True, set_passcode="tomatoes1")
    portal.devices_html = "<html><body>DEVICES-PAGE</body></html>"
    srv = _Serve(portal)
    try:
        # unauthed -> the LOGIN page (200), not a JSON 401
        r0 = requests.get(srv.url + "/devices", timeout=5)
        assert r0.status_code == 200 and "Passcode" in r0.text and "DEVICES-PAGE" not in r0.text
        s = requests.Session()
        s.post(srv.url + "/login", json={"passcode": "tomatoes1"}, timeout=5)
        r = s.get(srv.url + "/devices", timeout=5)
        assert r.status_code == 200 and "DEVICES-PAGE" in r.text
    finally:
        srv.close()


def test_history_page_served_when_provisioned(tmp_path):
    portal = _wizard_portal(tmp_path, provisioned=True, set_passcode="tomatoes1")
    portal.dashboard_html = "<html><head></head><body>DASHBOARD-PAGE</body></html>"
    srv = _Serve(portal)
    try:
        # unauthorized GET /history serves the LOGIN_PAGE
        r = requests.get(srv.url + "/history", timeout=5)
        assert r.status_code == 200 and "Passcode" in r.text

        # authorized GET /history serves dashboard_html with GP_VIEW_ONLY=false injected
        s = requests.Session()
        s.post(srv.url + "/login", json={"passcode": "tomatoes1"}, timeout=5)
        r = s.get(srv.url + "/history", timeout=5)
        assert r.status_code == 200
        assert "window.GP_VIEW_ONLY=false;" in r.text
        assert "DASHBOARD-PAGE" in r.text
    finally:
        srv.close()


def test_history_marks_history_nav_active_server_side(tmp_path):
    # UI-001 (pi arm): GET /history must mark the History tab active SERVER-side
    # (mirroring the edge's history_header_html), so the right tab is selected without
    # waiting on the dashboard JS. /admin (the same SPA) must mark Dashboard instead.
    # A real header template + a dashboard page carrying the header marker are needed
    # so render_header/inject_header actually splice the rendered nav in.
    portal = _wizard_portal(tmp_path, provisioned=True, set_passcode="tomatoes1")
    portal.portal_header_html = _read_repo("hardware/portal_header.html")
    portal.dashboard_html = (
        "<html><head></head><body>"
        + pt.PORTAL_HEADER_MARKER
        + "<main>DASH</main></body></html>"
    )
    srv = _Serve(portal)
    try:
        s = requests.Session()
        s.post(srv.url + "/login", json={"passcode": "tomatoes1"}, timeout=5)

        hist = s.get(srv.url + "/history", timeout=5).text
        assert 'id="nav-history" class="active"' in hist
        assert 'id="nav-dashboard" class="active"' not in hist

        # /admin (same SPA) marks Dashboard active, NOT History.
        admin = s.get(srv.url + "/admin", timeout=5).text
        assert 'id="nav-dashboard" class="active"' in admin
        assert 'id="nav-history" class="active"' not in admin
    finally:
        srv.close()


def test_timelapse_page_served_when_provisioned(tmp_path):
    portal = _wizard_portal(tmp_path, provisioned=True, set_passcode="tomatoes1")
    portal.timelapse_html = "<html><head></head><body>TIMELAPSE-PAGE</body></html>"
    srv = _Serve(portal)
    try:
        # unauthorized GET /timelapse serves the LOGIN_PAGE (not a JSON 401)
        r = requests.get(srv.url + "/timelapse", timeout=5)
        assert r.status_code == 200 and "Passcode" in r.text and "TIMELAPSE-PAGE" not in r.text
        # authorized -> the timelapse page with GP_VIEW_ONLY=false (admin: export panel shows)
        s = requests.Session()
        s.post(srv.url + "/login", json={"passcode": "tomatoes1"}, timeout=5)
        r = s.get(srv.url + "/timelapse", timeout=5)
        assert r.status_code == 200
        assert "window.GP_VIEW_ONLY=false;" in r.text
        assert "TIMELAPSE-PAGE" in r.text
    finally:
        srv.close()


def test_event_page_served_when_provisioned(tmp_path):
    portal = _wizard_portal(tmp_path, provisioned=True, set_passcode="tomatoes1")
    # A real header template + the header marker so render_header/inject_header splice the nav,
    # letting us assert History is marked active (the detail page has no nav item of its own).
    portal.portal_header_html = _read_repo("hardware/portal_header.html")
    portal.event_html = (
        "<html><head></head><body>EVENT-PAGE " + pt.PORTAL_HEADER_MARKER + "</body></html>"
    )
    srv = _Serve(portal)
    try:
        # unauthorized GET /event serves the LOGIN_PAGE (not a JSON 401)
        r = requests.get(srv.url + "/event?key=abc", timeout=5)
        assert r.status_code == 200 and "Passcode" in r.text and "EVENT-PAGE" not in r.text
        # authorized -> the event page, History marked active
        s = requests.Session()
        s.post(srv.url + "/login", json={"passcode": "tomatoes1"}, timeout=5)
        r = s.get(srv.url + "/event?key=abc", timeout=5)
        assert r.status_code == 200
        assert "EVENT-PAGE" in r.text
        assert 'id="nav-history" class="active"' in r.text
    finally:
        srv.close()


def test_settings_page_served_when_provisioned(tmp_path):
    portal = _wizard_portal(tmp_path, provisioned=True, set_passcode="tomatoes1")
    portal.settings_html = "<html><body>SETTINGS-PAGE</body></html>"
    srv = _Serve(portal)
    try:
        # unauthed -> the LOGIN page (200), not a JSON 401
        r0 = requests.get(srv.url + "/settings", timeout=5)
        assert r0.status_code == 200 and "Passcode" in r0.text and "SETTINGS-PAGE" not in r0.text
        s = requests.Session()
        s.post(srv.url + "/login", json={"passcode": "tomatoes1"}, timeout=5)
        r = s.get(srv.url + "/settings", timeout=5)
        assert r.status_code == 200 and "SETTINGS-PAGE" in r.text
    finally:
        srv.close()


def test_settings_change_passcode_requires_correct_current(tmp_path):
    portal = _wizard_portal(tmp_path, provisioned=True, set_passcode="tomatoes1")
    srv = _Serve(portal)
    try:
        # No session -> unauthorized.
        assert requests.post(srv.url + "/api/settings/passcode",
                             json={"current": "tomatoes1", "new": "carrots22"},
                             timeout=5).status_code == 401
        s = requests.Session()
        s.post(srv.url + "/login", json={"passcode": "tomatoes1"}, timeout=5)
        # Wrong current passcode is rejected; the stored one is unchanged.
        assert s.post(srv.url + "/api/settings/passcode",
                      json={"current": "WRONG", "new": "carrots22"}, timeout=5).status_code == 403
        # Too-short new passcode is rejected.
        assert s.post(srv.url + "/api/settings/passcode",
                      json={"current": "tomatoes1", "new": "short"}, timeout=5).status_code == 400
        # Correct current + valid new -> changed.
        assert s.post(srv.url + "/api/settings/passcode",
                      json={"current": "tomatoes1", "new": "carrots22"}, timeout=5).status_code == 200
        assert portal.pi_config.verify_passcode("carrots22") is True
        assert portal.pi_config.verify_passcode("tomatoes1") is False
    finally:
        srv.close()


def test_settings_update_token_persists_secret(tmp_path):
    portal = _wizard_portal(tmp_path, provisioned=True, set_passcode="tomatoes1")
    srv = _Serve(portal)
    try:
        s = requests.Session()
        s.post(srv.url + "/login", json={"passcode": "tomatoes1"}, timeout=5)
        assert s.post(srv.url + "/api/settings/token", json={"token": ""},
                      timeout=5).status_code == 400   # token required
        assert s.post(srv.url + "/api/settings/token", json={"token": "fastly-new-xyz"},
                      timeout=5).status_code == 200
        assert portal.pi_config.get_secret("fastly_api_token") == "fastly-new-xyz"
    finally:
        srv.close()


def test_settings_viewer_pass_sets_and_clears(tmp_path, monkeypatch):
    """The viewer gate: setting a password PUTs g.<gid>.viewer_pass into the
    garden_tokens Secret Store (where the edge enforces it) and keeps a Pi-side
    copy; clearing DELETEs it and makes the dashboard open again. The cloud call
    is stubbed so the test never touches Fastly."""
    import json
    from provision import fastly_api
    portal = _wizard_portal(tmp_path, provisioned=True, set_passcode="tomatoes1")
    # Provisioned-garden context the handler needs to locate the Secret Store.
    portal.pi_config.save_partial({"fastly": {"service_name": "garden-protector-backyard"}})
    portal.pi_config.set_secret("fastly_api_token", "FTOK")
    (tmp_path / "SVC1.json").write_text(json.dumps({
        "service_id": "SVC1", "service_name": "garden-protector-backyard",
        "garden_tokens_store_id": "STORE1"}))

    calls = []
    monkeypatch.setattr(fastly_api, "secret_put",
                        lambda store_id, name, value, token: calls.append(("put", store_id, name, value, token)) or {})
    monkeypatch.setattr(fastly_api, "secret_delete",
                        lambda store_id, name, token: calls.append(("del", store_id, name, token)))

    srv = _Serve(portal)
    try:
        # Unauthenticated -> 401.
        assert requests.post(srv.url + "/api/settings/viewer-pass",
                             json={"action": "set", "passcode": "letmein"},
                             timeout=5).status_code == 401
        s = requests.Session()
        s.post(srv.url + "/login", json={"passcode": "tomatoes1"}, timeout=5)

        # Too-short password is rejected; nothing pushed.
        assert s.post(srv.url + "/api/settings/viewer-pass",
                      json={"action": "set", "passcode": "ab"}, timeout=5).status_code == 400
        assert calls == []

        # Set -> pushes the secret + persists the Pi copy + flips the state flag.
        r = s.post(srv.url + "/api/settings/viewer-pass",
                   json={"action": "set", "passcode": "letmein"}, timeout=5)
        assert r.status_code == 200 and r.json()["viewer_pass_set"] is True
        assert calls[-1] == ("put", "STORE1", "g.backyard.viewer_pass", "letmein", "FTOK")
        assert portal.pi_config.get_secret("viewer_pass") == "letmein"
        assert s.get(srv.url + "/api/wizard/state", timeout=5).json()["has_viewer_pass"] is True

        # Clear -> deletes the secret + empties the Pi copy + flag goes false.
        r = s.post(srv.url + "/api/settings/viewer-pass", json={"action": "clear"}, timeout=5)
        assert r.status_code == 200 and r.json()["viewer_pass_set"] is False
        assert calls[-1] == ("del", "STORE1", "g.backyard.viewer_pass", "FTOK")
        assert portal.pi_config.get_secret("viewer_pass") == ""
        assert s.get(srv.url + "/api/wizard/state", timeout=5).json()["has_viewer_pass"] is False
    finally:
        srv.close()


def test_portal_system_stream(tmp_path):
    portal = _wizard_portal(tmp_path, provisioned=True, set_passcode="tomatoes1")
    srv = _Serve(portal)
    try:
        s = requests.Session()
        # Unauthorized check
        r = s.get(srv.url + "/api/system/stream", stream=True, timeout=5)
        assert r.status_code == 401

        # Log in
        s.post(srv.url + "/login", json={"passcode": "tomatoes1"}, timeout=5)

        # Fetch stream
        r = s.get(srv.url + "/api/system/stream", stream=True, timeout=5)
        assert r.status_code == 200

        # Read until the first SSE data line. chunk_size=1 so requests yields each
        # line as it arrives instead of buffering its default ~512 bytes first — the
        # server emits one small (~40B) event then sleeps 2s, so default buffering
        # blocked ~26s waiting for ~13 events to fill the buffer.
        lines = []
        for line in r.iter_lines(chunk_size=1):
            if line:
                lines.append(line.decode("utf-8"))
            if any(l.startswith("data:") for l in lines):
                break

        # Check that we received a data event with cpu and memory keys
        assert any(l.startswith("data:") for l in lines)
        data_line = [l for l in lines if l.startswith("data:")][0]
        import json
        payload = json.loads(data_line[5:].strip())
        assert "cpu" in payload
        assert "memory" in payload
    finally:
        srv.close()


def test_portal_state_stream(tmp_path, monkeypatch):
    """The Armed/Mitigation SSE feed: admin-gated, streams the proxied /api/state JSON
    so the header pills + dashboard can update without polling (the edge can't hold an
    SSE socket within its Compute budget, so this lives only on the Pi portal)."""
    import json
    portal = _wizard_portal(tmp_path, provisioned=True, set_passcode="tomatoes1")
    # Stub the edge read so the stream has state to push without a live edge.
    state = {"armed": True, "override_stop": False, "continue_mitigation": True}
    monkeypatch.setattr(portal, "proxy_get",
                        lambda path: (200, "application/json", json.dumps(state).encode()))
    srv = _Serve(portal)
    try:
        s = requests.Session()
        # Admin-gated.
        assert s.get(srv.url + "/api/state/stream", stream=True, timeout=5).status_code == 401
        s.post(srv.url + "/login", json={"passcode": "tomatoes1"}, timeout=5)

        r = s.get(srv.url + "/api/state/stream", stream=True, timeout=5)
        assert r.status_code == 200
        assert "text/event-stream" in r.headers.get("Content-Type", "")

        lines = []
        for line in r.iter_lines(chunk_size=1):
            if line:
                lines.append(line.decode("utf-8"))
            if any(l.startswith("data:") for l in lines):
                break
        data_line = [l for l in lines if l.startswith("data:")][0]
        payload = json.loads(data_line[5:].strip())
        assert payload["armed"] is True
        assert payload["continue_mitigation"] is True
    finally:
        srv.close()


# ===========================================================================
# ARCHIVE / HISTORY PROXY  (Part 1 — admin sees History; query forwarding)
# ===========================================================================

def test_archive_proxy_requires_admin(h):
    for p in ("/api/cameras", "/api/archive/days", "/api/archive?date=2026-06-22",
              "/api/archive/image?key=x"):
        assert requests.get(h.url + p, timeout=5).status_code == 401


def test_archive_day_proxy_forwards_query(h):
    # The portal must forward ?date= and ?limit= to the edge (it used to strip them).
    h.edge.archive_events = [{"date": "2026-06-22", "time": "04:45:07", "action": "none",
                              "species": "class-1", "confidence": 5, "device": "cam", "cid": "c", "key": "k"}]
    s = _admin_session(h)
    r = s.get(h.url + "/api/archive?date=2026-06-22&limit=12", timeout=5)
    assert r.status_code == 200 and r.json()["events"][0]["time"] == "04:45:07"
    assert ("GET", "/api/archive?date=2026-06-22&limit=12") in h.edge.raw_requests


def test_archive_days_and_cameras_proxy(h):
    h.edge.archive_days = ["2026-06-22", "2026-06-21"]
    h.edge.cameras = [{"device_id": "cam-front", "name": "Front", "type": "camera_csi"}]
    s = _admin_session(h)
    assert s.get(h.url + "/api/archive/days", timeout=5).json()["days"] == ["2026-06-22", "2026-06-21"]
    assert s.get(h.url + "/api/cameras", timeout=5).json()["cameras"][0]["name"] == "Front"


def test_archive_image_proxy_forwards_key_and_streams_jpeg(h):
    h.edge.latest_image = b"\xff\xd8ARCHIVEJPEG\xff\xd9"
    s = _admin_session(h)
    r = s.get(h.url + "/api/archive/image?key=g%2Fg1%2Fevidence%2Fx.jpg", timeout=5)
    assert r.status_code == 200
    assert r.headers["Content-Type"].startswith("image/jpeg")
    assert r.content == b"\xff\xd8ARCHIVEJPEG\xff\xd9"
    assert ("GET", "/api/archive/image?key=g%2Fg1%2Fevidence%2Fx.jpg") in h.edge.raw_requests


# ===========================================================================
# ALARMS  (proxy list/tag/delete + retention + the Alarms page)
# ===========================================================================

def test_alarm_routes_require_admin(h):
    # All alarm proxy + retention routes are admin-gated on the Pi portal (it's admin-only; the
    # public-edge viewer distinction lives on the edge, which the portal proxies WITH the token).
    assert requests.get(h.url + "/api/alarms", timeout=5).status_code == 401
    assert requests.get(h.url + "/api/alarm?key=x", timeout=5).status_code == 401
    assert requests.post(h.url + "/api/alarm-tag", json={"id": "x", "label": "good"}, timeout=5).status_code == 401
    assert requests.post(h.url + "/api/alarm/delete", json={"id": "x"}, timeout=5).status_code == 401
    assert requests.get(h.url + "/api/alarms/settings", timeout=5).status_code == 401
    assert requests.post(h.url + "/api/settings/alarm-retention", json={}, timeout=5).status_code == 401
    assert requests.post(h.url + "/api/alarms/run-now", json={}, timeout=5).status_code == 401
    assert requests.post(h.url + "/api/alarms/wipe-all", json={}, timeout=5).status_code == 401


def test_alarm_tag_delete_and_list_proxy(h):
    s = _admin_session(h)
    aid = "batch-1"
    # TAG -> proxied POST flips the fake edge's stored tag.
    r = s.post(h.url + "/api/alarm-tag", json={"id": aid, "label": "bad"}, timeout=5)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert ("POST", "/api/alarm-tag") in h.edge.requests
    # GET the alarm for a key (query forwarded incl. ?key=).
    r = s.get(h.url + "/api/alarm?key=" + urllib.parse.quote(aid, safe=""), timeout=5)
    assert r.status_code == 200 and r.json()["alarm"]["tag"] == "bad"
    # DELETE removes it.
    r = s.post(h.url + "/api/alarm/delete", json={"id": aid}, timeout=5)
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert ("POST", "/api/alarm/delete") in h.edge.requests
    # /api/alarms round-trips the edge's list + recommendation payload.
    h.edge.alarms_result = {"threshold_pct": 30, "min_labels": 3, "can_manage": True,
                            "recommendations": [{"species": "red-fox", "good": 3, "neutral": 0,
                                                 "bad": 0, "recommended_pct": None, "note": "ok"}],
                            "alarms": [{"id": "a1", "species": "red-fox"}]}
    r = s.get(h.url + "/api/alarms", timeout=5)
    assert r.status_code == 200 and r.json()["alarms"][0]["id"] == "a1"


def test_alarm_retention_settings_persist(tmp_path):
    # Mirrors test_maintenance_settings_defaults_and_persist: defaults, set, survive a reload.
    p = pt.Portal(pi_config=pt.pi_cfg.PiConfig(str(tmp_path)), garden_id="backyard")
    st = p.alarm_retention_settings()
    assert st == {"mode": "days", "keep_days": 90, "keep_count": 500,
                  "prune_hour": 3, "last_alarm_prune_date": None}
    p.set_alarm_retention_settings("count", keep_count=250, hour=4)
    st = p.alarm_retention_settings()
    assert st["mode"] == "count" and st["keep_count"] == 250 and st["prune_hour"] == 4
    p2 = pt.Portal(pi_config=pt.pi_cfg.PiConfig(str(tmp_path)), garden_id="backyard")
    assert p2.alarm_retention_settings()["mode"] == "count"


def test_run_alarm_prune_and_wipe_call_edge(tmp_path, monkeypatch):
    p = pt.Portal(pi_config=pt.pi_cfg.PiConfig(str(tmp_path)), garden_id="backyard", garden_token="t")
    p.set_alarm_retention_settings("count", keep_count=250)
    calls = []

    def fake_proxy_post(path, body, **kw):
        calls.append(path)
        if path.startswith("/api/alarms/prune"):
            return (200, "application/json", b'{"ok":true,"deleted":7,"kept":250}')
        return (200, "application/json", b'{"ok":true,"deleted":12}')

    monkeypatch.setattr(p, "proxy_post", fake_proxy_post)
    res = p.run_alarm_prune(trigger="manual")
    assert res["ok"] and res["deleted"] == 7
    assert calls[0].startswith("/api/alarms/prune?mode=count&keep=250")
    res = p.run_alarm_wipe(trigger="manual")
    assert res["ok"] and res["deleted"] == 12
    assert "/api/alarms/wipe" in calls


def test_should_prune_alarms_now_idempotent():
    import time as _t
    now = _t.localtime(_t.mktime((2026, 6, 24, 5, 0, 0, 0, 0, -1)))  # 05:00 local
    assert pt._should_prune_alarms_now({"prune_hour": 3, "last_alarm_prune_date": None}, now)
    # already ran today -> skip
    assert not pt._should_prune_alarms_now({"prune_hour": 3, "last_alarm_prune_date": "2026-06-24"}, now)
    # before the hour -> skip
    early = _t.localtime(_t.mktime((2026, 6, 24, 2, 0, 0, 0, 0, -1)))
    assert not pt._should_prune_alarms_now({"prune_hour": 3, "last_alarm_prune_date": None}, early)


def test_motion_settings_routes_require_admin(h):
    # Per-camera motion-trigger config is admin-only (the Pi portal is admin-gated).
    assert requests.get(h.url + "/api/gadget/motion-settings?device=cam-a", timeout=5).status_code == 401
    assert requests.post(h.url + "/api/gadget/motion-settings",
                         json={"device_id": "cam-a", "enabled": True}, timeout=5).status_code == 401


def test_motion_settings_get_handler_shape(h):
    s = _admin_session(h)
    # No ?device -> 400.
    assert s.get(h.url + "/api/gadget/motion-settings", timeout=5).status_code == 400
    # Unset device -> safe defaults (motion OFF) + the cadence options for the UI select.
    r = s.get(h.url + "/api/gadget/motion-settings?device=cam-a", timeout=5)
    assert r.status_code == 200
    j = r.json()
    assert j["device_id"] == "cam-a" and j["enabled"] is False and j["roi"] is None
    assert j["cadence_options"] == list(pt.pi_cfg.MOTION_CADENCE_OPTIONS)


def test_device_motion_settings_round_trip(tmp_path):
    # Mirrors test_alarm_retention_settings_persist: defaults, set (validated), survive a reload.
    p = pt.Portal(pi_config=pt.pi_cfg.PiConfig(str(tmp_path)), garden_id="backyard")
    assert p.device_motion_settings("cam-a") == pt.pi_cfg.DEFAULT_MOTION
    stored = p.set_device_motion_settings("cam-a", {
        "enabled": True, "cadence_s": 5, "confirm_frames": 4, "sensitivity": 0.8,
        "cooldown_s": 20, "roi": {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.4}})
    assert stored["enabled"] is True and stored["cadence_s"] == 5 and stored["roi"]["w"] == 0.5
    # Bad values are clamped to safe defaults on the way in (cadence 7 ∉ {1,2,5}).
    assert p.set_device_motion_settings("cam-a", {"cadence_s": 7})["cadence_s"] == 1
    # ...and the previous good ROI is replaced (a fresh write is the full config).
    p.set_device_motion_settings("cam-a", {"enabled": True, "cadence_s": 2})
    p2 = pt.Portal(pi_config=pt.pi_cfg.PiConfig(str(tmp_path)), garden_id="backyard")
    again = p2.device_motion_settings("cam-a")
    assert again["enabled"] is True and again["cadence_s"] == 2     # survived the reload
    assert p2.device_motion_settings("cam-b") == pt.pi_cfg.DEFAULT_MOTION   # per-camera isolation


def test_alarms_page_served_when_provisioned(tmp_path):
    portal = _wizard_portal(tmp_path, provisioned=True, set_passcode="tomatoes1")
    portal.portal_header_html = _read_repo("hardware/portal_header.html")
    portal.alarms_html = (
        "<html><head></head><body>ALARMS-PAGE " + pt.PORTAL_HEADER_MARKER + "</body></html>"
    )
    srv = _Serve(portal)
    try:
        # unauthorized GET /alarms serves the LOGIN_PAGE (not a JSON 401)
        r = requests.get(srv.url + "/alarms", timeout=5)
        assert r.status_code == 200 and "Passcode" in r.text and "ALARMS-PAGE" not in r.text
        # authorized -> the alarms page, its own nav link marked active
        s = requests.Session()
        s.post(srv.url + "/login", json={"passcode": "tomatoes1"}, timeout=5)
        r = s.get(srv.url + "/alarms", timeout=5)
        assert r.status_code == 200 and "ALARMS-PAGE" in r.text
        assert 'id="nav-alarms" class="active"' in r.text
    finally:
        srv.close()


# ===========================================================================
# TIMELAPSE EXPORT  (admin-only GIF/MP4 render -> poll -> download)
# ===========================================================================

def test_timelapse_endpoints_require_admin(h):
    assert requests.post(h.url + "/api/timelapse/render", json={}, timeout=5).status_code == 401
    assert requests.get(h.url + "/api/timelapse/status?job=x", timeout=5).status_code == 401
    assert requests.get(h.url + "/api/timelapse/download?job=x", timeout=5).status_code == 401


def test_start_render_busy(tmp_path):
    # One render at a time: a second start while active is refused with "busy".
    portal = _wizard_portal(tmp_path, provisioned=True)
    portal._render_active = True
    job_id, err = portal.start_render({"format": "gif"})
    assert job_id is None and err == "busy"


def test_start_render_single_winner_under_concurrency(tmp_path, monkeypatch):
    # The check-then-set of _render_active is lock-guarded, so when many admin POSTs race
    # (ThreadingHTTPServer runs each on its own thread) EXACTLY ONE launches and the rest
    # get "busy". Without the lock this is a TOCTOU and multiple renders can start.
    import threading
    portal = _wizard_portal(tmp_path, provisioned=True)
    gate = threading.Event()

    def blocking_run(job_id, opts):
        # Stand in for the worker: hold _render_active True until released, so every
        # racing caller after the winner sees the flag set.
        gate.wait(2.0)
        with portal._render_lock:
            portal._render_active = False

    monkeypatch.setattr(portal, "_run_render", blocking_run)

    results, rlock = [], threading.Lock()

    def fire():
        jid, err = portal.start_render({"format": "gif"})
        with rlock:
            results.append((jid, err))

    threads = [threading.Thread(target=fire) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(3)
    gate.set()

    winners = [r for r in results if r[0] is not None]
    assert len(winners) == 1, results                       # exactly one render launched
    assert all(err == "busy" for jid, err in results if jid is None)


def test_timelapse_render_poll_download(tmp_path, monkeypatch):
    """End-to-end portal flow: POST render -> background job pulls the filtered archive
    images -> status reports progress -> download streams the file as an attachment. The
    encoder itself is faked so the flow doesn't depend on Pillow/cv2 (the real encoders
    are unit-tested in test_timelapse.py)."""
    import json
    import time as _t
    portal = _wizard_portal(tmp_path, provisioned=True, set_passcode="tomatoes1")
    events = [
        {"date": "2026-06-22", "time": "08:00:00", "action": "none", "species": "none",
         "device": "cam-a", "key": "g/g/evidence/2026/06/22/a.jpg"},
        {"date": "2026-06-22", "time": "09:00:00", "action": "mitigate", "species": "raccoon",
         "device": "cam-a", "key": "g/g/evidence/2026/06/22/b.jpg"},
    ]

    def fake_proxy_get(path):
        if path == "/api/archive/days":
            return (200, "application/json", json.dumps({"days": ["2026-06-22"]}).encode())
        if path.startswith("/api/archive?"):
            return (200, "application/json", json.dumps({"events": events}).encode())
        if path.startswith("/api/archive/image"):
            return (200, "image/jpeg", b"\xff\xd8JPEG\xff\xd9")
        return (404, "application/json", b"{}")

    monkeypatch.setattr(portal, "proxy_get", fake_proxy_get)

    def fake_encode(frames_bytes, out_path, *, fmt="gif", fps=8, width=640, on_frame=None):
        n = 0
        for _b in frames_bytes:        # exercise the fetch loop + progress callback
            n += 1
            if on_frame:
                on_frame(n)
        with open(out_path, "wb") as f:
            f.write(b"RENDERED:" + str(n).encode())
        return out_path

    monkeypatch.setattr(pt.tl, "encode", fake_encode)

    srv = _Serve(portal)
    try:
        s = requests.Session()
        s.post(srv.url + "/login", json={"passcode": "tomatoes1"}, timeout=5)
        r = s.post(srv.url + "/api/timelapse/render",
                   json={"from": "2026-06-22", "to": "2026-06-22", "format": "gif",
                         "fps": 4, "width": 480}, timeout=5)
        assert r.status_code == 200
        job = r.json()["job_id"]

        st = {}
        for _ in range(100):
            st = s.get(srv.url + "/api/timelapse/status?job=" + job, timeout=5).json()
            if st["state"] in ("done", "error"):
                break
            _t.sleep(0.02)
        assert st["state"] == "done", st
        assert st["total"] == 2 and st["frames_done"] == 2
        # small, non-truncated archive => nothing flagged partial
        assert st.get("capped_partial") is False and st.get("capped_frames") is False

        d = s.get(srv.url + "/api/timelapse/download?job=" + job, timeout=5)
        assert d.status_code == 200
        assert "attachment" in d.headers.get("Content-Disposition", "")
        assert d.content == b"RENDERED:2"
    finally:
        srv.close()


def test_mp4_request_uses_gif_cap_when_cv2_absent(tmp_path, monkeypatch):
    """On a Pi without cv2 an MP4 request falls back to the GIF encoder, so the frame
    SELECTION must use the GIF cap up front — otherwise it would select up to the 1800
    MP4 cap and fail late in encode_gif at 240 with a self-contradictory error."""
    import json
    import time as _t
    portal = _wizard_portal(tmp_path, provisioned=True, set_passcode="tomatoes1")
    # A day with more frames than the GIF cap but well under the MP4 cap.
    n_events = pt.tl.GIF_MAX_FRAMES + 60
    events = [{"date": "2026-06-22", "time": "%02d:%02d:00" % (i // 60, i % 60),
               "action": "none", "species": "none", "device": "cam-a",
               "key": "g/g/evidence/2026/06/22/%03d.jpg" % i} for i in range(n_events)]

    def fake_proxy_get(path):
        if path == "/api/archive/days":
            return (200, "application/json", json.dumps({"days": ["2026-06-22"]}).encode())
        if path.startswith("/api/archive?"):
            return (200, "application/json", json.dumps({"events": events}).encode())
        if path.startswith("/api/archive/image"):
            return (200, "image/jpeg", b"\xff\xd8JPEG\xff\xd9")
        return (404, "application/json", b"{}")

    monkeypatch.setattr(portal, "proxy_get", fake_proxy_get)
    monkeypatch.setattr(pt.tl, "have_mp4", lambda: False)        # no OpenCV on this Pi
    seen = {}

    def fake_encode(frames_bytes, out_path, *, fmt="gif", fps=8, width=640, on_frame=None):
        n = sum(1 for _ in frames_bytes)
        seen["fmt"], seen["n"] = fmt, n
        with open(out_path, "wb") as f:
            f.write(b"X")
        return out_path

    monkeypatch.setattr(pt.tl, "encode", fake_encode)

    srv = _Serve(portal)
    try:
        s = requests.Session()
        s.post(srv.url + "/login", json={"passcode": "tomatoes1"}, timeout=5)
        r = s.post(srv.url + "/api/timelapse/render", json={"format": "mp4"}, timeout=5)
        assert r.status_code == 200
        job = r.json()["job_id"]
        st = {}
        for _ in range(100):
            st = s.get(srv.url + "/api/timelapse/status?job=" + job, timeout=5).json()
            if st["state"] in ("done", "error"):
                break
            _t.sleep(0.02)
        assert st["state"] == "done", st
        # Selected with the GIF cap, NOT the 1800 MP4 cap; encoder ran as gif; partial flagged.
        assert seen["fmt"] == "gif" and seen["n"] == pt.tl.GIF_MAX_FRAMES
        assert st["total"] == pt.tl.GIF_MAX_FRAMES and st["capped_frames"] is True
        assert st["format"] == "gif"
    finally:
        srv.close()


def test_timelapse_status_unknown_job_404(tmp_path):
    portal = _wizard_portal(tmp_path, provisioned=True, set_passcode="tomatoes1")
    srv = _Serve(portal)
    try:
        s = requests.Session()
        s.post(srv.url + "/login", json={"passcode": "tomatoes1"}, timeout=5)
        assert s.get(srv.url + "/api/timelapse/status?job=nope", timeout=5).status_code == 404
        assert s.get(srv.url + "/api/timelapse/download?job=nope", timeout=5).status_code == 404
    finally:
        srv.close()


# ===========================================================================
# RETENTION SWEEP — the cron + admin Storage page  (Part 3)
# ===========================================================================

def test_storage_page_and_maintenance_require_admin(h):
    # The /storage PAGE shows the login form (200) when unauthed; the /api/* stay JSON 401.
    r = requests.get(h.url + "/storage", timeout=5)
    assert r.status_code == 200 and "Passcode" in r.text
    assert requests.get(h.url + "/api/maintenance", timeout=5).status_code == 401
    assert requests.post(h.url + "/api/maintenance/run-now", timeout=5).status_code == 401
    assert requests.post(h.url + "/api/settings/retention", json={"retention_days": 7, "prune_hour": 2},
                         timeout=5).status_code == 401


def test_capture_endpoints_require_admin(h):
    assert requests.get(h.url + "/api/capture", timeout=5).status_code == 401
    assert requests.post(h.url + "/api/settings/capture",
                         json={"interval_s": 60, "quality": "saver"}, timeout=5).status_code == 401


def _provisioned_portal(tmp_path, *, edge):
    """A normal-mode portal backed by a real PiConfig (passcode set, provisioned),
    so maintenance settings persist to pi-garden.json."""
    pc = pt.pi_cfg.PiConfig(str(tmp_path))
    pc.set_passcode("hunter2")
    pc.save_partial({"provisioned": True, "garden": {"garden_id": "g1"}})
    return pt.Portal(session_secret=b"test-secret", edge=edge, dashboard_html="<html></html>",
                     storage_html="<html>storage</html>", pi_config=pc,
                     garden_id="g1", garden_token="tok-1",
                     rate_limiter=pt.RateLimiter())


def test_maintenance_settings_defaults_and_persist(tmp_path):
    p = _provisioned_portal(tmp_path, edge="http://unused")
    assert p.maintenance_settings() == {"retention_days": 30, "prune_hour": 3, "last_prune_date": None}
    p.set_maintenance_settings(7, 2)
    st = p.maintenance_settings()
    assert st["retention_days"] == 7 and st["prune_hour"] == 2
    # clamps out-of-range
    p.set_maintenance_settings(99999, 99)
    assert p.maintenance_settings() == {"retention_days": 3650, "prune_hour": 23, "last_prune_date": None}
    # survives a fresh PiConfig (== persisted to disk)
    p2 = pt.Portal(pi_config=pt.pi_cfg.PiConfig(str(tmp_path)))
    assert p2.maintenance_settings()["retention_days"] == 3650


def test_run_prune_success_and_error(tmp_path, monkeypatch):
    # Prune deletes DIRECTLY from FOS on the Pi (boto3), like the wipe — not the edge.
    p = _provisioned_portal(tmp_path, edge="http://unused")
    monkeypatch.setattr(p, "_fos_svc", lambda: {"service_id": "s", "fos_region": "r",
                        "fos_bucket": "b", "fos_access_key_id": "ak", "fos_secret_access_key": "sk"})
    monkeypatch.setattr(pt.usage_stats, "fos_prune",
                        lambda svc, prefix, cutoff, **kw: {"deleted": 12, "days_pruned": 2,
                                                           "failed": 0, "remaining": False, "sample": None})
    res = p.run_prune(days=30, trigger="manual")
    assert res["ok"] is True and res["deleted"] == 12 and res["remaining"] is False

    # FOS unavailable -> graceful failure, never raises
    monkeypatch.setattr(pt.usage_stats, "fos_prune", lambda *a, **k: None)
    res = p.run_prune(trigger="schedule")
    assert res["ok"] is False and "cloud storage" in res["error"]


def test_run_prune_uses_utc_cutoff_for_configured_days(tmp_path, monkeypatch):
    import datetime as _dt
    p = _provisioned_portal(tmp_path, edge="http://unused")
    p.set_maintenance_settings(7, 2)
    monkeypatch.setattr(p, "_fos_svc", lambda: {"service_id": "s", "fos_region": "r",
                        "fos_bucket": "b", "fos_access_key_id": "ak", "fos_secret_access_key": "sk"})
    seen = {}
    def capture(svc, prefix, cutoff, **kw):
        seen["prefix"] = prefix
        seen["cutoff"] = cutoff
        return {"deleted": 0, "days_pruned": 0, "failed": 0, "remaining": False, "sample": None}
    monkeypatch.setattr(pt.usage_stats, "fos_prune", capture)
    p.run_prune()  # days=None -> use the configured 7
    assert seen["prefix"] == "g/g1/evidence/"
    expected = (_dt.datetime.now(_dt.timezone.utc).date() - _dt.timedelta(days=7)).isoformat()
    assert seen["cutoff"] == expected   # cutoff = UTC today - retention days


def test_run_wipe_deletes_pi_side(tmp_path, monkeypatch):
    # The wipe deletes DIRECTLY from FOS on the Pi (boto3), not via the edge (which caps
    # backend sends per execution). run_wipe resolves FOS creds + calls usage_stats.fos_wipe.
    p = _provisioned_portal(tmp_path, edge="http://unused")
    monkeypatch.setattr(p, "_fos_svc", lambda: {"service_id": "svc", "fos_region": "r",
                                                "fos_bucket": "b", "fos_access_key_id": "ak",
                                                "fos_secret_access_key": "sk"})
    seen = {}
    def fake_wipe(svc, prefix, *, max_objects=None):
        seen["prefix"] = prefix
        return {"deleted": 9, "failed": 2, "remaining": True, "sample": "x: y"}
    monkeypatch.setattr(pt.usage_stats, "fos_wipe", fake_wipe)
    res = p.run_wipe(trigger="manual")
    assert seen["prefix"] == "g/g1/"      # scoped to THIS garden's prefix
    assert res["ok"] is True and res["deleted"] == 9 and res["remaining"] is True
    assert res["failed"] == 2             # FOS delete failures surface (not silently dropped)

    # FOS not configured (no creds) -> graceful failure, never raises
    monkeypatch.setattr(pt.usage_stats, "fos_wipe", lambda *a, **k: None)
    res = p.run_wipe(trigger="manual")
    assert res["ok"] is False and "cloud storage" in res["error"]


def test_capture_settings_defaults_and_persist(tmp_path):
    p = _provisioned_portal(tmp_path, edge="http://unused")
    base = p.capture_settings()
    assert base["interval_s"] == 30 and base["quality"] == "standard"
    p.set_capture_settings(60, "saver")
    s = p.capture_settings()
    assert s["interval_s"] == 60 and s["quality"] == "saver"
    # clamps interval and rejects an unknown quality (-> default)
    p.set_capture_settings(99999, "bogus")
    s = p.capture_settings()
    assert s["interval_s"] == 3600 and s["quality"] == "standard"
    p.set_capture_settings(1, "high")   # below the 5s floor -> clamps to 5
    assert p.capture_settings()["interval_s"] == 5
    # survives a fresh PiConfig (== persisted to disk)
    p2 = pt.Portal(pi_config=pt.pi_cfg.PiConfig(str(tmp_path)))
    assert p2.capture_settings()["quality"] == "high"


def test_capture_settings_daylight_only(tmp_path):
    p = _provisioned_portal(tmp_path, edge="http://unused")
    # New defaults: daylight-only on, night = motion capture.
    s = p.capture_settings()
    assert s["daylight_only"] is True and s["night_mode"] == "motion"
    assert s["dark_below"] == pt.pi_cfg.DEFAULT_DARK_BELOW
    # Persist a partial update of just the daylight knobs (interval/quality untouched).
    p.set_capture_settings(60, "standard", daylight_only=False, night_mode="pause", dark_below=300)
    s = p.capture_settings()
    assert s["daylight_only"] is False and s["night_mode"] == "pause"
    assert s["dark_below"] == 255  # clamped to 0..255
    # Bad night_mode falls back to the default; omitting a knob leaves it unchanged.
    p.set_capture_settings(60, "standard", night_mode="bogus")
    assert p.capture_settings()["night_mode"] == pt.pi_cfg.DEFAULT_NIGHT_MODE
    assert p.capture_settings()["daylight_only"] is False  # unchanged (not passed)


def test_capture_cost_estimates_table(tmp_path):
    p = _provisioned_portal(tmp_path, edge="http://unused")
    # no cameras yet -> count 0, table still present (all $0)
    est0 = p.capture_cost_estimates()
    assert est0["cameras"] == 0 and est0["table"]["30|standard"]["monthly_usd"] == 0
    # add two cameras (+ a non-camera + a removed camera that must NOT count)
    p.pi_config.save_partial({"devices": [
        {"device_id": "csi", "type": "camera_csi"},
        {"device_id": "usb", "type": "camera_usb"},
        {"device_id": "relay", "type": "deterrent_relay"},
        {"device_id": "old", "type": "camera_usb", "status": "removed"},
    ]})
    est = p.capture_cost_estimates()
    assert est["cameras"] == 2 and est["retention_days"] == 30
    # 2 cameras @ 30s -> 5760 photos/day; saver cheaper than standard cheaper than high
    cell = est["table"]["30|standard"]
    assert cell["photos_per_day"] == 5760 and cell["monthly_usd"] > 0
    iv = "30|"
    assert est["table"][iv + "saver"]["monthly_usd"] < est["table"][iv + "standard"]["monthly_usd"] \
        < est["table"][iv + "high"]["monthly_usd"]
    # more frequent costs more
    assert est["table"]["15|standard"]["monthly_usd"] > est["table"]["300|standard"]["monthly_usd"]
    # Per-operation breakdown (same detail/shape as the Costs page): storage + FOS Class A
    # upload + KV writes, each with a unit-rate string; the three costs sum to the monthly.
    ops = {o["label"]: o for o in cell["ops"]}
    assert ops["Photos uploaded — FOS Class A PUT"]["per_mo"] == 5760 * 30
    assert ops["Notes — KV Class A writes (2 / photo)"]["per_mo"] == 5760 * 30 * 2
    assert "1,000 ops" in ops["Photos uploaded — FOS Class A PUT"]["rate"]
    assert "100,000 ops" in ops["Notes — KV Class A writes (2 / photo)"]["rate"]
    assert "GB-month" in ops["Storage (GB-month)"]["rate"]
    # (table monthly_usd is rounded to 4dp; op costs are raw -> compare loosely)
    assert sum(o["cost"] for o in cell["ops"]) == pytest.approx(cell["monthly_usd"], abs=1e-3)


def test_storage_footprint_scoped_cached_and_invalidated(tmp_path, monkeypatch):
    p = _provisioned_portal(tmp_path, edge="http://unused")
    # No fastly service id -> no cloud storage set up yet -> available: False (never raises).
    assert p.storage_footprint() == {
        "available": False, "garden_id": "g1", "note": "No cloud storage is set up yet."}

    # With a service id, it lists ONLY this garden's prefix and reports the totals.
    p.pi_config.save_partial({"fastly": {"service_id": "SVC123"}})
    p._footprint_cache = None  # drop the cached "not set up yet"
    seen = {}
    def fake_inv(svc, garden_prefix=None):
        seen["prefix"] = garden_prefix
        return {"objects": 1234, "bytes": 5_000_000_000}
    monkeypatch.setattr(pt.usage_stats, "fos_inventory", fake_inv)
    fp = p.storage_footprint()
    assert seen["prefix"] == "g/g1/"  # this garden only, not the whole bucket
    assert fp == {"available": True, "garden_id": "g1",
                  "objects": 1234, "bytes": 5_000_000_000}

    # Cached: a second call does NOT re-list even if the bucket would now answer differently.
    monkeypatch.setattr(pt.usage_stats, "fos_inventory",
                        lambda *a, **k: {"objects": 0, "bytes": 0})
    assert p.storage_footprint()["objects"] == 1234

    # A successful prune busts the cache so the page shows the freed space next load.
    monkeypatch.setattr(pt.usage_stats, "fos_prune",
                        lambda svc, prefix, cutoff, **kw: {"deleted": 9, "days_pruned": 1,
                                                           "failed": 0, "remaining": False, "sample": None})
    p.run_prune(days=30, trigger="manual")
    assert p.storage_footprint()["objects"] == 0


def test_capture_http_roundtrip_no_restart(tmp_path, monkeypatch):
    # Saving capture settings must NOT bounce the camera daemon: the daemon hot-reads
    # them. Restarting garden-camera cascades (Wants=garden-update) into a full-stack
    # restart that kills the in-flight save ("Failed to fetch"), so this guards it.
    calls = []
    monkeypatch.setattr(pt.subprocess, "run", lambda *a, **k: calls.append(a) or None)
    portal = _provisioned_portal(tmp_path, edge="http://unused")
    server = ThreadingHTTPServer(("127.0.0.1", 0), pt.make_handler(portal))
    url = f"http://127.0.0.1:{server.server_address[1]}"
    t = threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True)
    t.start()
    try:
        s = requests.Session()
        s.post(url + "/login", data={"passcode": "hunter2"}, allow_redirects=False, timeout=5)
        # GET defaults + the preset list
        st = s.get(url + "/api/capture", timeout=5).json()
        assert st["interval_s"] == 30 and st["quality"] == "standard"
        assert "standard" in st["presets"] and "saver" in st["presets"]
        assert "table" in st["estimates"] and "30|standard" in st["estimates"]["table"]
        # set -> persists AND bounces the camera daemon so it takes effect now
        r = s.post(url + "/api/settings/capture", json={"interval_s": 300, "quality": "saver"}, timeout=5)
        assert r.status_code == 200 and r.json()["interval_s"] == 300 and r.json()["quality"] == "saver"
        assert s.get(url + "/api/capture", timeout=5).json()["quality"] == "saver"
        assert not calls, "saving capture settings must NOT restart the daemon (it hot-reads)"
        # bad input rejected
        assert s.post(url + "/api/settings/capture", json={"interval_s": 2, "quality": "saver"},
                      timeout=5).status_code == 400
        assert s.post(url + "/api/settings/capture", json={"interval_s": 60, "quality": "nope"},
                      timeout=5).status_code == 400
    finally:
        server.shutdown(); server.server_close()


def test_should_prune_now():
    import time as _t
    struct = _t.struct_time((2026, 6, 22, 4, 0, 0, 0, 0, -1))  # 04:00 local, 2026-06-22
    assert pt._should_prune_now({"prune_hour": 3, "last_prune_date": None}, struct) is True
    assert pt._should_prune_now({"prune_hour": 5, "last_prune_date": None}, struct) is False  # before the hour
    assert pt._should_prune_now({"prune_hour": 3, "last_prune_date": "2026-06-22"}, struct) is False  # already today


def test_maintenance_http_roundtrip_and_run_now(tmp_path, monkeypatch):
    edge = FakeEdge(armed=True)
    edge_url = edge.start()
    edge.prune_result = {"deleted": 5, "days_pruned": 1, "remaining": False, "cutoff": "2026-05-23"}
    portal = _provisioned_portal(tmp_path, edge=edge_url)
    server = ThreadingHTTPServer(("127.0.0.1", 0), pt.make_handler(portal))
    url = f"http://127.0.0.1:{server.server_address[1]}"
    t = threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True)
    t.start()
    try:
        s = requests.Session()
        s.post(url + "/login", data={"passcode": "hunter2"}, allow_redirects=False, timeout=5)
        # GET settings (defaults + edge configured because garden_token is set)
        st = s.get(url + "/api/maintenance", timeout=5).json()
        assert st["retention_days"] == 30 and st["edge_configured"] is True
        # storage page serves
        assert s.get(url + "/storage", timeout=5).status_code == 200
        # set retention
        r = s.post(url + "/api/settings/retention", json={"retention_days": 14, "prune_hour": 1}, timeout=5)
        assert r.status_code == 200 and r.json()["retention_days"] == 14
        assert s.get(url + "/api/maintenance", timeout=5).json()["retention_days"] == 14
        # Both prune (run-now) and wipe run Pi-side via boto3 now — stub the FOS layer.
        prune_calls, wipe_calls = [], []
        monkeypatch.setattr(portal, "_fos_svc", lambda: {"service_id": "s", "fos_region": "r",
                            "fos_bucket": "b", "fos_access_key_id": "ak", "fos_secret_access_key": "sk"})
        monkeypatch.setattr(pt.usage_stats, "fos_prune",
                            lambda svc, prefix, cutoff, **kw: prune_calls.append(prefix) or
                            {"deleted": 5, "days_pruned": 1, "failed": 0, "remaining": False, "sample": None})
        monkeypatch.setattr(pt.usage_stats, "fos_wipe",
                            lambda svc, prefix, **kw: wipe_calls.append(prefix) or
                            {"deleted": 7, "failed": 0, "remaining": False, "sample": None})
        # run now -> Pi-side prune of THIS garden's evidence prefix, with the configured days
        r = s.post(url + "/api/maintenance/run-now", timeout=5)
        assert r.status_code == 200 and r.json()["deleted"] == 5
        assert prune_calls == ["g/g1/evidence/"]
        # bad input rejected
        assert s.post(url + "/api/settings/retention", json={"retention_days": 0, "prune_hour": 99},
                      timeout=5).status_code == 400
        # the maintenance payload exposes garden_id for the Danger-zone confirm
        assert s.get(url + "/api/maintenance", timeout=5).json()["garden_id"] == "g1"
        # wipe-all needs the garden id typed EXACTLY — a refused confirm must NOT delete.
        assert s.post(url + "/api/maintenance/wipe-all", json={}, timeout=5).status_code == 400
        assert s.post(url + "/api/maintenance/wipe-all", json={"confirm": "nope"}, timeout=5).status_code == 400
        assert wipe_calls == []   # a bad confirm never triggers a delete
        # correct confirm -> Pi-side wipe runs
        r = s.post(url + "/api/maintenance/wipe-all", json={"confirm": "g1"}, timeout=5)
        assert r.status_code == 200 and r.json()["deleted"] == 7
        assert wipe_calls == ["g/g1/"]
    finally:
        server.shutdown(); server.server_close(); edge.stop()


def test_get_logs_reads_wal_telemetry_db(h, tmp_path, monkeypatch):
    """Regression: the telemetry writer runs in WAL mode, so /api/logs must NOT open the DB
    with immutable=1 — that flag makes SQLite IGNORE the -wal file. With a barely-checkpointed
    DB (all data still in the WAL) the old reader saw an empty/absent schema and 500'd with
    'no such table: events' — the Storage page's Recent-cleanups list. mode=ro reads the WAL."""
    import sqlite3
    db = str(tmp_path / "telemetry.db")
    w = sqlite3.connect(db)
    w.execute("PRAGMA journal_mode=WAL")
    w.execute("PRAGMA wal_autocheckpoint=0")  # keep data in the -wal; main .db stays schema-less
    w.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, ts REAL, cid TEXT, garden_id TEXT, "
              "device_id TEXT, node_id TEXT, component TEXT, op TEXT, caller TEXT, args TEXT, "
              "dur_ms INTEGER, outcome TEXT, detail TEXT)")
    w.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
    w.execute("INSERT INTO events (ts, component, op, outcome, detail) VALUES (?,?,?,?,?)",
              (1.0, "maintenance", "archive.prune", "ok", "Deleted 3 old photos"))
    w.commit()  # committed to the WAL, but NOT checkpointed into the main .db
    try:
        # Precondition: the OLD immutable reader genuinely can't see WAL-only data.
        imm = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
        with pytest.raises(sqlite3.OperationalError):
            imm.execute("SELECT COUNT(*) FROM events").fetchone()
        imm.close()

        monkeypatch.setenv("GP_TELEMETRY_DB", db)
        s = requests.Session()
        s.post(h.url + "/login", data={"passcode": "hunter2"}, timeout=5)
        r = s.get(h.url + "/api/logs?component=maintenance&limit=20", timeout=5)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 1 and body["events"][0]["op"] == "archive.prune"
    finally:
        w.close()


def test_proxy_get_short_ttl_cache_and_bust(tmp_path, monkeypatch):
    """Hot dashboard reads are cached briefly so the page fills fast instead of re-hitting
    the edge on every poll/tab; non-cacheable paths always pass through; any mutation
    (proxy_post) clears the cache so the next read is fresh."""
    p = _provisioned_portal(tmp_path, edge="http://unused")
    calls = {"n": 0}

    class FakeEdge:
        def proxy_get(self, path, **kw):
            calls["n"] += 1
            return (200, "application/json", b'{"armed": true}')

        def proxy_post(self, path, body, **kw):
            return (200, "application/json", b"{}")

    monkeypatch.setattr(p, "_edge", lambda: FakeEdge())

    assert p.proxy_get("/api/state")[0] == 200   # miss -> edge
    assert p.proxy_get("/api/state")[0] == 200   # hit -> cache
    assert calls["n"] == 1
    # /api/snapshot is live + cache-busted -> never cached
    p.proxy_get("/api/snapshot?device=x")
    p.proxy_get("/api/snapshot?device=x")
    assert calls["n"] == 3
    # a mutation clears the read cache -> the next state read hits the edge again
    p.proxy_post("/api/control", b'{"cmd":"arm"}')
    assert p.proxy_get("/api/state")[0] == 200
    assert calls["n"] == 4
    # an error response is not cached (so a blip doesn't stick)
    monkeypatch.setattr(p, "_edge", lambda: type("E", (), {
        "proxy_get": lambda self, path, **kw: (502, "application/json", b"{}")})())
    p.proxy_get("/api/cameras")
    p.proxy_get("/api/cameras")  # still passes through (502 not cached)
    # (no assertion on count here — different fake; the point is no exception + no caching)


# ===========================================================================
# DEFENSE-IN-DEPTH: LAN guard on the admin surfaces + login (PI-007 finding 1)
# ===========================================================================

def test_lan_guard_blocks_admin_and_login_off_lan(h, monkeypatch):
    """The admin routes + /login are LAN-only (deploy promises "LAN-only management").
    The passcode is still the primary gate, but an unfirewalled global IPv6 must not be
    able to reach /login or an admin route at all. Simulate an OFF-LAN client by making
    is_lan_addr return False; every gated surface should answer 403 "local network only"
    — and the *login page* form still gets HTML, not a JSON blob."""
    monkeypatch.setattr(pt, "is_lan_addr", lambda *a, **k: False)
    # Admin API route -> JSON 403 (not the usual 401), gated before the passcode check.
    r = requests.get(h.url + "/api/logs", timeout=5)
    assert r.status_code == 403, r.status_code
    assert r.json().get("error") == "local network only"
    # Login (form) -> 403 with the login PAGE (browser-friendly), no passcode accepted.
    r = requests.post(h.url + "/login", data={"passcode": "hunter2"},
                      allow_redirects=False, timeout=5)
    assert r.status_code == 403, r.status_code
    assert "Passcode" in r.text  # rendered the login page, not JSON
    # Login (JSON API) -> JSON 403.
    r = requests.post(h.url + "/login", json={"passcode": "hunter2"}, timeout=5)
    assert r.status_code == 403 and r.json().get("error") == "local network only"
    # And even a correct passcode does NOT mint a session off-LAN.
    assert "Set-Cookie" not in r.headers


def test_lan_guard_allows_loopback(h):
    """The guard is additive — a real LAN client (the harness comes from 127.0.0.1, a
    loopback address is_lan_addr accepts) still logs in and reaches an admin route."""
    s = requests.Session()
    r = s.post(h.url + "/login", data={"passcode": "hunter2"},
               allow_redirects=False, timeout=5)
    assert r.status_code == 303  # logged in over the LAN, no 403
    assert s.get(h.url + "/devices", timeout=5).status_code != 403


# ===========================================================================
# REQUEST BODY-SIZE CAP (PI-007 finding 4)
# ===========================================================================

def test_oversized_body_413(h):
    """A POST whose Content-Length exceeds MAX_BODY_BYTES is rejected with 413 (the
    resource-constrained Pi must not over-allocate for a giant pre-auth body). The 413
    is decided from the Content-Length header alone — _read_body never reads/allocates
    the oversized body — so the response comes back without consuming the payload."""
    big = b"x" * (pt.MAX_BODY_BYTES + 1)
    r = requests.post(h.url + "/login", data=big,
                      headers={"Content-Type": "application/x-www-form-urlencoded"},
                      timeout=5)
    assert r.status_code == 413, r.status_code
    assert r.json().get("error") == "request too large"


def test_normal_body_still_accepted(h):
    """A normal small login body is unaffected by the cap (sanity that the ceiling
    doesn't reject legitimate traffic)."""
    r = requests.post(h.url + "/login", data={"passcode": "hunter2"},
                      allow_redirects=False, timeout=5)
    assert r.status_code == 303  # accepted + logged in, not 413


# ===========================================================================
# REGISTRY SINGLE-WRITER LOCK (PI-007 finding 3)
# ===========================================================================

def _lock_ctx(tmp_path):
    """A registry ctx whose mirror + lock live in tmp_path; KV is stubbed so the test
    exercises the on-disk mirror path (the authoritative source) without a real Fastly."""
    return {
        "service_id": "svc-test",
        "configs_dir": str(tmp_path),
        "garden_state_store_id": "store-1",
        "token": "tok",
    }


def test_registry_write_roundtrip_under_lock(tmp_path, monkeypatch):
    """A normal load→mutate→save round-trip still works with the flock in place: write
    the gardens + devices blobs, read them back from the authoritative on-disk mirror."""
    from provision import fastly_api, registry
    monkeypatch.setattr(fastly_api, "kv_put", lambda *a, **k: None)
    monkeypatch.setattr(fastly_api, "kv_get", lambda *a, **k: None)
    ctx = _lock_ctx(tmp_path)

    gardens = registry.upsert_garden(registry.empty_gardens(), "backyard", "Backyard", "UTC", ts=1)
    registry.write_gardens(ctx, gardens)
    devices = registry.add_device(registry.empty_devices("backyard"),
                                  device_id="cam1", node_id="n1", kind="observer",
                                  dev_type="camera_usb", name="Front", ts=1)
    registry.write_devices(ctx, "backyard", devices)

    assert any(g["garden_id"] == "backyard" for g in registry.read_gardens(ctx)["gardens"])
    rd = registry.read_devices(ctx, "backyard")
    assert [d["device_id"] for d in rd["devices"]] == ["cam1"]
    # The sentinel lock file is a SEPARATE file from the mirror (so the lock fd is never
    # the file we rewrite) and both live under the configs dir.
    assert (tmp_path / "svc-test-registry.json").exists()
    assert (tmp_path / "svc-test-registry.lock").exists()


def test_registry_lock_serializes_concurrent_writers(tmp_path, monkeypatch):
    """The invariant is "one writer PROCESS at a time", ENFORCED by the flock — not a
    convention. Two threads each write a DIFFERENT half of the same mirror with an
    artificial delay between load and save (the classic lost-update window). Because
    write_* re-reads the whole mirror, an UNSERIALIZED interleave would drop one half;
    the lock makes them serialize, so BOTH survive.

    (Threads stand in for the portal's per-request `provision.cli` SUBPROCESSES — flock
    is cross-process, but a same-process thread race is the strictest, most reproducible
    way to prove serialization in one test run.)"""
    import time as _time
    from provision import fastly_api, registry
    monkeypatch.setattr(fastly_api, "kv_put", lambda *a, **k: None)
    monkeypatch.setattr(fastly_api, "kv_get", lambda *a, **k: None)
    ctx = _lock_ctx(tmp_path)

    # Force the lost-update window: sleep between the mirror load and save so the two
    # writers WOULD interleave if nothing serialized them.
    real_save = registry._save_mirror

    def slow_save(c, mirror):
        _time.sleep(0.15)
        real_save(c, mirror)

    monkeypatch.setattr(registry, "_save_mirror", slow_save)

    errors = []

    def write_garden_half():
        try:
            g = registry.upsert_garden(registry.read_gardens(ctx), "g-alpha", "A", "UTC", ts=1)
            registry.write_gardens(ctx, g)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def write_devices_half():
        try:
            d = registry.add_device(registry.read_devices(ctx, "g-beta"),
                                    device_id="cam", node_id="n", kind="observer",
                                    dev_type="camera_usb", name="C", ts=1)
            registry.write_devices(ctx, "g-beta", d)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t1 = threading.Thread(target=write_garden_half)
    t2 = threading.Thread(target=write_devices_half)
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert not errors, errors
    # If the writers had interleaved, one half would have been clobbered. Both present
    # == the lock serialized them.
    assert any(g["garden_id"] == "g-alpha" for g in registry.read_gardens(ctx)["gardens"])
    assert registry.read_devices(ctx, "g-beta")["devices"], "devices half was lost"
