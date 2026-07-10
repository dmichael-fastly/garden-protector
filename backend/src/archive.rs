//! CDN-fronted evidence archive (Step 3, RFC §2) — the FOS/CDN DATA LAYER.
//!
//! The durable archive lives in Fastly Object Storage (object keys carry the garden
//! dimension `g/<gid>/` from day one — even `default` — so nothing ever migrates). The
//! write is STRICTLY best-effort and happens AFTER the decision, off the fail-closed
//! path: the PUT is dispatched FIRE-AND-FORGET (`send_async`, never awaited) so it adds
//! NO latency to the Pi-timed `/api/evidence` response, and a missing `fos_config`/backend
//! or a dispatch error is swallowed (the Pi-facing `{action}` response and the 503 path
//! are never affected). Reads are served by a separate CDN VCL service that SigV4-signs to
//! the private bucket (see provision/vcl.py).
//!
//! OBSERVABILITY (EDGE-006): because the PUT is never awaited, the FOS HTTP status is
//! inherently invisible here. We add only LIGHTWEIGHT, off-path, NON-cloud signal: each
//! dispatch outcome (`Dispatched` / `DispatchFailed` / `Skipped`) bumps a per-instance
//! atomic counter whose running tally is folded into the existing `archive_put` `log_evt`
//! line — so a systematically broken bucket surfaces as a climbing `disp_fail`/`skip`
//! count in `log-tail` rather than a single easy-to-miss warn. The tally is process-local
//! (resets per instance) and never persisted to the cloud; it reports DISPATCH health, not
//! confirmed LANDINGS (confirm those via the archive History browse or a read-back).
//!
//! This module holds the credentials, SigV4 GET/PUT/DELETE/LIST sends, CDN-first read,
//! object-key build/parse, and retention math. The route HANDLERS (handle_archive_*)
//! stay in main.rs. Extracted verbatim in Phase 5a modularization — behavior unchanged.
//! The fastly host-ABI types compile natively, so this links under both wasm32 and
//! native `cargo test`.

use crate::sigv4::*;
use crate::{log_evt, now_secs, GARDEN_TOKENS_STORE};
use fastly::{secret_store::SecretStore, ConfigStore, Request};
use std::sync::atomic::{AtomicU64, Ordering};
use time::OffsetDateTime;

// ---------------------------------------------------------------------------
// EDGE-006: lightweight, PER-INSTANCE archive-PUT dispatch health.
//
// The PUT is fire-and-forget (send_async, never awaited), so the FOS HTTP STATUS
// (e.g. a 403 from expired creds) is INHERENTLY unobservable here — observing it
// would mean awaiting the response on the Pi-timed path, which is exactly what the
// off-path design forbids. What we CAN cheaply observe is the DISPATCH outcome:
// did we attempt the send, did `send_async` accept it, or did we skip/fail before
// the wire. These three running counts are kept in process-local atomics (no I/O,
// off the hot path) and folded into the EXISTING `archive_put` log line so a
// SYSTEMATICALLY broken bucket shows up as a climbing `disp_fail`/`skip` tally in
// `log-tail`, instead of needing someone to spot one warn line. They are NOT
// cloud-persisted (in-memory only, reset on each instance — same model as the
// per-instance login lockout map) and they make NO claim about whether bytes
// actually LANDED: confirm landings via the archive History browse / a read-back.
// ---------------------------------------------------------------------------
static ARCHIVE_PUT_DISPATCHED: AtomicU64 = AtomicU64::new(0);
static ARCHIVE_PUT_DISPATCH_FAIL: AtomicU64 = AtomicU64::new(0);
static ARCHIVE_PUT_SKIPPED: AtomicU64 = AtomicU64::new(0);

/// PUT-dispatch outcome for the per-instance health tally. `Dispatched` = `send_async`
/// accepted the request (NOT a confirmed landing). `DispatchFailed` = `send_async`
/// errored (e.g. missing/misconfigured backend). `Skipped` = bailed before the wire
/// (no creds / archive disabled).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum ArchivePutOutcome {
    Dispatched,
    DispatchFailed,
    Skipped,
}

/// Records one PUT-dispatch outcome and returns a compact `disp=N disp_fail=N skip=N`
/// running-tally suffix for the log line. PURE w.r.t. I/O (atomics only), so it is safe
/// off the hot path and unit-testable on the native target.
fn record_archive_put_outcome(outcome: ArchivePutOutcome) -> String {
    match outcome {
        ArchivePutOutcome::Dispatched => ARCHIVE_PUT_DISPATCHED.fetch_add(1, Ordering::Relaxed),
        ArchivePutOutcome::DispatchFailed => {
            ARCHIVE_PUT_DISPATCH_FAIL.fetch_add(1, Ordering::Relaxed)
        }
        ArchivePutOutcome::Skipped => ARCHIVE_PUT_SKIPPED.fetch_add(1, Ordering::Relaxed),
    };
    format!(
        "disp={} disp_fail={} skip={}",
        ARCHIVE_PUT_DISPATCHED.load(Ordering::Relaxed),
        ARCHIVE_PUT_DISPATCH_FAIL.load(Ordering::Relaxed),
        ARCHIVE_PUT_SKIPPED.load(Ordering::Relaxed),
    )
}

