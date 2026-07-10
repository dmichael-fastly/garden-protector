"""Tests for the cost calculator: the pure cost core (provision/cost_rates.py),
the usage fetchers (provision/usage_stats.py), and the two HTTP surfaces that
expose them — the admin console (/api/console/cost, /api/console/cost-rates) and
the Pi portal (/api/cost, /costs).

Live Fastly/S3 calls are never made: the fetcher unit tests monkeypatch the
transport, and the HTTP tests run in FASTLY_MOCK_MODE so every fetcher degrades to
'unavailable' (which is itself a behaviour worth asserting — a partial outage must
still return a renderable page, never a 500).
"""
import json
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

import pytest
import requests

import hardware.portal as pt
from provision import console, cost_rates, usage_stats

GIB = 1024 ** 3


# ===========================================================================
# Pure cost core (provision/cost_rates.py)
# ===========================================================================

def _usage(**over):
    u = {
        "window_days": 10,
        "fos": {"objects": 1000, "bytes": 2 * GIB},                  # 2 GB
        "fos_ops": {"class_a": 50_000, "class_b": 200_000},
        "cdn": {"requests": 100_000, "edge_requests": 90_000, "bandwidth_bytes": 5 * GIB},
        "compute": {"requests": 1_000_000},
        "store_ops": {"kv_a": 100_000, "kv_b": 2_000_000, "estimated": True},
    }
    u.update(over)
    return u


def test_compute_actual_hand_computed():
    a = cost_rates.compute_actual(_usage(), dict(cost_rates.DEFAULT_RATES))
    by = {l["key"]: l for l in a["lines"]}
    # Class A = 50000/1000 * 0.0025 = 0.125 ; Class B = 200000/10000 * 0.004 = 0.08
    assert by["fos_class_a"]["cost"] == pytest.approx(0.125)
    assert by["fos_class_b"]["cost"] == pytest.approx(0.08)
    # CDN egress = 5 GB * 0.12 = 0.60
    assert by["cdn_egress"]["cost"] == pytest.approx(0.60)
    # Storage over a 10-day window = 2 GB * (240/720) * 0.02
    assert by["storage"]["cost"] == pytest.approx(2 * (240 / 720) * 0.02)
    # Compute = 1,000,000/10,000 * 0.005 = 0.50
    assert by["compute"]["cost"] == pytest.approx(0.5)
    # KV = writes 100k/100k*0.65 + reads 2M/1M*0.55 = 0.65 + 1.10 = 1.75 (priced per class)
    assert by["store_ops"]["cost"] == pytest.approx(1.75)
    assert by["store_ops"]["estimated"] is True
    assert by["store_ops"]["qty"] == 2_100_000          # kv_a + kv_b
    assert a["total"] == pytest.approx(0.125 + 0.08 + 0.60 + by["storage"]["cost"] + 0.5 + 1.75)


def test_compute_monthly_projection_and_storage_floor():
    m = cost_rates.compute_monthly(_usage(), dict(cost_rates.DEFAULT_RATES))
    by = {l["key"]: l for l in m["lines"]}
    # Class A projected: 50000 * (30/10) = 150000 -> /1000 * 0.0025 = 0.375
    assert by["fos_class_a"]["cost"] == pytest.approx(0.375)
    assert by["fos_class_a"]["qty"] == 150_000
    # Storage applies the 30-day min-billing floor: 2 GB * max(240,720)/720 * 0.02 = 0.04
    assert by["storage"]["cost"] == pytest.approx(2 * 0.02)
    # CDN egress projected: 5 GB * 3 = 15 GB * 0.12 = 1.80
    assert by["cdn_egress"]["cost"] == pytest.approx(1.80)


def test_estimate_capture_monthly_hand_computed():
    rates = dict(cost_rates.DEFAULT_RATES)  # storage 0.02/GB-mo, class_a 0.0025/1k, kv_a 0.65/100k
    # 2 cameras @ 30s -> 5760 photos/day; standard ~35 KB; 30-day history.
    est = cost_rates.estimate_capture_monthly(
        photos_per_day=5760, bytes_per_photo=35_000, retention_days=30, rates=rates)
    # storage: 5760 * 30 * 35000 bytes = 6.048e9 -> /GiB = 5.6325 GB * 0.02 = 0.11265
    assert est["storage_usd"] == pytest.approx(5760 * 30 * 35_000 / (1024 ** 3) * 0.02)
    # PUTs: 5760 * 30 = 172800 FOS Class A/mo -> /1000 * 0.0025 = 0.432
    assert est["ops_usd"] == pytest.approx(172_800 / 1_000 * 0.0025)
    # KV writes: 172800 * 2 = 345600 Class A/mo -> /100000 * 0.65 = 2.2464
    assert est["kv_ops_usd"] == pytest.approx(172_800 * 2 / 100_000 * 0.65)
    assert est["monthly_usd"] == pytest.approx(est["storage_usd"] + est["ops_usd"] + est["kv_ops_usd"])
    assert est["photos_per_day"] == 5760 and est["billed_days"] == 30


