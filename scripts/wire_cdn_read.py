#!/usr/bin/env python3
"""Retrofit the CDN-first read path onto an ALREADY-provisioned garden service.

`provision.orchestrator.provision()` wires the CDN read path (cdn_host +
fos.cdn_secret + the `cdn_read` backend + the CDN read-signing VCL service) only
for fresh full provisions. Gardens stood up before that read their archive
images via the direct-FOS fallback. This script adds ONLY the missing CDN pieces,
in place, reusing the proven provisioning functions. It does NOT touch the edge's
existing FOS write path, the deployed Wasm, or any garden tokens.

Why a fresh FOS key: the CDN VCL needs a bucket-scoped access_key+secret_key for
its SigV4 dictionary. The existing scoped key's secret lives only in the edge
Secret Store (write-only — unreadable), so we mint a SECOND bucket-scoped key just
for the CDN service. Both keys are scoped to the same bucket; the edge write path
keeps using its original key untouched.

Idempotent-ish: skips creating the CDN service if one already exists by name, and
tolerates 409s when re-adding the backend / config item. Run from the repo root:

    python3 scripts/wire_cdn_read.py <service_id>
"""

import secrets
import sys

import requests

from provision import fastly_api, fos_setup, orchestrator


def _fos_config_items(store_id: str, token: str) -> dict:
    r = requests.get(
        f"https://api.fastly.com/resources/stores/config/{store_id}/items",
        headers={"Fastly-Key": token},
        timeout=15,
    )
    r.raise_for_status()
    return {it["item_key"]: it["item_value"] for it in r.json()}


def _resource_links(sid: str, version: int, token: str) -> dict:
    r = requests.get(
        f"https://api.fastly.com/service/{sid}/version/{version}/resource",
        headers={"Fastly-Key": token},
        timeout=15,
    )
    r.raise_for_status()
    return {x["name"]: x for x in r.json()}


def _backend_names(sid: str, version: int, token: str) -> set:
    r = requests.get(
        f"https://api.fastly.com/service/{sid}/version/{version}/backend",
        headers={"Fastly-Key": token},
        timeout=15,
    )
    r.raise_for_status()
    return {b["name"] for b in r.json()}


