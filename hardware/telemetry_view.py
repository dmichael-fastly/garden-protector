#!/usr/bin/env python3
"""Read-only viewer for the Pi telemetry DB — stdlib only, no Datasette required.

Opens the SQLite file in read-only/immutable mode (WAL-safe; never blocks the
writer). For a richer UI, Datasette also works (optional, not a dependency):

    datasette /var/lib/garden-protector/telemetry.db --immutable -p 8080

Examples:
    python3 hardware/telemetry_view.py                 # last 30 events (newest first)
    python3 hardware/telemetry_view.py --cid <id>      # one trip lifecycle (oldest first)
    python3 hardware/telemetry_view.py --component http
    python3 hardware/telemetry_view.py --stats         # dropped / write-error counters
"""
import argparse
import os
import sqlite3
import sys

DEFAULT_DB = os.environ.get("GP_TELEMETRY_DB", "/var/lib/garden-protector/telemetry.db")
FALLBACK_DB = os.path.expanduser("~/.local/state/garden-protector/telemetry.db")


def _connect(path):
    return sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)


def main():
    ap = argparse.ArgumentParser(description="Read-only Fastly Garden Protector telemetry viewer")
    ap.add_argument("--db", default=None, help="telemetry.db path (default: standard locations)")
    ap.add_argument("--cid", default=None, help="show one correlation id (a single trip), oldest first")
    ap.add_argument("--component", default=None, help="filter by component (camera|deterrent|http|fsm|trigger|system)")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--stats", action="store_true", help="show event count + dropped/write-error counters")
    args = ap.parse_args()

    path = args.db or (DEFAULT_DB if os.path.exists(DEFAULT_DB) else FALLBACK_DB)
    if not os.path.exists(path):
        print(f"No telemetry DB found at {path}", file=sys.stderr)
        sys.exit(1)
    conn = _connect(path)

    if args.stats:
        meta = dict(conn.execute("SELECT k, v FROM meta").fetchall())
        total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        print(f"db={path}")
        print(f"events={total} dropped={meta.get('dropped', '0')} write_errors={meta.get('write_errors', '0')}")
        return

    where, params = [], []
    if args.cid:
        where.append("cid = ?")
        params.append(args.cid)
    if args.component:
        where.append("component = ?")
        params.append(args.component)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    order = "ts ASC" if args.cid else "ts DESC"
    params.append(args.limit)

    rows = conn.execute(
        f"SELECT ts, cid, component, op, outcome, dur_ms, detail, args FROM events {clause} ORDER BY {order} LIMIT ?",
        params,
    ).fetchall()

    for ts, cid, comp, op, outcome, dur, detail, args in rows:
        d = f"{dur:.1f}ms" if dur is not None else "-"
        flag = "!" if outcome != "ok" else " "
        info = detail or args or ""  # fall back to args (e.g. fsm from/to) when no detail
        print(f"{ts:.3f} {flag}[{cid or '-':>16}] {comp}/{op} {outcome} {d} {info}")


if __name__ == "__main__":
    main()