def test_estimate_capture_monthly_levers_and_floor():
    rates = dict(cost_rates.DEFAULT_RATES)
    def cost(ppd, b, r=30):
        return cost_rates.estimate_capture_monthly(
            photos_per_day=ppd, bytes_per_photo=b, retention_days=r, rates=rates)["monthly_usd"]
    # More frequent (more photos/day) costs more; smaller photos cost less.
    assert cost(11520, 35_000) > cost(5760, 35_000)          # 15s vs 30s
    assert cost(5760, 14_000) < cost(5760, 35_000) < cost(5760, 120_000)  # saver<standard<high
    # Purging BELOW the 30-day floor saves nothing; ABOVE it costs more.
    assert cost(5760, 35_000, r=7) == pytest.approx(cost(5760, 35_000, r=30))
    assert cost(5760, 35_000, r=60) > cost(5760, 35_000, r=30)
    # No cameras -> no photos -> no cost.
    assert cost(0, 35_000) == 0


def test_estimate_capture_monthly_counts_kv_write_ops():
    # Every uploaded photo writes KV on the edge (latest_image + latest_event) -> KV Class A
    # writes, priced at Fastly's rack rate (0.65/100k) and overridable.
    base = dict(cost_rates.DEFAULT_RATES)
    e0 = cost_rates.estimate_capture_monthly(
        photos_per_day=5760, bytes_per_photo=35_000, retention_days=30, rates=base)
    # 172800 photos/mo * 2 writes = 345600 KV Class A/mo -> /100000 * 0.65
    assert e0["kv_ops_usd"] == pytest.approx(
        172_800 * cost_rates.KV_WRITES_PER_PHOTO / 100_000 * 0.65)
    assert e0["monthly_usd"] == pytest.approx(
        e0["storage_usd"] + e0["ops_usd"] + e0["kv_ops_usd"])

    priced = dict(cost_rates.DEFAULT_RATES)
    priced["kv_class_a_rate_per_100k"] = 1.30         # a negotiated (2x) per-op price
    e1 = cost_rates.estimate_capture_monthly(
        photos_per_day=5760, bytes_per_photo=35_000, retention_days=30, rates=priced)
    assert e1["kv_ops_usd"] == pytest.approx(
        172_800 * cost_rates.KV_WRITES_PER_PHOTO / 100_000 * 1.30)
    assert e1["monthly_usd"] > e0["monthly_usd"]      # higher KV rate -> higher total
    # Cadence drives KV 2:1 with photos: half as often -> half the KV cost.
    e_half = cost_rates.estimate_capture_monthly(
        photos_per_day=2880, bytes_per_photo=35_000, retention_days=30, rates=priced)
    assert e_half["kv_ops_usd"] == pytest.approx(e1["kv_ops_usd"] / 2)


def test_min_billed_days_drives_storage_floor():
    # Fastly bills a per-object storage minimum even if a photo is deleted sooner.
    rates = dict(cost_rates.DEFAULT_RATES)
    u = {"window_days": 7, "fos": {"objects": 10, "bytes": 1 * GIB}}   # 1 GB, 7-day window
    # Default 30-day minimum -> a full month is billed: 1 GB * 0.02 = 0.02.
    by = {l["key"]: l for l in cost_rates.compute_monthly(u, rates)["lines"]}
    assert by["storage"]["cost"] == pytest.approx(0.02)
    # Raise the minimum to 60 days -> two months billed -> 0.04.
    rates["min_billed_days"] = 60
    by = {l["key"]: l for l in cost_rates.compute_monthly(u, rates)["lines"]}
    assert by["storage"]["cost"] == pytest.approx(0.04)


def test_window_hours_drives_subday_core():
    # window_hours takes precedence over window_days and prices sub-day windows.
    # 12-hour window = half a day of storage cost: 1 GB * (12/720) * 0.02.
    u = {"window_hours": 12, "fos": {"objects": 1, "bytes": GIB}}
    by = {l["key"]: l for l in cost_rates.compute_actual(u, dict(cost_rates.DEFAULT_RATES))["lines"]}
    assert by["storage"]["cost"] == pytest.approx(1 * (12 / 720) * 0.02)