def main(sid: str) -> None:
    cfg = orchestrator.load_config(sid)
    if not cfg:
        sys.exit(f"no configs/{sid}.json")
    token = cfg["fastly_api_key"]
    name = cfg["service_name"]
    print(f"[wire-cdn] service {sid} ({name})")

    # 1. Discover the live FOS + store wiring from the active version.
    active = fastly_api.get_active_version(sid, token)
    links = _resource_links(sid, active, token)
    fos_cfg_store = links["fos_config"]["resource_id"]
    tokens_store = links["garden_tokens"]["resource_id"]
    fos = _fos_config_items(fos_cfg_store, token)
    bucket, region = fos["bucket"], fos["region"]
    print(f"[wire-cdn] active v{active}; bucket={bucket} region={region}")
    print(f"[wire-cdn] fos_config={fos_cfg_store} garden_tokens={tokens_store}")

    if "cdn_host" in fos and "cdn_read" in _backend_names(sid, active, token):
        print(
            "[wire-cdn] CDN already wired (cdn_host + cdn_read present) — nothing to do"
        )
        return

    # 2. cdn_url + cdn_secret (same shape the CLI/orchestrator generate).
    cdn_url = cfg.get("cdn_url") or f"https://{name}-cdn.global.ssl.fastly.net"
    cdn_secret = cfg.get("cdn_secret") or secrets.token_urlsafe(24)
    cdn_host = fastly_api.safe_cdn_host(cdn_url)
    if not cdn_host:
        sys.exit(f"cdn_url failed SSRF guard: {cdn_url}")

    # 3. Resolve the CDN read-signing service. Decide BEFORE minting a key so a
    #    refusal can't orphan an FOS key whose secret we'd never capture. A same-named
    #    service we can only REUSE if we already hold its read-gate secret in cfg — its
    #    cdn_secret lives in a write-only dictionary and is otherwise unrecoverable.
    cdn_svc_name = f"Garden Protector CDN {sid}"
    existing_cdn = fastly_api.find_service_by_name(cdn_svc_name, token)
    if existing_cdn and not (
        cfg.get("cdn_service_id") == existing_cdn["id"] and cfg.get("cdn_secret")
    ):
        sys.exit(
            f"a CDN service named {cdn_svc_name!r} already exists ({existing_cdn['id']}) but "
            "its read-gate secret isn't recorded in the config and can't be recovered "
            "(write-only dictionary) — delete that service in the Fastly UI and re-run"
        )

    # 4. Mint a SECOND bucket-scoped FOS key for the CDN VCL's SigV4 dictionary.
    #    Fresh description -> the create response includes the secret_key (a GET on
    #    an existing key would not), so this must be a not-yet-used description.
    if existing_cdn:
        print(f"[wire-cdn] reusing recorded CDN service {existing_cdn['id']}")
        cdn = {"cdn_service_id": existing_cdn["id"], "cdn_url": cfg["cdn_url"]}
        cdn_secret = cfg["cdn_secret"]
    else:
        cdn_key = fos_setup.ensure_fos_access_key(
            f"gp-{sid}-cdn-read",
            token,
            permission="read-write-objects",
            buckets=[bucket],
        )
        if not cdn_key.get("secret_key"):
            sys.exit(
                "CDN FOS key has no secret_key (description already existed?) — "
                "delete the gp-<sid>-cdn-read key in the Fastly UI and re-run"
            )
        print(
            f"[wire-cdn] minted CDN FOS key {cdn_key['access_key']} (scoped to {bucket})"
        )
        cdn = fastly_api.ensure_cdn_service(
            {
                "service_id": sid,
                "fos_region": region,
                "fos_bucket": bucket,
                "cdn_url": cdn_url,
                "cdn_secret": cdn_secret,
            },
            cdn_key["access_key"],
            cdn_key["secret_key"],
            token,
        )
        print(
            f"[wire-cdn] created CDN service {cdn['cdn_service_id']} @ {cdn['cdn_url']}"
        )

    # 5. Wire the edge: cdn_host (config), fos.cdn_secret (secret), cdn_read backend.
    try:
        fastly_api.config_item(fos_cfg_store, "cdn_host", cdn_host, token)
        print(f"[wire-cdn] fos_config.cdn_host = {cdn_host}")
    except RuntimeError as exc:
        print(f"[wire-cdn] cdn_host item already present ({exc}) — tolerated")
    fastly_api.secret_put(tokens_store, "fos.cdn_secret", cdn_secret, token)
    print("[wire-cdn] garden_tokens.fos.cdn_secret written")

    av = fastly_api.clone_version(sid, fastly_api.get_active_version(sid, token), token)
    try:
        fastly_api.add_backend(
            sid,
            av,
            {
                "name": "cdn_read",
                "address": cdn_host,
                "port": 443,
                "use_ssl": True,
                "ssl_cert_hostname": cdn_host,
                "ssl_sni_hostname": cdn_host,
                "override_host": cdn_host,
                "auto_loadbalance": False,
                "connect_timeout": 5000,
                "first_byte_timeout": 30000,
            },
            token,
        )
    except RuntimeError as exc:
        s = str(exc).lower()
        if not any(t in s for t in ("409", "duplicate", "already", "conflict")):
            raise
        print("[wire-cdn] cdn_read backend already present — tolerated")
    fastly_api.validate_version(sid, av, token)
    fastly_api.activate_version(sid, av, token)
    print(f"[wire-cdn] activated edge v{av} with cdn_read backend")

    # 6. Persist the new CDN facts (and backfill the discovered FOS/store ids so the
    #    stub config is complete for future ops). Never write the FOS secret_key here.
    cfg.update(
        {
            "cdn_service_id": cdn["cdn_service_id"],
            "cdn_url": cdn["cdn_url"],
            "cdn_secret": cdn_secret,
            "active_version": av,
            "fos_bucket": bucket,
            "fos_region": region,
            "fos_endpoint": fos.get("endpoint"),
            "fos_access_key_id": fos.get("access_key"),
            "garden_tokens_store_id": tokens_store,
            "garden_config_store_id": links.get("garden_config", {}).get("resource_id"),
            "garden_state_store_id": links.get("garden_state", {}).get("resource_id"),
            "garden_models_store_id": links.get("garden_models", {}).get("resource_id"),
        }
    )
    orchestrator.save_config(cfg)
    print(f"[wire-cdn] saved configs/{sid}.json")
    print(
        f"[wire-cdn] DONE. cdn_url={cdn['cdn_url']}  (secret in config + secret store)"
    )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python3 scripts/wire_cdn_read.py <service_id>")
    main(sys.argv[1])
