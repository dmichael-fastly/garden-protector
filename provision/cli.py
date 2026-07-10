"""gp-provision CLI (typer).

Handlers take a `SimpleNamespace` (so unit tests can drive them directly without
typer); the typer commands at the bottom pack their options into one and delegate.
Token resolves from --token -> $FASTLY_API_KEY -> prompt.
"""

import os
import pathlib
import secrets
import sys
import time
from types import SimpleNamespace

import typer

from . import fastly_api, orchestrator, registry
from .ids import DEFAULT_GARDEN

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Fastly Garden Protector — provisioning + registry control plane.")


def _resolve_token(args, *, required=True) -> str:
    token = getattr(args, "token", None) or os.environ.get("FASTLY_API_KEY")
    if not token and required:
        token = typer.prompt("Fastly API token", hide_input=True)
    return token or ""


def _load_cfg_or_die(service_id: str | None) -> dict:
    if not service_id:
        ids = orchestrator.list_service_ids()
        if len(ids) == 1:
            service_id = ids[0]
        elif not ids:
            print("No provisioned services found (configs/ is empty). Run `gp-provision provision` first.")
            sys.exit(1)
        else:
            print("Multiple services; pass --service-id one of:", ", ".join(ids))
            sys.exit(1)
    cfg = orchestrator.load_config(service_id)
    if not cfg:
        print(f"No config for service {service_id}.")
        sys.exit(1)
    return cfg


def _write_deploy_env(deploy_env: dict) -> pathlib.Path:
    """Write the Pi's ids + token to a gitignored configs/<gid>-<did>.env."""
    orchestrator.CONFIGS_DIR.mkdir(exist_ok=True)
    gid = deploy_env["GP_GARDEN_ID"]
    did = deploy_env["GP_DEVICE_ID"]
    p = orchestrator.CONFIGS_DIR / f"{gid}-{did}.env"
    lines = [f"{k}={v}" for k, v in deploy_env.items() if v != ""]
    p.write_text("\n".join(lines) + "\n")
    return p


# --- handlers -------------------------------------------------------------


def handle_provision(args) -> dict:
    token = _resolve_token(args)
    region = (getattr(args, "region", None) or "us-east-1").lower()
    service_name = args.service_name
    cfg = {
        "token": token,
        "service_name": service_name,
        "region": region,
        "bucket": getattr(args, "bucket", None) or f"gp-{service_name}-images",
        "cdn_url": getattr(args, "cdn_url", None) or f"https://{service_name}-cdn.global.ssl.fastly.net",
        "cdn_secret": getattr(args, "cdn_secret", None) or secrets.token_urlsafe(24),
        "package_path": getattr(args, "package_path", None) or str(orchestrator.REPO_ROOT / "backend" / "pkg" / "garden-protector-backend.tar.gz"),
        "model_path": getattr(args, "model_path", None) or str(orchestrator.REPO_ROOT / "tests" / "kv_store" / "mobilenet_v2.onnx"),
        "backend_url": getattr(args, "backend_url", None),
        "domain": getattr(args, "domain", None),
        "skip_archive": getattr(args, "skip_archive", False),
    }
    out = orchestrator.provision(cfg, ts=int(time.time()))
    print(f"Provisioned. Edge: {out['backend_url']}  (config: configs/{out['service_id']}.json)")
    return out


def _ctx_from_cfg(cfg: dict) -> dict:
    return {
        "service_id": cfg["service_id"],
        "token": cfg["fastly_api_key"],
        "garden_state_store_id": cfg["garden_state_store_id"],
        "garden_tokens_store_id": cfg.get("garden_tokens_store_id"),
        "garden_config_store_id": cfg.get("garden_config_store_id"),
        "backend_url": cfg.get("backend_url", ""),
        "configs_dir": str(orchestrator.CONFIGS_DIR),
    }


