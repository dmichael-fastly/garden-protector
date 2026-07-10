"""The registry control plane — the SINGLE-WRITER-PROCESS owner of `index/gardens`
and `index/g/<gid>/devices`, and the minter of per-garden tokens.

Pure blob builders + validators (unit-tested offline) are separated from the I/O
functions (which call `fastly_api` and are mockable). Every registry write
recomputes the WHOLE blob and PUTs it. That is safe only if there is one writer at
a time — but the Pi portal spawns a fresh `python -m provision.cli` SUBPROCESS per
admin request under a ThreadingHTTPServer, so two camera adds in flight at once are
two writer PROCESSES racing the same on-disk mirror + KV blob (load→mutate→save
interleaves → lost update). "Single writer" is therefore an invariant we ENFORCE,
not a convention we hope for: every load→mutate→save runs inside an exclusive,
cross-process `fcntl.flock` on a per-service sentinel (`configs/{sid}-registry.lock`),
so the racing CLI subprocesses serialize. The edge only READS these docs; the Pi
never touches them. (stdlib only — no extra dep ships to the Pi.)
"""

import contextlib
import json
import secrets

from . import fastly_api
from .ids import (
    DEFAULT_GARDEN,
    TOKEN_SLOT_CURRENT,
    TOKEN_SLOT_PREVIOUS,
    is_valid_id,
    token_secret_name,
)
from .taxonomy import validate_device_type

REGISTRY_VERSION = 1
GARDENS_KEY = "index/gardens"


class TokenStillLiveError(RuntimeError):
    """Raised when a deprovisioned garden's device was removed from the registry but
    its per-garden token could NOT be deleted from the Secret Store. The token is
    therefore STILL a valid edge credential. Surfaced (not swallowed) so the operator
    knows to retry — leaving it live is a security hole, not a cosmetic warning."""

    def __init__(self, gid: str, secret_names: list[str], cause: Exception):
        self.gid = gid
        self.secret_names = secret_names
        self.cause = cause
        super().__init__(
            f"garden {gid!r}: device unregistered but its token is STILL LIVE — "
            f"failed to delete Secret Store entries {secret_names}: {cause}. "
            f"Re-run unregister (or delete the secrets manually) to revoke it.")


def devices_key(gid: str) -> str:
    return f"index/g/{gid}/devices"


# ---------------------------------------------------------------------------
# Pure blob builders / mutators (no I/O)
# ---------------------------------------------------------------------------


def empty_gardens(ts: int = 0) -> dict:
    return {"v": REGISTRY_VERSION, "updated_ts": ts, "gardens": []}


def empty_devices(gid: str, ts: int = 0) -> dict:
    return {"v": REGISTRY_VERSION, "garden_id": gid, "updated_ts": ts, "devices": []}


def upsert_garden(reg: dict, garden_id: str, name: str, tz: str, ts: int,
                  status: str = "active", location: dict | None = None) -> dict:
    """Insert or update a garden in a gardens registry. Returns a NEW dict.
    Validates the id charset (parity with the edge `is_valid_id`).

    ``location`` ({address,lat,lon}) is the only field beyond name/tz the EDGE keeps
    (RFC decision #4: the edge needs to know WHERE a garden is). It is optional and
    absent by default. Since upsert rebuilds the entry from scratch, a prior
    location is PRESERVED when none is supplied (so a later re-upsert — e.g. a second
    device — doesn't silently drop the address)."""
    if not is_valid_id(garden_id):
        raise ValueError(f"invalid garden_id {garden_id!r} (charset [a-z0-9-], len 1-64)")
    gardens = [g for g in reg.get("gardens", []) if g["garden_id"] != garden_id]
    # Preserve original created_ts (and a previously-set location) on update.
    prior = next((g for g in reg.get("gardens", []) if g["garden_id"] == garden_id), None)
    created_ts = prior["created_ts"] if prior else ts
    if location is None and prior:
        location = prior.get("location")
    entry = {
        "garden_id": garden_id,
        "name": name,
        "tz": tz,
        "status": status,
        "created_ts": created_ts,
    }
    if location:
        entry["location"] = location
    gardens.append(entry)
    gardens.sort(key=lambda g: g["garden_id"])
    return {"v": REGISTRY_VERSION, "updated_ts": ts, "gardens": gardens}


