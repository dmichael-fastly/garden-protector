"""Unit tests for the ESP32-C3 node simulator's pure safety logic (hardware/node_sim.py)."""
from hardware import node_sim as ns


# --- hard spray cap -------------------------------------------------------

def test_cap_seconds_enforces_hard_cap():
    assert ns.cap_seconds(3) == 3
    assert ns.cap_seconds(10) == ns.HARD_CAP_SECONDS  # node never exceeds its cap
    assert ns.cap_seconds(0) == 0
    assert ns.cap_seconds(-5) == 0
    assert ns.cap_seconds("nan") == 0
    assert ns.cap_seconds(None) == 0


# --- post-spray refractory ------------------------------------------------

def test_refractory_active_window():
    now = 1_000_000
    assert ns.refractory_active(None, now) is False
    assert ns.refractory_active(now, now) is True
    assert ns.refractory_active(now - ns.REFRACTORY_SECS * 1000 + 1, now) is True
    assert ns.refractory_active(now - ns.REFRACTORY_SECS * 1000 - 1, now) is False


# --- actuate: the physical outcome of a gateway reply ---------------------

def test_actuate_only_on_explicit_spray_true():
    assert ns.actuate({"spray": False}) == (0, False)
    assert ns.actuate({"spray": False, "reason": "human"}) == (0, False)
    assert ns.actuate({}) == (0, False)
    assert ns.actuate(None) == (0, False)


def test_actuate_caps_and_confirms():
    assert ns.actuate({"spray": True, "seconds": 3}) == (3, True)
    # requested 10 -> capped to the hard cap, still confirmed (reservoir ok)
    assert ns.actuate({"spray": True, "seconds": 10}) == (ns.HARD_CAP_SECONDS, True)


def test_actuate_empty_reservoir_sprays_air():
    # commanded to spray, but the jug is empty -> INA219 confirms NO flow
    secs, confirmed = ns.actuate({"spray": True, "seconds": 3}, reservoir_ok=False)
    assert secs == 3 and confirmed is False


def test_actuate_zero_seconds_is_noop():
    assert ns.actuate({"spray": True, "seconds": 0}) == (0, False)


# --- telemetry snapshot: optional fields only when fitted -----------------

def _state():
    return {
        "battery_voltage": 4.12, "rssi": -61, "uptime_s": 100,
        "temperature_c": 18.5, "humidity_pct": 64.0, "rainfall_mm": 0.0,
        "raining": False, "lux_level": 200.0, "soil_moisture_pct": 38.0,
        "reservoir_ok": True, "presence_distance_cm": 220, "on_backup_power": False,
        "spray_confirmed": True,
    }


def test_build_snapshot_base_only():
    snap = ns.build_snapshot(_state(), 12345, fitted=())
    assert snap["battery_voltage"] == 4.12
    assert snap["raining"] is False
    # none of the optional peripheral fields appear when nothing is fitted
    for k in ("soil_moisture_pct", "reservoir_ok", "presence_distance_cm",
              "on_backup_power", "spray_confirmed"):
        assert k not in snap


def test_build_snapshot_includes_fitted_optional_fields():
    snap = ns.build_snapshot(_state(), 1, fitted=("soil", "reservoir", "presence", "backup"))
    assert snap["soil_moisture_pct"] == 38.0
    assert snap["reservoir_ok"] is True
    assert snap["presence_distance_cm"] == 220
    assert snap["on_backup_power"] is False
    # spray_confirmed needs both the fitted INA219 AND the post-burst flag
    assert "spray_confirmed" not in snap


def test_build_snapshot_spray_confirm_requires_flag():
    snap = ns.build_snapshot(_state(), 1, fitted=("spray_confirm",), include_spray_confirm=True)
    assert snap["spray_confirmed"] is True
    snap2 = ns.build_snapshot(_state(), 1, fitted=("spray_confirm",), include_spray_confirm=False)
    assert "spray_confirmed" not in snap2


# --- diurnal factor is bounded + deterministic ----------------------------

def test_diurnal_bounds():
    vals = [ns.diurnal(t, period_s=100.0) for t in range(0, 100, 5)]
    assert all(0.0 <= v <= 1.0 for v in vals)
    # deterministic
    assert ns.diurnal(50, period_s=100.0) == ns.diurnal(150, period_s=100.0)
