#!/usr/bin/env python3
"""provision/streaming.py — shared SSE + subprocess-streaming plumbing.

Factored out of ``provision/console.py`` so BOTH the admin console and the Pi
LAN portal (``hardware/portal.py``) drive the same ``gp-provision`` CLI and frame
its progress as Server-Sent Events through one code path. There is exactly one
provisioning pipeline; this module is the seam every long op streams through.

Nothing here touches Fastly or the network directly — it only frames objects as
SSE and runs ``python -m provision.cli <op>`` as a subprocess, relaying stdout
line-by-line. The Fastly token is passed to that subprocess via the environment
(``FASTLY_API_KEY``), never on argv and never echoed into the stream.
"""
import json
import os
import pathlib
import re
import subprocess

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Strip emoji/pictographs from a streamed log line for DISPLAY. The orchestrator's
# progress lines (orchestrator._log) still carry a friendly leading glyph as the in-band
# level convention (✓ = done, ⚠️ = warning — see _level_of, which classifies from it), but
# the browser now renders a leveled SVG icon (GP.logIcon) instead, so the raw glyph is
# removed before the line reaches the client. Covers the pictograph + symbol + arrow blocks
# plus the U+FE0F variation selector that trails glyphs like ⚠️.
_GLYPH_RE = re.compile(
    "[\U0001F000-\U0001FAFF☀-➿←-⇿⬀-⯿ℹ️Ⓐ-ⓩ]"
)


def strip_glyphs(s):
    """Remove emoji/pictographs and tidy the leftover whitespace (pure)."""
    return re.sub(r"\s{2,}", " ", _GLYPH_RE.sub("", s)).strip()


# ---------------------------------------------------------------------------
# SSE framing (pure).
# ---------------------------------------------------------------------------

def sse_event(obj, event=None):
    """Frame one object as a Server-Sent Event. Multi-line JSON is split across
    ``data:`` lines per the SSE spec; a blank line terminates the event."""
    payload = json.dumps(obj)
    out = []
    if event:
        out.append(f"event: {event}")
    for line in payload.split("\n"):
        out.append(f"data: {line}")
    out.append("")
    out.append("")
    return ("\n".join(out)).encode()


# ---------------------------------------------------------------------------
# op + params -> gp-provision argv (pure, unit-tested so the wiring can't drift).
# ---------------------------------------------------------------------------

def build_cli_args(op, params, *, mock=False):
    """Map a console/wizard operation + params to the `gp-provision` CLI argv (the
    part after `python -m provision.cli`). Pure + unit-tested so the wiring can't
    drift.

    Only NON-secret params come from the browser; the token is supplied via the
    subprocess environment (FASTLY_API_KEY) or skipped entirely in mock mode."""
    def opt(flag, key, default=None):
        v = params.get(key, default)
        return [flag, str(v)] if v not in (None, "", []) else []

    if op == "provision":
        args = ["provision", "--service-name", params["service_name"]]
        args += opt("--region", "region")
        args += opt("--domain", "domain")
        args += opt("--bucket", "bucket")
        if _truthy(params.get("skip_archive")):
            args += ["--skip-archive"]
        return args
    if op == "seed-registry":
        return ["seed-registry"] + opt("--service-id", "service_id")
    if op == "create-garden":
        # NB: garden NOTES are Pi-local only (kept in pi-garden.json) and are NEVER
        # passed to the edge — the coarse edge entry is name/tz/location only.
        args = ["create-garden", "--garden", params["garden"]]
        args += opt("--name", "name")
        args += opt("--tz", "tz")
        args += opt("--address", "address")
        args += opt("--lat", "lat")
        args += opt("--lon", "lon")
        args += opt("--service-id", "service_id")
        # Optional first device (e.g. the Pi gateway), mirroring register-device flags.
        args += opt("--device", "device")
        args += opt("--kind", "kind")
        args += opt("--type", "type")
        args += opt("--node", "node")
        return args
    if op == "update-garden":
        # Metadata-only edit of an existing garden (name/tz/location) — NO device,
        # NO token. Notes stay Pi-local, exactly as for create-garden.
        args = ["update-garden", "--garden", params["garden"]]
        args += opt("--name", "name")
        args += opt("--tz", "tz")
        args += opt("--address", "address")
        args += opt("--lat", "lat")
        args += opt("--lon", "lon")
        args += opt("--service-id", "service_id")
        return args
    if op == "register-device":
        args = ["register-device",
                "--garden", params["garden"],
                "--device", params["device"],
                "--kind", params["kind"],
                "--type", params["type"]]
        args += opt("--node", "node")
        args += opt("--name", "name")
        args += opt("--garden-name", "garden_name")
        args += opt("--tz", "tz")
        args += opt("--service-id", "service_id")
        return args
    if op == "rotate-token":
        # The prior (current) garden token is a SECRET — it must NOT land on argv
        # (process list) or in the SSE command echo. It travels via the child env
        # (GP_PRIOR_GARDEN_TOKEN, injected in stream_cli), exactly like FASTLY_API_KEY.
        # Only the non-secret --garden / --service-id flags go on argv.
        return ["rotate-token", "--garden", params["garden"]] + opt("--service-id", "service_id")
    if op == "unregister-device":
        args = ["unregister-device",
                "--garden", params["garden"],
                "--device", params["device"]]
        args += opt("--service-id", "service_id")
        return args
    if op == "edit-device":
        args = ["edit-device",
                "--garden", params["garden"],
                "--device", params["device"]]
        args += opt("--name", "name")
        args += opt("--node", "node")
        args += opt("--kind", "kind")
        args += opt("--type", "type")
        args += opt("--status", "status")
        # Alarm roles: booleans, so opt() forwards BOTH true and false (only an absent key is
        # skipped -> the CLI preserves the device's current role).
        args += opt("--can-trigger-alarm", "can_trigger_alarm")
        args += opt("--can-confirm-alarm", "can_confirm_alarm")
        args += opt("--service-id", "service_id")
        return args
    if op == "teardown":
        args = ["teardown"] + opt("--service-id", "service_id")
        if _truthy(params.get("remove_data")):
            args += ["--remove-data"]
        return args
    raise ValueError(f"unknown op {op!r}")