def test_compute_monthly_projects_from_subday_window():
    rates = dict(cost_rates.DEFAULT_RATES)
    # 1-hour window: a 30-day month is 720x the window, so ops project up by 720.
    u1 = {"window_hours": 1, "fos_ops": {"class_a": 10, "class_b": 0}}
    by1 = {l["key"]: l for l in cost_rates.compute_monthly(u1, rates)["lines"]}
    assert by1["fos_class_a"]["qty"] == 7200            # 10 * 720
    # 24-hour window projects up by 30.
    u2 = {"window_hours": 24, "fos_ops": {"class_a": 10}}
    by2 = {l["key"]: l for l in cost_rates.compute_monthly(u2, rates)["lines"]}
    assert by2["fos_class_a"]["qty"] == 300             # 10 * 30
    # Storage still honours the 30-day minimum floor even for a 1-hour window.
    u3 = {"window_hours": 1, "fos": {"bytes": GIB}}
    by3 = {l["key"]: l for l in cost_rates.compute_monthly(u3, rates)["lines"]}
    assert by3["storage"]["cost"] == pytest.approx(0.02)


def test_compute_degrades_when_all_unavailable():
    empty = {"window_days": 7, "fos": None, "fos_ops": None, "cdn": None,
             "compute": None, "store_ops": {"kv": None}}
    assert cost_rates.compute_actual(empty, dict(cost_rates.DEFAULT_RATES))["total"] == 0
    assert cost_rates.compute_monthly(empty, dict(cost_rates.DEFAULT_RATES))["total"] == 0


def test_egress_includes_compute_bandwidth():
    # Real photo egress flows through the Compute edge, so its bandwidth must count
    # toward CDN egress alongside the (often idle) CDN service's bandwidth.
    u = _usage(cdn={"bandwidth_bytes": 1 * GIB},
               compute={"requests": 10, "bandwidth_bytes": 3 * GIB})
    by = {l["key"]: l for l in cost_rates.compute_actual(u, dict(cost_rates.DEFAULT_RATES))["lines"]}
    # (1 + 3) GB * 0.12 = 0.48
    assert by["cdn_egress"]["cost"] == pytest.approx(4 * 0.12)


def test_custom_rate_prices_compute_and_store_ops():
    rates = dict(cost_rates.DEFAULT_RATES)
    rates["compute_rate_per_10k_req"] = 0.05      # $/10k req
    rates["kv_class_a_rate_per_100k"] = 1.00      # KV writes $/100k
    rates["kv_class_b_rate_per_1m"] = 2.00        # KV reads  $/1M
    by = {l["key"]: l for l in cost_rates.compute_actual(_usage(), rates)["lines"]}
    assert by["compute"]["cost"] == pytest.approx(1_000_000 / 10_000 * 0.05)   # 5.0
    # KV priced per class: writes 100k/100k*1.00 + reads 2M/1M*2.00 = 1.0 + 4.0 = 5.0
    assert by["store_ops"]["cost"] == pytest.approx(100_000 / 100_000 * 1.00 + 2_000_000 / 1_000_000 * 2.00)


def test_formatters_match_reference():
    assert cost_rates.fmt_usd(1234.5) == "$1,234"
    assert cost_rates.fmt_usd(12.345) == "$12.35"
    assert cost_rates.fmt_usd(0.004) == "$0.0040"
    assert cost_rates.fmt_n(2_500_000) == "2.50M"
    assert cost_rates.fmt_n(7_800) == "7.8K"
    assert cost_rates.fmt_n(123) == "123"


def test_rates_load_defaults_and_save_roundtrip(tmp_path):
    # No file -> plain defaults.
    assert cost_rates.load_rates(tmp_path) == cost_rates.DEFAULT_RATES
    # Save a partial override (+ a garbage value + an unknown key that must be ignored).
    merged = cost_rates.save_rates(tmp_path, {
        "class_a_rate_per_1k": "0.009", "min_billed_days": "15",
        "cdn_egress_rate_per_gb": "not-a-number", "bogus": 1})
    assert merged["class_a_rate_per_1k"] == 0.009
    assert merged["min_billed_days"] == 15 and isinstance(merged["min_billed_days"], int)
    assert merged["cdn_egress_rate_per_gb"] == cost_rates.DEFAULT_RATES["cdn_egress_rate_per_gb"]
    assert "bogus" not in merged
    assert (tmp_path / cost_rates.RATES_FILE).exists()
    # Reload picks up the persisted override.
    assert cost_rates.load_rates(tmp_path)["class_a_rate_per_1k"] == 0.009


# ===========================================================================
# Usage fetchers (provision/usage_stats.py)
# ===========================================================================

