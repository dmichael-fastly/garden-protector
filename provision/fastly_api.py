"""Typed wrappers over the Fastly REST API used by gp-provision.

Endpoints:
- stores:    POST /resources/stores/{kv,config,secret}
- resource:  POST /service/{sid}/version/{v}/resource  (link store by name)
- versions:  GET .../version, PUT .../{v}/clone, GET .../{v}/validate, PUT .../{v}/activate
- kv item:   PUT /resources/stores/kv/{id}/keys/{key}   (raw bytes)
- secret:    PUT /resources/stores/secret/{id}/secrets  (idempotent create-or-recreate)
- package:   PUT /service/{sid}/version/{v}/package      (multipart, field "package")
"""

import base64
import urllib.parse

from .client import fastly, fastly_raw, fastly_raw_get

# Hostname suffixes allowed for `cdn_url` when wiring the edge's CDN read path.
# `cdn_url` is user-controllable at provision time (`--cdn-url`), so anything that
# isn't an https Fastly host — bare IPs, localhost, link-local 169.254.169.254
# (cloud-metadata SSRF), attacker hostnames — is rejected before we point an edge
# backend at it. The old `.fastly.net` suffix let ANY `*.fastly.net` host through (e.g. an
# attacker-registered map hostname on shared Fastly space). The only shapes WE
# generate are `{service_name}-cdn.global.ssl.fastly.net` (cli.py + wire_cdn_read.py),
# plus the Fastly object-storage / Compute domains, so the allowlist now matches
# those precise shapes — a bare `evil.fastly.net` no longer passes.
#   - `-cdn.global.ssl.fastly.net`  the generated CDN read-signing service domain
#   - `.fastlystorage.app`          Fastly Object Storage (FOS) origin
#   - `.edgecompute.app`            Fastly Compute service default domain
_CDN_URL_ALLOWED_HOST_SUFFIXES = (
    "-cdn.global.ssl.fastly.net",
    ".fastlystorage.app",
    ".edgecompute.app",
)

# Safety cap on `find_service_by_name`'s pagination loop (100/page -> 50k services).
# Far past any real account; bounds the loop so a misbehaving API can't spin forever.
_MAX_SERVICE_PAGES = 500


def safe_cdn_host(cdn_url: str) -> str | None:
    """Return the bare hostname of `cdn_url` only if it's an `https://` URL on an
    allowlisted Fastly host, else `None`. `None` means "don't wire the CDN read
    backend" — the edge then falls back to the direct FOS read."""
    if not cdn_url:
        return None
    try:
        parsed = urllib.parse.urlsplit(cdn_url)
    except ValueError:
        return None
    if parsed.scheme != "https":
        return None
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return None
    if any(hostname.endswith(suffix) for suffix in _CDN_URL_ALLOWED_HOST_SUFFIXES):
        return hostname
    return None


# ---------------------------------------------------------------------------
# Services + versions
# ---------------------------------------------------------------------------


def create_service(name: str, service_type: str, token: str) -> dict:
    return fastly("POST", "/service", {"name": name, "type": service_type}, token=token)


def find_service_by_name(name: str, token: str) -> dict | None:
    """Return the service whose name matches, or None.

    `GET /service` is PAGINATED (default ~20/page). Reading only the first page
    misses an existing service on a busy account, so `provision()` would take the
    create branch and stand up a DUPLICATE service + bucket + scoped key + stores.
    Page through (`?page=N&per_page=100`) until a short/empty page; stop as soon as
    a match is found. A page that is neither a bare array nor a `{"data": [...]}`
    envelope (or any short page) ends the loop. `_MAX_SERVICE_PAGES` is a hard cap
    so a misbehaving API can't spin forever."""
    per_page = 100
    for page in range(1, _MAX_SERVICE_PAGES + 1):
        resp = fastly("GET", f"/service?page={page}&per_page={per_page}", token=token)
        items = (
            resp
            if isinstance(resp, list)
            else (resp.get("data", []) if isinstance(resp, dict) else [])
        )
        match = next((s for s in items if s.get("name") == name), None)
        if match:
            return match
        if len(items) < per_page:  # last (short/empty) page — nothing more to fetch
            break
    return None


