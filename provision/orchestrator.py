"""Provisioning orchestrator — one Fastly token stands up the whole deployment.

Numbered steps, each `save_state()`-checkpointed; the whole run is wrapped so ANY failure triggers
an auto-rollback `teardown`. `teardown` itself requires a `global`-scoped token and
NEVER falls back to a stored key (the unauthenticated-infra-teardown guard).

`configs/{service_id}.json` (live FOS keys + cdn_secret + admin token) is
gitignored. The committed Wasm package is deployed via the API (no Rust toolchain
needed on the host).
"""

import json
import os
import pathlib
import time

from . import fastly_api, fos_setup, registry

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
# GP_CONFIGS_DIR lets a caller (e.g. the Pi portal driving gp-provision as a
# subprocess) redirect ALL config artifacts to one dir, so the portal's pi-garden.json
# and the service config/registry/deploy-env land together. Defaults to <repo>/configs.
CONFIGS_DIR = pathlib.Path(os.environ.get("GP_CONFIGS_DIR") or (REPO_ROOT / "configs"))
STATE_FILE = REPO_ROOT / "setup-state.json"

# Names the Rust edge opens (must match `main.rs`).
KV_STATE = "garden_state"
KV_MODELS = "garden_models"
SECRET_TOKENS = "garden_tokens"
# Read-only Config Store holding this service's OWN garden id (`default_garden_id`),
# the fallback the edge uses for a header-less browser request (see the Rust
# `read_default_garden_id`). Linked here (empty); the item is written at
# create-garden time, once the garden id is known.
CONFIG_GARDEN = "garden_config"
MODEL_KEY = "mobilenet_v2.onnx"


def _log(msg: str) -> None:
    print(f"[gp-provision] {msg}")


# ---------------------------------------------------------------------------
# Local config + resumable state
# ---------------------------------------------------------------------------


def config_path(service_id: str) -> pathlib.Path:
    return CONFIGS_DIR / f"{service_id}.json"


def save_config(cfg: dict) -> pathlib.Path:
    CONFIGS_DIR.mkdir(exist_ok=True)
    p = config_path(cfg["service_id"])
    p.write_text(json.dumps(cfg, indent=2))
    return p


def load_config(service_id: str) -> dict | None:
    p = config_path(service_id)
    return json.loads(p.read_text()) if p.exists() else None


def list_service_ids() -> list[str]:
    if not CONFIGS_DIR.exists():
        return []
    return sorted(p.stem for p in CONFIGS_DIR.glob("*.json"))


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def clear_state() -> None:
    STATE_FILE.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Provision
# ---------------------------------------------------------------------------


def preflight(token: str) -> dict:
    """Confirm the token is reachable; return its scope info."""
    return fastly_api.token_scope(token)