def add_device(reg: dict, *, device_id: str, node_id: str, kind: str, dev_type: str,
               name: str, ts: int, status: str = "active",
               can_trigger_alarm: bool | None = None,
               can_confirm_alarm: bool | None = None) -> dict:
    """Append a device to a devices registry. Returns a NEW dict. Validates id
    charset, device taxonomy, and UNIQUENESS within the garden.

    Alarm roles default to confirm-only for cameras (so nothing TRIGGERS alarms until a
    device is deliberately opted in) and off/off for everything else; pass an explicit
    bool to override. The edge reads these via #[serde(default)] (missing => false)."""
    if not is_valid_id(device_id):
        raise ValueError(f"invalid device_id {device_id!r} (charset [a-z0-9-], len 1-64)")
    if not is_valid_id(node_id):
        raise ValueError(f"invalid node_id {node_id!r} (charset [a-z0-9-], len 1-64)")
    validate_device_type(kind, dev_type)  # raises on bad kind/type
    if any(d["device_id"] == device_id for d in reg.get("devices", [])):
        raise ValueError(f"device_id {device_id!r} already registered in garden {reg.get('garden_id')!r}")
    is_camera = str(dev_type).startswith("camera_")
    trigger = bool(can_trigger_alarm) if can_trigger_alarm is not None else False
    confirm = bool(can_confirm_alarm) if can_confirm_alarm is not None else is_camera
    devices = list(reg.get("devices", []))
    devices.append({
        "device_id": device_id,
        "node_id": node_id,
        "kind": kind,
        "type": dev_type,
        "name": name,
        "status": status,
        "can_trigger_alarm": trigger,
        "can_confirm_alarm": confirm,
    })
    return {
        "v": REGISTRY_VERSION,
        "garden_id": reg.get("garden_id"),
        "updated_ts": ts,
        "devices": devices,
    }


def delete_device(reg: dict, *, device_id: str, ts: int) -> dict:
    """Remove a device from a devices registry. Returns a NEW dict."""
    devices = [d for d in reg.get("devices", []) if d["device_id"] != device_id]
    if len(devices) == len(reg.get("devices", [])):
        raise ValueError(f"device_id {device_id!r} not found in garden {reg.get('garden_id')!r}")
    return {
        "v": REGISTRY_VERSION,
        "garden_id": reg.get("garden_id"),
        "updated_ts": ts,
        "devices": devices,
    }


def edit_device(reg: dict, *, device_id: str, name: str | None = None,
                node_id: str | None = None, kind: str | None = None,
                dev_type: str | None = None, status: str | None = None,
                can_trigger_alarm: bool | None = None,
                can_confirm_alarm: bool | None = None, ts: int) -> dict:
    """Edit an existing device's details in a devices registry. Returns a NEW dict.
    Validates the edited fields (ids, device taxonomy). Only the fields explicitly supplied
    are changed; the alarm roles are set only when a bool is passed (None preserves)."""
    devices = list(reg.get("devices", []))
    idx = next((i for i, d in enumerate(devices) if d["device_id"] == device_id), None)
    if idx is None:
        raise ValueError(f"device_id {device_id!r} not found in garden {reg.get('garden_id')!r}")

    dev = dict(devices[idx])

    if node_id is not None:
        node_id = node_id.strip()
        if not is_valid_id(node_id):
            raise ValueError(f"invalid node_id {node_id!r} (charset [a-z0-9-], len 1-64)")
        dev["node_id"] = node_id

    if name is not None:
        dev["name"] = name.strip()

    if status is not None:
        dev["status"] = status.strip()

    new_kind = kind if kind is not None else dev["kind"]
    new_type = dev_type if dev_type is not None else dev["type"]
    if kind is not None or dev_type is not None:
        validate_device_type(new_kind, new_type)  # raises on bad kind/type
        dev["kind"] = new_kind
        dev["type"] = new_type

    if can_trigger_alarm is not None:
        dev["can_trigger_alarm"] = bool(can_trigger_alarm)
    if can_confirm_alarm is not None:
        dev["can_confirm_alarm"] = bool(can_confirm_alarm)

    devices[idx] = dev
    return {
        "v": REGISTRY_VERSION,
        "garden_id": reg.get("garden_id"),
        "updated_ts": ts,
        "devices": devices,
    }



