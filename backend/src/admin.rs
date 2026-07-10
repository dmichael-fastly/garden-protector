//! Admin control-plane routes for the `/api/gardens` tree (RFC §6) — the per-garden,
//! token-gated read/control surface the Pi portal + console proxy to.
//!
//! OFF the fail-closed deterrent path: these routes serve the registry/device reads and
//! the dashboard arm/disarm/stop/resume control. Garden-scoped routes enforce the
//! per-garden token (`default` is Tokenless/open, so the single-tenant dashboard keeps
//! working) with IP lockout on repeated failures. The control write goes through the SAME
//! `key`/`write_flags` helper the heartbeat reads, so a STOP and the heartbeat can never
//! target different keys — that flag I/O DELIBERATELY stays in main.rs (sacred); this
//! module only calls it. Extracted verbatim in the Phase 5 modularization — behavior
//! unchanged. The fastly host-ABI types compile natively (wasm32 + native `cargo test`).

use crate::auth::{garden_auth_outcome, AuthOutcome};
use crate::contract_gen::{dev_key, is_valid_id};
use crate::safety::{apply_control, continue_mitigation};
use crate::{
    config_flags, flags_from, is_ip_locked_out, latest_event_from, latest_telemetry_from, log_evt,
    record_failed_attempt, reset_failed_attempts, write_flags, ControlRequest, GARDEN_AUTH_HEADER,
    STATE_STORE,
};
use fastly::http::{Method, StatusCode};
use fastly::kv_store::KVStore;
use fastly::{Error, Request, Response};
use std::net::IpAddr;

/// Parsed admin route. Owns its id strings so the caller can move `req` into the
/// handler without borrowing the request path.
#[derive(Debug, PartialEq)]
pub enum AdminRoute {
    ListGardens,                     // GET  /api/gardens
    CreateGarden,                    // POST /api/gardens              -> 405
    GetGarden(String),               // GET  /api/gardens/{gid}
    ListDevices(String),             // GET  /api/gardens/{gid}/devices
    RegisterDevice(String),          // POST /api/gardens/{gid}/devices -> 405
    DeviceEvent(String, String),     // GET  /api/gardens/{gid}/devices/{did}/event
    DeviceSnapshot(String, String),  // GET  /api/gardens/{gid}/devices/{did}/snapshot
    DeviceTelemetry(String, String), // GET  /api/gardens/{gid}/devices/{did}/telemetry
    GardenControl(String),           // POST /api/gardens/{gid}/control
}

/// PURE router for the `/api/gardens` tree. Returns `None` for anything outside the
/// tree or an unknown shape (-> 404). Requires the prefix to be followed by `/` or
/// end-of-path so `/api/gardensXYZ` does NOT masquerade as garden `XYZ`.
pub fn parse_admin_route(method: &Method, path: &str) -> Option<AdminRoute> {
    let rest = path.strip_prefix("/api/gardens")?;
    if !rest.is_empty() && !rest.starts_with('/') {
        return None;
    }
    let segs: Vec<&str> = rest.split('/').filter(|s| !s.is_empty()).collect();
    match (method, segs.as_slice()) {
        (&Method::GET, []) => Some(AdminRoute::ListGardens),
        (&Method::POST, []) => Some(AdminRoute::CreateGarden),
        (&Method::GET, [gid]) => Some(AdminRoute::GetGarden(gid.to_string())),
        (&Method::GET, [gid, "devices"]) => Some(AdminRoute::ListDevices(gid.to_string())),
        (&Method::POST, [gid, "devices"]) => Some(AdminRoute::RegisterDevice(gid.to_string())),
        (&Method::GET, [gid, "devices", did, "event"]) => {
            Some(AdminRoute::DeviceEvent(gid.to_string(), did.to_string()))
        }
        (&Method::GET, [gid, "devices", did, "snapshot"]) => {
            Some(AdminRoute::DeviceSnapshot(gid.to_string(), did.to_string()))
        }
        (&Method::GET, [gid, "devices", did, "telemetry"]) => Some(AdminRoute::DeviceTelemetry(
            gid.to_string(),
            did.to_string(),
        )),
        (&Method::POST, [gid, "control"]) => Some(AdminRoute::GardenControl(gid.to_string())),
        _ => None,
    }
}