def provision(cfg: dict, ts: int) -> dict:
    """Run the full provisioning flow. `cfg` requires: token, service_name,
    region, bucket, cdn_url, cdn_secret, package_path, model_path. Optional:
    skip_archive (skip FOS bucket + CDN + logging for a faster core deploy)."""
    token = cfg["token"]
    state: dict = {"step": 0}
    try:
        # 1. Preflight.
        _log("🔑 Checking your Fastly account…")
        preflight(token)
        _log("✓ Fastly account verified")

        # 2. Compute service (find-or-create) + domain.
        _log("☁️ Setting up your cloud service…")
        existing = fastly_api.find_service_by_name(cfg["service_name"], token)
        if existing:
            sid = existing["id"]
            new_service = False
            _log(f"♻️ Reusing your existing cloud service ({sid})")
        else:
            svc = fastly_api.create_service(cfg["service_name"], "wasm", token)
            sid = svc["id"]
            new_service = True
            _log(f"✓ Cloud service created ({sid})")
        state["service_id"] = sid
        state["new_service"] = new_service
        save_state(state)

        # 2b. Resolve ONE editable DRAFT version and do ALL versioned mutations on it
        #     (domain, resource links, package). A fresh service has an unlocked v1;
        #     a reused service's active version is LOCKED, so clone it first.
        if new_service:
            ver = fastly_api.latest_version(sid, token)  # the v1 draft
        else:
            ver = fastly_api.clone_version(
                sid, fastly_api.get_active_version(sid, token), token
            )
        _log(f"🔖 Working on version v{ver}")

        # 2c. Reachable domain (a Compute service has none until you add one).
        domain = cfg.get("domain") or f"{cfg['service_name']}.edgecompute.app"
        try:
            fastly_api.add_domain(sid, ver, domain, token)
            _log(f"🌐 Reserved your web address ({domain})")
        except RuntimeError as exc:
            s = str(exc).lower()
            if not ("409" in s or "duplicate" in s or "taken" in s or "already" in s):
                raise
            _log(f"🌐 Web address already set ({domain})")
        state["domain"] = domain
        state["backend_url"] = cfg.get("backend_url") or f"https://{domain}"
        save_state(state)

        # 3. KV + Secret stores (find-or-create; not versioned), linked to the draft
        #    by the names the Rust opens.
        _log("🗄️ Setting up garden storage…")
        state["garden_state_store_id"] = fastly_api.ensure_kv_store(
            f"{KV_STATE}_{sid}", token
        )
        state["garden_models_store_id"] = fastly_api.ensure_kv_store(
            f"{KV_MODELS}_{sid}", token
        )
        state["garden_tokens_store_id"] = fastly_api.ensure_secret_store(
            f"{SECRET_TOKENS}_{sid}", token
        )
        state["garden_config_store_id"] = fastly_api.ensure_config_store(
            f"{CONFIG_GARDEN}_{sid}", token
        )
        fastly_api.link_resource(
            sid, ver, KV_STATE, state["garden_state_store_id"], token
        )
        fastly_api.link_resource(
            sid, ver, KV_MODELS, state["garden_models_store_id"], token
        )
        fastly_api.link_resource(
            sid, ver, SECRET_TOKENS, state["garden_tokens_store_id"], token
        )
        # Linked empty — `default_garden_id` is written at create-garden time so a
        # header-less browser on a dedicated service resolves to ITS garden, not "default".
        fastly_api.link_resource(
            sid, ver, CONFIG_GARDEN, state["garden_config_store_id"], token
        )
        _log("✓ Garden storage ready")
        save_state(state)

        # 4. Upload the model weights to garden_models (so inference works live).
        model_path = pathlib.Path(cfg["model_path"])
        if model_path.exists():
            _log("🧠 Uploading the AI model — the big one, hang tight…")
            fastly_api.kv_put(
                state["garden_models_store_id"],
                MODEL_KEY,
                model_path.read_bytes(),
                token,
            )
            _log(
                f"✓ AI model uploaded ({model_path.stat().st_size // (1024 * 1024)} MB)"
            )
        else:
            _log(
                f"⚠️ AI model not found at {model_path}; inference will 503 until one is added"
            )

        # 5. Deploy the prebuilt Wasm package onto the draft + activate it.
        pkg_path = pathlib.Path(cfg["package_path"])
        if not pkg_path.exists():
            raise RuntimeError(
                f"package not found at {pkg_path}; run `fastly compute build` in backend/ first"
            )
        _log("🚀 Publishing your service to Fastly…")
        fastly_api.deploy_package(sid, ver, pkg_path.read_bytes(), token)
        fastly_api.validate_version(sid, ver, token)
        fastly_api.activate_version(sid, ver, token)
        state["active_version"] = ver
        # backend_url was set from the domain in step 2c; keep it.
        _log(f"✓ Service is live (v{ver})")
        save_state(state)

        # 6-8. Object archive (FOS bucket + CDN-signing service + logging).
        if not cfg.get("skip_archive"):
            _log("📦 Creating the photo album…")
            admin_key = fos_setup.ensure_fos_access_key(
                f"gp-{sid}-admin-temp", token, permission="read-write-admin"
            )
            fos_setup.ensure_fos_bucket(
                cfg["bucket"],
                cfg["region"],
                admin_key["access_key"],
                admin_key["secret_key"],
            )
            _log(f"✓ Photo album ready ({cfg['bucket']})")
            scoped = fos_setup.ensure_fos_access_key(
                f"gp-{sid}-objects",
                token,
                permission="read-write-objects",
                buckets=[cfg["bucket"]],
            )
            # The FOS access-keys API identifies a key by `access_key` (the GET/POST
            # response carries no `id`/`access_key_id`), so delete the temp admin key
            # by that field — else an empty id silently 404s and leaks a broad
            # read-write-admin key every run.
            fos_setup.delete_fos_access_key(
                admin_key.get("access_key")
                or admin_key.get("access_key_id")
                or admin_key.get("id", ""),
                token,
            )
            endpoint = fos_setup.region_endpoint(cfg["region"])
            # KEY-REUSE GUARD (CLOUD audit, mirrors scripts/wire_cdn_read.py:106-108):
            # ensure_fos_access_key is find-or-create by DESCRIPTION. On a reprovision it
            # takes the GET branch, which returns the existing key with NO `secret_key`
            # (the FOS LIST/GET access-key endpoint never echoes the secret — it's shown
            # only on the original create). Blindly passing that absent secret onward would
            # write an EMPTY `fos.secret_key` into the edge Secret Store AND an empty
            # SigV4 secret into the CDN service, silently breaking archive PUT + CDN
            # read-back — or crash on `.encode()` of None. So detect reuse and DON'T
            # re-write the secret: the value already in the Secret Store / CDN service from
            # the original provision is still valid and authoritative.
            scoped_secret = scoped.get("secret_key")
            key_reused = not scoped_secret
            if key_reused:
                _log(
                    "♻️ Reusing the existing photo-album access key "
                    "(its secret is unreadable on reuse — keeping the stored credential)"
                )
            state.update(
                {
                    "fos_region": cfg["region"],
                    "fos_bucket": cfg["bucket"],
                    "fos_endpoint": endpoint,
                    "fos_access_key_id": scoped["access_key"],
                }
            )
            # Never overwrite a known-good recorded secret with an empty one. On reuse,
            # carry the prior config's recorded secret forward so the final config write
            # (step 10) doesn't blank `fos_secret_access_key` for ops convenience.
            if scoped_secret:
                state["fos_secret_access_key"] = scoped_secret
            else:
                prior_secret = (load_config(sid) or {}).get("fos_secret_access_key")
                if prior_secret:
                    state["fos_secret_access_key"] = prior_secret
            save_state(state)

            # 6b. Wire the Compute-side best-effort archive WRITE path so the edge's
            #     archive_evidence isn't dead code: non-secret FOS fields in a
            #     `fos_config` Config Store, the bucket secret key in the Secret Store
            #     (NOT a Config Store), and the bucket as the `fos_archive` backend —
            #     all on a fresh Compute version. NOTE: the live archive PUT round-trip
            #     is not yet end-to-end verified (SigV4-from-Compute against a real
            #     bucket) — see provision/README.md.
            _log("🔗 Connecting the photo album to your service…")
            # CDN-first reads (user rule "pull files via the CDN, never direct from FOS"):
            # wire the edge to pull archive images THROUGH the CDN read-signing service.
            # SSRF-guard the user-supplied cdn_url before pointing an edge backend at it;
            # if it doesn't validate we skip CDN-read wiring and the edge falls back to a
            # direct FOS GET (so the archive still works, just uncached).
            cdn_host = fastly_api.safe_cdn_host(cfg.get("cdn_url", ""))
            fos_cfg_store = fastly_api.ensure_config_store(f"fos_config_{sid}", token)
            state["fos_config_store_id"] = fos_cfg_store  # track for teardown
            save_state(state)
            fos_items = [
                ("access_key", scoped["access_key"]),
                ("bucket", cfg["bucket"]),
                ("region", cfg["region"]),
                ("endpoint", endpoint),
            ]
            if cdn_host:
                fos_items.append(("cdn_host", cdn_host))
            for k, v in fos_items:
                try:
                    fastly_api.config_item(fos_cfg_store, k, v, token)
                except RuntimeError:
                    pass  # item exists -> tolerate (idempotent reprovision)
            # Only (re)write the bucket secret key when we actually minted a fresh one.
            # On key reuse `scoped_secret` is empty (see KEY-REUSE GUARD above) and the
            # existing Secret Store value is still valid — overwriting it with "" would
            # break archive SigV4 signing.
            if scoped_secret:
                fastly_api.secret_put(
                    state["garden_tokens_store_id"],
                    "fos.secret_key",
                    scoped_secret,
                    token,
                )
            # The CDN read gate's shared secret — server-side only. The edge sends it as
            # the x-fastly-key HEADER on the CDN hop; it never reaches the browser.
            if cdn_host and cfg.get("cdn_secret"):
                fastly_api.secret_put(
                    state["garden_tokens_store_id"],
                    "fos.cdn_secret",
                    cfg["cdn_secret"],
                    token,
                )
            av = fastly_api.clone_version(
                sid, fastly_api.get_active_version(sid, token), token
            )
            fastly_api.link_resource(sid, av, "fos_config", fos_cfg_store, token)
            # Backends are inherited when a version is cloned, so on a reprovision the
            # fos_archive backend already exists on the draft -> tolerate the 409
            # duplicate (mirrors the add_domain handling above).
            try:
                fastly_api.add_backend(
                    sid,
                    av,
                    {
                        "name": "fos_archive",
                        "address": endpoint,
                        "port": 443,
                        "use_ssl": True,
                        "ssl_cert_hostname": endpoint,
                        "ssl_sni_hostname": endpoint,
                        "override_host": endpoint,
                        "auto_loadbalance": False,
                        "connect_timeout": 5000,
                        "first_byte_timeout": 30000,
                    },
                    token,
                )
            except RuntimeError as exc:
                s = str(exc).lower()
                if not (
                    "409" in s or "duplicate" in s or "already" in s or "conflict" in s
                ):
                    raise
                _log(
                    "fos_archive backend already present on the cloned version (idempotent)"
                )
            # The CDN read-signing service as a TLS origin the edge reads archive images
            # from (cdn_signed_get). override_host so the Host sent is the CDN service's own
            # domain. The CDN service itself is created just below; a backend is only a
            # host:port config, so adding it first is fine. Idempotent on reprovision.
            if cdn_host:
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
                    if not (
                        "409" in s
                        or "duplicate" in s
                        or "already" in s
                        or "conflict" in s
                    ):
                        raise
                    _log(
                        "cdn_read backend already present on the cloned version (idempotent)"
                    )
            fastly_api.validate_version(sid, av, token)
            fastly_api.activate_version(sid, av, token)
            state["active_version"] = av
            _log(f"✓ Photo album connected (v{av})")
            save_state(state)

            _log("🌍 Setting up fast photo delivery…")
            cdn_cfg = {
                **cfg,
                "service_id": sid,
                "fos_region": cfg["region"],
                "fos_bucket": cfg["bucket"],
            }
            if key_reused:
                # No fresh secret to feed the CDN service's SigV4 dictionary. The CDN
                # service stood up by the ORIGINAL provision already has the correct
                # credential baked into its (write-only) dictionary, so we MUST NOT pass an
                # empty secret_key to ensure_cdn_service (it would create/wire a broken
                # gate). Reuse the recorded CDN service instead. find_service_by_name keeps
                # this honest even if cfg/state lost the id.
                recorded_cdn = load_config(sid) or {}
                cdn_service_id = state.get("cdn_service_id") or recorded_cdn.get(
                    "cdn_service_id"
                )
                if not cdn_service_id:
                    found = fastly_api.find_service_by_name(
                        cdn_cfg.get("cdn_service_name", f"Garden Protector CDN {sid}"),
                        token,
                    )
                    cdn_service_id = found["id"] if found else None
                if not cdn_service_id:
                    raise RuntimeError(
                        "FOS object key was reused (its secret is unreadable) but no existing "
                        "CDN read-signing service is recorded — cannot rebuild the SigV4 gate. "
                        "Tear down and re-provision this garden, or run scripts/wire_cdn_read.py."
                    )
                cdn = {"cdn_service_id": cdn_service_id, "cdn_url": cfg["cdn_url"]}
                _log(f"♻️ Reusing existing photo delivery service ({cdn_service_id})")
            else:
                cdn = fastly_api.ensure_cdn_service(
                    cdn_cfg, scoped["access_key"], scoped_secret, token
                )
            state.update(cdn)
            _log(f"✓ Photo delivery ready ({cdn['cdn_service_id']})")
            save_state(state)
            # NOTE: we intentionally do NOT ship edge logs to the cloud. Telemetry is
            # Pi-local (SQLite) only — an S3 logging endpoint piles up ~1 object/sec of
            # billable Class A writes in the images bucket. Live tail stays available
            # ephemerally via `fastly log-tail` (see provision/console.py).

        # 9. Seed the registry with the default garden (single writer; the local
        #    mirror is authoritative, write-through to KV).
        _log("🌱 Planting your garden…")
        registry.seed_registry(
            {
                "service_id": sid,
                "token": token,
                "garden_state_store_id": state["garden_state_store_id"],
                "configs_dir": str(CONFIGS_DIR),
            },
            ts,
        )
        _log("✓ Garden registered")

        # 10. Write the service config (gitignored — holds live keys + token).
        out = {
            "service_id": sid,
            "service_name": cfg["service_name"],
            "backend_url": state["backend_url"],
            "fastly_api_key": token,
            "garden_state_store_id": state["garden_state_store_id"],
            "garden_models_store_id": state["garden_models_store_id"],
            "garden_tokens_store_id": state["garden_tokens_store_id"],
            "garden_config_store_id": state["garden_config_store_id"],
            "fos_config_store_id": state.get("fos_config_store_id"),
            "active_version": state.get("active_version"),
            **{
                k: state.get(k)
                for k in (
                    "fos_region",
                    "fos_bucket",
                    "fos_endpoint",
                    "fos_access_key_id",
                    "fos_secret_access_key",
                    "cdn_service_id",
                    "cdn_url",
                )
            },
            "cdn_secret": cfg.get("cdn_secret"),
        }
        save_config(out)
        clear_state()
        _log("💾 Configuration saved")
        return out

    except Exception as exc:  # noqa: BLE001 — auto-rollback on failure
        # Only tear down resources we CREATED this run. If we were REUSING an
        # existing service (a redeploy), a transient failure must NOT delete it —
        # just clear the resumable state and surface the error.
        if state.get("new_service"):
            _log(
                f"PROVISION FAILED: {exc}\n           rolling back (created this run)…"
            )
            try:
                for line in perform_teardown(
                    state, token, opts={"remove_bucket": True, "remove_stores": True}
                ):
                    _log(f"rollback: {line}")
            finally:
                clear_state()
        else:
            _log(f"PROVISION FAILED on a REUSED service (not deleting it): {exc}")
            clear_state()
        raise


