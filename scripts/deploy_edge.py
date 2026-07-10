#!/usr/bin/env python3
"""scripts/deploy_edge.py — redeploy the prebuilt Compute package to the LIVE edge.

Mirrors the orchestrator's package step (provision/orchestrator.py): clone the
active version (so all resource links — KV/Secret/Config stores, logging,
domains — are inherited), upload the new package, and activate. Does NOT touch
stores or links; it only ships new code.

Run AFTER `cd backend && fastly compute build` (or `make build`).

  python3 scripts/deploy_edge.py                 # deploy backend/pkg/*.tar.gz
  python3 scripts/deploy_edge.py --service-id ... --package path.tar.gz

Service id + token come from configs/<service_id>.json + $FASTLY_API_KEY /
configs/.fastly_token (same as scripts/provision_viewer_pass.py).
"""
import argparse
import glob
import json
import os
import pathlib
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from provision import fastly_api

REPO = pathlib.Path(__file__).resolve().parent.parent


def _state(service_id):
    cfg = REPO / "configs"
    if service_id:
        path = cfg / f"{service_id}.json"
    else:
        cands = [p for p in glob.glob(str(cfg / "*.json")) if not p.endswith("-registry.json")]
        if len(cands) != 1:
            sys.exit(f"could not auto-detect service state in {cfg}; pass --service-id")
        path = pathlib.Path(cands[0])
    if not path.exists():
        sys.exit(f"state file not found: {path}")
    return json.loads(path.read_text())


def _token(state):
    tok = os.environ.get("FASTLY_API_KEY")
    if tok:
        return tok.strip()
    f = REPO / "configs" / ".fastly_token"
    if f.exists() and f.read_text().strip():
        return f.read_text().strip()
    if state.get("fastly_api_key"):
        return state["fastly_api_key"]
    sys.exit("no Fastly API token ($FASTLY_API_KEY, configs/.fastly_token, or state json)")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Redeploy the Compute package to the live edge")
    ap.add_argument("--service-id", default=None)
    ap.add_argument("--package", default=str(REPO / "backend" / "pkg" / "garden-protector-backend.tar.gz"))
    ap.add_argument(
        "--no-purge",
        action="store_true",
        help="skip the post-deploy purge_all (the CDN keeps serving old /static + HTML until it ages out)",
    )
    ap.add_argument(
        "--purge-wait",
        type=int,
        default=50,
        help="seconds to wait after purge_all for propagation across POPs before returning (0 to skip the wait)",
    )
    args = ap.parse_args(argv)

    pkg = pathlib.Path(args.package)
    if not pkg.exists():
        sys.exit(f"package not found at {pkg}; run `cd backend && fastly compute build` first")

    # Guard against shipping a STALE package. `make build` refreshes backend/bin/main.wasm
    # but does NOT repackage the tarball; if the staged wasm is newer than the package, the
    # package predates the last build and would deploy old code (silently — the version
    # activates fine, it just runs the wrong wasm). Repack from the fresh wasm first.
    wasm = REPO / "backend" / "bin" / "main.wasm"
    if wasm.exists() and pkg.stat().st_mtime < wasm.stat().st_mtime:
        sys.exit(
            f"refusing to deploy: {pkg.name} is OLDER than backend/bin/main.wasm, so it would "
            f"ship stale code.\nRepack from the staged wasm first:\n"
            f"  (cd backend && fastly compute pack --wasm-binary bin/main.wasm)"
        )

    state = _state(args.service_id)
    sid = state["service_id"]
    token = _token(state)

    active = fastly_api.get_active_version(sid, token)
    ver = fastly_api.clone_version(sid, active, token)
    print(f"[deploy] service={sid} active=v{active} -> cloned v{ver}; uploading {pkg.name} ({pkg.stat().st_size} bytes)")
    fastly_api.deploy_package(sid, ver, pkg.read_bytes(), token)
    fastly_api.activate_version(sid, ver, token)
    print(f"[deploy] activated v{ver}. Edge: {state.get('backend_url', '(see state json)')}")

    # Purge the whole edge cache. The CDN caches `/static` (gp.css/gp.js) ignoring the
    # `?v=` query and now also fronts the HTML pages, so without this an operator keeps
    # serving the old assets after the new wasm is live (the classic "deployed but the
    # page didn't change" trap). purge_all is always a HARD purge.
    if args.no_purge:
        print("[deploy] --no-purge set: SKIPPING purge_all (CDN may serve stale /static + HTML until it ages out)")
    else:
        fastly_api.purge_all(sid, token)
        print(f"[deploy] purge_all issued for service={sid}")
        # Propagation takes up to ~2 min across POPs; wait a beat so a probe immediately
        # after this script sees the new bytes rather than a half-purged edge.
        if args.purge_wait > 0:
            print(f"[deploy] waiting {args.purge_wait}s for purge to propagate across POPs...")
            time.sleep(args.purge_wait)
        print("[deploy] done. If a probe still shows old assets, give it up to ~2 min and re-check.")


if __name__ == "__main__":
    main()