def test_parse_window_presets_legacy_and_clamp():
    pw = usage_stats.parse_window
    # Canonical preset tokens.
    assert pw("1h")["hours"] == 1 and pw("1h")["token"] == "1h"
    assert pw("24h")["hours"] == 24 and pw("24h")["days"] == 1
    assert pw("7d")["hours"] == 168 and pw("7d")["days"] == 7
    assert pw("30d")["hours"] == 720 and pw("30d")["label"] == "30 days"
    # Legacy bare number == days (so the old ?window=7 keeps meaning 7 days).
    assert pw(7)["hours"] == 168 and pw("30")["hours"] == 720
    # Garbage / None -> the chosen default; ceiling clamps to 90 days.
    assert pw("nonsense")["token"] == "30d"
    assert pw(None, default="7d")["token"] == "7d"
    assert pw("9999d")["hours"] == 90 * 24
    # A valid non-preset window gets a synthesized token + label.
    assert pw("3d")["label"] == "3 days" and pw("3d")["token"] == "3d"


def test_granularity_scales_with_window():
    assert usage_stats._granularity(1) == "minute"     # 1-hour view: minute resolution
    assert usage_stats._granularity(24) == "hour"      # 24-hour view: hourly
    assert usage_stats._granularity(24 * 7) == "day"   # 7/30-day views: daily rollup


def test_gather_usage_accepts_window_token(monkeypatch):
    monkeypatch.setenv("FASTLY_MOCK_MODE", "1")
    svc = {"service_id": "S", "cdn_service_id": "C"}
    g = usage_stats.gather_usage(svc, "tok", "24h")
    assert g["window"] == "24h" and g["window_hours"] == 24 and g["window_days"] == 1
    assert g["fos"] is None and g["compute"] is None   # still degrades in mock mode


def test_gather_usage_picks_granularity_and_bounds(monkeypatch):
    monkeypatch.setattr(usage_stats.client, "is_mock_mode", lambda: False)
    seen = []
    monkeypatch.setattr(usage_stats.client, "fastly",
                        lambda method, path, **k: seen.append(path) or {"data": []})
    # FOS inventory needs real creds; leave them off so it short-circuits to None.
    usage_stats.gather_usage({"service_id": "S", "cdn_service_id": "C"}, "tok", "1h",
                             now=10_000)
    # 1-hour window -> by=minute, and the from/to span exactly one hour.
    assert all("by=minute" in p for p in seen)
    assert any("from=6400" in p and "to=10000" in p for p in seen)   # 10000 - 3600


def test_extract_fos_ops_flat_and_nested():
    assert usage_stats._extract_fos_ops(
        {"object_storage_class_a_operations_count": 5,
         "object_storage_class_b_operations_count": 7}) == (5, 7)
    assert usage_stats._extract_fos_ops(
        {"object_storage": {"class_a_operations_count": 3,
                            "class_b_operations_count": 9}}) == (3, 9)
    assert usage_stats._extract_fos_ops({}) == (0, 0)


def test_stats_records_flattens_list_and_map(monkeypatch):
    monkeypatch.setattr(usage_stats.client, "is_mock_mode", lambda: False)
    monkeypatch.setattr(usage_stats.client, "fastly",
                        lambda *a, **k: {"data": [{"requests": 1}, {"requests": 2}]})
    assert usage_stats._stats_records("tok", "/x") == [{"requests": 1}, {"requests": 2}]
    # /stats/aggregate sometimes keys records by timestamp -> flatten the values
    monkeypatch.setattr(usage_stats.client, "fastly",
                        lambda *a, **k: {"data": {"100": [{"requests": 1}], "200": {"requests": 2}}})
    recs = usage_stats._stats_records("tok", "/x")
    assert {"requests": 1} in recs and {"requests": 2} in recs


def test_stats_records_none_on_error_or_mock(monkeypatch):
    monkeypatch.setattr(usage_stats.client, "is_mock_mode", lambda: True)
    assert usage_stats._stats_records("tok", "/x") is None
    monkeypatch.setattr(usage_stats.client, "is_mock_mode", lambda: False)

    def boom(*a, **k):
        raise RuntimeError("HTTP 502")
    monkeypatch.setattr(usage_stats.client, "fastly", boom)
    assert usage_stats._stats_records("tok", "/x") is None
    assert usage_stats._stats_records("", "/x") is None   # no token