# ---------------------------------------------------------------------------
# Teardown (destructive — requires a global-scoped token, NEVER a stored key)
# ---------------------------------------------------------------------------


def require_global_token(token: str | None) -> str:
    """Destructive teardown must present a caller-supplied `global`-scoped token.
    NEVER falls back to the stored `fastly_api_key` (the infra-teardown guard).
    """
    if not token:
        raise PermissionError(
            "token_required: destructive teardown needs a global-scoped --token"
        )
    scope = fastly_api.token_scope(token)
    if scope.get("scope") != "global":
        raise PermissionError(
            f"insufficient_scope: teardown needs a 'global' token (got {scope.get('scope')!r})"
        )
    return token


def perform_teardown(state: dict, token: str, opts: dict | None = None):
    """Idempotent best-effort teardown. Yields progress strings. 404s are success.

    ORDER MATTERS: stop writes to FOS BEFORE emptying the
    bucket, or new objects race in and DeleteBucket fails with BucketNotEmpty. So:
      1. delete the Compute service — it's what writes archive objects + telemetry logs;
      2. delete the CDN read-signing service (reads only);
      3. REVOKE the scoped FOS access key so no more writes can happen;
      4. empty + delete the bucket with a FRESH temp admin key (the scoped key can't
         DeleteBucket), deleting that temp key afterwards;
      5. empty + delete EVERY store created at provision time — KV, Secret, AND Config
         (KV and Config both refuse to delete non-empty; leftover stores orphan the
         account across provision/teardown cycles)."""
    opts = opts or {}
    sid = state.get("service_id")
    from .client import fastly

    # 1) Compute service first — stops archive + telemetry-log writes to FOS.
    if sid:
        try:
            try:
                fastly(
                    "PUT",
                    f"/service/{sid}/version/{fastly_api.get_active_version(sid, token)}/deactivate",
                    token=token,
                    expect_empty=True,
                )
            except RuntimeError:
                pass
            fastly("DELETE", f"/service/{sid}", token=token, expect_empty=True)
            yield f"deleted Compute service {sid}"
        except RuntimeError as exc:
            yield f"service delete skipped: {exc}"

    # 2) CDN read-signing service (reads only; safe any time).
    if state.get("cdn_service_id"):
        try:
            fastly(
                "PUT",
                f"/service/{state['cdn_service_id']}/version/{fastly_api.get_active_version(state['cdn_service_id'], token)}/deactivate",
                token=token,
                expect_empty=True,
            )
            fastly(
                "DELETE",
                f"/service/{state['cdn_service_id']}",
                token=token,
                expect_empty=True,
            )
            yield f"deleted CDN service {state['cdn_service_id']}"
        except RuntimeError as exc:
            yield f"CDN delete skipped: {exc}"

    # 3) + 4) FOS: revoke the write creds FIRST (no more writes), then empty + delete the
    #    bucket with a fresh temp admin key (the scoped key can't DeleteBucket).
    if opts.get("remove_bucket") and state.get("fos_bucket"):
        if state.get("fos_access_key_id"):
            try:
                fos_setup.delete_fos_access_key(state["fos_access_key_id"], token)
                yield "revoked scoped FOS access key (no more writes)"
            except Exception as exc:  # noqa: BLE001
                yield f"scoped FOS key delete skipped: {exc}"
        admin = None
        try:
            admin = fos_setup.ensure_fos_access_key(
                f"gp-teardown-{sid}-{int(time.time())}",
                token,
                permission="read-write-admin",
            )
            region = state.get("fos_region") or "us-east-1"
            # A just-minted FOS key takes a few seconds to propagate; poll until it
            # AUTHENTICATES instead of a fixed sleep (sometimes too short -> AccessDenied,
            # sometimes wasteful). Bounded by a timeout; if it never propagates we proceed
            # anyway and delete_fos_bucket surfaces any failure.
            if not fos_setup.wait_for_fos_key(
                state["fos_bucket"], region, admin["access_key"], admin["secret_key"]
            ):
                yield "warning: fresh FOS admin key did not authenticate within timeout; trying delete anyway"
            fos_setup.delete_fos_bucket(
                state["fos_bucket"], region, admin["access_key"], admin["secret_key"]
            )
            yield f"emptied + deleted FOS bucket {state['fos_bucket']}"
        except Exception as exc:  # noqa: BLE001
            yield f"bucket delete skipped: {exc}"
        finally:
            if admin:
                try:
                    fos_setup.delete_fos_access_key(
                        admin.get("access_key")
                        or admin.get("access_key_id")
                        or admin.get("id", ""),
                        token,
                    )
                except Exception:  # noqa: BLE001
                    pass

    # 5) KV + Secret + Config stores (now unlinked — the service was deleted in step 1).
    #    Fastly refuses to delete a non-empty KV *or* Config store (409) -> empty first.
    #    ALL store types created at provision time must be torn down here, or repeated
    #    provision/teardown cycles leak orphaned stores on the account.
    if opts.get("remove_stores"):
        for key, endpoint in (
            ("garden_state_store_id", "kv"),
            ("garden_models_store_id", "kv"),
            ("garden_tokens_store_id", "secret"),
            ("garden_config_store_id", "config"),
            ("fos_config_store_id", "config"),
        ):
            store_id = state.get(key)
            if not store_id:
                continue
            try:
                if endpoint == "kv":
                    removed = fastly_api.kv_empty(store_id, token)
                    if removed:
                        yield f"emptied {removed} key(s) from kv store {store_id}"
                elif endpoint == "config":
                    removed = fastly_api.config_empty(store_id, token)
                    if removed:
                        yield f"emptied {removed} item(s) from config store {store_id}"
                fastly(
                    "DELETE",
                    f"/resources/stores/{endpoint}/{store_id}",
                    token=token,
                    expect_empty=True,
                )
                yield f"deleted {endpoint} store {store_id}"
            except RuntimeError as exc:
                yield f"{endpoint} store delete skipped: {exc}"