# ---------------------------------------------------------------------------
# I/O: single-writer reads + writes.
#
# Fastly KV is EVENTUALLY consistent, so a read immediately after a write is not
# guaranteed to see it — a naive KV read-modify-write loses rapid successive
# updates. So the LOCAL mirror (`configs/{sid}-registry.json`) is the AUTHORITATIVE
# source of truth (strongly consistent on disk), and KV is a write-THROUGH
# projection the edge reads. This mirrors the reference provisioner, whose source
# of truth is its database, not a KV read-back. `ctx` carries service_id +
# store ids + token + configs_dir.
# ---------------------------------------------------------------------------


def _mirror_path(ctx: dict) -> "object":
    import pathlib
    base = pathlib.Path(ctx.get("configs_dir") or (pathlib.Path(__file__).resolve().parent.parent / "configs"))
    return base / f"{ctx['service_id']}-registry.json"


def _lock_path(ctx: dict) -> "object":
    """Sentinel file the cross-process write lock is held on — a SEPARATE file from the
    mirror so the lock fd is never the same fd we rewrite (and so it survives an atomic
    replace of the mirror)."""
    return _mirror_path(ctx).with_suffix(".lock")


@contextlib.contextmanager
def _writer_lock(ctx: dict):
    """Hold an EXCLUSIVE, cross-process advisory lock for the duration of a
    load→mutate→save. Enforces the single-writer-PROCESS invariant against the portal's
    per-request `provision.cli` subprocesses (and any concurrent in-process caller),
    which would otherwise interleave read-modify-write and lose updates on the on-disk
    mirror + the KV blob the edge reads.

    `fcntl.flock` is stdlib + POSIX (the Pi is Linux); it's also present on macOS/CI. On
    a platform without it (e.g. Windows) we degrade to a no-op so imports never break —
    the real device is always POSIX, so the lock is always live where it matters."""
    try:
        import fcntl
    except ImportError:  # non-POSIX (Windows) — no flock; degrade to no-op
        yield
        return
    p = _lock_path(ctx)
    p.parent.mkdir(exist_ok=True)
    # Open (create) the sentinel and block until we own it; released on close/exit even
    # if the body raises. The fd is opened fresh each call so it's process- and
    # thread-safe (each holder has its own description).
    with open(p, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _load_mirror(ctx: dict) -> dict:
    p = _mirror_path(ctx)
    if p.exists():
        return json.loads(p.read_text())
    # First run on this host (or a lost mirror): HYDRATE from KV so we don't blindly
    # overwrite existing registry data. Read the gardens doc AND each garden's devices
    # doc — otherwise read_devices would return empty and write_devices would clobber
    # every previously-registered device for that garden (lost-update). KV is eventually
    # consistent, so this is best-effort recovery; once the local mirror exists it is
    # authoritative (the single writer owns it).
    mirror = {"gardens": empty_gardens(), "devices": {}}
    raw = fastly_api.kv_get(ctx["garden_state_store_id"], GARDENS_KEY, ctx["token"])
    if raw:
        mirror["gardens"] = json.loads(raw)
    for g in mirror["gardens"].get("gardens", []):
        gid = g["garden_id"]
        draw = fastly_api.kv_get(ctx["garden_state_store_id"], devices_key(gid), ctx["token"])
        if draw:
            mirror["devices"][gid] = json.loads(draw)
    return mirror


def _save_mirror(ctx: dict, mirror: dict) -> None:
    p = _mirror_path(ctx)
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps(mirror, indent=2))


def read_gardens(ctx: dict) -> dict:
    return _load_mirror(ctx)["gardens"]