def test_fos_ops_and_service_stats_sum(monkeypatch):
    monkeypatch.setattr(usage_stats.client, "is_mock_mode", lambda: False)
    monkeypatch.setattr(usage_stats.client, "fastly", lambda *a, **k: {"data": [
        {"object_storage_class_a_operations_count": 10, "object_storage_class_b_operations_count": 1,
         "requests": 100, "edge_requests": 90, "bandwidth": 1000},
        {"object_storage_class_a_operations_count": 5, "object_storage_class_b_operations_count": 2,
         "requests": 50, "edge_requests": 40, "bandwidth": 2000},
    ]})
    assert usage_stats.fos_ops("tok", 0, 1) == {"class_a": 15, "class_b": 3}
    s = usage_stats.service_stats("tok", "SVC", 0, 1)
    # New per-service store-op fields default to 0 for a VCL-shaped record.
    assert s == {"requests": 150, "edge_requests": 130, "bandwidth_bytes": 3000,
                 "kv_class_a": 0, "kv_class_b": 0, "object_class_a": 0, "object_class_b": 0}
    assert usage_stats.service_stats("tok", "", 0, 1) is None   # no service id


def test_service_stats_reads_compute_and_store_op_fields(monkeypatch):
    # A Compute (wasm) service reports traffic under `compute_requests` (NOT `requests`)
    # and exposes real per-service KV / Object-Store op counts.
    monkeypatch.setattr(usage_stats.client, "is_mock_mode", lambda: False)
    monkeypatch.setattr(usage_stats.client, "fastly", lambda *a, **k: {"data": [
        {"requests": 0, "compute_requests": 4520, "bandwidth": 7_000_000,
         "kv_store_class_a_operations": 38, "kv_store_class_b_operations": 25_636,
         "object_store_class_a_operations": 1, "object_store_class_b_operations": 2},
    ]})
    s = usage_stats.service_stats("tok", "WASM", 0, 1)
    assert s["requests"] == 4520            # compute_requests folded into requests
    assert s["bandwidth_bytes"] == 7_000_000
    assert s["kv_class_a"] == 38 and s["kv_class_b"] == 25_636
    assert s["object_class_a"] == 1 and s["object_class_b"] == 2


def test_estimate_store_ops_models_from_requests():
    # Fallback model (no real KV counters): split into writes (Class A) and reads (Class B).
    e = usage_stats.estimate_store_ops({"requests": 1000})
    assert e["estimated"] is True and e["measured"] is False
    assert e["kv_a"] == 500 and e["kv_b"] == 2700   # 0.5 writes + 2.7 reads per request
    # Unknown request volume -> nulls, not a fabricated zero.
    assert usage_stats.estimate_store_ops(None)["kv_a"] is None


def test_estimate_store_ops_prefers_real_counts_and_dedups():
    # When service_stats measured real per-service KV ops, use them (not the model), split by
    # class. KV Store == the renamed Object Store: Stats double-reports the SAME ops under both
    # names (identical counts), so dedup via per-class max instead of summing.
    e = usage_stats.estimate_store_ops(
        {"requests": 4520, "kv_class_a": 38, "kv_class_b": 25_636,
         "object_class_a": 38, "object_class_b": 25_636})
    assert e["measured"] is True and e["estimated"] is False
    assert e["kv_a"] == 38 and e["kv_b"] == 25_636
    # Older accounts may report only the object_store_* names -> still counted once.
    e2 = usage_stats.estimate_store_ops({"object_class_a": 5, "object_class_b": 7})
    assert e2["kv_a"] == 5 and e2["kv_b"] == 7 and e2["measured"] is True


def test_gather_usage_all_unavailable_in_mock(monkeypatch):
    monkeypatch.setenv("FASTLY_MOCK_MODE", "1")
    svc = {"service_id": "S", "cdn_service_id": "C", "fos_region": "us-east-1",
           "fos_bucket": "b", "fos_access_key_id": "ak", "fos_secret_access_key": "sk"}
    g = usage_stats.gather_usage(svc, "tok", 7)
    assert g["window_days"] == 7
    assert g["fos"] is None and g["fos_ops"] is None and g["cdn"] is None and g["compute"] is None
    assert g["store_ops"]["estimated"] is True


def test_gather_usage_skips_account_wide_aggregate(monkeypatch):
    # include_fos_aggregate=False (the per-garden portal path) must NOT hit /stats/aggregate
    # and must leave fos_ops None even with a working transport.
    monkeypatch.setattr(usage_stats.client, "is_mock_mode", lambda: False)
    seen = []
    monkeypatch.setattr(usage_stats.client, "fastly",
                        lambda method, path, **k: seen.append(path) or {"data": []})
    g = usage_stats.gather_usage({"service_id": "S", "cdn_service_id": "C"}, "tok", "24h",
                                 include_fos_aggregate=False)
    assert g["fos_ops"] is None
    assert not any("/stats/aggregate" in p for p in seen)
    # The per-service calls still happen.
    assert any("/stats/service/S" in p for p in seen)


# ===========================================================================
# Admin console HTTP  (/api/console/cost, /api/console/cost-rates)
# ===========================================================================

