"""End-to-end simulation: node_sim -> (real HTTP) gateway -> (real HTTP) fake edge.

This boots the actual Tier-1 gateway HTTP server and the fake edge, then drives
the ESP32-C3 node simulator through every decision branch the garden will hit once the
required (and all the optional) peripherals are fitted:

  * critter at night        -> spray (with the node-side hard cap)
  * human in frame          -> veto (never spray a person)
  * actively raining         -> suppressed (local fast path AND edge backstop)
  * disarmed / STOP          -> stand down
  * empty reservoir          -> commanded spray, but INA219 confirms NO flow
  * optional peripherals     -> their telemetry reaches the edge dashboard block
  * node goes silent         -> liveness flips, node_down alert fires

Everything is deterministic (scene colour -> verdict) and uses ephemeral ports.
"""
import threading
from http.server import ThreadingHTTPServer

import pytest

from hardware import gateway as gw
from hardware import node_sim as ns
from tests.fake_edge import FakeEdge


class Harness:
    def __init__(self, *, fitted=(), reservoir_ok=True, spray_seconds=3):
        self.edge = FakeEdge(armed=True)
        self.edge_url = self.edge.start()
        self.state = gw.GatewayState(spray_seconds=spray_seconds)
        self.edge_client = gw.EdgeClient(self.edge_url, node_id="node-sim")
        self.alerter = _CapturingAlerter()
        self.gw = gw.Gateway(self.state, self.edge_client, self.alerter)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), gw.make_handler(self.gw))
        self.gw_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._t = threading.Thread(
            target=self.server.serve_forever, args=(0.02,), daemon=True)
        self._t.start()
        self.node = ns.NodeSim(self.gw_url, fitted=fitted, reservoir_ok=reservoir_ok)
        self.gw.sync_once()  # learn armed=True from the edge

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.edge.stop()


class _CapturingAlerter(gw.Alerter):
    def __init__(self):
        super().__init__()
        self.fired = []

    def node_down(self, node_id, last_seen_ms):
        self.fired.append((node_id, last_seen_ms))


@pytest.fixture
def harness():
    h = Harness(fitted=ns.OPTIONAL_PERIPHERALS)
    yield h
    h.close()


def test_critter_at_night_sprays(harness):
    reply = harness.node.radar_trip("raccoon")
    assert reply["spray"] is True
    assert reply["seconds"] == 3
    assert harness.node.counters["sprays"] == 1
    # the edge recorded a mitigate event for the dashboard
    assert harness.edge.state()["latest_event"]["action"] == "mitigate"


def test_human_is_vetoed(harness):
    reply = harness.node.radar_trip("human")
    assert reply["spray"] is False
    assert reply["reason"] == "human"
    assert harness.node.counters["sprays"] == 0


def test_local_rain_suppression_skips_edge(harness):
    harness.node.set_rain(True)
    reply = harness.node.radar_trip("raccoon")  # incident carries raining=True
    assert reply == {"spray": False, "reason": "rain"}
    # local fast path means the edge never even classified this incident
    assert ("POST", "/api/evidence") not in harness.edge.requests


def test_edge_rain_backstop(harness):
    # Heartbeat reports rain (reaches the edge), but the incident telemetry is dry
    # so the gateway does NOT locally veto -> the EDGE backstop must suppress.
    harness.node.set_rain(True)
    harness.node.send_heartbeat()           # edge now knows it's raining (fresh)
    harness.node.set_rain(False)            # incident itself reports dry
    reply = harness.node.radar_trip("raccoon")
    assert reply == {"spray": False, "reason": "rain"}
    assert ("POST", "/api/evidence") in harness.edge.requests  # edge WAS consulted


def test_disarm_stands_down(harness):
    harness.edge.control("disarm")
    harness.gw.sync_once()
    reply = harness.node.radar_trip("raccoon")
    assert reply == {"spray": False, "reason": "disarmed"}


def test_empty_reservoir_sprays_air():
    h = Harness(fitted=ns.OPTIONAL_PERIPHERALS, reservoir_ok=False)
    try:
        reply = h.node.radar_trip("fox")
        assert reply["spray"] is True               # the edge said mitigate
        assert h.node.counters["sprays"] == 1       # the node opened the valve
        assert h.node.state["spray_confirmed"] is False  # ...but no water flowed
    finally:
        h.close()


def test_hard_cap_enforced_by_node():
    h = Harness(spray_seconds=10)  # gateway over-requests
    try:
        reply = h.node.radar_trip("raccoon")
        assert reply["seconds"] == 10               # gateway asked for 10
        secs, _ = ns.actuate(reply)
        assert secs == ns.HARD_CAP_SECONDS          # node still capped to 4
    finally:
        h.close()


def test_optional_peripherals_reach_edge(harness):
    harness.node.send_heartbeat()
    tel = harness.edge.state()["node"]["telemetry"]
    assert tel["soil_moisture_pct"] is not None
    assert "reservoir_ok" in tel
    assert "presence_distance_cm" in tel
    assert "on_backup_power" in tel
    assert harness.edge.state()["node"]["online"] is True


def test_node_liveness_and_down_alert(harness):
    harness.node.send_heartbeat()
    assert harness.gw.check_liveness_once() is False  # just heard from it
    # Force the last-seen far into the past, then re-check.
    harness.state.fold_telemetry({}, gw.now_ms() - (gw.NODE_OFFLINE_AFTER_SECS + 30) * 1000)
    assert harness.gw.check_liveness_once() is True
    assert len(harness.alerter.fired) == 1
    # The gateway also notified the edge (edge -> Twilio SMS path) on the transition.
    assert ("POST", "/api/alert") in harness.edge.requests
