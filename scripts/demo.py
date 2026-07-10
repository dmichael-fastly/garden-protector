#!/usr/bin/env python3
"""scripts/demo.py — a narrated, deterministic walkthrough of the whole push model.

Boots the fake edge + the real Tier-1 gateway + the ESP32-C3 node simulator in-process
and drives them through every decision branch the garden will hit, printing a
human-readable transcript with a ✓/✗ per scenario. Because it uses the deterministic
fake edge (scene colour -> verdict), it can show the **spray** path that the real
MobileNet edge can't for synthetic frames.

    python3 scripts/demo.py        # or: make demo

Exit code is non-zero if any scenario didn't behave as expected, so it doubles as a
smoke test of the assembled stack.
"""
import os
import sys
import threading
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("GP_TELEMETRY", "0")

from hardware import gateway as gw          # noqa: E402
from hardware import node_sim as ns         # noqa: E402
from tests.fake_edge import FakeEdge        # noqa: E402

# Tiny ANSI palette (no deps); degrade to plain text if not a TTY.
TTY = sys.stdout.isatty()
def c(code, s): return f"\033[{code}m{s}\033[0m" if TTY else s
GREEN, RED, DIM, BOLD, CYAN = "32", "31", "2", "1", "36"

results = []


def scenario(title):
    print("\n" + c(BOLD, f"▶ {title}"))


def check(label, ok, detail=""):
    mark = c(GREEN, "✓") if ok else c(RED, "✗")
    print(f"   {mark} {label}" + (c(DIM, f"  — {detail}") if detail else ""))
    results.append(ok)


def main():
    edge = FakeEdge(armed=True)
    edge_url = edge.start()
    state = gw.GatewayState(spray_seconds=3)
    edge_client = gw.EdgeClient(edge_url, node_id="node-demo")

    class _Alerter(gw.Alerter):
        def __init__(self): super().__init__(); self.fired = []
        def node_down(self, node_id, last_seen_ms): self.fired.append(node_id)

    alerter = _Alerter()
    g = gw.Gateway(state, edge_client, alerter)
    server = ThreadingHTTPServer(("127.0.0.1", 0), gw.make_handler(g))
    gw_url = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    node = ns.NodeSim(gw_url, fitted=ns.OPTIONAL_PERIPHERALS)
    g.sync_once()  # learn armed=True from the edge

    # Each scenario is independent, so clear the (real, separately-tested) post-spray
    # refractory before a trip — otherwise a prior spray's 6 s cooldown would short-
    # circuit the next trip locally and mask the branch we're demonstrating.
    def trip(scene, bed=1):
        node.last_burst_end_ms = None
        return node.radar_trip(scene, bed=bed)

    print(c(CYAN, "Fastly Garden Protector — end-to-end simulation"))
    print(c(DIM, f"  node {node.gateway_url}  ->  gateway  ->  fake edge {edge_url}"))
    print(c(DIM, "  (deterministic scene->verdict so the spray path is visible)"))

    try:
        scenario("A raccoon trips the radar at night (armed) → SPRAY")
        r = trip("raccoon")
        check("gateway commands a spray", r.get("spray") is True, f"reply={r}")
        check("node honours the hard cap", ns.actuate(r)[0] <= ns.HARD_CAP_SECONDS)
        check("edge logged a mitigate event", edge.state()["latest_event"]["action"] == "mitigate")

        scenario("A person is in the bed → VETO (never spray a human)")
        r = trip("human")
        check("spray withheld", r.get("spray") is False and r.get("reason") == "human", f"reply={r}")

        scenario("It's raining → SUPPRESS (local fast path, zero edge calls)")
        node.set_rain(True)
        before = len([x for x in edge.requests if x == ("POST", "/api/evidence")])
        r = trip("raccoon")
        after = len([x for x in edge.requests if x == ("POST", "/api/evidence")])
        check("spray suppressed for rain", r == {"spray": False, "reason": "rain"})
        check("edge was NOT consulted (local authority)", after == before)
        node.set_rain(False)

        scenario("Admin disarms from the console/dashboard → STAND DOWN")
        edge.control("disarm"); g.sync_once()
        r = trip("raccoon")
        check("disarmed → no spray", r == {"spray": False, "reason": "disarmed"})
        edge.control("arm"); g.sync_once()
        check("re-arm restores protection", trip("fox").get("spray") is True)

        scenario("The reservoir is empty → valve opens but no water flows")
        node.state["reservoir_ok"] = False
        node.last_burst_end_ms = None  # clear refractory from the prior spray
        r = trip("cat")
        secs, confirmed = ns.actuate(r, reservoir_ok=node.state["reservoir_ok"])
        check("spray commanded", r.get("spray") is True)
        check("INA219 confirms NO flow", confirmed is False, "spraying air — alert-worthy")
        node.state["reservoir_ok"] = True

        scenario("All optional peripherals report up to the edge")
        node.send_heartbeat()
        t = edge.state()["node"]["telemetry"]
        for field in ("soil_moisture_pct", "reservoir_ok", "presence_distance_cm", "on_backup_power"):
            check(f"telemetry carries {field}", field in t)

        scenario("The board goes silent → node-down alert (protection is offline)")
        state.fold_telemetry({}, gw.now_ms() - (gw.NODE_OFFLINE_AFTER_SECS + 30) * 1000)
        down = g.check_liveness_once()
        check("liveness flips to DOWN", down is True)
        check("local alerter fired", len(alerter.fired) == 1)
        check("edge notified for SMS", ("POST", "/api/alert") in edge.requests)
    finally:
        server.shutdown(); server.server_close(); edge.stop()

    passed = sum(results); total = len(results)
    ok = passed == total
    print("\n" + c(BOLD, c(GREEN if ok else RED, f"{passed}/{total} checks passed")))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
