"""hardware/telemetry.py — best-effort, observe-only telemetry for the Pi client.

Design invariants (LOCKED):
  * Telemetry must NEVER add latency to, or raise into, the safety path
    (disarm / heartbeat / deterrent control). The hot path is a single
    ``put_nowait`` onto a bounded queue; ALL disk I/O happens on a daemon writer
    thread, so ``disarm_all()`` / the heartbeat GET / FSM transitions never touch
    disk.
  * ``@traced`` / ``trace_span`` re-raise the ORIGINAL exception unchanged, so the
    existing fail-closed ``except`` handlers fire identically.
  * Best-effort: any failure (queue full, DB locked, sink down) is swallowed and
    counted — telemetry degrades to silence, never to a crash or a stall.
  * Global kill switch: ``GP_TELEMETRY=0`` makes the whole module a no-op.

Forward-compat (LOCKED): every row carries garden_id / device_id / node_id
(default "default") so single-garden -> multi-garden needs no migration.

Stdlib only: sqlite3 / threading / queue / uuid / contextvars (no new deps).
"""
import os
import sys
import time
import json
import queue
import sqlite3
import threading
import contextlib
import contextvars
import functools
import atexit
import uuid

# ---------------------------------------------------------------------------
# Module configuration / state
# ---------------------------------------------------------------------------

_ENABLED = os.environ.get("GP_TELEMETRY", "1") != "0"
_DEFAULT_DB = "/var/lib/garden-protector/telemetry.db"
_FALLBACK_DB = os.path.expanduser("~/.local/state/garden-protector/telemetry.db")
_QUEUE_MAX = 10_000

_lock = threading.Lock()
_q = None                # bounded queue.Queue, or None when uninitialized
_writer = None           # daemon writer thread
_initialized = False
_dropped = 0             # events dropped on queue backpressure
_write_errors = 0        # INSERTs that raised on the writer thread
_db_path_active = None   # path actually opened (may be the fallback)
_identity = {"garden_id": "default", "device_id": "default", "node_id": "default"}
_cid_var = contextvars.ContextVar("gp_cid", default=None)
_SENTINEL = object()     # shutdown marker pushed onto the queue


# ---------------------------------------------------------------------------
# Correlation context (per critter event)
# ---------------------------------------------------------------------------

def set_cid(cid):
    """Set the current correlation id for this thread's context (convenience for
    @traced HAL calls that don't take an explicit cid)."""
    _cid_var.set(cid)


def get_cid():
    return _cid_var.get()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def init(db_path=None, garden_id="default", device_id="default", node_id="default"):
    """Start the background SQLite writer. Idempotent and non-blocking: path
    resolution + DB open happen ON the writer thread, so a permission error never
    blocks or raises into the caller. ``GP_TELEMETRY=0`` -> no-op."""
    global _q, _writer, _initialized, _identity
    if not _ENABLED:
        return
    with _lock:
        if _initialized:
            return
        _identity = {"garden_id": garden_id, "device_id": device_id, "node_id": node_id}
        path = db_path or os.environ.get("GP_TELEMETRY_DB") or _DEFAULT_DB
        _q = queue.Queue(maxsize=_QUEUE_MAX)
        _writer = threading.Thread(
            target=_writer_loop, args=(path, _q), name="gp-telemetry-writer", daemon=True
        )
        _writer.start()
        _initialized = True
    atexit.register(shutdown)


def shutdown(timeout=2.0):
    """Flush and stop the writer. Safe to call multiple times and from a
    ``finally`` after ``disarm_all()``. Never raises."""
    global _q, _writer, _initialized
    if not _ENABLED:
        return
    with _lock:
        if not _initialized:
            return
        q, w = _q, _writer
        _initialized = False
        _q = None
        _writer = None
    if q is not None:
        try:
            q.put_nowait(_SENTINEL)
        except queue.Full:
            # Make room then signal; we must not block here.
            try:
                q.get_nowait()
            except Exception:
                pass
            try:
                q.put_nowait(_SENTINEL)
            except Exception:
                pass
    if w is not None:
        try:
            w.join(timeout=timeout)
        except Exception:
            pass


def stats():
    """Best-effort counters for the debug surface."""
    return {
        "enabled": _ENABLED,
        "initialized": _initialized,
        "dropped": _dropped,
        "write_errors": _write_errors,
        "queued": _q.qsize() if _q is not None else 0,
        "db_path": _db_path_active,
    }


# ---------------------------------------------------------------------------
# Hot path: emit (O(1), never blocks, never raises)
# ---------------------------------------------------------------------------

def emit(component, op, *, cid=None, caller=None, args=None, dur_ms=None,
         outcome="ok", detail=None):
    """Enqueue one telemetry row. No-op before init() / when disabled. The only
    work on the caller's thread is building a small tuple + a put_nowait; all disk
    I/O is deferred to the writer thread. Never raises."""
    global _dropped
    q = _q
    if not _ENABLED or q is None:
        return
    try:
        if caller is None:
            caller = _caller(2)
        row = (
            uuid.uuid4().hex,
            time.time(),
            cid if cid is not None else _cid_var.get(),
            _identity["garden_id"], _identity["device_id"], _identity["node_id"],
            component, op, caller,
            _safe_json(args),
            dur_ms, outcome, detail,
        )
        q.put_nowait(row)
    except queue.Full:
        _dropped += 1
    except Exception:
        # Telemetry must never raise into the caller.
        _dropped += 1


# ---------------------------------------------------------------------------
# Instrumentation surfaces: @traced decorator + trace_span context manager.
# BOTH re-raise the original exception unchanged so fail-closed handlers fire.
# ---------------------------------------------------------------------------

