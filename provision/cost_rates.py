"""Cost model for the project's Fastly footprint — pure rate constants + cost math.

This module is intentionally
**pure**: no network and no I/O beyond an optional rates-override file. Both the
admin console (``provision/console.py``) and the Pi LAN portal
(``hardware/portal.py``) import it so ONE cost model serves both surfaces — the
only difference between them is presentation.

The companion ``provision/usage_stats.py`` does the I/O (Fastly Stats API + FOS
object listing) and hands this module a plain ``usage`` dict; see ``compute_actual``
for that shape.
"""

import json
import pathlib

# ---------------------------------------------------------------------------
# Fastly list-price defaults (USD).
# ---------------------------------------------------------------------------
# Fastly published list ("rack") prices — https://www.fastly.com/pricing (fetched 2026-06-23).
# Each is the FIRST PAID tier of its meter; Fastly's free allowances (e.g. first 100 GB BW,
# first 10M Compute requests, first 5 GB storage) and volume discounts are NOT modeled, so the
# estimate is a conservative ceiling for small usage. Operators on a negotiated contract
# override ANY of these on the Costs page (persisted to configs/cost-rates.json).
DEFAULT_RATES = {
    "class_a_rate_per_1k": 0.0025,  # Object Storage Class A ops (PUT/POST/COPY/LIST), $/1,000
    "class_b_rate_per_10k": 0.004,  # Object Storage Class B ops (GET/HEAD), $/10,000 ($0.0004/1k)
    "storage_rate_per_gb_month": 0.02,  # Object Storage, $/GB-month (5 GB–50 TB tier)
    "cdn_egress_rate_per_gb": 0.12,  # Full Site Delivery bandwidth, North America (100 GB–10 TB)
    "min_billed_days": 30,  # storage minimum-billing floor (days)
    "compute_rate_per_10k_req": 0.005,  # Compute requests, $0.50/1M = $0.005/10,000
    # KV Store (a.k.a. Object Store) ops are billed per CLASS at very different rates + units,
    # so they're modeled as two knobs, not one. (Fastly also bills KV storage $/GB and Compute
    # vCPU-ms; those dimensions aren't tracked here — see the usage notes.)
    "kv_class_a_rate_per_100k": 0.65,  # KV Store Class A ops (writes), $/100,000 (100k–1M tier)
    "kv_class_b_rate_per_1m": 0.55,  # KV Store Class B ops (reads),  $/1,000,000 (1M–100M tier)
}

RATES_FILE = "cost-rates.json"  # under configs/ (gitignored, like other configs)

GIB = 1024**3  # bytes per GiB
HOURS_PER_MONTH = 720  # 30 days * 24 h — the GB-month basis used throughout

# Each uploaded photo also writes TWO KV entries on the edge — `latest_image` and
# `latest_event` (backend publish_evidence) — so KV write-ops scale 2:1 with photos
# and the forward estimate must count them. (The token/config look-ups per request are
# additional store ops we don't model here, so this is a conservative floor.)
KV_WRITES_PER_PHOTO = 2


# ---------------------------------------------------------------------------
# Rate persistence (the only I/O in this module).
# ---------------------------------------------------------------------------


def load_rates(configs_dir):
    """DEFAULT_RATES overlaid with any persisted overrides in
    ``configs/cost-rates.json``. A missing/corrupt file => plain defaults."""
    rates = dict(DEFAULT_RATES)
    p = pathlib.Path(configs_dir) / RATES_FILE
    if not p.exists():
        return rates
    try:
        saved = json.loads(p.read_text())
    except (OSError, ValueError):
        return rates
    return _coerce_into(rates, saved)


def save_rates(configs_dir, patch):
    """Merge a (possibly partial) ``patch`` of rate overrides over the current
    rates, persist the result, and return the merged dict. Only known keys are
    accepted and each is coerced to its proper numeric type, so a malformed POST
    can never corrupt the model."""
    d = pathlib.Path(configs_dir)
    d.mkdir(parents=True, exist_ok=True)
    merged = _coerce_into(load_rates(configs_dir), patch or {})
    (d / RATES_FILE).write_text(json.dumps(merged, indent=2))
    return merged