/// Config Store with the NON-SECRET FOS fields (access_key id, bucket, region,
/// endpoint). Absent (local dev / archive disabled) -> archive skipped.
const FOS_CONFIG_STORE: &str = "fos_config";
/// The bucket's SigV4 secret key is a credential, so it lives in the Secret Store
/// (NOT the Config Store, which is plaintext-readable), under this slash-free name.
const FOS_SECRET_KEY_NAME: &str = "fos.secret_key";
/// Backend name (the FOS bucket as a TLS origin) the provisioner registers on the
/// Compute service. Absent -> the `send` errors and is swallowed.
const FOS_BACKEND: &str = "fos_archive";
/// Max gap (1 day) between a Pi-provided capture timestamp and the edge clock before the
/// edge distrusts it and falls back to its own time for the archive-key date. Generous on
/// purpose: NTP-synced Pis are within seconds, so this only rejects gross garbage (e.g. an
/// unset clock reading ~1970) that would otherwise scatter frames across nonsense dates.
const MAX_CAPTURE_SKEW_SECS: i64 = 86_400;

// CDN-first reads (user rule: pull stored files via the CDN read-signing service, NEVER
// direct from FOS). For an image GET the edge fetches through the CDN VCL service
// (provision/vcl.py) so the read is CACHED + the private bucket stays fronted, then
// proxies the bytes — the viewer gate is preserved and the browser NEVER sees the CDN
// secret. The secret is sent SERVER-SIDE as the `x-fastly-key` HEADER (never `?key=` in
// the URL, never in logs), and is attached ONLY on the CDN hop — the direct-FOS fallback
// SigV4-signs and must never carry it. LIST stays FOS-direct by design:
// the CDN VCL deliberately blocks S3 LIST params (cross-tenant key-enumeration guard).
/// Backend name for the CDN read-signing service the provisioner registers on the edge
/// when the archive is wired. Absent -> `cdn_signed_get` returns `None` and the caller
/// falls back to the direct FOS read, so un-reprovisioned gardens keep working.
const CDN_BACKEND: &str = "cdn_read";
/// `fos_config` key holding the CDN service host (e.g. `svc-cdn.global.ssl.fastly.net`),
/// written at provision time. Absent -> no CDN read path; fall back to FOS-direct.
const CDN_HOST_KEY: &str = "cdn_host";
/// The CDN read gate's shared secret, in the Secret Store under this slash-free name
/// (NOT the Config Store — it's a credential). Sent as `x-fastly-key`.
const CDN_SECRET_KEY_NAME: &str = "fos.cdn_secret";

/// Slugs a label to the archive-key charset `[a-z0-9-]` (lowercase; runs of other
/// chars collapse to a single `-`; trimmed; capped). Keeps object keys path-safe AND
/// free of the `_` field delimiter so `parse_archive_key` round-trips. Empty -> "none".
pub fn slug_for_key(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut prev_dash = false;
    for c in s.chars() {
        let lc = c.to_ascii_lowercase();
        if lc.is_ascii_alphanumeric() {
            out.push(lc);
            prev_dash = false;
        } else if !prev_dash {
            out.push('-');
            prev_dash = true;
        }
    }
    let capped: String = out.trim_matches('-').chars().take(40).collect();
    let capped = capped.trim_matches('-').to_string();
    if capped.is_empty() {
        "none".to_string()
    } else {
        capped
    }
}

/// Inverted-time token for the within-day filename segment so a lexical-ascending
/// LIST returns NEWEST-first within a day (ListObjectsV2 is ascending-only, so the
/// "most recent N" read is one `max-keys=N` page with no tail-truncation risk). 5-digit
/// zero-padded `86400 - seconds_of_day` (range 1..=86400). Pure + unit-tested.
pub fn invert_seconds_of_day(hour: u8, minute: u8, second: u8) -> String {
    let secs = (hour as u32 * 3600 + minute as u32 * 60 + second as u32).min(86_399);
    format!("{:05}", 86_400 - secs)
}