def write_gardens(ctx: dict, reg: dict) -> None:
    # Lock the full load→mutate→save so a concurrent writer PROCESS can't interleave
    # and clobber the devices half of the same mirror (single-writer enforcement).
    with _writer_lock(ctx):
        mirror = _load_mirror(ctx)
        mirror["gardens"] = reg
        _save_mirror(ctx, mirror)
        fastly_api.kv_put(ctx["garden_state_store_id"], GARDENS_KEY, json.dumps(reg).encode(), ctx["token"])


def read_devices(ctx: dict, gid: str) -> dict:
    return _load_mirror(ctx)["devices"].get(gid) or empty_devices(gid)


def write_devices(ctx: dict, gid: str, reg: dict) -> None:
    # Lock the full load→mutate→save so a concurrent writer PROCESS can't interleave
    # and clobber the gardens half (or another garden's devices) of the same mirror.
    with _writer_lock(ctx):
        mirror = _load_mirror(ctx)
        mirror["devices"][gid] = reg
        _save_mirror(ctx, mirror)
        fastly_api.kv_put(ctx["garden_state_store_id"], devices_key(gid), json.dumps(reg).encode(), ctx["token"])


def seed_registry(ctx: dict, ts: int, *,
                  default_name: str = "Home", default_tz: str = "America/New_York") -> dict:
    """Ensure `index/gardens` contains the `default` garden (idempotent)."""
    reg = upsert_garden(read_gardens(ctx), DEFAULT_GARDEN, default_name, default_tz, ts)
    write_gardens(ctx, reg)
    return reg


def create_garden(ctx: dict, *, gid: str, name: str, tz: str, ts: int,
                  location: dict | None = None, status: str = "active") -> dict:
    """Create (or update) a garden entry WITH optional location and mint its
    per-garden token — the garden half of register_device, WITHOUT forcing a device.
    Idempotent: re-running updates the entry and re-mints (an idempotent PUT). The
    `default` garden stays tokenless. Returns {garden_id, garden, token}."""
    if not is_valid_id(gid):
        raise ValueError(f"invalid garden_id {gid!r} (charset [a-z0-9-], len 1-64)")
    # Mint FIRST (before persisting the entry) so a failed secret write commits
    # nothing — same ordering invariant as register_device.
    minted = None
    if gid != DEFAULT_GARDEN:
        secret_store_id = ctx.get("garden_tokens_store_id")
        if not secret_store_id:
            raise ValueError("garden_tokens_store_id required to mint a non-default garden token")
        minted = mint_token(secret_store_id, gid, ctx["token"])
    gardens = upsert_garden(read_gardens(ctx), gid, name, tz, ts, status=status, location=location)
    write_gardens(ctx, gardens)
    entry = next(g for g in gardens["gardens"] if g["garden_id"] == gid)
    return {"garden_id": gid, "garden": entry, "token": minted}


def update_garden(ctx: dict, *, gid: str, name: str, tz: str, ts: int,
                  location: dict | None = None, status: str = "active") -> dict:
    """Update a garden's coarse registry entry (name / tz / location) WITHOUT
    minting or rotating its token — the metadata-only counterpart to
    ``create_garden``, for post-setup edits from the Pi Settings page. A rename
    must NOT rotate the per-garden token (that would knock the Pi offline until it
    re-adopted it), so this never touches the Secret Store. ``upsert_garden``
    preserves ``created_ts`` and a prior ``location`` when none is supplied.
    Raises if the garden isn't already in the registry (use ``create_garden`` to
    add a new one). Returns the updated entry."""
    if not is_valid_id(gid):
        raise ValueError(f"invalid garden_id {gid!r} (charset [a-z0-9-], len 1-64)")
    gardens = read_gardens(ctx)
    if not any(g["garden_id"] == gid for g in gardens.get("gardens", [])):
        raise ValueError(f"garden {gid!r} not found in registry — create it first")
    gardens = upsert_garden(gardens, gid, name, tz, ts, status=status, location=location)
    write_gardens(ctx, gardens)
    return next(g for g in gardens["gardens"] if g["garden_id"] == gid)