def _opt_bool(v) -> bool | None:
    """Parse an OPTIONAL boolean CLI flag ("true"/"false"/"1"/"0"/None). `None`/blank -> None
    (leave unchanged); anything else maps to a bool. Used for the alarm-role edit flags so an
    absent flag preserves the device's current role."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s == "":
        return None
    return s in ("true", "1", "yes", "on")


def handle_seed_registry(args) -> dict:
    cfg = _load_cfg_or_die(getattr(args, "service_id", None))
    reg = registry.seed_registry(_ctx_from_cfg(cfg), int(time.time()))
    print(f"Seeded index/gardens ({len(reg['gardens'])} garden(s)).")
    return reg


def _sanitize_display_name(name: str, *, max_len: int = 80) -> str:
    """Bound length + strip control chars from a human display NAME before it is
    written to the Config Store as an edge-rendered header value (defense-in-depth;
    the edge already HTML-escapes on output). Drops C0/C1 control characters and the
    DEL char (incl. NUL, CR/LF that could split headers), collapses surrounding
    whitespace, and truncates to `max_len`. Pure."""
    if not name:
        return ""
    cleaned = "".join(ch for ch in name if ch == "\t" or (ord(ch) >= 0x20 and ord(ch) != 0x7F and not (0x80 <= ord(ch) <= 0x9F)))
    cleaned = cleaned.replace("\t", " ").strip()
    return cleaned[:max_len]


def _location_from_args(args) -> dict | None:
    """Assemble the coarse {address,lat,lon} the edge keeps, or None if unset."""
    address = getattr(args, "address", None) or None
    lat = getattr(args, "lat", None)
    lon = getattr(args, "lon", None)
    loc = {}
    if address:
        loc["address"] = address
    if lat is not None:
        loc["lat"] = lat
    if lon is not None:
        loc["lon"] = lon
    return loc or None


def _write_garden_env(gid: str, token, backend_url) -> pathlib.Path:
    """Surface a (deviceless) garden's token to the Pi via configs/<gid>-garden.env."""
    orchestrator.CONFIGS_DIR.mkdir(exist_ok=True)
    p = orchestrator.CONFIGS_DIR / f"{gid}-garden.env"
    lines = [f"GP_GARDEN_ID={gid}", f"GP_GARDEN_TOKEN={token or ''}",
             f"GP_BACKEND={backend_url or ''}"]
    p.write_text("\n".join(lines) + "\n")
    return p


def handle_create_garden(args) -> dict:
    """Create the NAMED garden (coarse entry + location + token) and, if a first
    device is supplied (e.g. the Pi gateway), register it too. Used by the wizard's
    deploy step after `provision`."""
    cfg = _load_cfg_or_die(getattr(args, "service_id", None))
    ctx = _ctx_from_cfg(cfg)
    name = getattr(args, "name", None) or args.garden
    tz = getattr(args, "tz", None) or "UTC"
    location = _location_from_args(args)

    result = registry.create_garden(ctx, gid=args.garden, name=name, tz=tz,
                                    ts=int(time.time()), location=location)
    # Tell the edge which garden a header-less (browser) request belongs to. Only a
    # NAMED garden needs this — gid "default" is already the header-less fallback.
    # POST-once: on a shared service the FIRST named garden wins (the bare "/" view);
    # specific gardens are reached via the header-sending console / admin routes.
    store_id = ctx.get("garden_config_store_id")
    if args.garden != DEFAULT_GARDEN and store_id:
        try:
            fastly_api.config_item(store_id, "default_garden_id", args.garden, cfg["fastly_api_key"])
        except RuntimeError:
            pass  # key already set (idempotent re-create / first-garden-wins); mock short-circuits
        # Also bake the display NAME so the edge dashboard's shared header shows it
        # server-side (no async pop-in). Sanitize first (length-bound + strip control
        # chars) so an attacker-supplied name can't smuggle CR/LF or control bytes into
        # the stored header value — defense-in-depth atop the edge's HTML escaping.
        # Best-effort + independently caught: a failure here must not abort garden
        # creation; the header just falls back to blank.
        try:
            fastly_api.config_item(store_id, "default_garden_name",
                                   _sanitize_display_name(name), cfg["fastly_api_key"])
        except RuntimeError:
            pass
    _write_garden_env(args.garden, result["token"], ctx.get("backend_url", ""))
    tok_note = "TOKENLESS (default garden)" if result["token"] is None else "token minted"
    print(f"✓ Garden '{args.garden}' ({name}) created — {tok_note}"
          + (f", location {location}" if location else "") + ".")

    # Optional first device (the Pi gateway is registered as device #1 by the wizard).
    if getattr(args, "device", None):
        dev_ctx = {**ctx, "garden_name": name, "garden_tz": tz, "garden_location": location}
        dres = registry.register_device(
            dev_ctx, gid=args.garden, did=args.device,
            node_id=getattr(args, "node", None) or "pi-01",
            kind=args.kind, dev_type=args.type,
            name=getattr(args, "name", None) or args.device, ts=int(time.time()),
        )
        p = _write_deploy_env(dres["deploy_env"])
        print(f"✓ Registered first device '{args.device}' ({args.kind}/{args.type}); "
              f"Pi deploy env -> {p} (gitignored).")
        result["device"] = dres["device"]
    return result