/// Builds the durable object key. DATE-FIRST (so one LIST per day returns the whole
/// day across cameras) with the event metadata embedded (so the archive feed needs
/// no sidecar fetch). The within-day filename is prefixed with an INVERTED-time token
/// (`invert_seconds_of_day`) so lexical-ascending == chronological-descending, letting
/// the day read fetch only the newest N in one page:
/// `g/<gid>/evidence/YYYY/MM/DD/<INV>_HHMMSS_<action>_<species>_<confpct>_<did>_<batch>_<cid>.jpg`
/// — UTC; fields are `_`-delimited and each individually `_`-free (slugged); the readable
/// `HHMMSS` is kept after `<INV>` for debugging/parse. The `<batch>` segment is the shared
/// capture-batch id (every camera the Pi pushed in one tick carries the SAME value, so the
/// UI can group them as one multi-angle set); it slugs to "none" when the Pi sent no batch.
/// The garden dimension is ALWAYS present (even `default`). NOTE: the `<batch>` field was
/// added after the original 7-field scheme — `parse_archive_key` reads BOTH (count-based),
/// so pre-existing objects keep showing in the date-browse (no migration).
pub fn evidence_object_key(
    gid: &str,
    did: &str,
    action: &str,
    species: &str,
    conf: f32,
    cid: &str,
    batch: &str,
    ts_secs: i64,
) -> String {
    let dt = OffsetDateTime::from_unix_timestamp(ts_secs).unwrap_or(OffsetDateTime::UNIX_EPOCH);
    let confpct = (conf * 100.0).round().clamp(0.0, 100.0) as u32;
    let inv = invert_seconds_of_day(dt.hour(), dt.minute(), dt.second());
    format!(
        "g/{}/evidence/{:04}/{:02}/{:02}/{}_{:02}{:02}{:02}_{}_{}_{}_{}_{}_{}.jpg",
        gid,
        dt.year(),
        u8::from(dt.month()),
        dt.day(),
        inv,
        dt.hour(),
        dt.minute(),
        dt.second(),
        slug_for_key(action),
        slug_for_key(species),
        confpct,
        slug_for_key(did),
        slug_for_key(batch),
        slug_for_key(cid),
    )
}

/// Inverse of `evidence_object_key` for the archive feed: parses one date-first key
/// into a JSON event record, or `None` for anything that doesn't match the current
/// scheme (e.g. a legacy device-first object). PURE + unit-tested.
pub fn parse_archive_key(gid: &str, key: &str) -> Option<serde_json::Value> {
    let rest = key.strip_prefix(&format!("g/{}/evidence/", gid))?;
    let segs: Vec<&str> = rest.split('/').collect();
    if segs.len() != 4 {
        return None; // expect YYYY / MM / DD / filename.jpg
    }
    let (y, mo, d) = (segs[0], segs[1], segs[2]);
    let fname = segs[3].strip_suffix(".jpg")?;
    let parts: Vec<&str> = fname.split('_').collect();
    if parts.len() < 7 {
        return None;
    }
    // parts[0] = <INV> (inverted-time sort token, 5 digits); parts[1] = HHMMSS.
    let inv = parts[0];
    if inv.len() != 5 || !inv.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let hms = parts[1];
    if hms.len() != 6 || !hms.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let conf: u32 = parts[4].parse().ok()?;
    // Two layouts share this parser, disambiguated by field COUNT (every field is slugged,
    // so none can contain the `_` delimiter — the count is unambiguous):
    //   8+ fields -> current scheme: parts[6] = <batch>, parts[7..] = <cid>.
    //   7  fields -> legacy scheme (no batch): parts[6] = <cid>, batch is "" (ungrouped).
    let (batch, cid) = if parts.len() >= 8 {
        (parts[6].to_string(), parts[7..].join("_"))
    } else {
        (String::new(), parts[6..].join("_"))
    };
    Some(serde_json::json!({
        "date": format!("{}-{}-{}", y, mo, d),
        "time": format!("{}:{}:{}", &hms[0..2], &hms[2..4], &hms[4..6]),
        "action": parts[2],
        "species": parts[3],
        "confidence": conf,
        "device": parts[5],
        "batch": batch,
        "cid": cid,
        "key": key,
    }))
}

/// The FOS archive credentials + bucket addressing, read from `fos_config` (non-secret)
/// + the Secret Store (`fos.secret_key`). `None` when the archive isn't provisioned /
/// the secret hasn't propagated yet — every archive op degrades to a no-op.
pub struct FosCreds {
    pub access_key: String,
    pub secret_key: String,
    pub bucket: String,
    pub region: String,
    pub endpoint: String,
}