def _truthy(v):
    return str(v).lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# SUBPROCESS STREAMER — runs the CLI and yields SSE frames.
# ---------------------------------------------------------------------------

def stream_cli(cfg, op, params, *, write):
    """Run `python -m provision.cli <op …>` and push each stdout line to ``write``
    as an SSE frame. Token comes from the environment (or mock mode). ``write``
    raises on a disconnected client, which terminates the subprocess.

    ``cfg`` is any object exposing ``python_exe``, ``mock`` and ``configs_dir``
    (e.g. console.ConsoleConfig or the portal's StreamConfig)."""
    try:
        argv = [cfg.python_exe, "-m", "provision.cli"] + build_cli_args(op, params, mock=cfg.mock)
    except (KeyError, ValueError) as e:
        write(sse_event({"line": f"bad request: {e}", "level": "error"}))
        write(sse_event({"done": True, "ok": False, "code": -1}, event="done"))
        return

    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"   # emoji in progress lines, locale-independent
    # Secret params travel via the child ENV (never argv / the echoed command line):
    # rotate-token's prior garden token, like FASTLY_API_KEY. build_cli_args has
    # already excluded it from argv; the CLI reads it from GP_PRIOR_GARDEN_TOKEN.
    if op == "rotate-token" and params.get("prior_token"):
        env["GP_PRIOR_GARDEN_TOKEN"] = str(params["prior_token"])
    if cfg.mock:
        env["FASTLY_MOCK_MODE"] = "1"
        # The mock transport never uses the token, but the CLI still REQUIRES one
        # (it would otherwise prompt on stdin and abort, since we close stdin). Hand
        # it a placeholder so the dry-run flows run unattended.
        if not env.get("FASTLY_API_KEY"):
            env["FASTLY_API_KEY"] = "MOCK-TOKEN"

    write(sse_event({"line": f"$ gp-provision {' '.join(build_cli_args(op, params, mock=cfg.mock))}"
                     + ("   [MOCK MODE — no Fastly calls]" if cfg.mock else ""), "level": "cmd"}))

    code = pump_process(argv, env, write)
    if code is None:  # client disconnected mid-stream
        return

    ok = code == 0
    # On success, hand back any deploy-env the op produced so the modal can show it.
    extra = {}
    if ok and op in ("register-device", "create-garden"):
        extra["deploy_env"] = _read_deploy_env(cfg.configs_dir, params.get("garden"), params.get("device"))
    write(sse_event({"done": True, "ok": ok, "code": code, **extra}, event="done"))


def pump_process(argv, env, write):
    """Run ``argv`` and stream each stdout line to ``write`` as an SSE frame.
    Returns the exit code, or ``None`` if the client disconnected (the subprocess
    is then terminated). Generic so the streaming path is testable without the CLI."""
    # Force UTF-8 on the decode side so the friendly emoji in the CLI's progress lines
    # survive even when the portal runs under a non-UTF-8 locale (e.g. a bare systemd
    # service on the Pi); the encode side is forced via PYTHONIOENCODING in the child env.
    proc = subprocess.Popen(
        argv, cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, text=True, bufsize=1,
        encoding="utf-8", errors="replace",
    )
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line:
                # Classify from the original (the leading glyph still encodes the level),
                # but ship a glyph-free line — the client renders a leveled SVG icon instead.
                write(sse_event({"line": strip_glyphs(line), "level": _level_of(line)}))
        proc.wait()
        return proc.returncode
    except (BrokenPipeError, ConnectionResetError):
        proc.terminate()
        return None
    finally:
        if proc.poll() is None:
            proc.terminate()


def _level_of(line):
    s = line.strip()
    if s.startswith("[gp-provision]"):
        s = s[len("[gp-provision]"):].strip()
    # Leading glyphs are the friendly convention from orchestrator._log: a ✓ marks a
    # finished step (green) and a ⚠️ a non-fatal warning (amber). Check these BEFORE the
    # keyword fallbacks so a friendly done line isn't miscolored.
    if s.startswith("✓"):
        return "ok"
    if s.startswith("⚠️"):
        return "warn"
    low = s.lower()
    if "fail" in low or "error" in low or "traceback" in low:
        return "error"
    if "warning" in low or "warn" in low:
        return "warn"
    return "info"


def _read_deploy_env(configs_dir, gid, did):
    if not (gid and did):
        return None
    p = pathlib.Path(configs_dir) / f"{gid}-{did}.env"
    if not p.exists():
        return None
    env = {}
    for line in p.read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    return env