def handle_update_garden(args) -> dict:
    """Update an EXISTING garden's name/tz/location in the registry WITHOUT touching
    its token — the post-setup edit path the Pi Settings page drives. Notes stay
    Pi-local and are never passed here (parity with create-garden)."""
    cfg = _load_cfg_or_die(getattr(args, "service_id", None))
    ctx = _ctx_from_cfg(cfg)
    name = getattr(args, "name", None) or args.garden
    tz = getattr(args, "tz", None) or "UTC"
    location = _location_from_args(args)
    entry = registry.update_garden(ctx, gid=args.garden, name=name, tz=tz,
                                   ts=int(time.time()), location=location)
    print(f"✓ Garden '{args.garden}' updated — name {name!r}, tz {tz}"
          + (f", location {location}" if location else "") + ".")
    return entry


def handle_register_device(args) -> dict:
    cfg = _load_cfg_or_die(getattr(args, "service_id", None))
    ctx = {
        **_ctx_from_cfg(cfg),
        "garden_name": getattr(args, "garden_name", None) or args.garden,
        "garden_tz": getattr(args, "tz", None) or "UTC",
        # Reuse the garden's CURRENT token (passed via env so it never lands on
        # argv / the process list) so adding a device never rotates it. Absent ->
        # register_device mints a fresh one (first device in a new garden).
        "existing_token": os.environ.get("GP_EXISTING_GARDEN_TOKEN") or None,
    }
    result = registry.register_device(
        ctx, gid=args.garden, did=args.device, node_id=getattr(args, "node", None) or "pi-01",
        kind=args.kind, dev_type=args.type, name=getattr(args, "name", None) or args.device,
        ts=int(time.time()),
    )
    p = _write_deploy_env(result["deploy_env"])
    tok_note = "TOKENLESS (default garden)" if result["token"] is None else "token minted"
    known = "known type" if result["token_is_known_type"] else "custom type"
    print(f"Registered {args.kind}/{args.type} ({known}) '{args.device}' in garden '{args.garden}' — {tok_note}.")
    print(f"Pi deploy env written to {p} (gitignored). Source it on the Pi before launching client.py.")
    return result


def handle_unregister_device(args) -> dict:
    cfg = _load_cfg_or_die(getattr(args, "service_id", None))
    ctx = _ctx_from_cfg(cfg)
    try:
        result = registry.unregister_device(
            ctx, gid=args.garden, did=args.device, ts=int(time.time())
        )
    except registry.TokenStillLiveError as e:
        # The device WAS removed from the registry, but the garden token could not be
        # revoked — it is STILL a valid edge credential. Fail loudly (non-zero exit) so
        # the operator knows to retry rather than believing the garden is fully gone.
        print(f"⚠️ Device '{args.device}' was unregistered, but the garden token is STILL LIVE.")
        print(f"   {e}")
        sys.exit(2)

    # Delete the local .env file if it exists
    env_path = orchestrator.CONFIGS_DIR / f"{args.garden}-{args.device}.env"
    deleted_env = False
    if env_path.exists():
        env_path.unlink()
        deleted_env = True

    print(f"Unregistered device '{args.device}' from garden '{args.garden}'.")
    if deleted_env:
        print(f"✓ Deleted local deploy env {env_path}.")
    if result.get("deleted_token"):
        print(f"✓ Garden '{args.garden}' has no devices left; deleted its garden tokens from Secret Store.")
    return result


def handle_edit_device(args) -> dict:
    cfg = _load_cfg_or_die(getattr(args, "service_id", None))
    ctx = _ctx_from_cfg(cfg)
    result = registry.edit_device_in_registry(
        ctx, gid=args.garden, did=args.device,
        name=getattr(args, "name", None),
        node_id=getattr(args, "node", None),
        kind=getattr(args, "kind", None),
        dev_type=getattr(args, "type", None),
        status=getattr(args, "status", None),
        can_trigger_alarm=_opt_bool(getattr(args, "can_trigger_alarm", None)),
        can_confirm_alarm=_opt_bool(getattr(args, "can_confirm_alarm", None)),
        ts=int(time.time())
    )

    # Update local .env file if node changed
    env_path = orchestrator.CONFIGS_DIR / f"{args.garden}-{args.device}.env"
    if env_path.exists() and getattr(args, "node", None) is not None:
        try:
            lines = env_path.read_text().splitlines()
            new_lines = []
            for line in lines:
                if line.startswith("GP_NODE_ID="):
                    new_lines.append(f"GP_NODE_ID={args.node}")
                else:
                    new_lines.append(line)
            env_path.write_text("\n".join(new_lines) + "\n")
            print(f"✓ Updated local deploy env {env_path} with new node ID.")
        except Exception as e:
            print(f"Warning: could not update local deploy env: {e}")

    print(f"✓ Edited device '{args.device}' in garden '{args.garden}'.")
    return result



