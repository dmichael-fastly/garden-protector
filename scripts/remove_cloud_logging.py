#!/usr/bin/env python3
"""Take a garden's edge logs OUT of the cloud.

Policy: telemetry is Pi-local (SQLite) only. We do NOT persist edge logs in the FOS
images bucket — each log object is a billable Class A write and they pile up ~1/sec
(tens of thousands of tiny `telemetry/...log.gz` objects per day). This script makes an
already-provisioned service comply:

  1. --endpoint : delete the "Garden Protector Telemetry" S3 logging endpoint from the
                  Compute service (clone -> delete -> validate -> activate) so no NEW
                  telemetry/ objects are written. Idempotent (no-op if already gone).
  2. --purge    : delete the existing `telemetry/` objects already in the FOS bucket.

With no flags it does whatever the available credentials allow, skipping (with a note)
any step whose creds are absent — so it's safe to run from the provisioning host (has the
Fastly token) for --endpoint, and on the Pi (has the FOS secret) for --purge.

  python scripts/remove_cloud_logging.py configs/<service_id>.json [--endpoint] [--purge]

Token resolution (for --endpoint): $GP_FASTLY_TOKEN, else the config's `fastly_api_key`,
else a `.fastly_token` file next to the config. FOS creds (for --purge) come from the
config's `fos_*` fields.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ENDPOINT_NAME = "Garden Protector Telemetry"
TELEMETRY_PREFIX = "telemetry/"


def _resolve_token(svc, config_path):
    tok = os.environ.get("GP_FASTLY_TOKEN") or svc.get("fastly_api_key")
    if tok:
        return tok.strip()
    side = os.path.join(os.path.dirname(os.path.abspath(config_path)), ".fastly_token")
    if os.path.exists(side):
        with open(side) as fh:
            return fh.read().strip()
    return None


def remove_endpoint(svc, config_path):
    from provision import fastly_api
    sid = svc.get("service_id")
    tok = _resolve_token(svc, config_path)
    if not (sid and tok):
        print("  [endpoint] SKIP — need service_id + a Fastly token")
        return
    new_ver = fastly_api.delete_logging_endpoint(sid, ENDPOINT_NAME, token=tok)
    if new_ver is None:
        print(f"  [endpoint] already absent on {sid} (nothing to do)")
    else:
        print(f"  [endpoint] removed '{ENDPOINT_NAME}' from {sid} — activated v{new_ver}")


def purge_objects(svc):
    region = svc.get("fos_region")
    bucket = svc.get("fos_bucket")
    ak = svc.get("fos_access_key_id")
    sk = svc.get("fos_secret_access_key")
    endpoint = svc.get("fos_endpoint")
    if not (region and bucket and ak and sk and endpoint):
        print("  [purge] SKIP — need fos_region/bucket/endpoint + access key + secret")
        return
    import boto3
    from botocore.config import Config
    s3 = boto3.client("s3", region_name=region, endpoint_url="https://" + endpoint,
                      aws_access_key_id=ak, aws_secret_access_key=sk,
                      config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))
    paginator = s3.get_paginator("list_objects_v2")
    batch, deleted = [], 0
    for page in paginator.paginate(Bucket=bucket, Prefix=TELEMETRY_PREFIX):
        for o in page.get("Contents", []):
            batch.append({"Key": o["Key"]})
            if len(batch) == 1000:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
                deleted += len(batch)
                print(f"  [purge] deleted {deleted}…")
                batch = []
    if batch:
        s3.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
        deleted += len(batch)
    print(f"  [purge] done — {deleted} telemetry/ objects removed from {bucket}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Remove a garden's cloud edge-log shipping.")
    ap.add_argument("config", help="path to configs/<service_id>.json")
    ap.add_argument("--endpoint", action="store_true", help="delete the S3 logging endpoint")
    ap.add_argument("--purge", action="store_true", help="delete existing telemetry/ objects")
    args = ap.parse_args(argv)
    with open(args.config) as fh:
        svc = json.load(fh)
    # Default: attempt both (each self-skips when its creds are missing).
    do_endpoint = args.endpoint or not (args.endpoint or args.purge)
    do_purge = args.purge or not (args.endpoint or args.purge)
    print(f"service: {svc.get('service_name') or svc.get('service_id')}")
    if do_endpoint:
        remove_endpoint(svc, args.config)
    if do_purge:
        purge_objects(svc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