def latest_version(service_id: str, token: str) -> int:
    versions = fastly("GET", f"/service/{service_id}/version", token=token) or []
    return max((int(v["number"]) for v in versions), default=1)


def get_active_version(service_id: str, token: str) -> int:
    versions = fastly("GET", f"/service/{service_id}/version", token=token) or []
    active = [int(v["number"]) for v in versions if v.get("active")]
    return max(active) if active else latest_version(service_id, token)


def clone_version(service_id: str, base: int, token: str) -> int:
    clone = fastly("PUT", f"/service/{service_id}/version/{base}/clone", token=token)
    return int(clone["number"])


def validate_version(service_id: str, version: int, token: str) -> dict:
    return fastly(
        "GET", f"/service/{service_id}/version/{version}/validate", token=token
    )


def activate_version(service_id: str, version: int, token: str) -> None:
    fastly(
        "PUT",
        f"/service/{service_id}/version/{version}/activate",
        token=token,
        expect_empty=True,
    )


def purge_all(service_id: str, token: str) -> dict:
    """Invalidate the ENTIRE edge cache for a service (`POST /service/{id}/purge_all`).

    Always a HARD purge (purge-all can't be soft). Required after a deploy because the
    CDN caches `/static` (gp.css/gp.js) ignoring the `?v=` query and now also fronts the
    HTML pages — without this an operator keeps serving the old bytes until they age out.
    Propagation takes up to ~2 minutes; callers should wait before probing the live edge.
    Returns the API's `{"status": "ok"}` envelope (or `{}` under FASTLY_MOCK_MODE).
    """
    return fastly("POST", f"/service/{service_id}/purge_all", token=token)


# ---------------------------------------------------------------------------
# Stores (find-or-create) + resource links
# ---------------------------------------------------------------------------


def _store_id(resp: dict) -> str:
    """Tolerate the bare `{"id":...}` and the `{"data":{"id":...}}` envelope."""
    if not isinstance(resp, dict):
        return ""
    return resp.get("id") or (resp.get("data") or {}).get("id") or ""


def _find_store(endpoint: str, name: str, token: str) -> dict | None:
    try:
        resp = fastly("GET", endpoint, token=token)
    except RuntimeError:
        return None
    items = resp if isinstance(resp, list) else resp.get("data", [])
    return next((i for i in items if i.get("name") == name), None)


def ensure_kv_store(name: str, token: str) -> str:
    existing = _find_store("/resources/stores/kv", name, token)
    if existing:
        return _store_id(existing)
    return _store_id(
        fastly("POST", "/resources/stores/kv", {"name": name}, token=token)
    )


def ensure_config_store(name: str, token: str) -> str:
    existing = _find_store("/resources/stores/config", name, token)
    if existing:
        return _store_id(existing)
    return _store_id(
        fastly("POST", "/resources/stores/config", {"name": name}, token=token)
    )


def ensure_secret_store(name: str, token: str) -> str:
    existing = _find_store("/resources/stores/secret", name, token)
    if existing:
        return _store_id(existing)
    return _store_id(
        fastly("POST", "/resources/stores/secret", {"name": name}, token=token)
    )


def link_resource(
    service_id: str, version: int, link_name: str, resource_id: str, token: str
) -> None:
    """Link a store to a service version by its short name (the name the Rust
    `*::open()` uses). 409/already-linked is treated as success (idempotent)."""
    try:
        fastly(
            "POST",
            f"/service/{service_id}/version/{version}/resource",
            {"name": link_name, "resource_id": resource_id},
            token=token,
        )
    except RuntimeError as exc:
        s = str(exc).lower()
        if not ("409" in s or "conflict" in s or "already" in s):
            raise


