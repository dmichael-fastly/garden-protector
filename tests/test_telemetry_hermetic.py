"""Pins the CI-003 fix: the suite is hermetic against an inherited GP_TELEMETRY=0.

hardware/telemetry._ENABLED is read ONCE at import (telemetry.py:37 —
`_ENABLED = os.environ.get("GP_TELEMETRY", "1") != "0"`). A developer who exports
GP_TELEMETRY=0 in their shell would otherwise turn init()/emit() into no-ops and
make the 6 tests in test_telemetry.py fail FALSELY (they emit then read the DB back).

The defense is the autouse `_hermetic_telemetry_env` fixture in conftest.py, which
(1) clears GP_TELEMETRY from the env and (2) forces telemetry._ENABLED = True so the
already-imported module behaves as enabled. These tests guard that fixture so it can't
silently regress (e.g. someone deletes one of the two halves).

NOTE: never `export GP_TELEMETRY=0` for the suite at large — the fixture neutralizes a
LEAKED value, it is not a way to run the suite with telemetry disabled.
"""
import os
import sqlite3

import hardware.telemetry as t


def test_autouse_fixture_forces_enabled_true():
    # The autouse conftest fixture must leave the module enabled regardless of what the
    # developer's shell had exported. If this is False, the fixture's _ENABLED half is gone.
    assert t._ENABLED is True


def test_autouse_fixture_clears_gp_telemetry_from_env():
    # The fixture also scrubs the env var so nothing downstream re-derives "disabled" by
    # re-reading os.environ. If this fails, the delenv half of the fixture is gone.
    assert "GP_TELEMETRY" not in os.environ


def test_emit_writes_row_even_with_gp_telemetry_zero_inherited(tmp_path, monkeypatch):
    # Simulate the exact trap: a developer has GP_TELEMETRY=0 exported. The autouse
    # fixture should have already neutralized it; re-setting the env here must NOT
    # re-disable the module (because _ENABLED is import-cached and forced True). A real
    # emit -> shutdown -> DB-readback must still find the row, proving telemetry tests
    # cannot fail falsely from an inherited kill switch.
    monkeypatch.setenv("GP_TELEMETRY", "0")
    assert t._ENABLED is True  # import-cached + fixture-forced; env change is inert

    db = str(tmp_path / "telemetry.db")
    t.init(db_path=db, garden_id="g1", device_id="d1", node_id="n1")
    try:
        t.emit("system", "probe", cid="hermetic00000000", args={"bytes": 1})
    finally:
        t.shutdown()

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT component, op, outcome FROM events").fetchall()
    finally:
        conn.close()
    assert rows == [("system", "probe", "ok")]
