"""Property-based fail-closed invariants for the spray decision core.

These assert the ONE rule that must never break, over the whole input space:
a deterrent only ever turns ON via the single sanctioned path, and the node never
exceeds its hard cap. If any of these can be falsified, the system can spray when it
shouldn't — the exact failure mode the whole design forbids.

SAFETY INVARIANT INDEX (the named fail-closed suite per CHARTER.md). Each invariant
is enforced in product code and pinned by a test that demonstrably fails if broken:

  1. Heartbeat fails closed when garden_state is unreadable.
     enforced: backend/src/main.rs `handle_status` / `heartbeat_continue`
     pinned by: main.rs `test_heartbeat_continue_fails_closed_on_unreadable_flags`
  2. Pi client stops the deterrent on a 3 s timeout / HTTP error.
     enforced: hardware/client.py mitigation loop (disarm on non-2xx / timeout)
     pinned by: tests/test_client.py `test_fail_closed_on_http_error`,
                `test_fail_closed_on_network_timeout`, `test_local_watchdog_overrun`
  3. Rain veto is monotone + idempotent (raining -> mitigate becomes none; never the
     reverse, re-applying is a no-op).
     enforced: backend/src/main.rs `rain_should_suppress`
     pinned by: main.rs `test_rain_veto_is_monotone_and_idempotent`
  4. Gateway spray core never sprays on no-reply / timeout / any local veto.
     enforced: hardware/gateway.py `local_precheck` + `resolve_motion`;
               node hard cap in hardware/node_sim.py `cap_seconds` / `actuate`
     pinned by: the property tests in THIS file (incl. the full-chain test below).
"""
from hypothesis import given, strategies as st

from hardware import gateway as gw
from hardware import node_sim as ns


# --- gateway.resolve_motion: the final spray decision -----------------------

@given(
    precheck=st.one_of(st.none(), st.text(min_size=0, max_size=12)),
    edge_ok=st.booleans(),
    action=st.text(min_size=0, max_size=12),
    reason=st.one_of(st.none(), st.text(max_size=12)),
    secs=st.integers(min_value=0, max_value=60),
)
def test_spray_only_via_the_sanctioned_path(precheck, edge_ok, action, reason, secs):
    out = gw.resolve_motion(precheck, edge_ok, action, reason, secs)
    if out.get("spray"):
        # The ONLY way to spray: nothing vetoed locally, the edge was reachable, and
        # it explicitly said mitigate. Anything else must be a stand-down.
        assert precheck is None
        assert edge_ok is True
        assert action == "mitigate"
        assert out["seconds"] == secs
    else:
        assert out["spray"] is False
        assert "reason" in out  # a withhold always explains itself


@given(precheck=st.text(min_size=0, max_size=12), edge_ok=st.booleans(),
       action=st.text(max_size=12), secs=st.integers(0, 60))
def test_any_local_precheck_blocks_spray(precheck, edge_ok, action, secs):
    # A non-None precheck (even an empty string) always withholds, regardless of edge.
    out = gw.resolve_motion(precheck, edge_ok, action, None, secs)
    assert out == {"spray": False, "reason": precheck}


@given(action=st.text(max_size=12), reason=st.one_of(st.none(), st.text(max_size=12)), secs=st.integers(0, 60))
def test_edge_unreachable_always_fails_closed(action, reason, secs):
    assert gw.resolve_motion(None, False, action, reason, secs) == {
        "spray": False, "reason": "edge_unreachable"}


# --- gateway.local_precheck: clear iff every veto is clear ------------------

@given(st.booleans(), st.booleans(), st.booleans(), st.booleans(), st.booleans())
def test_precheck_clear_iff_fully_clear(armed, stop, maint, irr, rain):
    r = gw.local_precheck(armed, stop, maint, irr, rain)
    fully_clear = armed and not stop and not maint and not irr and not rain
    assert (r is None) == fully_clear


# --- full chain: local_precheck -> resolve_motion ---------------------------

@given(
    armed=st.booleans(), stop=st.booleans(), maint=st.booleans(),
    irr=st.booleans(), rain=st.booleans(), edge_ok=st.booleans(),
    action=st.sampled_from(["mitigate", "none", "", "spray", "MITIGATE"]),
    secs=st.integers(min_value=0, max_value=60),
)
def test_full_chain_spray_implies_every_gate_open(
    armed, stop, maint, irr, rain, edge_ok, action, secs
):
    # Compose the two gateway stages exactly as gateway.py does and assert the ONLY
    # path to a real spray: every local veto clear AND the edge reachable AND the edge
    # explicitly said "mitigate". Falsifying this means the chain can spray with a veto
    # active or the edge unreachable — the core fail-closed promise.
    precheck = gw.local_precheck(armed, stop, maint, irr, rain)
    out = gw.resolve_motion(precheck, edge_ok, action, None, secs)
    if out.get("spray"):
        assert armed and not stop and not maint and not irr and not rain, "a veto was active"
        assert precheck is None
        assert edge_ok is True, "edge was unreachable"
        assert action == "mitigate", f"edge did not say mitigate (said {action!r})"
        assert out["seconds"] == secs
    else:
        assert out["spray"] is False
        assert "reason" in out  # a withhold always explains itself


# --- node hard cap + actuate ------------------------------------------------

@given(st.integers(min_value=-1000, max_value=1000))
def test_cap_seconds_is_bounded(x):
    c = ns.cap_seconds(x)
    assert 0 <= c <= ns.HARD_CAP_SECONDS
    if x >= 0:
        assert c <= x  # never sprays longer than requested either


@given(spray=st.booleans(), secs=st.integers(-5, 60), reservoir=st.booleans())
def test_actuate_never_overshoots_and_failscloses(spray, secs, reservoir):
    out_secs, confirmed = ns.actuate({"spray": spray, "seconds": secs}, reservoir_ok=reservoir)
    assert 0 <= out_secs <= ns.HARD_CAP_SECONDS
    if not spray:
        assert out_secs == 0 and confirmed is False
    if out_secs > 0:
        assert spray is True          # only an explicit spray:true actuates
        assert confirmed == reservoir  # flow confirmed iff the reservoir isn't empty


@given(st.one_of(st.none(), st.dictionaries(st.text(max_size=6), st.text(max_size=6)),
                 st.text(), st.integers()))
def test_actuate_ignores_malformed_replies(reply):
    # Any reply lacking spray:true -> no actuation (fail-closed against junk).
    out_secs, confirmed = ns.actuate(reply)
    assert out_secs == 0 and confirmed is False