# ---------------------------------------------------------------------------
# KV items + secrets (the registry blobs + per-garden tokens)
# ---------------------------------------------------------------------------


def kv_put(store_id: str, key: str, value: bytes, token: str) -> None:
    fastly_raw(
        "PUT",
        f"/resources/stores/kv/{store_id}/keys/{key}",
        value,
        content_type="application/octet-stream",
        token=token,
    )


def kv_get(store_id: str, key: str, token: str) -> bytes | None:
    """Read a KV value as BYTE-EXACT bytes. Returns None on 404 (absent key).

    Goes through `fastly_raw_get` so the value is returned UNPARSED — no json.loads
    -> re-json.dumps round-trip that would reorder JSON keys and corrupt non-JSON
    values (the `garden_models` store holds the raw ONNX model bytes). JSON parsing
    is the caller's job (the registry re-parses these docs)."""
    try:
        return fastly_raw_get(
            f"/resources/stores/kv/{store_id}/keys/{key}", token=token
        )
    except RuntimeError as exc:
        if "404" in str(exc):
            return None
        raise


def kv_list_keys(store_id: str, token: str) -> list:
    """All keys in a KV store (paginated). [] if the store is gone/empty."""
    keys, cursor = [], None
    while True:
        path = f"/resources/stores/kv/{store_id}/keys" + (
            f"?cursor={cursor}" if cursor else ""
        )
        try:
            r = fastly("GET", path, token=token)
        except RuntimeError:
            break
        keys.extend(r.get("data", []) if isinstance(r, dict) else (r or []))
        cursor = (
            (r.get("meta") or {}).get("next_cursor") if isinstance(r, dict) else None
        )
        if not cursor:
            break
    return keys


def kv_empty(store_id: str, token: str) -> int:
    """Delete every key in a KV store so it can be deleted (Fastly refuses to delete a
    non-empty KV store -> 409). Returns the number of keys removed."""
    n = 0
    for key in kv_list_keys(store_id, token):
        try:
            fastly(
                "DELETE",
                f"/resources/stores/kv/{store_id}/keys/{key}",
                token=token,
                expect_empty=True,
            )
            n += 1
        except RuntimeError:
            pass
    return n


def config_empty(store_id: str, token: str) -> int:
    """Delete every item in a Config Store so it can be deleted (Fastly refuses to
    delete a non-empty config store -> 409, same as KV). Returns the count removed."""
    n = 0
    try:
        r = fastly("GET", f"/resources/stores/config/{store_id}/items", token=token)
    except RuntimeError:
        return 0
    items = r.get("data", []) if isinstance(r, dict) else (r or [])
    for item in items:
        key = item.get("item_key") if isinstance(item, dict) else item
        if not key:
            continue
        try:
            fastly(
                "DELETE",
                f"/resources/stores/config/{store_id}/item/{key}",
                token=token,
                expect_empty=True,
            )
            n += 1
        except RuntimeError:
            pass
    return n


def config_item(store_id: str, key: str, value: str, token: str) -> dict:
    """Write a Config Store item (non-secret config). POST fails if the key exists;
    the caller treats that as idempotent."""
    return fastly(
        "POST",
        f"/resources/stores/config/{store_id}/item",
        {"item_key": key, "item_value": value},
        token=token,
    )


def secret_put(store_id: str, name: str, value: str, token: str) -> dict:
    """Create-or-recreate a secret (PUT is idempotent — ideal for rotation). The
    value is base64-encoded (the API requires it; supports binary)."""
    return fastly(
        "PUT",
        f"/resources/stores/secret/{store_id}/secrets",
        {"name": name, "secret": base64.b64encode(value.encode()).decode()},
        token=token,
    )


