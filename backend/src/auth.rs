//! Per-garden device-token auth + the optional human VIEWER GATE.
//!
//! Two distinct concerns, both OFF the fail-closed safety decision (rain/liveness/
//! control): (1) `garden_auth_outcome`/`decide_auth` authenticate the Pi's per-garden
//! device token (enforce-iff-token; the `default` garden is tokenless; a provisioned
//! garden whose store errors fails CLOSED -> Rejected); (2) the viewer gate is an
//! optional viewer-password wall in front of the view-only edge dashboard
//! (enforce-iff-configured; fails OPEN for availability). NEITHER is consulted by
//! `/api/status`, `/api/evidence`, `/api/telemetry`, `/api/alert`, or the heartbeat.
//!
//! Pure decision cores (`decide_auth`, `decide_viewer_gate`, `browser_read_allowed`,
//! cookie mint/verify) are split from their thin I/O shells so the whole enforcement
//! is unit-testable. Extracted verbatim in Phase 5b modularization — behavior
//! unchanged. The fastly host-ABI types compile natively, so this links under both
//! wasm32 and native `cargo test`.

use crate::contract_gen::token_secret_name;
use crate::sigv4::{hex_lower, hmac_sha256};
use crate::util::constant_time_eq;
use crate::{
    log_evt, now_secs, GARDEN_AUTH_HEADER, GARDEN_TOKENS_STORE, VIEWER_COOKIE, VIEWER_PASS_SLOT,
};
use fastly::http::StatusCode;
use fastly::{secret_store::SecretStore, Request, Response};

/// Outcome of a per-garden auth check.
/// - `Tokenless`: the garden has no token configured (always true for `default`);
///   enforcement is skipped (callers proceed as before).
/// - `Authorized`: a token is configured and the presented credential matches.
/// - `Rejected`: a token is configured but the credential is absent/wrong, OR the
///   secret store could not be read for a non-default garden (fail closed).
#[derive(Debug, PartialEq, Eq)]
pub enum AuthOutcome {
    Tokenless,
    Authorized,
    Rejected,
}

/// PURE auth decision (no I/O) — the whole testable core of enforcement. Given the
/// already-fetched `current`/`previous` token secrets for the claimed garden and the
/// presented credential, decide the outcome. `is_default` short-circuits to
/// `Tokenless` (the `default` garden is never issued a token — guard, or local dev
/// breaks). With no token configured (both `None`) a non-default garden is
/// `Tokenless` too (enforce-iff-token). A configured token with an absent/mismatched
/// credential is `Rejected`. Matching is constant-time against current THEN previous
/// (the rotation window).
pub fn decide_auth(
    is_default: bool,
    provided: Option<&str>,
    current: Option<&str>,
    previous: Option<&str>,
) -> AuthOutcome {
    if is_default {
        return AuthOutcome::Tokenless;
    }
    // enforce-iff-token: a garden with no token configured is not enforced.
    if current.is_none() && previous.is_none() {
        return AuthOutcome::Tokenless;
    }
    let provided = match provided {
        Some(p) if !p.is_empty() => p,
        _ => return AuthOutcome::Rejected,
    };
    let matches = |expected: Option<&str>| {
        expected.is_some_and(|e| constant_time_eq(provided.as_bytes(), e.as_bytes()))
    };
    if matches(current) || matches(previous) {
        AuthOutcome::Authorized
    } else {
        AuthOutcome::Rejected
    }
}