def _coerce_into(rates, patch):
    """Overlay ``patch`` onto ``rates`` for known keys only, coercing types
    (min_billed_days is an int; everything else a float). Bad values are skipped."""
    for k in DEFAULT_RATES:
        if k not in patch:
            continue
        try:
            rates[k] = int(patch[k]) if k == "min_billed_days" else float(patch[k])
        except (TypeError, ValueError):
            pass  # keep the prior value on a bad input
    return rates


# ---------------------------------------------------------------------------
# Cost math (pure). Both functions consume the same ``usage`` dict and return
# {"lines": [<line>...], "total": <float>} where each <line> is:
#   {"key", "label", "qty", "unit", "cost", "estimated"}
# ``qty`` is the underlying measured number (ops, GB, requests) for display;
# ``cost`` is its dollar contribution; ``estimated`` flags modeled (non-billed)
# numbers so the UI can label them.
#
# usage shape (all leaf numbers may be None = "unavailable" -> qty 0, cost 0):
#   {
#     "window_hours": number,   # primary window length; prices sub-day windows
#     "window_days":  number,   # legacy fallback (used only when window_hours absent)
#     "fos":      {"objects": int|None, "bytes": int|None},
#     "fos_ops":  {"class_a": int|None, "class_b": int|None},   # account-wide; None when skipped
#     "cdn":      {"requests": int|None, "edge_requests": int|None,
#                  "bandwidth_bytes": int|None},
#     "compute":  {"requests": int|None, "bandwidth_bytes": int|None, ...},
#     "store_ops":{"kv_a": int|None, "kv_b": int|None,   # KV Class A (writes) / B (reads)
#                  "estimated": bool, "measured": bool},
#   }
# Egress is the sum of CDN and Compute bandwidth: real photo delivery flows through
# the Compute edge service, not just the (often idle) CDN delivery service.
# ---------------------------------------------------------------------------


def compute_actual(usage, rates):
    """Cost of the *measured* usage over the observed window (cost-to-date).

    Op/bandwidth/request counts are already windowed by the Stats ``from``/``to``.
    Storage is a point-in-time footprint, so its windowed cost is the current GB
    held for the window's fraction of a month."""
    u = _norm(usage)
    window_frac = (
        u["window_hours"] / HOURS_PER_MONTH
    )  # window as a fraction of a 30-day month
    lines = [
        _line(
            "storage",
            "FOS storage",
            u["stored_gb"],
            "GB",
            u["stored_gb"] * window_frac * rates["storage_rate_per_gb_month"],
        ),
        _line(
            "fos_class_a",
            "FOS Class A ops",
            u["class_a"],
            "ops",
            _price(u["class_a"], rates["class_a_rate_per_1k"], 1_000),
        ),
        _line(
            "fos_class_b",
            "FOS Class B ops",
            u["class_b"],
            "ops",
            _price(u["class_b"], rates["class_b_rate_per_10k"], 10_000),
        ),
        _line(
            "cdn_egress",
            "CDN egress",
            u["egress_gb"],
            "GB",
            u["egress_gb"] * rates["cdn_egress_rate_per_gb"],
        ),
        _line(
            "compute",
            "Compute requests",
            u["compute_req"],
            "req",
            _price(u["compute_req"], rates["compute_rate_per_10k_req"], 10_000),
        ),
        _line(
            "store_ops",
            "KV store ops",
            u["kv_a"] + u["kv_b"],
            "ops",
            _store_ops_cost(u["kv_a"], u["kv_b"], rates),
            estimated=True,
        ),
    ]
    return {"lines": lines, "total": sum(l["cost"] for l in lines)}