/// `Ok(creds)` or `Err(reason)` — the reason preserves the three distinct skip cases
/// (not-provisioned / incomplete-config / secret-propagating) for the edge log, since
/// these are operationally different (incomplete config is a bug to fix; a propagating
/// secret is transient + expected right after a provision).
pub fn fos_creds() -> Result<FosCreds, &'static str> {
    let cfg = ConfigStore::try_open(FOS_CONFIG_STORE)
        .map_err(|_| "fos_config store unavailable (archive not provisioned)")?;
    let get = |k: &str| cfg.try_get(k).ok().flatten();
    let (access_key, bucket, region) = match (get("access_key"), get("bucket"), get("region")) {
        (Some(a), Some(b), Some(r)) => (a, b, r),
        _ => return Err("fos_config present but incomplete (need access_key+bucket+region)"),
    };
    let endpoint =
        get("endpoint").unwrap_or_else(|| format!("{}.object.fastlystorage.app", region));
    let secret_key = SecretStore::open(GARDEN_TOKENS_STORE)
        .ok()
        .and_then(|s| s.try_get(FOS_SECRET_KEY_NAME).ok().flatten())
        .map(|s| String::from_utf8_lossy(&s.plaintext()).into_owned())
        .ok_or("fos.secret_key not readable in garden_tokens (Secret Store propagating?)")?;
    Ok(FosCreds {
        access_key,
        secret_key,
        bucket,
        region,
        endpoint,
    })
}

/// All inner texts of `<open>...<close>` occurrences (cheap, dependency-free XML
/// scraping for the S3 ListObjectsV2 response). PURE. Our keys/prefixes never carry
/// XML-special chars, so no entity decoding is needed.
pub fn xml_inner_all(xml: &str, open: &str, close: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut rest = xml;
    while let Some(i) = rest.find(open) {
        let after = &rest[i + open.len()..];
        match after.find(close) {
            Some(j) => {
                out.push(after[..j].to_string());
                rest = &after[j + close.len()..];
            }
            None => break,
        }
    }
    out
}

/// Sign + send a GET to the FOS bucket (path-style) and return `(status, body)`.
/// `canonical_uri` is `/<bucket>[/<key>]`; `canonical_query` is the already-sorted-
/// encoded query (or ""). Off any safety path; a transport error -> `None`.
///
/// `bypass_cache` controls the edge cache for THIS subrequest. FOS responses carry no
/// `Cache-Control`/`ETag` (only `Date`), so Compute's default cache holds them for ~1h —
/// fine for an immutable object GET, but FATAL for a LIST: after a wipe/prune the objects
/// are gone yet the cached LIST keeps reporting deleted photos for up to an hour. Callers
/// that need a LIVE view (the listing/days path) pass `true` (-> `with_pass(true)`, send
/// straight to the backend, never cache); the immutable image-read fallback passes `false`
/// to keep its intentional edge caching. Scoped per-call ON PURPOSE so the LIST fix can't
/// silently disable caching on any other GET that shares this helper.
pub fn fos_signed_get(
    creds: &FosCreds,
    canonical_uri: &str,
    canonical_query: &str,
    bypass_cache: bool,
    trace_id: &str,
) -> Option<(u16, Vec<u8>)> {
    let (amz_date, date_stamp) = amz_times(now_secs());
    let empty_hash = sha256_hex(b"");
    let auth = sigv4_authorization(
        "GET",
        &creds.endpoint,
        canonical_uri,
        canonical_query,
        &empty_hash,
        &creds.access_key,
        &creds.secret_key,
        &creds.region,
        "s3",
        &amz_date,
        &date_stamp,
    );
    let url = if canonical_query.is_empty() {
        format!("https://{}{}", creds.endpoint, canonical_uri)
    } else {
        format!(
            "https://{}{}?{}",
            creds.endpoint, canonical_uri, canonical_query
        )
    };
    let req = Request::get(url)
        .with_header("Host", creds.endpoint.as_str())
        .with_header("x-amz-date", amz_date.as_str())
        .with_header("x-amz-content-sha256", empty_hash.as_str())
        .with_header("Authorization", auth.as_str())
        // `with_pass(true)` sends straight to FOS and never caches the response. We do this
        // for the LIST path because FOS LIST carries no Cache-Control/ETag, so the default
        // ~1h edge cache would otherwise serve a stale listing after a wipe/prune.
        .with_pass(bypass_cache);
    match req.send(FOS_BACKEND) {
        Ok(mut resp) => Some((resp.get_status().as_u16(), resp.take_body().into_bytes())),
        Err(e) => {
            log_evt(
                trace_id,
                "archive",
                "fos_get",
                "warn",
                &format!("send failed: {}", e),
            );
            None
        }
    }
}

/// The CDN read URL for an object `key` (the bucket-relative key the edge writes, e.g.
/// `g/<gid>/evidence/.../x.jpg` — NO bucket prefix). The `/img/` vanity prefix is what
/// the CDN VCL strips before re-prepending the bucket (provision/vcl.py). PURE.
pub fn cdn_img_url(host: &str, key: &str) -> String {
    format!("https://{}/img/{}", host, key)
}

