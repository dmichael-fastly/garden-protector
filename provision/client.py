"""Fastly API transport — `fastly()` (JSON) + `fastly_raw()` (bytes).

urllib + tenacity retry/backoff with jitter, `Fastly-Key` auth, except->RuntimeError
translation. `FASTLY_MOCK_MODE=1` short-circuits the wire so the CLI dry-run and
unit tests never touch the network (it also RECORDS calls for assertions).
"""

import json
import os
import urllib.error
import urllib.request

import tenacity

API_BASE = "https://api.fastly.com"
_RETRYABLE_HTTP_CODES = (429, 500, 502, 503, 504)

# Recorded calls when FASTLY_MOCK_MODE is set: list of (method, path, body).
MOCK_CALLS: list[tuple] = []


def is_mock_mode() -> bool:
    return os.environ.get("FASTLY_MOCK_MODE", "") not in ("", "0", "false")


def _mock_response(method: str, path: str, body):
    """Canned responses good enough for a dry-run / offline test. Mirrors the
    minimal shapes the orchestrator + registry rely on."""
    MOCK_CALLS.append((method, path, body))
    # Match on the path only — strip any query string (e.g. /service?page=1&per_page=100
    # from the paginated find_service_by_name, or KV ?cursor=) so canned shapes still hit.
    p = path.split("?", 1)[0].rstrip("/")
    if method == "POST" and p == "/service":
        return {"id": "MOCKSVC", "version": 1}
    if p.endswith("/clone"):
        return {"number": 2}
    if method == "POST" and p in (
        "/resources/stores/kv",
        "/resources/stores/config",
        "/resources/stores/secret",
    ):
        return {"id": f"MOCK-{p.rsplit('/', 1)[1].upper()}-STORE"}
    if p == "/tokens/self":
        return {"scope": "global", "services": []}
    if method == "GET" and p.endswith("/keys"):
        return {"data": []}
    if method == "GET" and (p == "/service" or p.endswith("/version")):
        return []
    return {}


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in _RETRYABLE_HTTP_CODES
    return isinstance(exc, (urllib.error.URLError, ConnectionError, TimeoutError))


def _request(
    method, path, *, data, headers, expect_empty, max_retries, timeout, raw=False
):
    """Issue one retried Fastly request. `raw=True` returns the response body as
    UNPARSED bytes (no decode, no json.loads) — needed for byte-exact KV values such
    as the ONNX model. Otherwise returns parsed JSON (or {} when empty)."""
    url = API_BASE + path
    try:
        for attempt in tenacity.Retrying(
            retry=tenacity.retry_if_exception(_is_retryable),
            stop=tenacity.stop_after_attempt(max_retries + 1),
            wait=tenacity.wait_exponential(multiplier=1, min=1, max=8)
            + tenacity.wait_random(min=0, max=2),
            reraise=True,
        ):
            with attempt:
                req = urllib.request.Request(
                    url, data=data, headers=headers, method=method
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    body = resp.read()
                    if raw:
                        return body
                    raw_text = body.decode()
                    if expect_empty or not raw_text.strip():
                        return {}
                    return json.loads(raw_text)
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {exc.code} {method} {path}\n    {body_text}") from exc
    except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
        raise RuntimeError(f"Network error on {method} {path}: {exc}") from exc


def fastly(
    method, path, body=None, *, token, expect_empty=False, max_retries=3, timeout=30
):
    """JSON Fastly API call -> parsed JSON (or {} when empty)."""
    if is_mock_mode():
        return _mock_response(method, path, body)
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Fastly-Key": token, "Accept": "application/json"}
    if data:
        hdrs["Content-Type"] = "application/json"
    return _request(
        method,
        path,
        data=data,
        headers=hdrs,
        expect_empty=expect_empty,
        max_retries=max_retries,
        timeout=timeout,
    )


def fastly_raw(
    method,
    path,
    data: bytes,
    *,
    content_type,
    token,
    expect_empty=False,
    max_retries=3,
    timeout=60,
):
    """Raw-bytes Fastly API call (KV value PUT, Wasm package upload)."""
    if is_mock_mode():
        MOCK_CALLS.append((method, path, f"<{len(data)} bytes>"))
        return {}
    hdrs = {
        "Fastly-Key": token,
        "Accept": "application/json",
        "Content-Type": content_type,
    }
    return _request(
        method,
        path,
        data=data,
        headers=hdrs,
        expect_empty=expect_empty,
        max_retries=max_retries,
        timeout=timeout,
    )


def fastly_raw_get(path, *, token, max_retries=3, timeout=60) -> bytes:
    """GET a Fastly resource and return the response body as UNPARSED bytes.

    Used for KV value reads: routing a KV GET through `fastly()` would json.loads
    then re-json.dumps the value, reordering keys and CORRUPTING any non-JSON (e.g.
    the raw ONNX model bytes in `garden_models`). This keeps the transport byte-exact;
    JSON parsing stays in the caller (the registry layer). 404 surfaces as RuntimeError
    (so callers can detect an absent key), like the other transports."""
    if is_mock_mode():
        MOCK_CALLS.append(("GET", path, None))
        # No canned KV value in mock mode -> treat as absent (404), so the registry
        # hydrate path falls through to an empty mirror without re-serializing.
        raise RuntimeError(f"HTTP 404 GET {path}\n    (mock: no KV value)")
    hdrs = {"Fastly-Key": token, "Accept": "application/octet-stream"}
    return _request(
        "GET",
        path,
        data=None,
        headers=hdrs,
        expect_empty=False,
        max_retries=max_retries,
        timeout=timeout,
        raw=True,
    )