def traced(component, op):
    """Decorator that records an op's duration/outcome. For functions returning
    ``bytes`` (e.g. ``capture_image``) it records ONLY the byte count, never the
    payload. Re-raises the original exception unchanged."""
    def deco(fn):
        origin = f"{getattr(fn, '__module__', '?')}.{getattr(fn, '__qualname__', fn.__name__)}"

        @functools.wraps(fn)
        def wrapper(*a, **k):
            if not _ENABLED:
                return fn(*a, **k)
            t0 = time.perf_counter()
            outcome, detail, rv = "ok", None, None
            try:
                rv = fn(*a, **k)
                return rv
            except BaseException as e:  # observe; do NOT swallow
                outcome, detail = "error", f"{type(e).__name__}: {e}"
                raise
            finally:
                dur = (time.perf_counter() - t0) * 1000.0
                summary = {"bytes": len(rv)} if isinstance(rv, (bytes, bytearray)) else None
                try:
                    emit(component, op, caller=origin, args=summary,
                         dur_ms=dur, outcome=outcome, detail=detail)
                except Exception:
                    pass  # a telemetry bug must never mask the real outcome
        return wrapper
    return deco


@contextlib.contextmanager
def trace_span(component, op, *, cid=None, args=None, caller=None):
    """Context manager recording the wrapped block's duration/outcome. Re-raises
    the original exception unchanged."""
    if not _ENABLED:
        yield
        return
    if caller is None:
        caller = _caller(3)
    t0 = time.perf_counter()
    outcome, detail = "ok", None
    try:
        yield
    except BaseException as e:  # observe; do NOT swallow
        outcome, detail = "error", f"{type(e).__name__}: {e}"
        raise
    finally:
        dur = (time.perf_counter() - t0) * 1000.0
        try:
            emit(component, op, cid=cid, caller=caller, args=args,
                 dur_ms=dur, outcome=outcome, detail=detail)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

_INSERT_SQL = (
    "INSERT OR REPLACE INTO events "
    "(id, ts, cid, garden_id, device_id, node_id, component, op, caller, args, dur_ms, outcome, detail) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY,
  ts REAL NOT NULL,
  cid TEXT,
  garden_id TEXT NOT NULL DEFAULT 'default',
  device_id TEXT NOT NULL DEFAULT 'default',
  node_id   TEXT NOT NULL DEFAULT 'default',
  component TEXT NOT NULL,
  op TEXT NOT NULL,
  caller TEXT,
  args TEXT,
  dur_ms REAL,
  outcome TEXT NOT NULL,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_cid ON events(cid);
CREATE INDEX IF NOT EXISTS idx_events_comp_op ON events(component, op);
CREATE INDEX IF NOT EXISTS idx_events_garden ON events(garden_id);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def _safe_json(args):
    if args is None:
        return None
    try:
        return json.dumps(args, default=str)
    except Exception:
        return None


def _caller(depth):
    """Best-effort 'module.func:line' of the calling frame (cheap; no inspect)."""
    try:
        f = sys._getframe(depth)
        return f"{f.f_globals.get('__name__', '?')}.{f.f_code.co_name}:{f.f_lineno}"
    except Exception:
        return None


def _open_db(path):
    """Open a WAL sqlite DB at ``path``; fall back to ~/.local/state; else None.
    Runs on the writer thread so permission errors never reach the caller."""
    global _db_path_active, _dropped, _write_errors
    for candidate in (path, _FALLBACK_DB):
        try:
            d = os.path.dirname(candidate)
            if d:
                os.makedirs(d, exist_ok=True)
            conn = sqlite3.connect(candidate, timeout=2.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=2000")
            conn.executescript(_SCHEMA_SQL)
            conn.commit()
            # Carry counters across restarts (best-effort).
            try:
                for k, v in conn.execute(
                    "SELECT k, v FROM meta WHERE k IN ('dropped','write_errors')"
                ).fetchall():
                    if k == "dropped":
                        _dropped = int(v)
                    elif k == "write_errors":
                        _write_errors = int(v)
            except Exception:
                pass
            _db_path_active = candidate
            return conn
        except Exception as e:
            sys.stderr.write(f"[telemetry] could not open {candidate}: {e}\n")
            continue
    sys.stderr.write("[telemetry] disabled: no writable telemetry.db location\n")
    return None


def _writer_loop(path, q):
    """Daemon thread: drain the queue and INSERT each row. The connection lives
    here (sqlite objects are thread-affine). Errors are counted, never raised."""
    global _write_errors
    conn = _open_db(path)
    if conn is None:
        # No DB: still drain so producers never block on a full queue.
        while True:
            if q.get() is _SENTINEL:
                return
    
    retention_days = int(os.environ.get("GP_TELEMETRY_RETENTION_DAYS", "7"))
    last_cleanup = 0
    
    try:
        while True:
            item = q.get()
            if item is _SENTINEL:
                break
            
            # Run cleanup at most once an hour to keep DB size managed without overhead
            now = time.time()
            if now - last_cleanup > 3600:
                last_cleanup = now
                cutoff = now - (retention_days * 86400)
                try:
                    conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
                    conn.commit()
                except Exception as e:
                    sys.stderr.write(f"[telemetry] automatic cleanup failed: {e}\n")

            try:
                conn.execute(_INSERT_SQL, item)
                conn.commit()
            except Exception as e:
                _write_errors += 1
                sys.stderr.write(f"[telemetry] write error: {e}\n")
    finally:
        try:
            conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('dropped',?)", (str(_dropped),))
            conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('write_errors',?)", (str(_write_errors),))
            conn.commit()
            conn.close()
        except Exception:
            pass