/// Fetch `key` through the CDN read-signing service (cached, private-bucket-fronting),
/// authenticating SERVER-SIDE with the shared secret as the `x-fastly-key` HEADER.
/// Returns `(status, body)`, or `None` when the CDN isn't wired (no `cdn_host` /
/// `fos.cdn_secret`) or the send fails — the caller then falls back to a direct FOS GET.
/// The secret never appears in the URL/logs, and is attached ONLY here (never on the FOS
/// fallback). Off any safety path.
pub fn cdn_signed_get(key: &str, trace_id: &str) -> Option<(u16, Vec<u8>)> {
    let host = ConfigStore::try_open(FOS_CONFIG_STORE)
        .ok()
        .and_then(|cfg| cfg.try_get(CDN_HOST_KEY).ok().flatten())?;
    let secret = SecretStore::open(GARDEN_TOKENS_STORE)
        .ok()
        .and_then(|s| s.try_get(CDN_SECRET_KEY_NAME).ok().flatten())
        .map(|s| String::from_utf8_lossy(&s.plaintext()).into_owned())?;
    let req = Request::get(cdn_img_url(&host, key)).with_header("x-fastly-key", secret.as_str());
    match req.send(CDN_BACKEND) {
        Ok(mut resp) => {
            let status = resp.get_status().as_u16();
            log_evt(
                trace_id,
                "archive",
                "cdn_get",
                "ok",
                &format!("status={}", status),
            );
            Some((status, resp.take_body().into_bytes()))
        }
        Err(e) => {
            log_evt(
                trace_id,
                "archive",
                "cdn_get",
                "warn",
                &format!("send failed (falling back to FOS): {}", e),
            );
            None
        }
    }
}

/// One ListObjectsV2 page (XML body) for `prefix` + `continuation` token. `None` on
/// non-200/transport error. NOTE: Fastly Object Storage does NOT reliably honor the S3
/// `max-keys` parameter (a request carrying it fails) NOR `delimiter`/CommonPrefixes (a
/// delimiter list silently omits prefixes — verified live), so we only ever do a plain
/// flat paginated list and derive structure (the set of days) from the keys in code.
fn fos_list(
    creds: &FosCreds,
    prefix: &str,
    continuation: Option<&str>,
    trace_id: &str,
) -> Option<String> {
    let mut params: Vec<(&str, String)> =
        vec![("list-type", "2".into()), ("prefix", prefix.into())];
    if let Some(c) = continuation {
        params.push(("continuation-token", c.into()));
    }
    let cq = canonical_query(&params);
    let canonical_uri = format!("/{}", creds.bucket);
    // bypass_cache=true: a LIST must always reflect the CURRENT bucket contents. FOS LIST
    // has no Cache-Control/ETag, so without this the listing (used by /api/archive/days,
    // the date browse, AND the prune/wipe sweep) is served from the ~1h edge cache and keeps
    // showing objects that a wipe/prune already deleted. This is the durable fix for "deleted
    // photos still appear in the listing".
    match fos_signed_get(creds, &canonical_uri, &cq, true, trace_id) {
        Some((200, body)) => Some(String::from_utf8_lossy(&body).into_owned()),
        Some((st, _)) => {
            log_evt(
                trace_id,
                "archive",
                "list",
                "warn",
                &format!("prefix={} status={}", prefix, st),
            );
            None
        }
        None => None,
    }
}

/// All object keys under `prefix`, following continuation tokens up to `MAX_PAGES`.
/// Used by the prune sweep (it wants every key under an expired day to delete them).
pub fn fos_list_keys(creds: &FosCreds, prefix: &str, trace_id: &str) -> Vec<String> {
    const MAX_PAGES: usize = 10; // 10 * 1000 keys/day is far beyond any home garden
    let mut keys = Vec::new();
    let mut cont: Option<String> = None;
    for _ in 0..MAX_PAGES {
        let xml = match fos_list(creds, prefix, cont.as_deref(), trace_id) {
            Some(x) => x,
            None => break,
        };
        keys.extend(xml_inner_all(&xml, "<Key>", "</Key>"));
        let truncated = xml_inner_all(&xml, "<IsTruncated>", "</IsTruncated>")
            .first()
            .map(|s| s == "true")
            .unwrap_or(false);
        cont = xml_inner_all(&xml, "<NextContinuationToken>", "</NextContinuationToken>")
            .into_iter()
            .next();
        if !truncated || cont.is_none() {
            break;
        }
    }
    keys
}