def compute_monthly(usage, rates):
    """Estimated cost for a full 30-day month, projecting the observed window.

    Flow/ops project linearly (``* 720 / window_hours``); storage applies the
    30-day minimum-billing floor (``max(window_hours, 720) / 720``), which for any
    window shorter than a month is just "one full month of the current footprint".
    Short windows (e.g. the 1-hour view, a 720x projection) are inherently noisier
    — the UI flags that — but the math is the same linear extrapolation throughout."""
    u = _norm(usage)
    proj = HOURS_PER_MONTH / u["window_hours"]  # window -> month
    window_hours = u["window_hours"]
    # Fastly bills a per-object storage minimum (default 30 days) even if a photo is
    # deleted sooner, so storage never costs less than `min_billed_days` of the current
    # footprint — e.g. a garden that started today is already billed a full month.
    min_hours = max(1, int(rates.get("min_billed_days") or 30)) * 24
    storage_factor = (
        max(window_hours, min_hours) / HOURS_PER_MONTH
    )  # >= min-billing floor
    lines = [
        _line(
            "storage",
            "FOS storage",
            u["stored_gb"],
            "GB-month",
            u["stored_gb"] * storage_factor * rates["storage_rate_per_gb_month"],
        ),
        _line(
            "fos_class_a",
            "FOS Class A ops",
            round(u["class_a"] * proj),
            "ops/mo",
            _price(u["class_a"] * proj, rates["class_a_rate_per_1k"], 1_000),
        ),
        _line(
            "fos_class_b",
            "FOS Class B ops",
            round(u["class_b"] * proj),
            "ops/mo",
            _price(u["class_b"] * proj, rates["class_b_rate_per_10k"], 10_000),
        ),
        _line(
            "cdn_egress",
            "CDN egress",
            u["egress_gb"] * proj,
            "GB/mo",
            u["egress_gb"] * proj * rates["cdn_egress_rate_per_gb"],
        ),
        _line(
            "compute",
            "Compute requests",
            round(u["compute_req"] * proj),
            "req/mo",
            _price(u["compute_req"] * proj, rates["compute_rate_per_10k_req"], 10_000),
        ),
        _line(
            "store_ops",
            "KV store ops",
            round((u["kv_a"] + u["kv_b"]) * proj),
            "ops/mo",
            _store_ops_cost(u["kv_a"] * proj, u["kv_b"] * proj, rates),
            estimated=True,
        ),
    ]
    return {"lines": lines, "total": sum(l["cost"] for l in lines)}


def estimate_capture_monthly(*, photos_per_day, bytes_per_photo, retention_days, rates):
    """Forward-looking monthly cost of a capture setting (cadence + photo size),
    reusing :func:`compute_monthly` so the Storage-page estimate and the Costs page
    share ONE model. Returns ``{monthly_usd, storage_usd, ops_usd, kv_ops_usd,
    photos_per_day, stored_mb, billed_days}``.

    Storage applies the ``min_billed_days`` floor: an object is billed at least that
    many days even if purged sooner, so keeping photos for <= the floor costs the same
    — the only levers are how many photos (``photos_per_day``) and how big
    (``bytes_per_photo``). We bake the floor into the steady-state footprint and run a
    full-month window (proj 1, storage_factor 1) so the floor isn't double-counted.

    Per uploaded photo the edge does one FOS PUT (Class A) AND two KV writes
    (``KV_WRITES_PER_PHOTO``), so both scale with cadence. The KV writes are KV Class A
    ops, priced via ``kv_class_a_rate_per_100k``, so cadence is an even bigger lever than
    storage+PUTs alone suggest."""
    min_days = max(1, int(rates.get("min_billed_days") or 30))
    billed_days = max(int(retention_days or 0), min_days)
    stored_bytes = photos_per_day * billed_days * bytes_per_photo
    photos_per_month = photos_per_day * 30
    usage = {
        "window_hours": HOURS_PER_MONTH,  # proj=1; floor encoded in stored_bytes
        "fos": {"bytes": stored_bytes},
        "fos_ops": {"class_a": photos_per_month},  # one FOS PUT per uploaded photo
        # latest_image + latest_event written per upload -> KV Class A (write) ops, cadence-scaled.
        "store_ops": {"kv_a": photos_per_month * KV_WRITES_PER_PHOTO},
    }
    monthly = compute_monthly(usage, rates)
    by_key = {l["key"]: l["cost"] for l in monthly["lines"]}
    return {
        "monthly_usd": monthly["total"],
        "storage_usd": by_key.get("storage", 0.0),
        "ops_usd": by_key.get("fos_class_a", 0.0),
        "kv_ops_usd": by_key.get("store_ops", 0.0),
        "photos_per_day": photos_per_day,
        "stored_mb": stored_bytes / (1024**2),
        "billed_days": billed_days,
    }


