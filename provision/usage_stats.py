"""Live usage fetchers for the cost calculator.

Three substrates feed the cost model (see ``provision/cost_rates.py``):

  * **Fastly Historical Stats API** — FOS operation counts (account-wide) via
    ``/stats/aggregate`` and per-service CDN + Compute counters via
    ``/stats/service/{id}``.
  * **FOS object listing** — exact image count + bytes by paginating the bucket
    with boto3 (reuses ``fos_setup._s3_client``); optionally scoped to one garden
    by the ``g/<gid>/`` key prefix.
  * **A modeled estimate** of KV/Secret/Config operation counts, derived from edge
    request volume. Fastly's Stats API has NO field for store ops and the edge's
    per-op ``log_evt`` records are log-tail only (not historically queryable), so
    these are a labeled estimate, not a billed count.

Every fetcher **degrades to None on any failure** (mock mode, absent token,
missing boto3, network error, unexpected payload) so a partial outage still
renders a page — the caller maps None to an "unavailable" row.
"""

import concurrent.futures
import re
import time

from . import client, fos_setup

# Default edge-activity model for the KV-ops estimate, used ONLY as a fallback when the
# per-service Stats don't carry real KV counters (a real Compute service always does, so
# this rarely fires). Split by class because Fastly prices KV writes/reads differently:
# each edge request reads a handful of flag/config/state keys (Class B) and a fraction
# write (Class A). Rough + clearly surfaced as an estimate.
DEFAULT_OP_MODEL = {
    "kv_write_per_request": 0.5,  # Class A: state/flag writes amortized per request
    "kv_read_per_request": 2.7,  # Class B: state + flag + config + secret reads per request
}


# ---------------------------------------------------------------------------
# Window selection. The cost pages let you scope usage to a trailing window;
# these are the canonical choices (1 hour … 30 days). A window is carried around
# as a stable token ("1h"/"24h"/"7d"/"30d") plus its duration in hours, so the
# cost core can price sub-day windows — not just whole days.
# ---------------------------------------------------------------------------
WINDOW_CHOICES = [
    {"token": "1h", "label": "1 hour", "hours": 1},
    {"token": "24h", "label": "24 hours", "hours": 24},
    {"token": "7d", "label": "7 days", "hours": 24 * 7},
    {"token": "30d", "label": "30 days", "hours": 24 * 30},
]
_WINDOW_BY_TOKEN = {c["token"]: c for c in WINDOW_CHOICES}
DEFAULT_WINDOW = "30d"
MAX_WINDOW_HOURS = 24 * 90  # 90-day ceiling (matches the old day-based clamp)


def parse_window(value, default=DEFAULT_WINDOW):
    """Normalize a window selector into ``{token, label, hours, days}``.

    Accepts a preset token (``1h``/``24h``/``7d``/``30d``), a ``<n>h``/``<n>d``
    string, or a bare number (legacy: interpreted as **days**, so the old
    ``?window=7`` keeps meaning "7 days"). Unrecognized/garbage input falls back
    to ``default``. Hours are clamped to ``[1, 90 days]``; a value that matches a
    known preset snaps to it, otherwise a token + label are synthesized."""
    hours = _value_to_hours(value)
    if hours is None:
        hours = _WINDOW_BY_TOKEN.get(default, _WINDOW_BY_TOKEN[DEFAULT_WINDOW])["hours"]
    hours = max(1, min(int(hours), MAX_WINDOW_HOURS))
    for c in WINDOW_CHOICES:
        if c["hours"] == hours:
            return _win(c["token"], c["label"], hours)
    if hours % 24 == 0:
        d = hours // 24
        return _win(f"{d}d", f"{d} days", hours)
    return _win(f"{hours}h", f"{hours} hours", hours)


