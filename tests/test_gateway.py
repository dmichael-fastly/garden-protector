"""Unit tests for the Tier-1 Pi gateway (hardware/gateway.py).

Focus: the FAIL-CLOSED spray decision. The pure core (local_precheck /
resolve_motion / rain_active / node_is_down / irrigation window) is exhaustively
covered, then handle_motion is driven against a stub edge to prove the wiring
(local veto short-circuits the edge; an edge error fails closed).
"""
import base64
import io
import json

import pytest

from hardware import gateway as gw


# --- local_precheck: order of authority + all withhold reasons -------------

def test_local_precheck_proceeds_when_clear():
    assert gw.local_precheck(armed=True, override_stop=False, maintenance=False,
                             irrigation=False, rain_active=False) is None


def test_local_precheck_order_of_authority():
    # disarmed outranks everything
    assert gw.local_precheck(False, True, True, True, True) == "disarmed"
    # stop outranks maintenance/irrigation/rain
    assert gw.local_precheck(True, True, True, True, True) == "stop"
    # maintenance outranks irrigation/rain
    assert gw.local_precheck(True, False, True, True, True) == "maintenance"
    # irrigation outranks rain
    assert gw.local_precheck(True, False, False, True, True) == "irrigation"
    # rain is the last local veto
    assert gw.local_precheck(True, False, False, False, True) == "rain"


# --- resolve_motion: the final spray decision is fail-closed ---------------

def test_resolve_motion_local_veto_never_sprays():
    assert gw.resolve_motion("maintenance", True, "mitigate", None, 3) == {
        "spray": False, "reason": "maintenance"}


def test_resolve_motion_edge_unreachable_fails_closed():
    assert gw.resolve_motion(None, False, "none", None, 3) == {
        "spray": False, "reason": "edge_unreachable"}


def test_resolve_motion_mitigate_sprays_requested_seconds():
    assert gw.resolve_motion(None, True, "mitigate", None, 3) == {"spray": True, "seconds": 3}


def test_resolve_motion_none_surfaces_edge_reason():
    assert gw.resolve_motion(None, True, "none", "human", 3) == {"spray": False, "reason": "human"}
    # no explicit reason -> generic veto
    assert gw.resolve_motion(None, True, "none", None, 3) == {"spray": False, "reason": "veto"}


# --- rain_active freshness ------------------------------------------------

def test_rain_active_only_fresh_and_raining():
    now = 1_000_000
    assert gw.rain_active({"raining": True, "last_local_ms": now - 5_000}, now) is True
    assert gw.rain_active({"raining": False, "last_local_ms": now}, now) is False
    stale = {"raining": True, "last_local_ms": now - (gw.RAIN_TELEMETRY_FRESH_SECS + 5) * 1000}
    assert gw.rain_active(stale, now) is False
    assert gw.rain_active({"raining": True}, now) is False  # no receipt stamp
    assert gw.rain_active({}, now) is False
    assert gw.rain_active({"raining": True, "last_local_ms": now + 5000}, now) is True  # skew


# --- node liveness --------------------------------------------------------

def test_node_is_down():
    now = 1_000_000
    assert gw.node_is_down(None, now) is True  # never seen
    assert gw.node_is_down(now - 10_000, now) is False
    assert gw.node_is_down(now - (gw.NODE_OFFLINE_AFTER_SECS + 1) * 1000, now) is True
    assert gw.node_is_down(now + 5000, now) is False  # future stamp (skew)


# --- irrigation window ----------------------------------------------------

def test_in_irrigation_window_simple_and_wrap():
    # 06:00-06:30 window
    assert gw.in_irrigation_window([(360, 390)], 365) is True
    assert gw.in_irrigation_window([(360, 390)], 400) is False
    # wraps midnight 23:30-00:30
    assert gw.in_irrigation_window([(1410, 30)], 1420) is True
    assert gw.in_irrigation_window([(1410, 30)], 10) is True
    assert gw.in_irrigation_window([(1410, 30)], 200) is False


def test_minutes_of_day_with_offset():
    # epoch 0 = 1970-01-01 00:00 UTC -> 0 minutes; +60 min offset -> 60
    assert gw.minutes_of_day(0, 0) == 0
    assert gw.minutes_of_day(0, 60) == 60
    assert gw.minutes_of_day(3661, 0) == 61  # 01:01:01


# --- GatewayState ---------------------------------------------------------

