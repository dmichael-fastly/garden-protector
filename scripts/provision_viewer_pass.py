#!/usr/bin/env python3
"""scripts/provision_viewer_pass.py — set (or clear) a garden's OPTIONAL viewer
password on the Fastly edge.

The edge dashboard is view-only and, when a viewer password is configured for a
garden, gates viewing behind it (see backend/src/main.rs `viewer_gate_for`). The
password lives in the `garden_tokens` Secret Store under `g.<gid>.viewer_pass`.
This pushes the password from the Pi/operator `.env` (GP_VIEWER_PASSCODE) up to
the edge, so the `.env` stays the single source of truth.

Usage:
  GP_VIEWER_PASSCODE=hunter2 python3 scripts/provision_viewer_pass.py        # set
  python3 scripts/provision_viewer_pass.py --passcode hunter2                # set
  python3 scripts/provision_viewer_pass.py --clear                           # remove (open)
  python3 scripts/provision_viewer_pass.py --garden backyard --passcode ...  # per-garden

Service id + tokens-store id are read from configs/<service_id>.json (written by
gp-provision); the Fastly API token from $FASTLY_API_KEY or configs/.fastly_token.
"""
import argparse
import glob
import json
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from provision import fastly_api
from provision.ids import token_secret_name

VIEWER_PASS_SLOT = "viewer_pass"  # parity with backend VIEWER_PASS_SLOT
REPO = pathlib.Path(__file__).resolve().parent.parent


def _load_state(service_id: str | None) -> dict:
    cfg = REPO / "configs"
    if service_id:
        path = cfg / f"{service_id}.json"
    else:
        # Auto-detect the single live service state (exclude *-registry.json).
        cands = [p for p in glob.glob(str(cfg / "*.json")) if not p.endswith("-registry.json")]
        if len(cands) != 1:
            sys.exit(f"could not auto-detect service state in {cfg} ({len(cands)} candidates); "
                     f"pass --service-id")
        path = pathlib.Path(cands[0])
    if not path.exists():
        sys.exit(f"state file not found: {path}")
    return json.loads(path.read_text())


def _token(state: dict) -> str:
    tok = os.environ.get("FASTLY_API_KEY")
    if tok:
        return tok.strip()
    tok_file = REPO / "configs" / ".fastly_token"
    if tok_file.exists() and tok_file.read_text().strip():
        return tok_file.read_text().strip()
    if state.get("fastly_api_key"):
        return state["fastly_api_key"]
    sys.exit("no Fastly API token ($FASTLY_API_KEY, configs/.fastly_token, or state json)")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Set/clear a garden's edge viewer password")
    ap.add_argument("--service-id", default=None)
    ap.add_argument("--garden", default=os.environ.get("GP_GARDEN_ID", "default"))
    ap.add_argument("--passcode", default=os.environ.get("GP_VIEWER_PASSCODE", ""))
    ap.add_argument("--clear", action="store_true", help="remove the viewer password (dashboard becomes open)")
    args = ap.parse_args(argv)

    state = _load_state(args.service_id)
    store_id = state.get("garden_tokens_store_id")
    if not store_id:
        sys.exit("garden_tokens_store_id missing from state json")
    token = _token(state)
    name = token_secret_name(args.garden, VIEWER_PASS_SLOT)

    if args.clear or not args.passcode:
        if not args.clear:
            print("[viewer-pass] no passcode given (GP_VIEWER_PASSCODE empty) -> clearing the gate")
        fastly_api.secret_delete(store_id, name, token)
        print(f"[viewer-pass] CLEARED {name} (garden '{args.garden}' dashboard is now OPEN, view-only)")
    else:
        fastly_api.secret_put(store_id, name, args.passcode, token)
        print(f"[viewer-pass] SET {name} (garden '{args.garden}' dashboard now requires the viewer password)")
    print("[viewer-pass] note: Secret Store changes take ~15-20s to propagate to the edge.")


if __name__ == "__main__":
    main()