/// 400 for a malformed tenancy id on an admin route (these routes REJECT, unlike
/// the safety paths). Returns `Some(response)` if invalid.
fn reject_invalid_id(id: &str, field: &str, trace_id: &str) -> Option<Response> {
    if is_valid_id(id) {
        None
    } else {
        log_evt(
            trace_id,
            "admin",
            "validate_id",
            "error",
            &format!("invalid {}='{}' -> 400", field, id.escape_default()),
        );
        Some(Response::from_status(StatusCode::BAD_REQUEST).with_body(format!("Invalid {}", field)))
    }
}

/// Enforces the per-garden token for a garden-scoped admin route. Returns
/// `Some(401)` to reject, `None` to proceed. `default` is Tokenless (open), so the
/// existing single-tenant dashboard keeps working unauthenticated.
fn reject_unauthorized(
    provided: Option<&str>,
    gid: &str,
    trace_id: &str,
    client_ip: IpAddr,
) -> Option<Response> {
    if is_ip_locked_out(client_ip) {
        log_evt(
            trace_id,
            "admin",
            "rate_limit",
            "warn",
            &format!("garden={} ip={} is locked out -> 429", gid, client_ip),
        );
        return Some(
            Response::from_status(StatusCode::TOO_MANY_REQUESTS)
                .with_body("Too many failed attempts. Please come back later."),
        );
    }

    if garden_auth_outcome(gid, provided) == AuthOutcome::Rejected {
        record_failed_attempt(client_ip);
        log_evt(
            trace_id,
            "admin",
            "auth",
            "warn",
            &format!("garden={} unauthorized -> 401", gid),
        );
        Some(
            Response::from_status(StatusCode::UNAUTHORIZED)
                .with_body("Unauthorized: invalid or missing X-Garden-Auth"),
        )
    } else {
        reset_failed_attempts(client_ip);
        None
    }
}

/// Reads a raw registry index doc (`index/gardens` or `index/g/<gid>/devices`) from
/// `garden_state` and serves it as JSON. An absent doc yields `empty` (so the
/// dashboard renders an empty list rather than 404). A store error -> 503.
fn admin_serve_registry(
    kv_key: &str,
    empty: serde_json::Value,
    trace_id: &str,
) -> Result<Response, Error> {
    match KVStore::open(STATE_STORE) {
        Ok(Some(store)) => {
            let body = store.lookup_str(kv_key).ok().flatten();
            match body {
                Some(s) => {
                    log_evt(
                        trace_id,
                        "admin",
                        "registry_read",
                        "ok",
                        &format!("key={} bytes={}", kv_key, s.len()),
                    );
                    Ok(Response::from_status(StatusCode::OK)
                        .with_header("Content-Type", "application/json")
                        .with_body(s))
                }
                None => {
                    log_evt(
                        trace_id,
                        "admin",
                        "registry_read",
                        "ok",
                        &format!("key={} absent -> empty", kv_key),
                    );
                    Ok(Response::from_status(StatusCode::OK).with_body_json(&empty)?)
                }
            }
        }
        _ => {
            log_evt(
                trace_id,
                "admin",
                "registry_read",
                "error",
                &format!("key={} store unavailable -> 503", kv_key),
            );
            Ok(Response::from_status(StatusCode::SERVICE_UNAVAILABLE)
                .with_body("State store unavailable"))
        }
    }
}

/// Lenient per-garden flag read for the DISPLAY/CONTROL path (not the heartbeat).
/// `default` uses the Config-Store authority; a non-default garden defaults to
/// (disarmed, not-stopped) so an `arm` on a fresh garden behaves intuitively.
fn read_garden_flags(gid: &str) -> (bool, bool) {
    let defaults = if gid == "default" {
        config_flags()
    } else {
        (false, false)
    };
    match KVStore::open(STATE_STORE) {
        Ok(Some(store)) => flags_from(&store, defaults, gid),
        _ => defaults,
    }
}

/// `GET /api/gardens/{gid}` — garden flags + derived continue. (The device list is
/// the separate `/devices` route; RFC §6.)
fn admin_get_garden(gid: &str, trace_id: &str) -> Result<Response, Error> {
    let (armed, override_stop) = read_garden_flags(gid);
    let body = serde_json::json!({
        "garden_id": gid,
        "armed": armed,
        "override_stop": override_stop,
        "continue_mitigation": continue_mitigation(armed, override_stop),
    });
    log_evt(
        trace_id,
        "admin",
        "get_garden",
        "ok",
        &format!(
            "garden={} armed={} override_stop={}",
            gid, armed, override_stop
        ),
    );
    Ok(Response::from_status(StatusCode::OK).with_body_json(&body)?)
}