/// Sign + send a single-object DELETE (path-style `/<bucket>/<key>`). Mirrors `fos_signed_get`
/// (empty-body hash, same three signed headers; no Content-MD5). Returns the HTTP status, or
/// `None` on transport error. CRITICAL: the response body is DRAINED — on Compute an unread
/// body pins its backend connection, and the pool exhausts after ~16, so undrained deletes
/// failed ~7028/7044 in a wipe. Draining frees the connection so many run per execution.
/// (FOS has no working bulk DeleteObjects — POST ?delete hangs — so this is per-object.)
pub fn fos_signed_delete(creds: &FosCreds, key: &str, _trace_id: &str) -> Result<u16, String> {
    let canonical_uri = format!("/{}/{}", creds.bucket, key);
    let (amz_date, date_stamp) = amz_times(now_secs());
    let empty_hash = sha256_hex(b"");
    let auth = sigv4_authorization(
        "DELETE",
        &creds.endpoint,
        &canonical_uri,
        "",
        &empty_hash,
        &creds.access_key,
        &creds.secret_key,
        &creds.region,
        "s3",
        &amz_date,
        &date_stamp,
    );
    let req = Request::delete(format!("https://{}{}", creds.endpoint, canonical_uri))
        .with_header("Host", creds.endpoint.as_str())
        .with_header("x-amz-date", amz_date.as_str())
        .with_header("x-amz-content-sha256", empty_hash.as_str())
        .with_header("Authorization", auth.as_str());
    match req.send(FOS_BACKEND) {
        Ok(mut resp) => {
            let st = resp.get_status().as_u16();
            let _ = resp.take_body().into_bytes(); // DRAIN -> release the connection to the pool
            Ok(st)
        }
        Err(e) => Err(format!("{}", e)),
    }
}

/// Builds the durable object key the evidence PUT will use, choosing the OBJECT-KEY time
/// the same way `archive_evidence` did internally: prefer the Pi-provided capture timestamp
/// so every camera pushed in one tick lands on ONE timeline second (the whole point of the
/// multi-angle correlation), falling back to the edge clock when it's absent or implausibly
/// far off (`MAX_CAPTURE_SKEW_SECS`), so an unset Pi clock can't file frames under 1970 and
/// pollute the date-browse. Computing the key HERE (in the handler) instead of inside
/// `archive_evidence` lets the SAME key be embedded in the dashboard's `latest_event` and used
/// for the PUT, so the "View details" link always points at the object actually written.
/// `cid` = the request trace id.
#[allow(clippy::too_many_arguments)]
pub fn evidence_key_for(
    gid: &str,
    did: &str,
    action: &str,
    species: &str,
    conf: f32,
    cid: &str,
    batch: &str,
    capture_ts: Option<i64>,
) -> String {
    let now = now_secs();
    let key_ts = match capture_ts {
        Some(ts) if (ts - now).abs() <= MAX_CAPTURE_SKEW_SECS => ts,
        _ => now,
    };
    evidence_object_key(gid, did, action, species, conf, cid, batch, key_ts)
}

/// Best-effort, FIRE-AND-FORGET PUT of the evidence JPEG to the FOS archive at the
/// (precomputed) `object_key`. The PUT is dispatched via `send_async` and NEVER awaited, so it
/// adds NO latency to the Pi-facing `/api/evidence` response (the timed deterrent path) — a
/// missing store/keys is skipped and a dispatch error is swallowed; neither can affect the
/// response. The key is built by `evidence_key_for` (caller-side) so the action/species/conf
/// metadata is embedded in the (date-first) key and the archive feed needs no sidecar.
pub fn archive_evidence(image: &[u8], object_key: &str, trace_id: &str) {
    let creds = match fos_creds() {
        Ok(c) => c,
        Err(why) => {
            let tally = record_archive_put_outcome(ArchivePutOutcome::Skipped);
            log_evt(
                trace_id,
                "kv",
                "archive_put",
                "skip",
                &format!("{} [{}]", why, tally),
            );
            return;
        }
    };
    // SigV4 signing ALWAYS uses the real edge clock (a stale signing time -> the bucket
    // rejects the PUT as expired); this is independent of the OBJECT-KEY time baked into
    // `object_key` by `evidence_key_for`.
    let now = now_secs();
    let (amz_date, date_stamp) = amz_times(now);
    let canonical_uri = format!("/{}/{}", creds.bucket, object_key); // path-style addressing
    let payload_hash = sha256_hex(image);
    let auth = sigv4_authorization(
        "PUT",
        &creds.endpoint,
        &canonical_uri,
        "",
        &payload_hash,
        &creds.access_key,
        &creds.secret_key,
        &creds.region,
        "s3",
        &amz_date,
        &date_stamp,
    );

    let req = Request::put(format!("https://{}{}", creds.endpoint, canonical_uri))
        .with_header("Host", creds.endpoint.as_str())
        .with_header("x-amz-date", amz_date.as_str())
        .with_header("x-amz-content-sha256", payload_hash.as_str())
        .with_header("Authorization", auth.as_str())
        .with_header("Content-Type", "image/jpeg")
        .with_body(image.to_vec());

    // FIRE-AND-FORGET (send_async, PendingRequest intentionally dropped): dispatch the
    // PUT and DO NOT block on FOS's response, so this adds NO latency to the Pi-facing
    // `/api/evidence` response — the deterrent path the Pi times. Per the Fastly SDK a
    // dropped PendingRequest is NOT cancelled ("the request will continue sending even
    // after the program that initiated it exits"), which finally makes this module's
    // off-path claim TRUE (it was previously a synchronous blocking send on the timed
    // path, stacking variable FOS latency onto every deterrent). TRADE-OFF: the PUT's
    // final HTTP status is no longer observable here (we never await it) — only the
    // DISPATCH outcome is logged; confirm actual landings via the archive History browse
    // or a live PUT->read-back, not this log line. EDGE-006: each dispatch outcome also
    // bumps a per-instance health tally folded into the log (`disp/disp_fail/skip`) so a
    // SYSTEMATICALLY failing dispatch is visible as a climbing count, not just a lone warn.
    match req.send_async(FOS_BACKEND) {
        Ok(_pending) => {
            let tally = record_archive_put_outcome(ArchivePutOutcome::Dispatched);
            log_evt(
                trace_id,
                "kv",
                "archive_put",
                "ok",
                &format!(
                    "key={} dispatched async (off-path, fire-and-forget) [{}]",
                    object_key, tally
                ),
            )
        }
        Err(e) => {
            let tally = record_archive_put_outcome(ArchivePutOutcome::DispatchFailed);
            log_evt(
                trace_id,
                "kv",
                "archive_put",
                "warn",
                &format!(
                    "key={} async dispatch failed: {} (best-effort, ignored) [{}]",
                    object_key, e, tally
                ),
            )
        }
    }
}