def secret_delete(store_id: str, name: str, token: str) -> None:
    try:
        fastly(
            "DELETE",
            f"/resources/stores/secret/{store_id}/secrets/{name}",
            token=token,
            expect_empty=True,
        )
    except RuntimeError as exc:
        if "404" not in str(exc):
            raise


# ---------------------------------------------------------------------------
# Compute package deploy (prebuilt .tar.gz — no Rust toolchain on the host)
# ---------------------------------------------------------------------------


def deploy_package(service_id: str, version: int, pkg_bytes: bytes, token: str) -> None:
    boundary = "----gp-package-boundary"
    body = (
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="package"; filename="package.tar.gz"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        + pkg_bytes
        + f"\r\n--{boundary}--\r\n".encode()
    )
    fastly_raw(
        "PUT",
        f"/service/{service_id}/version/{version}/package",
        body,
        content_type=f"multipart/form-data; boundary={boundary}",
        token=token,
    )


def add_domain(service_id: str, version: int, domain: str, token: str) -> None:
    fastly(
        "POST",
        f"/service/{service_id}/version/{version}/domain",
        {"name": domain},
        token=token,
    )


def add_backend(service_id: str, version: int, payload: dict, token: str) -> None:
    fastly(
        "POST", f"/service/{service_id}/version/{version}/backend", payload, token=token
    )


def token_scope(token: str) -> dict:
    """GET /tokens/self — used by teardown to require a `global`-scoped token."""
    return fastly("GET", "/tokens/self", token=token)


# ---------------------------------------------------------------------------
# Edge dictionaries (CDN credential + auth tables)
# ---------------------------------------------------------------------------


def create_dictionary(
    service_id: str, version: int, name: str, token: str, *, write_only: bool = True
) -> str:
    d = fastly(
        "POST",
        f"/service/{service_id}/version/{version}/dictionary",
        {"name": name, "write_only": write_only},
        token=token,
    )
    return d["id"]


def dictionary_item(
    service_id: str, dict_id: str, key: str, value: str, token: str
) -> None:
    fastly(
        "POST",
        f"/service/{service_id}/dictionary/{dict_id}/item",
        {"item_key": key, "item_value": value},
        token=token,
    )


# ---------------------------------------------------------------------------
# CDN read-signing service + cloud-logging teardown
# ---------------------------------------------------------------------------


