"""CDN read-signing VCL for the evidence/time-lapse archive.

A VCL delivery service fronts the PRIVATE FOS bucket: browsers GET
`https://<cdn>/img/<key>?key=<secret>`; the service gates on the shared secret,
SigV4-signs the origin read with Fastly's native `digest.awsv4_hmac`, caches the
immutable object, and shields the origin for offload.

Two Garden-Protector-specific changes from the base FOS-fronting pattern:
  1. the public read URL carries an `/img/` vanity prefix that is stripped to the
     bucket-relative object key the edge writes (`g/<gid>/<did>/evidence/...`);
  2. the cache-key/query allow-list is RESTRICTIVE — single-object reads only, NO
     S3 LIST params — so a holder of the one shared read secret cannot enumerate
     every garden's object keys (cross-tenant metadata leak). Re-add LIST params
     only behind a per-garden-scoped list endpoint (RFC §6 / Step 4).

Shielding is ON for origin offload. The edge<->shield auth handoff uses a baked-in
random `X-Edge-CDN-Auth` secret rather than `Fastly-FF` (which is derived from a
client-controllable header and is therefore spoofable): the edge stamps the secret
on every bereq in `miss_pass`, the
shield's `vcl_recv` skips its own auth gate ONLY on an exact match, and any
client-supplied `X-Edge-CDN-Auth` is stripped on ingress before the gate reads it.

Credentials live in write-only edge dictionaries `fos_credentials`
(access_key/secret_key/bucket/region) and `cdn_auth` (secret), populated by
`fastly_api.ensure_cdn_service`. `__CDN_SECRET__` (the read gate's fallback) and
`__SHIELD_SECRET__` (the edge<->shield marker) are substituted at load.
"""

import secrets


def load_vcl(cdn_secret: str) -> str:
    """Return the CDN delivery VCL with the read-gate secret and a fresh random
    edge<->shield marker secret substituted. A new shield secret per call is fine:
    it is baked identically into the single compiled artifact that runs on BOTH the
    edge and shield POPs, so the two sides always agree by construction."""
    return _VCL_TEMPLATE.replace("__CDN_SECRET__", cdn_secret).replace(
        "__SHIELD_SECRET__", secrets.token_hex(32)
    )