def test_state_fold_and_operating_params():
    st = gw.GatewayState(interval_s=300)
    st.set_arm_state(True, False)
    st.fold_telemetry({"raining": True, "temperature_c": 18.5}, 1_000_000)
    params = st.operating_params()
    assert params == {"interval_s": 300, "armed": True, "maintenance": False}
    # override_stop makes the node-facing "armed" false even if armed is true
    st.set_arm_state(True, True)
    assert st.operating_params()["armed"] is False


def test_state_motion_inputs_uses_irrigation_and_rain():
    st = gw.GatewayState(irrigation_windows=[(0, 1440)])  # all-day window
    st.set_arm_state(True, False)
    st.fold_telemetry({"raining": True}, 1_000_000)
    inp = st.motion_inputs(1_000_000)
    assert inp["armed"] is True
    assert inp["irrigation"] is True
    assert inp["rain_active"] is True


# --- handle_motion wiring (stub edge) -------------------------------------

class _StubEdge:
    node_id = "node-1"

    def __init__(self, action="mitigate", reason=None, raise_exc=False):
        self.action, self.reason, self.raise_exc = action, reason, raise_exc
        self.evidence_calls = 0

    def post_evidence(self, jpeg, trace_id):
        self.evidence_calls += 1
        if self.raise_exc:
            raise RuntimeError("edge down")
        return {"action": self.action, "reason": self.reason}

    def post_telemetry(self, blob, trace_id):
        return {}

    def get_state(self, trace_id):
        return {"armed": True, "override_stop": False}

    def post_alert(self, event, node_id, last_seen_ms, detail=None, trace_id=None):
        self.alerts = getattr(self, "alerts", [])
        self.alerts.append((event, node_id))
        return {"dispatched": False, "reason": "not_configured"}


def _motion_body(scene_bytes=b"jpegbytes", raining=False):
    return {
        "bed": 1,
        "jpeg_b64": base64.b64encode(scene_bytes).decode(),
        "telemetry": {"raining": raining},
    }


def _gateway(edge, **state_kw):
    st = gw.GatewayState(**state_kw)
    return gw.Gateway(st, edge, gw.Alerter()), st


def test_handle_motion_armed_mitigate_sprays():
    edge = _StubEdge(action="mitigate")
    g, st = _gateway(edge, spray_seconds=3)
    st.set_arm_state(True, False)
    out = g.handle_motion(_motion_body())
    assert out == {"spray": True, "seconds": 3}
    assert edge.evidence_calls == 1
    assert st.snapshot()["counters"]["sprays"] == 1


def test_handle_motion_disarmed_short_circuits_edge():
    edge = _StubEdge(action="mitigate")
    g, st = _gateway(edge)
    st.set_arm_state(False, False)  # disarmed
    out = g.handle_motion(_motion_body())
    assert out == {"spray": False, "reason": "disarmed"}
    assert edge.evidence_calls == 0  # never spent an edge call


def test_handle_motion_edge_error_fails_closed():
    edge = _StubEdge(raise_exc=True)
    g, st = _gateway(edge)
    st.set_arm_state(True, False)
    out = g.handle_motion(_motion_body())
    assert out == {"spray": False, "reason": "edge_unreachable"}


def test_handle_motion_incident_rain_vetoes_locally():
    edge = _StubEdge(action="mitigate")
    g, st = _gateway(edge)
    st.set_arm_state(True, False)
    out = g.handle_motion(_motion_body(raining=True))  # rain reading rides the incident
    assert out == {"spray": False, "reason": "rain"}
    assert edge.evidence_calls == 0  # local rain veto is the authoritative fast path


def test_handle_motion_human_veto_from_edge():
    edge = _StubEdge(action="none", reason="human")
    g, st = _gateway(edge)
    st.set_arm_state(True, False)
    out = g.handle_motion(_motion_body())
    assert out == {"spray": False, "reason": "human"}
    assert edge.evidence_calls == 1


# --- arm-state sync + node-down alert -------------------------------------

def test_sync_once_pulls_arm_state_from_edge():
    edge = _StubEdge()  # get_state -> armed True, override False
    g, st = _gateway(edge)
    assert st.snapshot()["armed"] is False  # starts fail-closed
    assert g.sync_once() is True
    assert st.snapshot()["armed"] is True


class _CapturingAlerter(gw.Alerter):
    def __init__(self):
        super().__init__()
        self.fired = []

    def node_down(self, node_id, last_seen_ms):
        self.fired.append((node_id, last_seen_ms))