/// Thin I/O shell over [`decide_auth`]: resolves the token secrets for the claimed
/// `gid` and decides. The `default` garden never touches the Secret Store (zero
/// round-trips — keeps the default safety path byte-for-byte unchanged). For a
/// non-default garden, a Secret Store that cannot be opened fails CLOSED (treated as
/// `Rejected`), since a real garden is only ever provisioned WITH a token.
///
/// NOTE: the LOOKUP uses `try_get` (not the infallible `get`, which PANICS if the
/// store/secret is missing) so an absent token is handled gracefully as `None`. The
/// only residual panic is `Secret::plaintext()` on a pathological host READ error of
/// an already-resolved handle (0.9.5 exposes no fallible `Secret::plaintext`); that
/// aborts the request, which still FAILS SAFE (evidence -> Pi sees a non-200/error ->
/// disarm; control -> dashboard error). This is NEVER called from `/api/status` or
/// the heartbeat (LOCKED: no secret lookup there).
pub fn garden_auth_outcome(gid: &str, provided: Option<&str>) -> AuthOutcome {
    if gid == "default" {
        return AuthOutcome::Tokenless;
    }
    let store = match SecretStore::open(GARDEN_TOKENS_STORE) {
        Ok(s) => s,
        Err(_) => return AuthOutcome::Rejected, // non-default garden, store missing -> fail closed
    };
    // FAIL-CLOSED on a host READ error: `try_get` returns Ok(None) when the secret is
    // ABSENT (legitimate enforce-iff-token) vs Err on a host error. We must NOT collapse
    // both to None (`.ok()`), or two erroring reads would look like "no token configured"
    // -> Tokenless -> auth SKIPPED for a provisioned garden. Any Err -> Rejected.
    let read = |slot: &str| -> Result<Option<String>, ()> {
        match store.try_get(&token_secret_name(gid, slot)) {
            Ok(Some(s)) => Ok(Some(String::from_utf8_lossy(&s.plaintext()).into_owned())),
            Ok(None) => Ok(None),
            Err(_) => Err(()),
        }
    };
    let current = match read("token_current") {
        Ok(v) => v,
        Err(_) => return AuthOutcome::Rejected,
    };
    let previous = match read("token_previous") {
        Ok(v) => v,
        Err(_) => return AuthOutcome::Rejected,
    };
    decide_auth(false, provided, current.as_deref(), previous.as_deref())
}

// ---------------------------------------------------------------------------
// VIEWER GATE — the OPTIONAL viewer-password wall in front of the (view-only)
// edge dashboard. Distinct from the per-garden device token: that auths the Pi
// (machine); this auths a human viewer. Enforce-iff-configured: absent password
// => dashboard open (today's behavior). NEVER touches a safety path — the Pi
// endpoints (/api/evidence,/telemetry,/status,/alert) and the gateway's
// /api/state sync are not gated.
// ---------------------------------------------------------------------------

/// Outcome of the viewer gate (PURE).
#[derive(Debug, PartialEq, Eq)]
pub enum ViewerGate {
    Open,      // no viewer password configured -> serve as today
    Allowed,   // configured AND a valid viewer cookie was presented
    NeedLogin, // configured but no/invalid cookie -> show the login page
}

/// PURE gate decision: given the configured password (if any) and whether the
/// presented cookie verified, decide what to do. An empty configured password is
/// treated as "not configured" (Open), so a blank secret can't lock people out.
pub fn decide_viewer_gate(configured_pass: Option<&str>, cookie_valid: bool) -> ViewerGate {
    match configured_pass {
        None => ViewerGate::Open,
        Some(p) if p.is_empty() => ViewerGate::Open,
        Some(_) => {
            if cookie_valid {
                ViewerGate::Allowed
            } else {
                ViewerGate::NeedLogin
            }
        }
    }
}

/// Mint a signed viewer cookie value `exp.hexHMAC` where the MAC is
/// HMAC-SHA256(viewer_pass, "viewer|<exp>"). `exp` is an absolute epoch-second
/// deadline. Verifiable only by someone who knows the stored password (PURE).
pub fn mint_viewer_cookie(pass: &[u8], exp: i64) -> String {
    let mac = hmac_sha256(pass, format!("viewer|{}", exp).as_bytes());
    format!("{}.{}", exp, hex_lower(&mac))
}

/// Verify a viewer cookie against the stored password at time `now` (PURE).
/// Checks the HMAC in constant time, then the expiry. Any malformed cookie ->
/// false.
pub fn verify_viewer_cookie(cookie: &str, pass: &[u8], now: i64) -> bool {
    let (exp_str, mac_hex) = match cookie.split_once('.') {
        Some(parts) => parts,
        None => return false,
    };
    let exp: i64 = match exp_str.parse() {
        Ok(v) => v,
        Err(_) => return false,
    };
    if exp <= now {
        return false;
    }
    let expected = hex_lower(&hmac_sha256(pass, format!("viewer|{}", exp).as_bytes()));
    constant_time_eq(mac_hex.as_bytes(), expected.as_bytes())
}