/// Derives the "YYYY-MM-DD" from a date-first evidence object KEY. Format-agnostic — works
/// for ANY filename under a `g/<gid>/evidence/YYYY/MM/DD/` path (incl. legacy/old-format
/// objects, so the prune sweep can still find + delete them). `None` if it doesn't match.
/// PURE + unit-tested.
pub fn day_from_key(gid: &str, key: &str) -> Option<String> {
    let rest = key.strip_prefix(&format!("g/{}/evidence/", gid))?;
    let segs: Vec<&str> = rest.split('/').collect();
    if segs.len() >= 4 {
        let (y, mo, d) = (segs[0], segs[1], segs[2]);
        if y.len() == 4
            && mo.len() == 2
            && d.len() == 2
            && [y, mo, d]
                .iter()
                .all(|s| s.bytes().all(|b| b.is_ascii_digit()))
        {
            return Some(format!("{}-{}-{}", y, mo, d));
        }
    }
    None
}

/// Validates a `YYYY-MM-DD` and returns the day's object-key prefix, or `None`. PURE.
pub fn archive_day_prefix(gid: &str, date: &str) -> Option<String> {
    let segs: Vec<&str> = date.split('-').collect();
    if segs.len() != 3 {
        return None;
    }
    let (y, m, d) = (segs[0], segs[1], segs[2]);
    let ok = y.len() == 4
        && m.len() == 2
        && d.len() == 2
        && [y, m, d]
            .iter()
            .all(|s| s.bytes().all(|b| b.is_ascii_digit()));
    if ok {
        Some(format!("g/{}/evidence/{}/{}/{}/", gid, y, m, d))
    } else {
        None
    }
}

/// The retention cutoff `YYYY-MM-DD`: the date `days` before `now`. Days strictly OLDER
/// than this (lexically `<`) are pruned; `[cutoff ..= today]` are kept. PURE + tested.
pub fn prune_cutoff_date(now_secs: i64, days: u32) -> String {
    let cutoff = now_secs - (days as i64) * 86_400;
    let dt = OffsetDateTime::from_unix_timestamp(cutoff).unwrap_or(OffsetDateTime::UNIX_EPOCH);
    format!(
        "{:04}-{:02}-{:02}",
        dt.year(),
        u8::from(dt.month()),
        dt.day()
    )
}

/// True if `day` (YYYY-MM-DD) is older than `cutoff`. ISO dates sort lexically ==
/// chronologically, so this is a plain string compare (no timezone math). PURE.
pub fn day_is_expired(day: &str, cutoff: &str) -> bool {
    day < cutoff
}

/// Selects the expired days to prune this run, OLDEST first, capped at `max_days` so one
/// invocation is bounded (the rest are swept on the next run). PURE + unit-tested.
pub fn days_to_prune(all_days: &[String], cutoff: &str, max_days: usize) -> Vec<String> {
    let mut out: Vec<String> = all_days
        .iter()
        .filter(|d| day_is_expired(d, cutoff))
        .cloned()
        .collect();
    out.sort(); // oldest first
    out.truncate(max_days);
    out
}

/// Selects the days to fully WIPE this run: ALL days, OLDEST first, capped at `max_days`
/// so one invocation is bounded (the rest are swept on the next run). Unlike
/// [`days_to_prune`] there is no cutoff — every day is a target. PURE + unit-tested.
pub fn days_to_wipe(all_days: &[String], max_days: usize) -> Vec<String> {
    let mut out: Vec<String> = all_days.to_vec();
    out.sort(); // oldest first
    out.truncate(max_days);
    out
}