def handle_rotate_token(args) -> dict:
    cfg = _load_cfg_or_die(getattr(args, "service_id", None))
    if args.garden == DEFAULT_GARDEN:
        print("The default garden is tokenless; nothing to rotate.")
        sys.exit(1)
    # The prior (current) garden token is a SECRET: prefer it from the environment
    # (GP_PRIOR_GARDEN_TOKEN — how the console/portal pass it, off argv + off the SSE
    # echo), falling back to the --prior-token flag for a direct interactive CLI run.
    prior = os.environ.get("GP_PRIOR_GARDEN_TOKEN") or getattr(args, "prior_token", None)
    if not prior:
        print("rotate-token needs the garden's CURRENT token via GP_PRIOR_GARDEN_TOKEN "
              "or --prior-token (from its deploy env).")
        sys.exit(1)
    new = registry.rotate_token(cfg["garden_tokens_store_id"], args.garden, cfg["fastly_api_key"],
                                prior_current=prior)
    print(f"Rotated token for garden '{args.garden}'. Update the Pi's GP_GARDEN_TOKEN to: {new}")
    return {"garden_id": args.garden, "token": new}


def handle_teardown(args) -> None:
    token = orchestrator.require_global_token(getattr(args, "token", None) or os.environ.get("FASTLY_API_KEY"))
    cfg = _load_cfg_or_die(getattr(args, "service_id", None))
    state = {
        "service_id": cfg["service_id"],
        "cdn_service_id": cfg.get("cdn_service_id"),
        "fos_bucket": cfg.get("fos_bucket"), "fos_region": cfg.get("fos_region"),
        "fos_access_key_id": cfg.get("fos_access_key_id"), "fos_secret_access_key": cfg.get("fos_secret_access_key"),
        "garden_state_store_id": cfg.get("garden_state_store_id"),
        "garden_models_store_id": cfg.get("garden_models_store_id"),
        "garden_tokens_store_id": cfg.get("garden_tokens_store_id"),
        "garden_config_store_id": cfg.get("garden_config_store_id"),
        "fos_config_store_id": cfg.get("fos_config_store_id"),
    }
    for line in orchestrator.perform_teardown(state, token, opts={
        "remove_bucket": getattr(args, "remove_data", False), "remove_stores": True}):
        print(f"  {line}")
    orchestrator.config_path(cfg["service_id"]).unlink(missing_ok=True)
    print("Teardown complete.")


# --- typer surface --------------------------------------------------------


@app.command("provision", help="Stand up the Compute service + stores + (optionally) FOS/CDN.")
def cmd_provision(
    service_name: str = typer.Option(..., "--service-name"),
    token: str = typer.Option(None, "--token"),
    region: str = typer.Option("us-east-1", "--region"),
    domain: str = typer.Option(None, "--domain", help="Reachable edge domain (default <service-name>.edgecompute.app)."),
    bucket: str = typer.Option(None, "--bucket"),
    cdn_url: str = typer.Option(None, "--cdn-url"),
    backend_url: str = typer.Option(None, "--backend-url", help="Override the edge URL written to configs."),
    package_path: str = typer.Option(None, "--package"),
    model_path: str = typer.Option(None, "--model"),
    skip_archive: bool = typer.Option(False, "--skip-archive", help="Skip FOS bucket + CDN (core deploy only)."),
):
    handle_provision(SimpleNamespace(**locals()))


@app.command("seed-registry", help="Ensure index/gardens has the default garden.")
def cmd_seed(service_id: str = typer.Option(None, "--service-id")):
    handle_seed_registry(SimpleNamespace(service_id=service_id))