def mint_token(secret_store_id: str, gid: str, token: str, *, value: str | None = None) -> str:
    """Mint (or rotate-in) a per-garden token into the Secret Store under
    `g.<gid>.token_current`. GUARD: the `default` garden is NEVER issued a token
    (it must stay tokenless or local dev / the single-tenant trip path breaks).
    Returns the token value."""
    if gid == DEFAULT_GARDEN:
        raise ValueError("refusing to mint a token for the 'default' garden (it must stay tokenless)")
    value = value or secrets.token_urlsafe(32)
    fastly_api.secret_put(secret_store_id, token_secret_name(gid, TOKEN_SLOT_CURRENT), value, token)
    # Clear any stale `previous` slot from a prior rotation (e.g. a re-registered
    # garden after a partial teardown that left the Secret Store): the edge accepts
    # current OR previous, so a lingering previous would be an unintended valid
    # credential. An initial mint must leave exactly ONE valid token. (404-tolerant.)
    fastly_api.secret_delete(secret_store_id, token_secret_name(gid, TOKEN_SLOT_PREVIOUS), token)
    return value


def rotate_token(secret_store_id: str, gid: str, token: str, *, prior_current: str,
                 new_value: str | None = None) -> str:
    """Rotate a garden's token: write the prior token into the `previous` slot,
    then mint a fresh `current`. During the window the edge accepts BOTH (it tries
    current then previous), so a Pi still on the old token keeps working until it
    redeploys. Secrets are write-only via the API, so the caller MUST supply
    `prior_current` (from the gitignored deploy env). `default` is never tokenized.
    """
    if gid == DEFAULT_GARDEN:
        raise ValueError("refusing to rotate a token for the 'default' garden")
    new_value = new_value or secrets.token_urlsafe(32)
    fastly_api.secret_put(secret_store_id, token_secret_name(gid, TOKEN_SLOT_PREVIOUS), prior_current, token)
    fastly_api.secret_put(secret_store_id, token_secret_name(gid, TOKEN_SLOT_CURRENT), new_value, token)
    return new_value


def register_device(ctx: dict, *, gid: str, did: str, node_id: str, kind: str,
                    dev_type: str, name: str, ts: int) -> dict:
    """Full device onboarding (the control-plane single write path):
      validate ids + taxonomy + uniqueness -> ensure the garden exists ->
      ensure the per-garden token (non-default) -> write the devices registry.
    Returns a dict with the device, the token (or None for default), and the deploy
    env the Pi needs. `ctx` carries the resolved store ids + token + backend.

    The per-garden token identifies the GARDEN, not a device: adding a second
    camera must NOT rotate it (that would silently break the first camera and the
    gateway, which all share the one token, until each re-adopted the new value).
    So if the caller passes ``ctx["existing_token"]`` (the Pi's current
    ``garden_token`` from secrets.json), we re-PUT that SAME value (idempotent) and
    return it; only the FIRST device in a fresh garden mints a brand-new token.
    """
    token = ctx["token"]
    secret_store_id = ctx.get("garden_tokens_store_id")
    existing_token = ctx.get("existing_token") or None

    if not is_valid_id(gid):
        raise ValueError(f"invalid garden_id {gid!r}")
    known = validate_device_type(kind, dev_type)

    # Validate uniqueness against the authoritative mirror BEFORE any write (so an
    # invalid/duplicate request fails before mutating anything).
    devices = read_devices(ctx, gid)
    devices = add_device(devices, device_id=did, node_id=node_id, kind=kind,
                         dev_type=dev_type, name=name, ts=ts)

    # Ensure the token FIRST (before persisting the device). If the secret write
    # fails, nothing is committed to the registry, so re-running the command works
    # (the PUT is idempotent). The reverse order would leave an orphan device that
    # fails the uniqueness check on retry. The `default` garden stays tokenless.
    # `value=existing_token` keeps an already-provisioned garden's token stable
    # across device adds; falling back to None mints a fresh one for a new garden.
    minted = None
    if gid != DEFAULT_GARDEN:
        if not secret_store_id:
            raise ValueError("garden_tokens_store_id required to mint a non-default garden token")
        minted = mint_token(secret_store_id, gid, token, value=existing_token)

    # Now persist: ensure the garden is in index/gardens, then append the device.
    gardens = read_gardens(ctx)
    if not any(g["garden_id"] == gid for g in gardens.get("gardens", [])):
        gardens = upsert_garden(gardens, gid, ctx.get("garden_name", gid),
                                ctx.get("garden_tz", "UTC"), ts,
                                location=ctx.get("garden_location"))
        write_gardens(ctx, gardens)
    write_devices(ctx, gid, devices)

    return {
        "garden_id": gid,
        "device": {"device_id": did, "node_id": node_id, "kind": kind, "type": dev_type, "name": name},
        "token": minted,
        "token_is_known_type": known,
        "deploy_env": {
            "GP_GARDEN_ID": gid,
            "GP_DEVICE_ID": did,
            "GP_NODE_ID": node_id,
            "GP_GARDEN_TOKEN": minted or "",
            "GP_BACKEND": ctx.get("backend_url", ""),
        },
    }