/// Extract a single cookie value from a `Cookie:` header (PURE). Returns the
/// first match for `name`. No dependency on a cookie crate.
pub fn cookie_from_header(header: Option<&str>, name: &str) -> Option<String> {
    let header = header?;
    for part in header.split(';') {
        let part = part.trim();
        if let Some((k, v)) = part.split_once('=') {
            if k.trim() == name {
                return Some(v.trim().to_string());
            }
        }
    }
    None
}

/// I/O shell: read a garden's configured viewer password from the Secret Store.
/// Returns `None` when absent OR on any store error — i.e. the gate FAILS OPEN
/// (the view-only dashboard stays reachable), matching its pre-gate behavior.
/// This is a dashboard convenience, never a safety path, so failing open here is
/// the safe-for-availability choice (cf. the token path, which fails closed).
pub fn read_viewer_pass(gid: &str) -> Option<String> {
    let store = SecretStore::open(GARDEN_TOKENS_STORE).ok()?;
    match store.try_get(&token_secret_name(gid, VIEWER_PASS_SLOT)) {
        Ok(Some(s)) => Some(String::from_utf8_lossy(&s.plaintext()).into_owned()),
        _ => None,
    }
}

/// Resolve the viewer gate for a request: read the configured password, verify
/// any `gp_viewer` cookie against it, and return the gate decision.
pub fn viewer_gate_for(req: &Request, gid: &str) -> ViewerGate {
    let pass = read_viewer_pass(gid);
    let cookie_valid = match pass.as_deref() {
        Some(p) if !p.is_empty() => cookie_from_header(req.get_header_str("Cookie"), VIEWER_COOKIE)
            .map(|c| verify_viewer_cookie(&c, p.as_bytes(), now_secs()))
            .unwrap_or(false),
        _ => false,
    };
    decide_viewer_gate(pass.as_deref(), cookie_valid)
}

/// PURE: may a browser READ be served? A valid per-garden token (the trusted Pi
/// portal / console proxy, which forwards `X-Garden-Auth` for an already-authenticated
/// admin) bypasses the public viewer gate; otherwise a configured viewer password
/// without a valid cookie (`NeedLogin`) blocks. No password configured (`Open`) ->
/// allowed, so the un-gated default/legacy behavior is unchanged.
pub fn browser_read_allowed(auth: AuthOutcome, gate: ViewerGate) -> bool {
    auth == AuthOutcome::Authorized || gate != ViewerGate::NeedLogin
}

/// Shared 401-or-pass for the browser-facing READ endpoints (`/api/state`,
/// `/api/snapshot`, `/api/cameras`, `/api/gadget`). The trusted LAN portal / console
/// proxy sends a valid `X-Garden-Auth` token and bypasses the public viewer gate
/// (so the LAN admin dashboard keeps working even when a viewer password is set); a
/// public browser falls through to the viewer gate. Returns the 401 the caller
/// short-circuits on, or `None` to serve.
pub fn viewer_block(req: &Request, gid: &str, trace_id: &str, component: &str) -> Option<Response> {
    let auth = match req.get_header_str(GARDEN_AUTH_HEADER) {
        Some(tok) => garden_auth_outcome(gid, Some(tok)),
        None => AuthOutcome::Tokenless,
    };
    // Fast path: a valid token bypasses without even reading the viewer pass.
    let gate = if auth == AuthOutcome::Authorized {
        ViewerGate::Open
    } else {
        viewer_gate_for(req, gid)
    };
    if browser_read_allowed(auth, gate) {
        None
    } else {
        log_evt(
            trace_id,
            component,
            "gate",
            "ok",
            &format!("garden={} -> 401 (viewer login)", gid),
        );
        Some(Response::from_status(StatusCode::UNAUTHORIZED).with_body("Viewer login required"))
    }
}