def _value_to_hours(value):
    """A window value -> hour count, or None when unrecognizable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) * 24  # legacy bare number == days
    s = str(value).strip().lower()
    if not s:
        return None
    if s in _WINDOW_BY_TOKEN:
        return _WINDOW_BY_TOKEN[s]["hours"]
    try:
        if s.endswith("h"):
            return int(s[:-1])
        if s.endswith("d"):
            return int(s[:-1]) * 24
        return int(s) * 24  # legacy bare digits == days
    except ValueError:
        return None


def _win(token, label, hours):
    days = hours / 24
    if days == int(days):
        days = int(days)
    return {"token": token, "label": label, "hours": int(hours), "days": days}


def _granularity(window_hours):
    """Pick the Stats API ``by`` rollup for a window: minute resolution for the
    short (1-hour) view so partial clock-hours aren't over-counted, hourly for a
    day or two, daily for the long windows (30 daily records beat 720 hourly)."""
    if window_hours <= 2:
        return "minute"
    if window_hours <= 48:
        return "hour"
    return "day"


def window_bounds_hours(window_hours, *, now=None):
    """(from_ts, to_ts) epoch-second bounds for a trailing ``window_hours`` window."""
    to_ts = int(now if now is not None else time.time())
    return to_ts - int(window_hours) * 3600, to_ts


def window_epoch(window_days, *, now=None):
    """Legacy day-based bounds; prefer :func:`window_bounds_hours`."""
    return window_bounds_hours(int(window_days) * 24, now=now)


# ---------------------------------------------------------------------------
# Fastly Historical Stats API.
# ---------------------------------------------------------------------------


def _stats_records(token, path):
    """GET a Stats endpoint and normalize its payload to a flat list of records.

    The Stats API returns {"data": [...]} for /stats/service and either a list or
    a {ts: record|[records]} map for /stats/aggregate; we flatten both. Returns
    None on any failure so the caller can show 'unavailable'."""
    if not token or client.is_mock_mode():
        return None
    try:
        payload = client.fastly("GET", path, token=token)
    except Exception:  # noqa: BLE001 — any transport/HTTP error => unavailable, not a crash
        return None
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        out = []
        for v in data.values():
            if isinstance(v, list):
                out.extend(v)
            elif isinstance(v, dict):
                out.append(v)
        return out
    return []


def _int(d, key):
    try:
        return int(d.get(key) or 0)
    except (TypeError, ValueError, AttributeError):
        return 0


def _extract_fos_ops(record):
    """Class A / Class B counts from a /stats/aggregate record. Ported from
    ``usage.py:_extract_fos_ops`` — the fields are usually flattened with an
    ``object_storage_`` prefix but may be nested under an ``object_storage`` dict."""
    a = _int(record, "object_storage_class_a_operations_count")
    b = _int(record, "object_storage_class_b_operations_count")
    if a or b:
        return a, b
    sub = record.get("object_storage") if isinstance(record, dict) else None
    if isinstance(sub, dict):
        return _int(sub, "class_a_operations_count"), _int(
            sub, "class_b_operations_count"
        )
    return 0, 0


def fos_ops(token, from_ts, to_ts, by="hour"):
    """Account-wide FOS Class A/B operation counts over the window. None if
    unavailable. NOTE: Fastly reports these account-wide — not per-service."""
    recs = _stats_records(token, f"/stats/aggregate?by={by}&from={from_ts}&to={to_ts}")
    if recs is None:
        return None
    a = b = 0
    for r in recs:
        ca, cb = _extract_fos_ops(r)
        a += ca
        b += cb
    return {"class_a": a, "class_b": b}


def service_stats(token, service_id, from_ts, to_ts, by="hour"):
    """Per-service traffic + store-op counters over the window. None if unavailable
    or no service id.

    Handles BOTH service types: a VCL/delivery service reports its traffic under
    ``requests`` (and ``compute_requests`` is 0); a Compute (wasm) service reports
    under ``compute_requests`` (and ``requests`` is 0). We sum both so ``requests``
    is the real total either way. ``kv_*``/``object_*`` are the per-service Fastly
    KV / Object-Store op counts (a Compute-only product, distinct from the FOS S3
    image bucket measured by :func:`fos_ops`)."""
    if not service_id:
        return None
    recs = _stats_records(
        token, f"/stats/service/{service_id}?by={by}&from={from_ts}&to={to_ts}"
    )
    if recs is None:
        return None
    out = {
        "requests": 0,
        "edge_requests": 0,
        "bandwidth_bytes": 0,
        "kv_class_a": 0,
        "kv_class_b": 0,
        "object_class_a": 0,
        "object_class_b": 0,
    }
    for r in recs:
        # VCL services count `requests`; Compute (wasm) services count `compute_requests`.
        # One of the two is always 0, so summing both gives the right total per type.
        out["requests"] += _int(r, "requests") + _int(r, "compute_requests")
        out["edge_requests"] += _int(r, "edge_requests")
        out["bandwidth_bytes"] += _int(r, "bandwidth")
        out["kv_class_a"] += _int(r, "kv_store_class_a_operations")
        out["kv_class_b"] += _int(r, "kv_store_class_b_operations")
        out["object_class_a"] += _int(r, "object_store_class_a_operations")
        out["object_class_b"] += _int(r, "object_store_class_b_operations")
    return out


# ---------------------------------------------------------------------------
# FOS object inventory (exact, via S3 listing).
# ---------------------------------------------------------------------------


def fos_inventory(svc, garden_prefix=None):
    """Exact {objects, bytes} for the bucket (or one garden's ``g/<gid>/`` prefix)
    by paginating ``list_objects_v2``. Reuses ``fos_setup._s3_client``. Returns
    None when mock-mode, boto3 is absent, the bucket/keys aren't configured, or any
    S3 error occurs. (Listing itself costs Class A LIST ops — surfaced in the UI.)"""
    region = svc.get("fos_region")
    bucket = svc.get("fos_bucket")
    ak = svc.get("fos_access_key_id")
    sk = svc.get("fos_secret_access_key")
    if client.is_mock_mode() or not (region and bucket and ak and sk):
        return None
    try:
        s3 = fos_setup._s3_client(region, ak, sk)
        paginator = s3.get_paginator("list_objects_v2")
        kwargs = {"Bucket": bucket}
        if garden_prefix:
            kwargs["Prefix"] = garden_prefix
        objects = 0
        total = 0
        for page in paginator.paginate(**kwargs):
            for o in page.get("Contents", []):
                objects += 1
                total += int(o.get("Size") or 0)
        return {"objects": objects, "bytes": total}
    except Exception:  # noqa: BLE001 — missing boto3 / auth / network => unavailable
        return None


_EVIDENCE_DATE_RE = re.compile(r"/evidence/(\d{4})/(\d{2})/(\d{2})/")


def _key_date(key):
    """The ``YYYY-MM-DD`` embedded in an archive key path (``.../evidence/YYYY/MM/DD/...``),
    or None if the key doesn't match (so the prune never touches non-dated objects)."""
    m = _EVIDENCE_DATE_RE.search(key)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def _fos_bulk_delete(
    svc, list_prefix, *, match=None, max_objects=None, count_days=False
):
    """Shared Pi-side bulk delete. Paginates ``list_prefix`` and deletes every key where
    ``match(key)`` is truthy (``match=None`` -> all), via boto3 ``delete_objects`` (1000/batch)
    with a per-object ``delete_object`` fallback if FOS rejects the batch op. This lives on the
    Pi because the EDGE can't bulk-delete — Fastly Compute caps backend sends per execution
    (~16) — and FOS has no working bulk DeleteObjects to call from the edge. ``max_objects``
    bounds one call (the portal loops while ``remaining``). Returns
    ``{deleted, failed, remaining, sample[, days_pruned]}`` or None when FOS unconfigured/mock."""
    region = svc.get("fos_region")
    bucket = svc.get("fos_bucket")
    ak = svc.get("fos_access_key_id")
    sk = svc.get("fos_secret_access_key")
    if client.is_mock_mode() or not (region and bucket and ak and sk and list_prefix):
        return None

    deleted = 0
    failed = 0
    sample = [None]  # boxed so the inner closure can set it
    days = set()

    def _result(remaining):
        r = {
            "deleted": deleted,
            "failed": failed,
            "remaining": remaining,
            "sample": sample[0],
        }
        if count_days:
            r["days_pruned"] = len(days)
        return r

    try:
        s3 = fos_setup._s3_client(region, ak, sk)

        def flush(objs):
            nonlocal deleted, failed
            if not objs:
                return
            try:
                resp = s3.delete_objects(
                    Bucket=bucket, Delete={"Objects": objs, "Quiet": True}
                )
                errs = resp.get("Errors") or []
                failed += len(errs)
                deleted += len(objs) - len(errs)
                if errs and sample[0] is None:
                    sample[0] = f"{errs[0].get('Code')}: {errs[0].get('Message')}"[:140]
            except Exception as e:  # noqa: BLE001 — batch unsupported? per-object fallback
                if sample[0] is None:
                    sample[0] = f"batch->single ({str(e)[:80]})"
                for o in objs:
                    try:
                        s3.delete_object(Bucket=bucket, Key=o["Key"])
                        deleted += 1
                    except Exception as e2:  # noqa: BLE001
                        failed += 1
                        if sample[0] is None:
                            sample[0] = str(e2)[:140]

        paginator = s3.get_paginator("list_objects_v2")
        batch = []
        for page in paginator.paginate(Bucket=bucket, Prefix=list_prefix):
            for o in page.get("Contents", []):
                key = o["Key"]
                if match is not None and not match(key):
                    continue
                batch.append({"Key": key})
                if count_days:
                    d = _key_date(key)
                    if d:
                        days.add(d)
                if len(batch) >= 1000:
                    flush(batch)
                    batch = []
                    if max_objects and (deleted + failed) >= max_objects:
                        return _result(True)
        flush(batch)
        return _result(False)
    except Exception as e:  # noqa: BLE001 — listing/auth/network failure
        if sample[0] is None:
            sample[0] = str(e)[:140]
        return _result(deleted > 0)


def fos_wipe(svc, garden_prefix, *, max_objects=None):
    """Delete EVERY object under ``garden_prefix`` (the operator's delete-all). Pi-side because
    the edge can't bulk-delete. See :func:`_fos_bulk_delete`."""
    return _fos_bulk_delete(svc, garden_prefix, match=None, max_objects=max_objects)


def fos_prune(svc, evidence_prefix, cutoff, *, max_objects=None):
    """Delete archived objects whose embedded date is STRICTLY BEFORE ``cutoff`` (YYYY-MM-DD)
    — the retention sweep, Pi-side (the edge prune hit the same ~16/execution backend cap on
    large expired days). Keys with no parseable date are skipped. ``days_pruned`` = distinct
    expired dates touched. See :func:`_fos_bulk_delete`."""
    return _fos_bulk_delete(
        svc,
        evidence_prefix,
        match=lambda k: (_key_date(k) or "9999-99-99") < cutoff,
        max_objects=max_objects,
        count_days=True,
    )


# ---------------------------------------------------------------------------
# Modeled KV/Secret/Config op estimate (no billing source exists).
# ---------------------------------------------------------------------------


def estimate_store_ops(compute_stats, model=None):
    """Per-service KV Store op counts, split by class: ``kv_a`` (Class A / writes) and
    ``kv_b`` (Class B / reads), priced at different Fastly rates. PREFERS the real counts
    :func:`service_stats` returns (``kv_class_a/b`` + ``object_class_a/b``) — those are
    billed, not modeled — and only falls back to the request-volume model when absent.

    None counts (unknown volume) when compute stats are unavailable, so the caller can
    render 'unavailable' rather than a fabricated zero."""
    m = {**DEFAULT_OP_MODEL, **(model or {})}
    if not compute_stats:
        return {
            "kv_a": None,
            "kv_b": None,
            "estimated": True,
            "measured": False,
            "model": m,
        }
    # Real per-service KV ops if service_stats measured them (Compute services do). Fastly
    # renamed "Object Store" -> "KV Store" and Stats reports the SAME ops under BOTH field
    # names (identical counts), so take the per-class MAX to dedup, not the sum.
    if any(
        k in compute_stats
        for k in ("kv_class_a", "kv_class_b", "object_class_a", "object_class_b")
    ):
        a = max(
            compute_stats.get("kv_class_a") or 0,
            compute_stats.get("object_class_a") or 0,
        )
        b = max(
            compute_stats.get("kv_class_b") or 0,
            compute_stats.get("object_class_b") or 0,
        )
        return {"kv_a": a, "kv_b": b, "estimated": False, "measured": True, "model": m}
    req = compute_stats.get("requests") or 0
    return {
        "kv_a": round(req * m["kv_write_per_request"]),
        "kv_b": round(req * m["kv_read_per_request"]),
        "estimated": True,
        "measured": False,
        "model": m,
    }


# ---------------------------------------------------------------------------
# One-shot assembly.
# ---------------------------------------------------------------------------


def gather_usage(
    svc,
    token,
    window,
    *,
    garden_prefix=None,
    now=None,
    op_model=None,
    include_fos_aggregate=True,
):
    """Assemble the full ``usage`` dict consumed by ``cost_rates``. Each piece is
    fetched independently and degrades to None on failure, so the result is always
    well-formed even with a missing token / boto3 / network.

    ``window`` is anything :func:`parse_window` accepts — a token (``1h``/``24h``/
    ``7d``/``30d``) or a legacy bare day count. The Stats rollup (``by``) scales
    with the window so the short views stay accurate and the long ones stay light.

    ``include_fos_aggregate`` controls the account-wide ``/stats/aggregate`` FOS-ops
    fetch (``fos_ops``). It's account-wide — there's no per-bucket breakdown — so a
    per-garden caller (the Pi portal) passes ``False`` to skip it and leave
    ``fos_ops`` None; the account-wide admin console keeps the default ``True``.

    The substrate calls (Compute stats, CDN stats, optional FOS op stats, S3 inventory)
    are independent, so they run concurrently — page latency is the slowest single
    call, not their sum. Each still fails soft to None on its own."""
    win = parse_window(window)
    from_ts, to_ts = window_bounds_hours(win["hours"], now=now)
    by = _granularity(win["hours"])
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        f_compute = ex.submit(
            service_stats, token, svc.get("service_id"), from_ts, to_ts, by
        )
        f_cdn = ex.submit(
            service_stats, token, svc.get("cdn_service_id"), from_ts, to_ts, by
        )
        f_ops = (
            ex.submit(fos_ops, token, from_ts, to_ts, by)
            if include_fos_aggregate
            else None
        )
        f_fos = ex.submit(fos_inventory, svc, garden_prefix)
        compute = f_compute.result()
        cdn = f_cdn.result()
        ops = f_ops.result() if f_ops is not None else None
        fos = f_fos.result()
    return {
        "window": win["token"],
        "window_label": win["label"],
        "window_hours": win["hours"],
        "window_days": win["days"],
        "from": from_ts,
        "to": to_ts,
        "fos": fos,
        "fos_ops": ops,
        "cdn": cdn,
        # Pass the full per-service dict through (requests + bandwidth + store-op counts).
        "compute": compute,
        "store_ops": estimate_store_ops(compute, model=op_model),
    }