# -- internal helpers -------------------------------------------------------


def _norm(usage):
    """Flatten the nested usage dict into scalars, mapping every None -> 0 so the
    math never trips on an 'unavailable' resource.

    The window is normalized to **hours** so sub-day windows (1h/24h) price
    correctly. ``window_hours`` wins when present; otherwise it's derived from the
    legacy ``window_days`` field. Floored to 1 hour to keep projections finite."""
    usage = usage or {}
    fos = usage.get("fos") or {}
    ops = usage.get("fos_ops") or {}
    cdn = usage.get("cdn") or {}
    comp = usage.get("compute") or {}
    store = usage.get("store_ops") or {}
    window_hours = usage.get("window_hours")
    if window_hours is None:
        window_hours = (usage.get("window_days") or 0) * 24
    return {
        "window_hours": max(1.0, float(window_hours or 0)),
        "stored_gb": (fos.get("bytes") or 0) / GIB,
        "class_a": ops.get("class_a") or 0,
        "class_b": ops.get("class_b") or 0,
        # Real photo egress flows through the Compute edge, not just the CDN service.
        "egress_gb": (
            (cdn.get("bandwidth_bytes") or 0) + (comp.get("bandwidth_bytes") or 0)
        )
        / GIB,
        "compute_req": comp.get("requests") or 0,
        # KV Store ops, split by class (priced at different rates/units). Back-compat: an old
        # caller passing a single "kv" total is treated as Class B (reads, the common case).
        "kv_a": store.get("kv_a") or 0,
        "kv_b": (
            store.get("kv_b") if store.get("kv_b") is not None else store.get("kv")
        )
        or 0,
    }


def _price(count, rate_per_unit, unit):
    """(count / unit) * rate — the canonical per-N-operations pricing."""
    return (count / unit) * rate_per_unit


def _store_ops_cost(kv_a, kv_b, rates):
    """KV Store cost = Class A (writes, $/100k) + Class B (reads, $/1M), priced separately
    because Fastly bills the two classes at very different rates and units."""
    return _price(kv_a, rates["kv_class_a_rate_per_100k"], 100_000) + _price(
        kv_b, rates["kv_class_b_rate_per_1m"], 1_000_000
    )


def _line(key, label, qty, unit, cost, *, estimated=False):
    return {
        "key": key,
        "label": label,
        "qty": qty,
        "unit": unit,
        "cost": cost,
        "estimated": estimated,
    }


# ---------------------------------------------------------------------------
# Display formatters
# ---------------------------------------------------------------------------


def fmt_usd(n):
    """USD: >=1000 -> "$1,234" (no cents); 1-999 -> "$12.34"; <1 -> "$0.0040"."""
    n = float(n or 0)
    if n >= 1000:
        return "$" + f"{n:,.0f}"
    if n >= 1:
        return "$" + f"{n:.2f}"
    return "$" + f"{n:.4f}"


def fmt_n(n):
    """Compact count: 1.23B / 4.56M / 7.8K / 123."""
    n = float(n or 0)
    if n >= 1e9:
        return f"{n / 1e9:.2f}B"
    if n >= 1e6:
        return f"{n / 1e6:.2f}M"
    if n >= 1e3:
        return f"{n / 1e3:.1f}K"
    return f"{int(n):,}"