@contextmanager
def _console_server(configs_dir):
    cfg = console.ConsoleConfig(configs_dir=configs_dir, mock=True)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), console.make_handler(cfg))
    t = threading.Thread(target=srv.serve_forever, args=(0.02,), daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def _write_service(configs_dir, sid="S1"):
    (configs_dir / f"{sid}.json").write_text(json.dumps({
        "service_id": sid, "service_name": "gp-test", "cdn_service_id": "C1",
        "fos_region": "us-east-1", "fos_bucket": "b",
        "fos_access_key_id": "ak", "fos_secret_access_key": "sk",
    }))


@pytest.fixture
def console_url(tmp_path, monkeypatch):
    monkeypatch.setenv("FASTLY_MOCK_MODE", "1")   # no live Fastly/S3 calls
    _write_service(tmp_path)
    with _console_server(tmp_path) as url:
        yield url, tmp_path


def test_console_cost_requires_service_id(console_url):
    url, _ = console_url
    assert requests.get(url + "/api/console/cost", timeout=5).status_code == 400


def test_console_cost_unknown_service_404(console_url):
    url, _ = console_url
    r = requests.get(url + "/api/console/cost?service_id=NOPE", timeout=5)
    assert r.status_code == 404


def test_console_cost_ok_and_graceful_in_mock(console_url):
    url, _ = console_url
    r = requests.get(url + "/api/console/cost?service_id=S1&window=30", timeout=5)
    assert r.status_code == 200
    d = r.json()
    assert d["service_id"] == "S1" and d["window_days"] == 30
    # Mock mode -> every measured resource is unavailable, totals 0, page still renders.
    assert d["usage"]["fos"] is None and d["usage"]["cdn"] is None
    assert d["actual"]["total"] == 0 and d["monthly"]["total"] == 0
    assert d["rates"]["class_a_rate_per_1k"] == cost_rates.DEFAULT_RATES["class_a_rate_per_1k"]
    # The breakdown lines are always present so the table renders.
    keys = {l["key"] for l in d["monthly"]["lines"]}
    assert {"storage", "fos_class_a", "fos_class_b", "cdn_egress", "compute", "store_ops"} <= keys


def test_console_cost_accepts_window_token(console_url):
    url, _ = console_url
    d = requests.get(url + "/api/console/cost?service_id=S1&window=1h", timeout=5).json()
    assert d["window"] == "1h" and d["window_hours"] == 1
    assert d["window_label"] == "1 hour"
    assert d["usage"]["window"] == "1h" and d["usage"]["window_hours"] == 1


def test_console_save_rates_persists_and_reflects(console_url):
    url, configs_dir = console_url
    r = requests.post(url + "/api/console/cost-rates",
                      json={"class_a_rate_per_1k": 0.0099}, timeout=5)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["rates"]["class_a_rate_per_1k"] == 0.0099
    assert (configs_dir / cost_rates.RATES_FILE).exists()
    # A subsequent cost read uses the persisted rate.
    d = requests.get(url + "/api/console/cost?service_id=S1", timeout=5).json()
    assert d["rates"]["class_a_rate_per_1k"] == 0.0099


def test_console_cost_blocked_without_session_when_passcode_set(tmp_path, monkeypatch):
    monkeypatch.setenv("FASTLY_MOCK_MODE", "1")
    _write_service(tmp_path)
    console.save_auth_record(tmp_path, console.make_auth_record("hunter2"))
    with _console_server(tmp_path) as url:
        assert requests.get(url + "/api/console/cost?service_id=S1", timeout=5).status_code == 401


# ===========================================================================
# Pi portal HTTP  (/api/cost, /costs)
# ===========================================================================

class _PortalHarness:
    def __init__(self):
        self.portal = pt.Portal(
            admin_passcode="hunter2", session_secret=b"test-secret", edge="",
            costs_html="<html><body>What your garden costs</body></html>",
            rate_limiter=pt.RateLimiter(max_fails=5, window_s=60, lockout_s=300))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), pt.make_handler(self.portal))
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._t = threading.Thread(target=self.server.serve_forever, args=(0.02,), daemon=True)
        self._t.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def portal(monkeypatch):
    monkeypatch.setenv("FASTLY_MOCK_MODE", "1")
    h = _PortalHarness()
    yield h
    h.close()


def _portal_session(h):
    s = requests.Session()
    s.post(h.url + "/login", data={"passcode": "hunter2"}, allow_redirects=False, timeout=5)
    return s


def test_portal_cost_requires_admin(portal):
    assert requests.get(portal.url + "/api/cost", timeout=5).status_code == 401  # API -> JSON 401
    # the /costs PAGE shows the login form (200) when unauthed, not a JSON 401
    r = requests.get(portal.url + "/costs", timeout=5)
    assert r.status_code == 200 and "Passcode" in r.text


