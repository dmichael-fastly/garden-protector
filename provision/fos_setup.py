"""Fastly Object Storage (FOS) bucket + access-key lifecycle.

The bucket is created via boto3 against the S3-compatible FOS endpoint
  (`{region}.object.fastlystorage.app`, sigv4, path-addressing);
- FOS access keys are minted via the FASTLY API
  (`/resources/object-storage/access-keys`): a temporary `read-write-admin` key
  creates the bucket, then a permanent BUCKET-SCOPED `read-write-objects` key is
  minted for runtime and the temp admin key deleted.
"""

from .client import fastly

# Region -> Fastly Shield POP (subset; extend as needed).
SHIELD_MAP = {
    "us-east-1": "iad-va-us",
    "us-west-1": "sea-wa-us",
    "us-central-1": "mdw-il-us",
    "eu-central-1": "fra-de-eu",
    "ap-southeast-1": "syd-au-aus",
}


def region_endpoint(region: str) -> str:
    return f"{region}.object.fastlystorage.app"


def _s3_client(region: str, access_key: str, secret_key: str):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=f"https://{region_endpoint(region)}",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 5, "mode": "standard"},
        ),
    )


def ensure_fos_access_key(
    description: str, token: str, *, permission: str, buckets=None
) -> dict:
    """Find-or-create an FOS access key by description. `permission` is
    `read-write-admin` (temp) or `read-write-objects` (permanent). `buckets`
    scopes the key to specific buckets. Returns {access_key, secret_key, ...}."""
    existing = fastly("GET", "/resources/object-storage/access-keys", token=token)
    items = existing if isinstance(existing, list) else existing.get("data", [])
    for k in items:
        if k.get("description") == description:
            return k
    payload = {"permission": permission, "description": description}
    if buckets:
        payload["buckets"] = list(buckets)
    return fastly("POST", "/resources/object-storage/access-keys", payload, token=token)


def delete_fos_access_key(key_id: str, token: str) -> None:
    try:
        fastly(
            "DELETE",
            f"/resources/object-storage/access-keys/{key_id}",
            token=token,
            expect_empty=True,
        )
    except RuntimeError as exc:
        if "404" not in str(exc):
            raise


def _error_code(exc: Exception) -> str:
    """Extract the S3/boto error code (e.g. '404', 'NoSuchBucket') robustly."""
    return getattr(exc, "response", {}).get("Error", {}).get("Code", "")


_ABSENT_CODES = {"404", "NoSuchBucket", "NotFound"}


def ensure_fos_bucket(name: str, region: str, access_key: str, secret_key: str) -> bool:
    """Idempotent bucket create (head -> create). Returns True if present/created.
    Only a true 404/NoSuchBucket is treated as 'absent' — an auth/region error must
    propagate, NOT be misread as absent (which would then create_bucket blindly)."""
    s3 = _s3_client(region, access_key, secret_key)
    try:
        s3.head_bucket(Bucket=name)
        return True
    except Exception as exc:  # noqa: BLE001
        if _error_code(exc) not in _ABSENT_CODES:
            raise
    s3.create_bucket(Bucket=name)
    return True


# Auth errors that mean a freshly-minted FOS key has NOT propagated yet (vs. a real
# "bucket gone" which means the key already works). Retry on these during the poll.
_AUTH_NOT_READY_CODES = {
    "InvalidAccessKeyId",
    "SignatureDoesNotMatch",
    "AccessDenied",
    "403",
    "401",
}


def wait_for_fos_key(
    name: str,
    region: str,
    access_key: str,
    secret_key: str,
    *,
    timeout: float = 30.0,
    interval: float = 1.5,
) -> bool:
    """Poll head_bucket with a FRESH key until it AUTHENTICATES, bounded by `timeout`.

    A just-minted FOS access key takes a few seconds to propagate; using it before then
    fails with InvalidAccessKeyId/AccessDenied. This replaces a fixed `time.sleep` —
    it returns as soon as the key works (a clean head, OR a real NoSuchBucket/404 which
    still proves the key authenticated), retries only on auth-not-ready errors, and
    returns False if it never propagates within the timeout (caller proceeds anyway and
    surfaces any subsequent failure)."""
    import time

    s3 = _s3_client(region, access_key, secret_key)
    deadline = time.monotonic() + timeout
    while True:
        try:
            s3.head_bucket(Bucket=name)
            return True
        except Exception as exc:  # noqa: BLE001
            code = _error_code(exc)
            if code in _ABSENT_CODES:
                return (
                    True  # key authenticated; bucket just doesn't exist (already gone)
                )
            if code not in _AUTH_NOT_READY_CODES:
                return (
                    True  # some other error — key is usable, let the real op report it
                )
            if time.monotonic() >= deadline:
                return False
            time.sleep(interval)


def delete_fos_bucket(name: str, region: str, access_key: str, secret_key: str) -> None:
    """Empty-first delete (best-effort): (1) abort
    incomplete multipart uploads — they keep a bucket "not empty"; (2) paginate-delete
    every object; (3) delete the bucket, RETRYING on BucketNotEmpty since S3 listing is
    eventually consistent (a delete right after emptying can still 409). Tolerates an
    already-absent bucket (matched by error CODE)."""
    import time

    s3 = _s3_client(region, access_key, secret_key)
    try:
        # 1) Abort any incomplete multipart uploads.
        try:
            for u in s3.list_multipart_uploads(Bucket=name).get("Uploads", []):
                s3.abort_multipart_upload(
                    Bucket=name, Key=u["Key"], UploadId=u["UploadId"]
                )
        except Exception:  # noqa: BLE001 — best-effort
            pass
        # 2) Delete every object (paginated, batched up to 1000/req).
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=name):
            objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            for j in range(0, len(objs), 1000):
                s3.delete_objects(Bucket=name, Delete={"Objects": objs[j : j + 1000]})
        # 3) Delete the bucket, retrying on the eventual-consistency BucketNotEmpty.
        for attempt in range(15):
            try:
                s3.delete_bucket(Bucket=name)
                return
            except Exception as exc:  # noqa: BLE001
                if _error_code(exc) == "BucketNotEmpty" and attempt < 14:
                    time.sleep(2 if attempt <= 5 else 5)
                    continue
                raise
    except Exception as exc:  # noqa: BLE001 — teardown is best-effort
        if _error_code(exc) not in _ABSENT_CODES:
            raise
