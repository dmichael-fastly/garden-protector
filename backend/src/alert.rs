//! Node-down alerting (edge -> Twilio SMS) — the `POST /api/alert` route + its helpers.
//!
//! A dead board fails *closed* (no spray), so loss of protection is SILENT — the gateway
//! detects the ONLINE->DOWN transition and POSTs `/api/alert`, and the edge dispatches the
//! SMS so the Twilio credentials never leave the edge. STRICTLY notify-only + best-effort:
//! absent Twilio config -> no-op (200 `not_configured`); nothing here ever touches a
//! fail-closed branch. The request builders are pure + unit-tested; the live send needs a
//! `twilio_api` backend + creds, mirroring the evidence-archive pattern. Extracted verbatim
//! in the Phase 5 modularization — behavior unchanged. The fastly host-ABI types compile
//! natively, so this links under both wasm32 and native `cargo test`.

use crate::auth::{garden_auth_outcome, AuthOutcome};
use crate::util::{base64_encode, form_urlencode};
use crate::{
    body_exceeds_limit, log_evt, resolve_safety_id, GARDEN_AUTH_HEADER, GARDEN_TOKENS_STORE,
    MAX_BODY_BYTES,
};
use fastly::http::StatusCode;
use fastly::{secret_store::SecretStore, ConfigStore, Error, Request, Response};
use serde::Deserialize;

/// Config Store with the non-secret Twilio fields (`account_sid`, `from`, `to`).
const TWILIO_CONFIG_STORE: &str = "twilio_config";
/// The Twilio auth token is a credential -> Secret Store (reuse `garden_tokens`),
/// under this slash-free name (Secret Store names forbid `/`).
const TWILIO_AUTH_TOKEN_NAME: &str = "twilio.auth_token";
/// Backend (api.twilio.com) the provisioner registers; absent -> send errors -> no-op.
const TWILIO_BACKEND: &str = "twilio_api";

/// Alert body for `POST /api/alert` — the gateway posts this on a node-down (or
/// other liveness) transition so the edge can dispatch an SMS while keeping the
/// SMS credentials at the edge. Notify-only; never on a fail-closed safety path.
#[derive(Deserialize)]
struct AlertRequest {
    event: String,
    #[serde(default)]
    node_id: String,
    #[serde(default)]
    detail: Option<String>,
}

/// Twilio HTTP Basic auth header value: `Basic base64(account_sid:auth_token)`.
pub fn twilio_basic_auth(account_sid: &str, auth_token: &str) -> String {
    format!(
        "Basic {}",
        base64_encode(format!("{}:{}", account_sid, auth_token).as_bytes())
    )
}

/// The `From/To/Body` form body for Twilio's Messages API.
pub fn twilio_form_body(from: &str, to: &str, body: &str) -> String {
    format!(
        "From={}&To={}&Body={}",
        form_urlencode(from),
        form_urlencode(to),
        form_urlencode(body)
    )
}

/// The SMS text for a node-down alert (pure; trace-grep-friendly).
pub fn node_down_message(gid: &str, node_id: &str, detail: Option<&str>) -> String {
    format!(
        "Fastly Garden Protector: node '{}' in garden '{}' is DOWN — deterrent protection is OFFLINE (fail-closed: no spray).{}",
        node_id, gid, detail.map(|d| format!(" {}", d)).unwrap_or_default()
    )
}