def ensure_cdn_service(
    cfg: dict, fos_access_key: str, fos_secret_key: str, token: str
) -> dict:
    """FIND-or-create the VCL delivery service that SigV4-signs reads to the private
    FOS bucket and gates them with `cdn_secret`. Returns {cdn_service_id, cdn_url}.

    Reprovision-safe (CLOUD audit, mirrors scripts/wire_cdn_read.py:89-101): Fastly
    lets duplicate service names accumulate, so an UNCONDITIONAL create stood up a new
    billable CDN read-gate every run, of which teardown only ever recorded the LAST —
    leaking the rest forever. We now look the service up by name first. A same-named
    service can only be REUSED when the caller already holds its `cdn_secret`: the gate
    secret lives in a write-only dictionary (`cdn_auth`) and is otherwise unrecoverable,
    so without it the existing read gate is orphaned/unusable. The orchestrator records
    `cdn_secret` in cfg/state, so the normal reprovision path reuses cleanly; a stray
    same-named service whose secret we don't hold raises rather than silently colliding."""
    from .fos_setup import SHIELD_MAP, region_endpoint
    from .vcl import load_vcl

    region = cfg["fos_region"]
    bucket = cfg["fos_bucket"]
    cdn_secret = cfg["cdn_secret"]
    name = cfg.get("cdn_service_name", f"Garden Protector CDN {cfg['service_id']}")
    domain = cfg["cdn_url"].replace("https://", "").replace("http://", "")
    fos_host = region_endpoint(region)

    existing = find_service_by_name(name, token)
    if existing:
        if not cdn_secret:
            raise RuntimeError(
                f"CDN service named {name!r} already exists ({existing['id']}) but no "
                "cdn_secret was provided to reuse its write-only read gate (unrecoverable) "
                "— delete that service in the Fastly UI and re-run"
            )
        # Reuse the existing read-signing service. Its dictionaries/backend/VCL are
        # already in place; we do NOT recreate them (and CANNOT re-read the write-only
        # cdn_secret), so the recorded cdn_secret stays authoritative.
        return {"cdn_service_id": existing["id"], "cdn_url": cfg["cdn_url"]}

    svc = create_service(name, "vcl", token)
    sid = svc["id"]
    add_domain(sid, 1, domain, token)
    # Shield the FOS origin for cache offload, and set override_host so the upstream
    # Host sent to FOS is the bucket-region host (e.g. us-east-1.object.fastlystorage.app),
    # NOT this CDN service's own domain. This is the NATIVE Fastly way to set the
    # origin Host (do it on the backend, not in VCL); the VCL signs bereq.http.host,
    # which override_host populates, so the SigV4 host matches the real origin AND the
    # shielded fetch targets FOS instead of looping back into this service ("Same
    # machine same service" 503). The edge<->shield auth handoff uses the VCL's
    # baked-in X-Edge-CDN-Auth marker (not the client-spoofable Fastly-FF).
    add_backend(
        sid,
        1,
        {
            "name": "fos_origin",
            "address": fos_host,
            "port": 443,
            "use_ssl": True,
            "ssl_cert_hostname": fos_host,
            "ssl_sni_hostname": fos_host,
            "override_host": fos_host,
            "shield": SHIELD_MAP.get(region, "iad-va-us"),
            "auto_loadbalance": False,
            "connect_timeout": 5000,
            "first_byte_timeout": 60000,
            "between_bytes_timeout": 30000,
        },
        token,
    )
    cred_dict = create_dictionary(sid, 1, "fos_credentials", token)
    dictionary_item(sid, cred_dict, "access_key", fos_access_key, token)
    dictionary_item(sid, cred_dict, "secret_key", fos_secret_key, token)
    dictionary_item(sid, cred_dict, "bucket", bucket, token)
    dictionary_item(sid, cred_dict, "region", region, token)
    auth_dict = create_dictionary(sid, 1, "cdn_auth", token)
    dictionary_item(sid, auth_dict, "secret", cdn_secret, token)
    fastly(
        "POST",
        f"/service/{sid}/version/1/vcl",
        {"name": "main", "main": True, "content": load_vcl(cdn_secret)},
        token=token,
    )
    validate_version(sid, 1, token)
    activate_version(sid, 1, token)
    return {"cdn_service_id": sid, "cdn_url": cfg["cdn_url"]}


def delete_logging_endpoint(
    service_id: str, name: str = "Garden Protector Telemetry", *, token: str
) -> int | None:
    """Remove an S3 logging endpoint so the edge stops shipping logs to the FOS bucket.
    Telemetry is Pi-local (SQLite) only — we do NOT persist edge logs in the cloud (each
    log object is a billable Class A write, and they pile up ~1/sec). Clone->delete->
    validate->activate with rollback. Returns the new active version, or None when the
    endpoint isn't present (idempotent no-op)."""
    active = get_active_version(service_id, token)
    existing = (
        fastly("GET", f"/service/{service_id}/version/{active}/logging/s3", token=token)
        or []
    )
    if not any(e.get("name") == name for e in existing):
        return None
    new_ver = clone_version(service_id, active, token)
    try:
        fastly(
            "DELETE",
            f"/service/{service_id}/version/{new_ver}/logging/s3/{urllib.parse.quote(name)}",
            token=token,
            expect_empty=True,
        )
        validate_version(service_id, new_ver, token)
        activate_version(service_id, new_ver, token)
        return new_ver
    except RuntimeError:
        # Roll back to the previously-active version.
        activate_version(service_id, active, token)
        raise