/// `POST /api/gardens/{gid}/control` — per-garden arm/disarm/stop/resume. Writes the
/// garden's flags via the SAME `key`/`write_flags` helper the heartbeat reads.
fn admin_garden_control(mut req: Request, gid: &str, trace_id: &str) -> Result<Response, Error> {
    let body = req.take_body().into_bytes();
    let cmd_req: ControlRequest = match serde_json::from_slice(&body) {
        Ok(c) => c,
        Err(_) => {
            log_evt(
                trace_id,
                "admin",
                "control_parse",
                "error",
                "malformed JSON body -> 400",
            );
            return Ok(Response::from_status(StatusCode::BAD_REQUEST)
                .with_body("Expected JSON body {\"cmd\": \"arm|disarm|stop|resume\"}"));
        }
    };
    let (armed, override_stop) = read_garden_flags(gid);
    let (new_armed, new_override) = match apply_control(&cmd_req.cmd, armed, override_stop) {
        Some(s) => s,
        None => {
            log_evt(
                trace_id,
                "admin",
                "control_apply",
                "error",
                &format!("unknown cmd '{}' -> 400", cmd_req.cmd.escape_default()),
            );
            return Ok(Response::from_status(StatusCode::BAD_REQUEST)
                .with_body(format!("Unknown command: {}", cmd_req.cmd)));
        }
    };
    if let Err(e) = write_flags(new_armed, new_override, gid) {
        eprintln!(
            "[BACKEND ERROR] Failed to write garden control state: {}",
            e
        );
        log_evt(
            trace_id,
            "admin",
            "control_write",
            "error",
            &format!("garden={} {} -> 503 (not persisted)", gid, e),
        );
        return Ok(Response::from_status(StatusCode::SERVICE_UNAVAILABLE)
            .with_body("State store unavailable; command not persisted"));
    }
    log_evt(
        trace_id,
        "admin",
        "control_apply",
        "ok",
        &format!(
            "garden={} cmd={} armed={} override_stop={}",
            gid, cmd_req.cmd, new_armed, new_override
        ),
    );
    let resp_body = serde_json::json!({
        "garden_id": gid,
        "armed": new_armed,
        "override_stop": new_override,
        "continue_mitigation": continue_mitigation(new_armed, new_override),
    });
    Ok(Response::from_status(StatusCode::OK).with_body_json(&resp_body)?)
}