/// Every `YYYY-MM-DD` the garden has evidence for, newest first. Shared by the days listing
/// AND the prune sweep. NOTE: FOS's ListObjectsV2 `delimiter`/CommonPrefixes is UNRELIABLE
/// (it silently omits prefixes — verified live: a base list with `delimiter=/` returned
/// only ONE year even though objects existed under another), so we CANNOT walk
/// year->month->day. Instead we flat-list the evidence prefix (paginated) and derive the
/// distinct days from the key paths via `day_from_key` (format-agnostic, so legacy objects
/// count too). Keys list ASCENDING (oldest first), so under the page cap the OLDEST days
/// are always covered — exactly what prune needs; a very large archive may not surface the
/// newest days in browse until older ones are swept (reduce archive volume to avoid).
pub fn evidence_days(creds: &FosCreds, gid: &str, trace_id: &str) -> Vec<String> {
    const MAX_DAY_PAGES: usize = 60; // FOS has no max-keys; pages are ~1000 keys each
    let base = format!("g/{}/evidence/", gid);
    let mut days: Vec<String> = Vec::new();
    let mut cont: Option<String> = None;
    for _ in 0..MAX_DAY_PAGES {
        let xml = match fos_list(creds, &base, cont.as_deref(), trace_id) {
            Some(x) => x,
            None => break,
        };
        for k in xml_inner_all(&xml, "<Key>", "</Key>") {
            if let Some(day) = day_from_key(gid, &k) {
                days.push(day);
            }
        }
        let truncated = xml_inner_all(&xml, "<IsTruncated>", "</IsTruncated>")
            .first()
            .map(|s| s == "true")
            .unwrap_or(false);
        cont = xml_inner_all(&xml, "<NextContinuationToken>", "</NextContinuationToken>")
            .into_iter()
            .next();
        days.sort();
        days.dedup(); // bound memory across pages (a page is mostly one day)
        if !truncated || cont.is_none() {
            break;
        }
    }
    days.reverse(); // newest first
    days
}

/// PURE scope check for an already-percent-decoded archive image key: it must be a
/// plain forward path UNDER this garden's `g/<gid>/evidence/` prefix, end in `.jpg`,
/// contain no traversal (`..`) or empty segment (`//`), and use only our key charset
/// `[a-z0-9/_.-]`. The charset allow-list is the real guard: it rejects any leftover
/// `%` (double-encoding), whitespace, or other byte that a URL parser might later
/// normalize — so the SIGNED path and the SENT path can never diverge. Unit-tested.
pub fn archive_image_key_ok(gid: &str, key: &str) -> bool {
    key.starts_with(&format!("g/{}/evidence/", gid))
        && key.ends_with(".jpg")
        && !key.contains("..")
        && !key.contains("//")
        && key
            .bytes()
            .all(|b| matches!(b, b'a'..=b'z' | b'0'..=b'9' | b'/' | b'_' | b'.' | b'-'))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_record_archive_put_outcome_tally_format_and_monotonic() {
        // EDGE-006: the dispatch-health tally is a per-instance running count folded into
        // the log line. We assert (a) the compact `disp=N disp_fail=N skip=N` shape and
        // (b) that each outcome bumps exactly its own counter (monotonic, never decreases).
        // Counters are process-global atomics, so compare DELTAS, not absolutes.
        fn parse(tally: &str) -> (u64, u64, u64) {
            let mut d = (0u64, 0u64, 0u64);
            for part in tally.split_whitespace() {
                let (k, v) = part.split_once('=').expect("k=v");
                let v: u64 = v.parse().expect("u64");
                match k {
                    "disp" => d.0 = v,
                    "disp_fail" => d.1 = v,
                    "skip" => d.2 = v,
                    other => panic!("unexpected tally key {}", other),
                }
            }
            d
        }

        let before = parse(&record_archive_put_outcome(ArchivePutOutcome::Dispatched));
        let after_disp = parse(&record_archive_put_outcome(ArchivePutOutcome::Dispatched));
        // Dispatched bumps only `disp`.
        assert_eq!(after_disp.0, before.0 + 1);
        assert_eq!(after_disp.1, before.1);
        assert_eq!(after_disp.2, before.2);

        let after_fail = parse(&record_archive_put_outcome(
            ArchivePutOutcome::DispatchFailed,
        ));
        assert_eq!(after_fail.1, after_disp.1 + 1);
        assert_eq!(after_fail.0, after_disp.0);

        let after_skip = parse(&record_archive_put_outcome(ArchivePutOutcome::Skipped));
        assert_eq!(after_skip.2, after_fail.2 + 1);
        assert_eq!(after_skip.0, after_fail.0);
        assert_eq!(after_skip.1, after_fail.1);
    }
}