def test_node_down_alert_fires_once_on_transition():
    edge = _StubEdge()
    st = gw.GatewayState()
    alerter = _CapturingAlerter()
    g = gw.Gateway(st, edge, alerter)
    # Simulate a node that was online then went silent long ago.
    st.fold_telemetry({"raining": False}, gw.now_ms() - (gw.NODE_OFFLINE_AFTER_SECS + 10) * 1000)
    assert g.check_liveness_once() is True
    assert len(alerter.fired) == 1, "alert fires on ONLINE->DOWN"
    # The edge is also notified (best-effort) so it can SMS — exactly once per transition.
    assert getattr(edge, "alerts", []) == [("node_down", "node-1")]
    # Staying down does NOT re-alert.
    assert g.check_liveness_once() is True
    assert len(alerter.fired) == 1
    # A fresh beat brings it back online; a later silence re-alerts.
    st.fold_telemetry({"raining": False}, gw.now_ms())
    assert g.check_liveness_once() is False
    st.fold_telemetry({"raining": False}, gw.now_ms() - (gw.NODE_OFFLINE_AFTER_SECS + 10) * 1000)
    assert g.check_liveness_once() is True
    assert len(alerter.fired) == 2


# --- _apply_pi_config: provisioned pi_config overrides a stale .env ----------

def test_apply_pi_config_overrides_stale_env(tmp_path):
    """When provisioned, configs/ (pi-garden.json + secrets.json) is the source of
    truth for edge/garden/token — so a stale .env can't misroute the node heartbeat
    (the bug that showed the node offline on the real dashboard)."""
    from types import SimpleNamespace
    from hardware import pi_config

    configs = tmp_path / "configs"
    configs.mkdir()
    pc = pi_config.PiConfig(str(configs))
    pc.save_partial({
        "provisioned": True,
        "node_id": "pi-01",
        "garden": {"garden_id": "real-garden"},
        "fastly": {"backend_url": "https://real-edge.example/"},
    })
    pc.set_secret("garden_token", "real-tok")

    # args as if parsed with a STALE .env (old edge + default garden)
    args = SimpleNamespace(edge="https://old-step3.example", garden_id="default",
                           device_id="default", node_id="default", garden_token="stale-tok",
                           configs_dir=str(configs))
    gw._apply_pi_config(args)
    assert args.edge == "https://real-edge.example"   # slash stripped
    assert args.garden_id == "real-garden"
    assert args.node_id == "pi-01"
    assert args.garden_token == "real-tok"


def test_apply_pi_config_noop_when_unprovisioned(tmp_path):
    from types import SimpleNamespace
    args = SimpleNamespace(edge="http://localhost:7878", garden_id="default",
                           device_id="default", node_id="default", garden_token="",
                           configs_dir=str(tmp_path / "configs"))
    gw._apply_pi_config(args)            # no configs dir -> no change, no raise
    assert args.edge == "http://localhost:7878"
    assert args.garden_id == "default"


# --- EdgeClient._headers: keys are the generated contract constants (PI-FINDING-1)
# The Pi->edge wire headers are single-sourced in contract/spec.toml -> the generated
# provision.contract_gen.HEADER_* constants (which the Rust edge reads). The gateway's
# edge headers must use those constants by reference (not re-typed literals), so a
# `make gen` rename can't silently diverge the gateway from the edge.

def test_edge_client_headers_use_generated_header_consts():
    from provision import contract_gen as cg

    edge = gw.EdgeClient("http://edge", garden_id="backyard", device_id="pi-01",
                         node_id="node-a", token="s3cr3t")
    h = edge._headers("deadbeefdeadbeef", {"Content-Type": "image/jpeg"})

    assert h[cg.HEADER_TRACE_ID] == "deadbeefdeadbeef"
    assert h[cg.HEADER_GARDEN_ID] == "backyard"
    assert h[cg.HEADER_DEVICE_ID] == "pi-01"
    assert h[cg.HEADER_NODE_ID] == "node-a"
    assert h[cg.HEADER_AUTH] == "s3cr3t"
    # The gateway's edge headers are EXACTLY the generated identity/trace/auth
    # constants plus the caller's `extra` — no stray hand-typed key on the contract.
    assert set(h) == {
        "Content-Type",
        cg.HEADER_TRACE_ID, cg.HEADER_GARDEN_ID, cg.HEADER_DEVICE_ID,
        cg.HEADER_NODE_ID, cg.HEADER_AUTH,
    }


def test_edge_client_headers_omit_auth_when_tokenless():
    # An empty token => no X-Garden-Auth header (don't transmit an empty credential).
    from provision import contract_gen as cg
    edge = gw.EdgeClient("http://edge")  # default tokenless garden
    assert cg.HEADER_AUTH not in edge._headers("tr")