_VCL_TEMPLATE = r"""
ratecounter auth_fail_rc {}
penaltybox auth_fail_pb {}

sub miss_pass {
  # Edge->shield auth marker: stamp the baked-in secret on every outgoing bereq so
  # the shield POP's vcl_recv knows this request already cleared edge auth and can
  # skip its own gate. Unspoofable from outside (only this compiled VCL knows it).
  # With shielding off the bereq goes straight to FOS, which ignores the header.
  set bereq.http.X-Edge-CDN-Auth = "__SHIELD_SECRET__";

  # SigV4-sign the read ONLY on the direct-to-origin fetch: req.backend.is_shield
  # is TRUE on the edge when a shield is selected, so this block runs on the POP
  # that actually talks to FOS (the shield, or the edge when shielding is off) and
  # never double-signs.
  if ((req.method == "GET" || req.method == "HEAD") && !req.backend.is_shield) {
    declare local var.fosAccessKey STRING;
    declare local var.fosSecretKey STRING;
    declare local var.fosBucket STRING;
    declare local var.fosRegion STRING;
    declare local var.fosHost STRING;
    declare local var.canonicalHeaders STRING;
    declare local var.signedHeaders STRING;
    declare local var.canonicalRequest STRING;
    declare local var.canonicalQuery STRING;
    declare local var.stringToSign STRING;
    declare local var.dateStamp STRING;
    declare local var.signature STRING;
    declare local var.scope STRING;

    set var.fosAccessKey = table.lookup(fos_credentials, "access_key", "missing");
    set var.fosSecretKey = table.lookup(fos_credentials, "secret_key", "missing");
    set var.fosBucket = table.lookup(fos_credentials, "bucket", "missing");
    set var.fosRegion = table.lookup(fos_credentials, "region", "us-east-1");
    # The host VALUE in the SigV4 signature must equal the Host FOS actually
    # receives. We do NOT set bereq.http.host here — the fos_origin backend's native
    # `override_host` (in fastly_api.ensure_cdn_service) sets the real upstream Host.
    # We only compute the same value to feed the canonical request below, because
    # override_host is applied AFTER this sub runs: at signing time bereq.http.host
    # is still the inbound CDN domain, so signing it yields SignatureDoesNotMatch.
    set var.fosHost = var.fosRegion ".object.fastlystorage.app";

    set bereq.http.x-amz-content-sha256 = digest.hash_sha256("");
    # Round x-amz-date down to the minute so concurrent reads of the same object
    # collapse to ONE signature (request coalescing) while staying inside SigV4's
    # 15-minute validity window.
    set bereq.http.x-amz-date = strftime({"%Y%m%dT%H%M00Z"}, now);

    # Path-style addressing: prepend the bucket unless the first path segment is
    # already the bucket. The edge writes keys WITHOUT a bucket prefix
    # (g/<gid>/...) and vcl_recv has already stripped the public /img/ prefix.
    if (regsub(bereq.url.path, "^/([^/]+)/.*$", "\1") != var.fosBucket) {
      set bereq.url = "/" var.fosBucket bereq.url;
    }
    # Drop our auth 'key' param so it never reaches FOS or affects signing.
    set bereq.url = querystring.filter(bereq.url, "key");
    set var.canonicalQuery = querystring.sort(bereq.url.qs);

    set var.dateStamp = strftime({"%Y%m%d"}, now);
    set var.canonicalHeaders = ""
      "host:" var.fosHost LF
      "x-amz-content-sha256:" bereq.http.x-amz-content-sha256 LF
      "x-amz-date:" bereq.http.x-amz-date LF
    ;
    set var.signedHeaders = "host;x-amz-content-sha256;x-amz-date";
    set var.canonicalRequest = ""
      req.method LF
      bereq.url.path LF
      var.canonicalQuery LF
      var.canonicalHeaders LF
      var.signedHeaders LF
      digest.hash_sha256("")
    ;
    set var.scope = var.dateStamp "/" var.fosRegion "/s3/aws4_request";
    set var.stringToSign = ""
      "AWS4-HMAC-SHA256" LF
      bereq.http.x-amz-date LF
      var.scope LF
      regsub(digest.hash_sha256(var.canonicalRequest), "^0x", "")
    ;
    set var.signature = digest.awsv4_hmac(
      var.fosSecretKey, var.dateStamp, var.fosRegion, "s3", var.stringToSign);
    set bereq.http.Authorization = "AWS4-HMAC-SHA256 "
      "Credential=" var.fosAccessKey "/" var.scope ", "
      "SignedHeaders=" var.signedHeaders ", "
      "Signature=" regsub(var.signature, "^0x", "");
    unset bereq.http.Accept;
    unset bereq.http.Accept-Language;
    unset bereq.http.User-Agent;
  }
}

sub vcl_recv {
  # Strip any client-supplied X-Edge-CDN-Auth before the gate reads it: only the
  # edge's own miss_pass (with the secret baked into VCL) may set a matching value,
  # so a spoofed header from a client is dropped here and the edge gate still fires.
  if (req.restarts == 0 && req.http.X-Edge-CDN-Auth != "__SHIELD_SECRET__") {
    unset req.http.X-Edge-CDN-Auth;
  }

  # Normalize the public /img/ read prefix to the bucket-relative object key. The
  # edge writes keys with NO 'img/' segment; miss_pass then prepends the bucket.
  # Without this, the bucket-prefix guard would sign /<bucket>/img/<key> -> 404.
  if (req.url.path ~ "^/img/") {
    set req.url = regsub(req.url, "^/img/", "/");
  }

  # Read gate: require the shared secret via ?key= or x-fastly-key, UNLESS this is
  # the trusted edge->shield hop (X-Edge-CDN-Auth matches the baked secret). The
  # table.lookup fallback is the substituted cdn_secret so an unprovisioned dict
  # still fails closed on an unguessable value rather than an empty key.
  #
  # CONSTANT-LENGTH compare: hash BOTH the presented key and the expected secret with
  # digest.hash_sha256 before the `!=`. VCL string `!=` is a byte compare that can
  # short-circuit at the first differing byte, so comparing the raw secret leaks a
  # timing signal proportional to the matching prefix. Hashing first makes every
  # comparison a fixed 64-hex-char byte compare regardless of the input — no length
  # or prefix oracle. (The penaltybox below still rate-limits guessers.)
  declare local var.expectedKeyHash STRING;
  set var.expectedKeyHash = digest.hash_sha256(table.lookup(cdn_auth, "secret", "__CDN_SECRET__"));
  if (req.restarts == 0 && req.http.X-Edge-CDN-Auth != "__SHIELD_SECRET__" &&
      digest.hash_sha256(subfield(req.url.qs, "key", "&")) != var.expectedKeyHash &&
      digest.hash_sha256(req.http.x-fastly-key) != var.expectedKeyHash) {
    # penalty-box repeat offenders by client IP (2 fails/min -> 1m box)
    declare local var.fails INTEGER;
    set var.fails = ratelimit.ratecounter_increment(auth_fail_rc, client.ip, 1);
    if (var.fails >= 2) {
      ratelimit.penaltybox_add(auth_fail_pb, client.ip, 1m);
    }
    error 401 "Unauthorized";
  }
  if (req.restarts == 0 &&
      req.http.X-Edge-CDN-Auth != "__SHIELD_SECRET__" &&
      ratelimit.penaltybox_has(auth_fail_pb, client.ip)) {
    error 429 "rate limited";
  }

  # Cache-key hardening (post-auth — the gate above already read ?key= from the
  # original req.url). RESTRICTIVE allow-list: keep ONLY single-object GET params,
  # dropping the auth key AND all S3 LIST params (list-type/prefix/delimiter/...).
  # Listing is deliberately unsupported so the one shared read secret can't be used
  # to enumerate every garden's keys (cross-tenant leak).
  set req.url = querystring.filter_except(req.url,
    "versionId,partNumber,response-content-type,response-content-disposition,response-cache-control");
  set req.url = querystring.sort(req.url);

  # Disable serve-while-revalidate on shield nodes so only the edge serves stale.
  if (fastly.ff.visits_this_service > 1) {
    set req.max_stale_while_revalidate = 0s;
  }

#FASTLY recv
  if (req.method != "HEAD" && req.method != "GET") {
    return(pass);
  }
  return(lookup);
}

sub vcl_hash {
  # Hash on the full URL (path+query) AND host so query variants never collide and
  # the cache key never includes the (already-stripped) auth secret.
  set req.hash += req.url;
  set req.hash += req.http.host;
#FASTLY hash
  return(hash);
}

sub vcl_hit {
#FASTLY hit
  return(deliver);
}

sub vcl_miss {
#FASTLY miss
  call miss_pass;
  return(fetch);
}

sub vcl_pass {
#FASTLY pass
  call miss_pass;
  return(pass);
}

sub vcl_fetch {
#FASTLY fetch
  unset beresp.http.x-amz-id-2;
  unset beresp.http.x-amz-request-id;
  unset beresp.http.x-amz-version-id;
  # Archive objects are immutable (unique date+time+cid keys, never overwritten), so
  # cache aggressively at the Fastly edge AND tell clients to keep them effectively
  # forever. FOS sets NO Cache-Control on the object, so we add one here: 30 days (the
  # 30-day default) + `immutable` so browsers never revalidate.
  if (beresp.status == 200) {
    set beresp.cacheable = true;
    set beresp.ttl = 2592000s; # 30 days
    set beresp.http.Cache-Control = "public, max-age=2592000, immutable";
  }
  return(deliver);
}

sub vcl_error {
#FASTLY error
  return(deliver);
}

sub vcl_deliver {
#FASTLY deliver
  unset resp.http.x-amz-id-2;
  unset resp.http.x-amz-request-id;
  unset resp.http.server;
  return(deliver);
}

sub vcl_log {
#FASTLY log
}
"""