def test_portal_costs_page_served_when_authed(portal):
    s = _portal_session(portal)
    r = s.get(portal.url + "/costs", timeout=5)
    assert r.status_code == 200 and "What your garden costs" in r.text


def test_portal_cost_graceful_without_deployment(portal):
    s = _portal_session(portal)
    r = s.get(portal.url + "/api/cost", timeout=5)
    assert r.status_code == 200
    # No pi_config/deployment wired -> available:false, never a 500.
    assert r.json()["available"] is False


class _FakePiConfig:
    """Just enough of pi_config for the portal /api/cost path + its capture/maintenance
    helpers, so the per-garden cards can be exercised without a real deployment."""
    def __init__(self, d):
        self.dir = d
        self._cfg = {
            "fastly": {"service_name": "gp-test", "service_id": "S1", "cdn_service_id": "C1"},
            "devices": [{"type": "camera_csi", "status": "active"},
                        {"type": "camera_usb", "status": "active"}],   # 2 cameras
        }

    def load(self):
        return self._cfg

    def get_secret(self, key):
        return "tok" if key == "fastly_api_token" else None

    def capture_settings(self):
        return {"interval_s": 900, "quality": "standard", "daylight_only": True}


def test_portal_cost_cards_are_per_garden(portal, tmp_path, monkeypatch):
    # Wire a minimal deployment + synthetic per-garden usage, then assert the cards
    # show per-garden numbers (NOT the old account-wide millions / 0s).
    (tmp_path / "S1.json").write_text(json.dumps({
        "service_id": "S1", "service_name": "gp-test", "cdn_service_id": "C1",
        "fos_region": "us-east-1", "fos_bucket": "b",
        "fos_access_key_id": "ak", "fos_secret_access_key": "sk"}))
    portal.portal.pi_config = _FakePiConfig(tmp_path)

    # KV + Object counts are identical (same product, double-reported) -> must dedup to 60,010.
    compute = {"requests": 200_000, "edge_requests": 0, "bandwidth_bytes": 500_000_000,
               "kv_class_a": 10, "kv_class_b": 60_000, "object_class_a": 10, "object_class_b": 60_000}
    fake_usage = {
        "window": "30d", "window_label": "30 days", "window_hours": 720, "window_days": 30,
        "from": 0, "to": 1,
        "fos": {"objects": 54, "bytes": 1_000_000},
        "fos_ops": {"class_a": 13_127_325, "class_b": 4_679_138},   # account-wide (context only)
        "cdn": {"requests": 60, "edge_requests": 33, "bandwidth_bytes": 2_345_842,
                "kv_class_a": 0, "kv_class_b": 0, "object_class_a": 0, "object_class_b": 0},
        "compute": compute,
        "store_ops": usage_stats.estimate_store_ops(compute),
    }
    monkeypatch.setattr(usage_stats, "gather_usage", lambda *a, **k: fake_usage)

    s = _portal_session(portal)
    d = s.get(portal.url + "/api/cost", timeout=5).json()
    assert d["available"] is True
    cards = {i["label"]: i for i in d["items"]}

    # 1) Photos kept safe: exact kept count + a MODELED saved/mo (2 cams @ 900s = 5,760/mo),
    #    never the account-wide millions.
    kept = cards["Photos kept safe"]["detail"]
    assert "54 kept" in kept and "5.8K saved/mo" in kept
    assert "M saved/mo" not in kept

    # 2) Photo deliveries: real bytes sent (Compute + CDN), not "0 MB".
    deliv = cards["Photo deliveries"]["detail"]
    assert "sent/mo" in deliv and "0 MB" not in deliv

    # 3) Always-on guarding (compute checks) and 4) Notes & look-ups (KV/store ops) are
    #    broken out into their own cards, each with its own count.
    assert "200.0K checks/mo" in cards["Always-on guarding"]["detail"]
    assert "60.0K notes & look-ups/mo" in cards["Notes & look-ups"]["detail"]  # deduped (not 120K)

    # Headline reconciles with the sum of the visible cards.
    def _money_to_float(s):
        return 0.0 if s in ("$0.00", "less than 1¢") else float(s.replace("$", "").replace(",", ""))
    card_sum = sum(_money_to_float(c["monthly"]) for c in d["items"] if c.get("available"))
    assert d["monthly_total"] == pytest.approx(card_sum, abs=0.01)
    # Account-wide FOS Class A (13.1M * $0.0025/1k = $32.82) is rolled into "Photos kept
    # safe" + the headline.
    assert _money_to_float(cards["Photos kept safe"]["monthly"]) > 30
    assert d["monthly_total"] > 33

    # 5) Each card lists the Fastly operations rolled up into it (per-section breakdown),
    #    in the RIGHT section.
    def ops_of(label):
        return {o["label"]: o for o in cards[label]["ops"]}
    # Always-on guarding -> compute requests
    assert ops_of("Always-on guarding")["Compute requests"]["count"] == 200_000
    # Notes & look-ups -> KV writes/reads, deduped (no separate Object Store rows)
    notes_ops = ops_of("Notes & look-ups")
    assert notes_ops["KV store Class A ops — writes"]["count"] == 10
    assert notes_ops["KV store Class B ops — reads"]["count"] == 60_000
    assert not any("Object Store" in l for l in notes_ops)
    # Photos kept safe -> storage count + modeled uploads + account-wide FOS Class A
    kept_ops = ops_of("Photos kept safe")
    assert kept_ops["Photos in storage"]["count"] == 54
    assert kept_ops["Photos uploaded — FOS Class A PUT"]["per_mo"] == 5_760
    assert kept_ops["Photos uploaded — FOS Class A PUT"]["basis"] == "modeled"
    fos_a = kept_ops["FOS Class A ops — PUT/POST/COPY/LIST"]
    assert fos_a["count"] == 13_127_325 and "account-wide" in fos_a["scope"]
    # The account-wide row is starred and its cost IS included in the card/headline total.
    assert fos_a["star"] is True and fos_a["cost"] == pytest.approx(13_127_325 / 1000 * 0.0025)
    # Each op row carries its unit rate.
    assert "1,000 ops" in fos_a["rate"]
    assert "GB-month" in kept_ops["Storage used (GB-month)"]["rate"]
    assert ops_of("Always-on guarding")["Compute requests"]["rate"].endswith("10,000 requests")
    # Photo deliveries -> account-wide FOS Class B GETs (starred, in total) + egress rate
    deliv_ops = ops_of("Photo deliveries")
    assert deliv_ops["FOS Class B ops — GET/HEAD"]["count"] == 4_679_138
    assert deliv_ops["FOS Class B ops — GET/HEAD"]["star"] is True
    assert "GB" in deliv_ops["Data sent — Compute + CDN egress"]["rate"]