# --- request body-size cap (PI-FINDING-2) ----------------------------------
# A malicious/buggy LAN device must not be able to exhaust the Pi's RAM with an
# oversized POST. The handler's _read_json rejects an over-cap Content-Length
# WITHOUT reading, and bounds the actual read so a lying Content-Length can't
# over-allocate. We drive the real BaseHTTPRequestHandler subclass with stubbed
# rfile/wfile (no socket), exactly as the http.server contract allows.

class _FakeHeaders:
    """Minimal stand-in for the handler's parsed headers (only .get is used)."""
    def __init__(self, mapping):
        self._m = mapping

    def get(self, key, default=None):
        return self._m.get(key, default)


def _make_post_handler(body, content_length=None, route="/heartbeat"):
    """Build a real Handler over a stub edge with the request body preloaded into a
    BytesIO rfile, then return (handler, captured_response_dict)."""
    edge = _StubEdge()
    g = gw.Gateway(gw.GatewayState(), edge, gw.Alerter())
    handler_cls = gw.make_handler(g)
    h = handler_cls.__new__(handler_cls)  # bypass __init__ (no real socket)

    h.rfile = io.BytesIO(body)
    h.wfile = io.BytesIO()
    h.path = route
    if content_length is None:
        content_length = len(body)
    h.headers = _FakeHeaders({"Content-Length": str(content_length)})

    captured = {}

    def _capture(code, obj):
        captured["code"] = code
        captured["obj"] = obj

    h._send_json = _capture  # don't touch the (absent) socket
    return h, captured


def test_read_json_rejects_oversized_content_length(monkeypatch):
    # A declared Content-Length over the cap is rejected 413 WITHOUT reading the body
    # (the body here is empty, proving we never tried to read `length` bytes).
    monkeypatch.setattr(gw, "MAX_BODY_BYTES", 1024)
    h, captured = _make_post_handler(b"", content_length=gw.MAX_BODY_BYTES + 1)
    h.do_POST()
    assert captured["code"] == 413
    assert captured["obj"] == {"error": "payload too large"}


def test_read_json_small_lying_content_length_cannot_overallocate(monkeypatch):
    # A LYING *small* Content-Length is harmless by construction: rfile.read(n) only
    # pulls n bytes off the wire, so a huge body behind a small declared length is
    # NEVER read into memory (no exhaustion). The 11 bytes we do read aren't JSON, so
    # the request is rejected 400 — the point is that the oversized tail never landed
    # in RAM, which we prove by checking the read stopped at the bounded size.
    monkeypatch.setattr(gw, "MAX_BODY_BYTES", 1024)
    big = b"x" * (gw.MAX_BODY_BYTES + 500)
    h, captured = _make_post_handler(big, content_length=10)  # lies: claims only 10
    h.do_POST()
    assert captured["code"] == 400  # not 413, but critically: not OOM either
    # Only the bounded number of bytes was consumed from the wire (cap not breached).
    assert h.rfile.tell() <= 11


def test_read_json_ignores_body_without_content_length(monkeypatch):
    # No (or zero) Content-Length: there is no declared body, so we read NOTHING and
    # treat it as an empty object. Critically, an unread huge tail never lands in RAM
    # (no over-allocation) AND we never over-read past the declared length, which on a
    # real blocking socket would hang the keep-alive connection until it timed out.
    monkeypatch.setattr(gw, "MAX_BODY_BYTES", 1024)
    big = b"x" * (gw.MAX_BODY_BYTES + 500)
    h, captured = _make_post_handler(big, content_length=0)  # absent/zero length
    h.do_POST()
    # Empty body -> handle_heartbeat({}) answers 200; the oversized tail was never read.
    assert captured["code"] == 200
    assert h.rfile.tell() == 0


def test_read_json_accepts_normal_body(monkeypatch):
    # A normal small JSON heartbeat is read and handled (200) under the cap.
    monkeypatch.setattr(gw, "MAX_BODY_BYTES", 1024)
    body = json.dumps({"raining": False, "temperature_c": 18.0}).encode()
    h, captured = _make_post_handler(body, route="/heartbeat")
    h.do_POST()
    assert captured["code"] == 200
    # /heartbeat returns the operating params block.
    assert set(captured["obj"]) == {"interval_s", "armed", "maintenance"}


def test_default_max_body_bytes_is_a_few_mb():
    # Sanity: the shipped ceiling is small (a few MB) — generous for a base64 JPEG
    # heartbeat, tight enough to bound a hostile allocation.
    assert gw.MAX_BODY_BYTES == 4 * 1024 * 1024