@app.command("create-garden", help="Create a named garden (coarse entry + location + token); optionally register a first device.")
def cmd_create_garden(
    garden: str = typer.Option(..., "--garden"),
    name: str = typer.Option(None, "--name", help="Garden display name (default = garden id)."),
    tz: str = typer.Option("UTC", "--tz"),
    address: str = typer.Option(None, "--address", help="Coarse location the edge keeps."),
    lat: float = typer.Option(None, "--lat"),
    lon: float = typer.Option(None, "--lon"),
    device: str = typer.Option(None, "--device", help="Optional first device id (e.g. the Pi gateway)."),
    kind: str = typer.Option(None, "--kind", help="observer | deterrent (with --device)."),
    type: str = typer.Option(None, "--type", help="device type (with --device)."),
    node: str = typer.Option("pi-01", "--node"),
    service_id: str = typer.Option(None, "--service-id"),
):
    handle_create_garden(SimpleNamespace(**locals()))


@app.command("update-garden", help="Update an existing garden's name/tz/location in the registry (no token change).")
def cmd_update_garden(
    garden: str = typer.Option(..., "--garden"),
    name: str = typer.Option(None, "--name", help="Garden display name (default = garden id)."),
    tz: str = typer.Option("UTC", "--tz"),
    address: str = typer.Option(None, "--address", help="Coarse location the edge keeps."),
    lat: float = typer.Option(None, "--lat"),
    lon: float = typer.Option(None, "--lon"),
    service_id: str = typer.Option(None, "--service-id"),
):
    handle_update_garden(SimpleNamespace(**locals()))


@app.command("register-device", help="Register a device (camera, motion/heat sensor, deterrent…) + mint its garden token.")
def cmd_register(
    garden: str = typer.Option(..., "--garden"),
    device: str = typer.Option(..., "--device"),
    kind: str = typer.Option(..., "--kind", help="observer | deterrent"),
    type: str = typer.Option(..., "--type", help="camera_usb | motion_pir | heat_thermal | solenoid | …"),
    node: str = typer.Option("pi-01", "--node"),
    name: str = typer.Option(None, "--name"),
    garden_name: str = typer.Option(None, "--garden-name"),
    tz: str = typer.Option("UTC", "--tz"),
    service_id: str = typer.Option(None, "--service-id"),
):
    handle_register_device(SimpleNamespace(**locals()))


@app.command("unregister-device", help="Unregister a device from a garden and delete its token if it is the last device.")
def cmd_unregister_device(
    garden: str = typer.Option(..., "--garden"),
    device: str = typer.Option(..., "--device"),
    service_id: str = typer.Option(None, "--service-id"),
):
    handle_unregister_device(SimpleNamespace(**locals()))


@app.command("edit-device", help="Edit an existing device's registration details.")
def cmd_edit_device(
    garden: str = typer.Option(..., "--garden"),
    device: str = typer.Option(..., "--device"),
    name: str = typer.Option(None, "--name", help="New display name of the device."),
    node: str = typer.Option(None, "--node", help="New parent node ID."),
    kind: str = typer.Option(None, "--kind", help="observer | deterrent"),
    type: str = typer.Option(None, "--type", help="camera_usb | motion_pir | ..."),
    status: str = typer.Option(None, "--status", help="active | inactive | ..."),
    can_trigger_alarm: str = typer.Option(None, "--can-trigger-alarm", help="true | false — this device starts alarms"),
    can_confirm_alarm: str = typer.Option(None, "--can-confirm-alarm", help="true | false — this device corroborates alarms"),
    service_id: str = typer.Option(None, "--service-id"),
):
    handle_edit_device(SimpleNamespace(**locals()))



@app.command("rotate-token", help="Rotate a garden's token (current -> previous, mint new current).")
def cmd_rotate(
    garden: str = typer.Option(..., "--garden"),
    # Optional: the SECRET prior token is preferably passed via the GP_PRIOR_GARDEN_TOKEN
    # env var (keeps it off argv / the process list / the SSE echo); --prior-token stays
    # as a fallback for a direct interactive run.
    prior_token: str = typer.Option(None, "--prior-token", help="The garden's CURRENT token (from its deploy env). Prefer GP_PRIOR_GARDEN_TOKEN env."),
    service_id: str = typer.Option(None, "--service-id"),
):
    handle_rotate_token(SimpleNamespace(garden=garden, prior_token=prior_token, service_id=service_id))


@app.command("teardown", help="Destroy the service + stores + bucket (requires a GLOBAL-scoped --token).")
def cmd_teardown(
    token: str = typer.Option(None, "--token"),
    service_id: str = typer.Option(None, "--service-id"),
    remove_data: bool = typer.Option(False, "--remove-data", help="Also empty + delete the FOS bucket."),
):
    handle_teardown(SimpleNamespace(token=token, service_id=service_id, remove_data=remove_data))


if __name__ == "__main__":
    app()
