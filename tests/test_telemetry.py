"""Tests for hardware/telemetry.py.

Focus: the invariants that protect the safety path — emit never raises, drops on
backpressure, @traced/trace_span re-raise the ORIGINAL exception unchanged, the
kill switch disables cleanly, identity columns are carried, byte payloads are
summarized (never stored), and a bad DB path degrades to the fallback. All DB I/O
goes to a tmp_path DB — never /var.
"""
import queue
import sqlite3

import pytest

import hardware.telemetry as t


def _read_rows(db):
    """Read committed rows (read-only, WAL-aware) after shutdown."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute(
            "SELECT cid, garden_id, device_id, node_id, component, op, args, outcome, dur_ms "
            "FROM events ORDER BY ts"
        ).fetchall()
    finally:
        conn.close()


def test_emit_and_shutdown_writes_row_with_identity(tmp_path):
    db = str(tmp_path / "telemetry.db")
    t.init(db_path=db, garden_id="g1", device_id="d1", node_id="n1")
    try:
        t.emit("camera", "capture.usb", cid="abc1230000000000", args={"bytes": 5}, dur_ms=1.2)
    finally:
        t.shutdown()

    rows = _read_rows(db)
    assert len(rows) == 1
    cid, g, d, n, comp, op, _args, outcome, _dur = rows[0]
    assert (cid, g, d, n) == ("abc1230000000000", "g1", "d1", "n1")
    assert (comp, op, outcome) == ("camera", "capture.usb", "ok")


def test_traced_passes_value_and_summarizes_bytes(tmp_path):
    db = str(tmp_path / "telemetry.db")
    t.init(db_path=db)
    try:
        @t.traced("camera", "capture.test")
        def cap():
            return b"\x00" * 123

        out = cap()
        assert out == b"\x00" * 123  # value passes through unchanged
    finally:
        t.shutdown()

    rows = _read_rows(db)
    assert len(rows) == 1
    args_json = rows[0][6]
    assert args_json is not None and '"bytes": 123' in args_json
    # The raw JPEG/bytes payload must NEVER be persisted.
    assert "\\u0000" not in args_json and "\\x00" not in args_json


def test_traced_reraises_original_exception_and_records_error(tmp_path):
    db = str(tmp_path / "telemetry.db")
    t.init(db_path=db)
    try:
        @t.traced("deterrent", "sprinkler.on")
        def boom():
            raise RuntimeError("relay stuck")

        with pytest.raises(RuntimeError, match="relay stuck"):
            boom()
    finally:
        t.shutdown()

    rows = _read_rows(db)
    assert len(rows) == 1
    assert rows[0][7] == "error"  # outcome


def test_trace_span_records_ok_and_error_and_reraises(tmp_path):
    db = str(tmp_path / "telemetry.db")
    t.init(db_path=db)
    try:
        with t.trace_span("http", "POST /api/evidence", cid="trip000000000001"):
            pass
        with pytest.raises(ValueError):
            with t.trace_span("http", "GET /api/status", cid="trip000000000001"):
                raise ValueError("net down")
    finally:
        t.shutdown()

    rows = _read_rows(db)
    by_op = {r[5]: r[7] for r in rows}
    assert by_op.get("POST /api/evidence") == "ok"
    assert by_op.get("GET /api/status") == "error"
    assert all(r[8] is not None for r in rows)  # dur_ms always recorded


def test_kill_switch_noop_but_still_reraises(monkeypatch):
    monkeypatch.setattr(t, "_ENABLED", False)

    t.emit("x", "y")  # no-op, must not raise

    with pytest.raises(ValueError):
        with t.trace_span("x", "y"):
            raise ValueError("boom")

    @t.traced("x", "y")
    def f():
        raise KeyError("k")

    with pytest.raises(KeyError):
        f()


def test_backpressure_increments_dropped_without_raising(monkeypatch):
    monkeypatch.setattr(t, "_ENABLED", True)
    monkeypatch.setattr(t, "_q", queue.Queue(maxsize=1))
    monkeypatch.setattr(t, "_identity", {"garden_id": "d", "device_id": "d", "node_id": "d"})

    before = t._dropped
    t.emit("c", "fill")      # fills the single slot
    t.emit("c", "overflow")  # must be dropped, never raise
    assert t._dropped >= before + 1


def test_emit_is_noop_before_init(monkeypatch):
    monkeypatch.setattr(t, "_ENABLED", True)
    monkeypatch.setattr(t, "_q", None)
    # No queue yet: emit must return silently (this is what keeps the existing
    # client test fixture, which never calls init(), safe).
    t.emit("c", "op")


def test_permission_error_falls_back_to_fallback_db(tmp_path, monkeypatch):
    fallback = str(tmp_path / "fallback.db")
    monkeypatch.setattr(t, "_FALLBACK_DB", fallback)
    # Primary path can't be created (parent /dev/null is a file -> NotADirectory).
    t.init(db_path="/dev/null/nope/telemetry.db", garden_id="g", device_id="d", node_id="n")
    try:
        t.emit("system", "probe")
    finally:
        t.shutdown()

    rows = _read_rows(fallback)
    assert len(rows) >= 1