def unregister_device(ctx: dict, *, gid: str, did: str, ts: int) -> dict:
    """Full device unregistration (the control-plane single write path):
      delete the device from devices registry -> if it is the last device in the
      non-default garden, delete the garden token from Secret Store.
    Returns a dict with the unregistered device id and garden id.
    """
    token = ctx["token"]
    secret_store_id = ctx.get("garden_tokens_store_id")

    if not is_valid_id(gid):
        raise ValueError(f"invalid garden_id {gid!r}")
    if not is_valid_id(did):
        raise ValueError(f"invalid device_id {did!r}")

    # Read and delete from registry
    devices_reg = read_devices(ctx, gid)
    devices_reg = delete_device(devices_reg, device_id=did, ts=ts)
    write_devices(ctx, gid, devices_reg)

    # Check if there are any devices left in this garden
    remaining = devices_reg.get("devices", [])
    deleted_token = False
    if len(remaining) == 0 and gid != DEFAULT_GARDEN and secret_store_id:
        current = token_secret_name(gid, TOKEN_SLOT_CURRENT)
        previous = token_secret_name(gid, TOKEN_SLOT_PREVIOUS)
        # secret_delete is already 404-tolerant (a missing slot is fine), so anything
        # that reaches here is a REAL failure where the token may still be a valid edge
        # credential. The device is already out of the registry, so we must NOT half-
        # commit silently: surface it (TokenStillLiveError) so the operator knows the
        # token is LIVE and retries, rather than believing the garden is fully gone.
        try:
            fastly_api.secret_delete(secret_store_id, current, token)
            fastly_api.secret_delete(secret_store_id, previous, token)
            deleted_token = True
        except Exception as e:  # noqa: BLE001 — re-raised as a typed, actionable error
            raise TokenStillLiveError(gid, [current, previous], e) from e

    return {
        "garden_id": gid,
        "device_id": did,
        "deleted_token": deleted_token,
    }


def edit_device_in_registry(ctx: dict, *, gid: str, did: str, name: str | None = None,
                             node_id: str | None = None, kind: str | None = None,
                             dev_type: str | None = None, status: str | None = None,
                             can_trigger_alarm: bool | None = None,
                             can_confirm_alarm: bool | None = None, ts: int) -> dict:
    """Full device edit in the registry (control-plane single write path)."""
    if not is_valid_id(gid):
        raise ValueError(f"invalid garden_id {gid!r}")

    devices_reg = read_devices(ctx, gid)
    devices_reg = edit_device(devices_reg, device_id=did, name=name, node_id=node_id,
                              kind=kind, dev_type=dev_type, status=status,
                              can_trigger_alarm=can_trigger_alarm,
                              can_confirm_alarm=can_confirm_alarm, ts=ts)
    write_devices(ctx, gid, devices_reg)

    # Return the updated device entry
    updated_dev = next(d for d in devices_reg["devices"] if d["device_id"] == did)
    return {
        "garden_id": gid,
        "device": updated_dev
    }