/// Best-effort SMS dispatch. Returns `(dispatched, reason)`. Absent/partial config or
/// a missing token -> `(false, "not_configured")`; a send error -> `(false, "send_error")`.
/// NEVER panics, NEVER blocks a safety path (this is its own non-safety route).
fn dispatch_twilio(message: &str, trace_id: &str) -> (bool, &'static str) {
    let cfg = match ConfigStore::try_open(TWILIO_CONFIG_STORE) {
        Ok(c) => c,
        Err(_) => return (false, "not_configured"),
    };
    let get = |k: &str| cfg.try_get(k).ok().flatten();
    let (sid, from, to) = match (get("account_sid"), get("from"), get("to")) {
        (Some(s), Some(f), Some(t)) => (s, f, t),
        _ => return (false, "not_configured"),
    };
    let token = match SecretStore::open(GARDEN_TOKENS_STORE)
        .ok()
        .and_then(|s| s.try_get(TWILIO_AUTH_TOKEN_NAME).ok().flatten())
        .map(|s| String::from_utf8_lossy(&s.plaintext()).into_owned())
    {
        Some(t) => t,
        None => return (false, "not_configured"),
    };
    let url = format!(
        "https://api.twilio.com/2010-04-01/Accounts/{}/Messages.json",
        sid
    );
    let req = Request::post(url)
        .with_header("Authorization", twilio_basic_auth(&sid, &token).as_str())
        .with_header("Content-Type", "application/x-www-form-urlencoded")
        .with_body(twilio_form_body(&from, &to, message));
    match req.send(TWILIO_BACKEND) {
        Ok(resp) => {
            let code = resp.get_status().as_u16();
            log_evt(
                trace_id,
                "alert",
                "twilio",
                "ok",
                &format!("status={}", code),
            );
            (code < 400, "sent")
        }
        Err(e) => {
            log_evt(
                trace_id,
                "alert",
                "twilio",
                "warn",
                &format!("send failed: {} (best-effort)", e),
            );
            (false, "send_error")
        }
    }
}

/// `POST /api/alert` — the gateway's liveness-transition notification. Enforce-iff-token
/// (non-default), then best-effort SMS dispatch. Always returns 200 with whether the SMS
/// went out, so a missing Twilio config is visible but never an error.
pub fn handle_alert(mut req: Request, trace_id: &str, garden_id: &str) -> Result<Response, Error> {
    let gid = resolve_safety_id(garden_id, trace_id, "alert", "garden_id");
    let auth = req
        .get_header_str(GARDEN_AUTH_HEADER)
        .map(|s| s.to_string());
    if garden_auth_outcome(gid, auth.as_deref()) == AuthOutcome::Rejected {
        log_evt(
            trace_id,
            "alert",
            "auth",
            "warn",
            &format!("garden={} unauthorized -> 401", gid),
        );
        return Ok(Response::from_status(StatusCode::UNAUTHORIZED)
            .with_body("Unauthorized: invalid or missing X-Garden-Auth"));
    }
    if body_exceeds_limit(req.get_header_str("content-length"), MAX_BODY_BYTES) {
        log_evt(
            trace_id,
            "alert",
            "size",
            "warn",
            &format!("content-length over {} bytes -> 413", MAX_BODY_BYTES),
        );
        return Ok(
            Response::from_status(StatusCode::PAYLOAD_TOO_LARGE).with_body("Payload too large")
        );
    }
    let body = req.take_body().into_bytes();
    if body.len() > MAX_BODY_BYTES {
        log_evt(
            trace_id,
            "alert",
            "size",
            "warn",
            &format!(
                "buffered {} bytes over {} -> 413",
                body.len(),
                MAX_BODY_BYTES
            ),
        );
        return Ok(
            Response::from_status(StatusCode::PAYLOAD_TOO_LARGE).with_body("Payload too large")
        );
    }
    let alert: AlertRequest = match serde_json::from_slice(&body) {
        Ok(a) => a,
        Err(_) => {
            log_evt(
                trace_id,
                "alert",
                "parse",
                "error",
                "malformed JSON body -> 400",
            );
            return Ok(Response::from_status(StatusCode::BAD_REQUEST)
                .with_body("Expected a JSON alert object {event, node_id, detail?}"));
        }
    };
    log_evt(
        trace_id,
        "alert",
        "received",
        "warn",
        &format!(
            "garden={} event={} node={}",
            gid,
            alert.event.escape_default(),
            alert.node_id.escape_default()
        ),
    );
    let message = node_down_message(gid, &alert.node_id, alert.detail.as_deref());
    let (dispatched, reason) = dispatch_twilio(&message, trace_id);
    Ok(Response::from_status(StatusCode::OK).with_body_json(
        &serde_json::json!({"dispatched": dispatched, "reason": reason, "event": alert.event}),
    )?)
}