/// Dispatches a parsed [`AdminRoute`]. Validation order: 400 (bad id) -> 401 (auth)
/// -> handler. `ListGardens` is the (open) overview; all garden-scoped routes
/// enforce the per-garden token via [`garden_auth_outcome`].
pub fn handle_admin(req: Request, trace_id: &str, route: AdminRoute) -> Result<Response, Error> {
    let client_ip = req
        .get_client_ip_addr()
        .unwrap_or(IpAddr::V4(std::net::Ipv4Addr::new(127, 0, 0, 1)));
    // Own the auth header up front so we can move `req` into a handler later.
    let provided = req
        .get_header_str(GARDEN_AUTH_HEADER)
        .map(|s| s.to_string());
    match route {
        // Registry is control-plane-write-only (RFC §4): the edge never RMWs it.
        AdminRoute::CreateGarden | AdminRoute::RegisterDevice(_) => {
            log_evt(
                trace_id,
                "admin",
                "registry_write",
                "warn",
                "registry is control-plane-write-only -> 405",
            );
            Ok(Response::from_status(StatusCode::METHOD_NOT_ALLOWED)
                .with_header("Allow", "GET")
                .with_body("Registry is control-plane-write-only; use gp-provision (the single registry writer)."))
        }
        AdminRoute::ListGardens => admin_serve_registry(
            "index/gardens",
            serde_json::json!({"v": 1, "updated_ts": 0, "gardens": []}),
            trace_id,
        ),
        AdminRoute::GetGarden(gid) => {
            if let Some(r) = reject_invalid_id(&gid, "garden_id", trace_id) {
                return Ok(r);
            }
            if let Some(r) = reject_unauthorized(provided.as_deref(), &gid, trace_id, client_ip) {
                return Ok(r);
            }
            admin_get_garden(&gid, trace_id)
        }
        AdminRoute::ListDevices(gid) => {
            if let Some(r) = reject_invalid_id(&gid, "garden_id", trace_id) {
                return Ok(r);
            }
            if let Some(r) = reject_unauthorized(provided.as_deref(), &gid, trace_id, client_ip) {
                return Ok(r);
            }
            admin_serve_registry(
                &format!("index/g/{}/devices", gid),
                serde_json::json!({"v": 1, "garden_id": gid, "updated_ts": 0, "devices": []}),
                trace_id,
            )
        }
        AdminRoute::DeviceEvent(gid, did) => {
            if let Some(r) = reject_invalid_id(&gid, "garden_id", trace_id) {
                return Ok(r);
            }
            if let Some(r) = reject_invalid_id(&did, "device_id", trace_id) {
                return Ok(r);
            }
            if let Some(r) = reject_unauthorized(provided.as_deref(), &gid, trace_id, client_ip) {
                return Ok(r);
            }
            let event = match KVStore::open(STATE_STORE) {
                Ok(Some(store)) => latest_event_from(&store, &gid, &did),
                _ => serde_json::Value::Null,
            };
            log_evt(
                trace_id,
                "admin",
                "device_event",
                "ok",
                &format!("garden={} device={}", gid, did),
            );
            Ok(Response::from_status(StatusCode::OK).with_body_json(&event)?)
        }
        AdminRoute::DeviceSnapshot(gid, did) => {
            if let Some(r) = reject_invalid_id(&gid, "garden_id", trace_id) {
                return Ok(r);
            }
            if let Some(r) = reject_invalid_id(&did, "device_id", trace_id) {
                return Ok(r);
            }
            if let Some(r) = reject_unauthorized(provided.as_deref(), &gid, trace_id, client_ip) {
                return Ok(r);
            }
            let bytes = match KVStore::open(STATE_STORE) {
                Ok(Some(store)) => store
                    .lookup_bytes(&dev_key(&gid, &did, "latest_image"))
                    .ok()
                    .flatten(),
                _ => None,
            };
            match bytes {
                Some(b) => {
                    log_evt(
                        trace_id,
                        "admin",
                        "device_snapshot",
                        "ok",
                        &format!("garden={} device={} bytes={}", gid, did, b.len()),
                    );
                    Ok(Response::from_status(StatusCode::OK)
                        .with_header("Content-Type", "image/jpeg")
                        .with_body(b))
                }
                None => {
                    log_evt(
                        trace_id,
                        "admin",
                        "device_snapshot",
                        "ok",
                        &format!("garden={} device={} no snapshot -> 404", gid, did),
                    );
                    Ok(Response::from_status(StatusCode::NOT_FOUND)
                        .with_body("No snapshot available"))
                }
            }
        }
        AdminRoute::DeviceTelemetry(gid, did) => {
            if let Some(r) = reject_invalid_id(&gid, "garden_id", trace_id) {
                return Ok(r);
            }
            if let Some(r) = reject_invalid_id(&did, "device_id", trace_id) {
                return Ok(r);
            }
            if let Some(r) = reject_unauthorized(provided.as_deref(), &gid, trace_id, client_ip) {
                return Ok(r);
            }
            let telemetry = match KVStore::open(STATE_STORE) {
                Ok(Some(store)) => latest_telemetry_from(&store, &gid, &did),
                _ => serde_json::Value::Null,
            };
            log_evt(
                trace_id,
                "admin",
                "device_telemetry",
                "ok",
                &format!("garden={} device={}", gid, did),
            );
            Ok(Response::from_status(StatusCode::OK).with_body_json(&telemetry)?)
        }
        AdminRoute::GardenControl(gid) => {
            if let Some(r) = reject_invalid_id(&gid, "garden_id", trace_id) {
                return Ok(r);
            }
            if let Some(r) = reject_unauthorized(provided.as_deref(), &gid, trace_id, client_ip) {
                return Ok(r);
            }
            admin_garden_control(req, &gid, trace_id)
        }
    }
}