def test_portal_cost_rates_requires_admin(portal):
    r = requests.post(portal.url + "/api/cost-rates",
                      json={"class_a_rate_per_1k": 0.01}, timeout=5)
    assert r.status_code == 401


def test_portal_cost_rates_409_without_config(portal):
    # Authed, but the harness wires no pi_config -> nowhere to persist -> 409, not 500.
    s = _portal_session(portal)
    r = s.post(portal.url + "/api/cost-rates",
               json={"class_a_rate_per_1k": 0.01}, timeout=5)
    assert r.status_code == 409


# --- Pi-side archive delete (usage_stats.fos_prune date filter) --------------

def test_key_date_extracts_utc_date_from_archive_key():
    k = "g/gid/evidence/2026/06/22/02082_232518_none_class-417_6_cam_c1.jpg"
    assert usage_stats._key_date(k) == "2026-06-22"
    assert usage_stats._key_date("g/gid/evidence/nope.jpg") is None  # no YYYY/MM/DD path
    assert usage_stats._key_date("g/gid/other/2026/06/22/x.jpg") is None  # not under /evidence/


def test_fos_prune_deletes_only_expired_keys(monkeypatch):
    # The prune match keeps keys whose embedded date is < cutoff; undated keys are skipped.
    deleted = []

    class FakeS3:
        def get_paginator(self, _):
            return self
        def paginate(self, **kw):
            return [{"Contents": [
                {"Key": "g/g/evidence/2026/06/01/a.jpg"},   # old -> delete
                {"Key": "g/g/evidence/2026/06/20/b.jpg"},   # within window -> keep
                {"Key": "g/g/evidence/2026/06/22/c.jpg"},   # today -> keep
                {"Key": "g/g/misc/d.jpg"},                  # undated -> skip
            ]}]
        def delete_objects(self, Bucket, Delete):
            deleted.extend(o["Key"] for o in Delete["Objects"])
            return {"Errors": []}

    monkeypatch.setattr(usage_stats.client, "is_mock_mode", lambda: False)
    monkeypatch.setattr(usage_stats.fos_setup, "_s3_client", lambda *a: FakeS3())
    svc = {"fos_region": "r", "fos_bucket": "b",
           "fos_access_key_id": "ak", "fos_secret_access_key": "sk"}
    res = usage_stats.fos_prune(svc, "g/g/evidence/", "2026-06-15")
    assert deleted == ["g/g/evidence/2026/06/01/a.jpg"]   # only the expired one
    assert res["deleted"] == 1 and res["failed"] == 0 and res["days_pruned"] == 1
