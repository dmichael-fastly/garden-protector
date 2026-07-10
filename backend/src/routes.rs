//! HTTP route handlers for the browser/dashboard READ surface + the viewer login —
//! everything OFF the fail-closed safety path: the view-only (viewer-gated) dashboard
//! HTML, static assets + favicon, the viewer login, the read APIs (`/api/state`,
//! `/api/snapshot`, `/api/cameras`, `/api/gadget`), and the archive browse/prune handlers
//! (`/api/archive*`). The Pi-facing SAFETY handlers (`/api/evidence`, `/api/telemetry`,
//! `/api/status`), the `/api/control` mutation, and the `main()` dispatcher DELIBERATELY
//! stay in main.rs. Extracted verbatim in the Phase 5 modularization — behavior unchanged.
//! Links under both wasm32 and native `cargo test` (host-ABI types compile natively).

use crate::alarms::*;
use crate::archive::*;
use crate::auth::*;
use crate::contract_gen::{dev_key, MITIGATE_THRESHOLD};
use crate::util::*;
use crate::{
    alarm_log_key, alarms_header_html, browser_garden_id, build_state, dashboard_header_html,
    event_header_html, help_header_html, history_header_html, is_ip_locked_out, latest_event_from,
    latest_telemetry_from, load_alarm_log, log_evt, now_ms, now_secs, read_devices,
    record_failed_attempt, reset_failed_attempts, resolve_safety_id, timelapse_header_html,
    viewer_login_html, ALARMS_HTML, DASHBOARD_HTML, EVENT_HTML, FAVICON_ICO, GARDEN_AUTH_HEADER,
    HELP_HTML, STATE_STORE, TIMELAPSE_HTML, VIEWER_COOKIE, VIEWER_SESSION_TTL_SECS,
};
use fastly::http::StatusCode;
use fastly::kv_store::KVStore;
use fastly::{Error, Request, Response};
use std::net::IpAddr;

/// Cache policy for the server-rendered HTML pages (dashboard / timelapse / help).
/// `no-cache` = a shared/intermediary cache (the CDN) MUST revalidate with the origin
/// before serving, so a deploy is never masked by stale page HTML. Unlike the static
/// assets (gp.css/gp.js) these pages carry no ETag — the garden name + viewer-gate
/// state are spliced server-side per request — so we deliberately do NOT set `s-maxage`
/// (that would let the CDN hold stale HTML with no revalidation path). Mirrors the
/// "never serve stale after deploy" intent of the static-asset policy; the post-deploy
/// `purge_all` (scripts/deploy_edge.py) is still the belt to this suspenders.
const HTML_CACHE_CONTROL: &str = "no-cache";

/// Which nav tab the dashboard page is serving — the page HTML is identical for `/`,
/// `/admin`, and `/history`; only the ACTIVE nav link differs. Branching here lets the
/// `/history` route mark its own nav link active server-side instead of relying on the
/// dashboard JS to re-point it after load.
#[derive(Clone, Copy)]
pub enum DashboardNav {
    Dashboard,
    History,
}

/// Serves the dashboard HTML, VIEW-ONLY (controls hidden), behind the optional
/// viewer-password gate. With no viewer password configured it serves as before;
/// with one configured it serves the login page until a valid `gp_viewer` cookie
/// is presented. `nav` selects which nav link is marked active server-side (`/` and
/// `/admin` -> Dashboard; `/history` -> History).
pub fn serve_dashboard(req: Request, trace_id: &str, nav: DashboardNav) -> Result<Response, Error> {
    let gid_owned = browser_garden_id();
    let gid = resolve_safety_id(&gid_owned, trace_id, "dashboard", "garden_id");
    match viewer_gate_for(&req, gid) {
        ViewerGate::NeedLogin => {
            log_evt(
                trace_id,
                "dashboard",
                "gate",
                "ok",
                &format!("garden={} -> viewer login", gid),
            );
            Ok(Response::from_status(StatusCode::OK)
                .with_header("Content-Type", "text/html; charset=utf-8")
                .with_header("Cache-Control", HTML_CACHE_CONTROL)
                .with_body(viewer_login_html("")))
        }
        gate => {
            log_evt(
                trace_id,
                "dashboard",
                "serve",
                "ok",
                &format!("served view-only HTML ({:?})", gate),
            );
            Ok(Response::from_status(StatusCode::OK)
                .with_header("Content-Type", "text/html; charset=utf-8")
                .with_header("Cache-Control", HTML_CACHE_CONTROL)
                .with_body(dashboard_view_only_html(nav)))
        }
    }
}

/// `GET /login` — the explicit sign-in page. The dashboard normally renders the
/// viewer login INLINE at `/` (and `/history` etc.) when the gate trips, but the
/// shared `gp.js` `api()` wrapper sends a browser to `/login` whenever any read API
/// returns 401 (e.g. an expired viewer cookie mid-session). The Pi portal serves a
/// real `/login`; the edge previously had no such route, so that redirect landed on
/// "Endpoint not found". This makes `/login` a first-class route on the edge too:
/// `NeedLogin` -> serve the login page; already authed / no gate -> 303 back to `/`
/// (mirrors `handle_viewer_login`'s post-login redirect) so nobody gets stranded on a
/// pointless login form.
pub fn serve_login(req: Request, trace_id: &str) -> Result<Response, Error> {
    let gid_owned = browser_garden_id();
    let gid = resolve_safety_id(&gid_owned, trace_id, "login", "garden_id");
    match viewer_gate_for(&req, gid) {
        ViewerGate::NeedLogin => {
            log_evt(
                trace_id,
                "login",
                "gate",
                "ok",
                &format!("garden={} -> viewer login", gid),
            );
            Ok(Response::from_status(StatusCode::OK)
                .with_header("Content-Type", "text/html; charset=utf-8")
                .with_header("Cache-Control", HTML_CACHE_CONTROL)
                .with_body(viewer_login_html("")))
        }
        gate => {
            // Already authenticated (Allowed) or no gate configured (Open): there is
            // nothing to log in to, so bounce to the dashboard rather than show a form.
            log_evt(
                trace_id,
                "login",
                "gate",
                "ok",
                &format!("garden={} {:?} -> redirect /", gid, gate),
            );
            Ok(Response::from_status(StatusCode::SEE_OTHER).with_header("Location", "/"))
        }
    }
}

/// Serves the Fastly favicon (repo-root `favicon.ico`, baked into the Wasm binary).
/// Bypasses the viewer gate so it also loads on the login page.
pub fn serve_favicon(trace_id: &str) -> Result<Response, Error> {
    log_evt(trace_id, "dashboard", "favicon", "ok", "served favicon.ico");
    Ok(Response::from_status(StatusCode::OK)
        .with_header("Content-Type", "image/x-icon")
        .with_header("Cache-Control", "public, max-age=86400")
        .with_body(FAVICON_ICO.to_vec()))
}

/// Serves a baked-in shared UI asset (gp.css / gp.js). Ungated like the favicon
/// (stylesheets/scripts aren't sensitive and the login page needs them).
///
/// Cache strategy: a content-hash ETag + `no-cache` so the browser ALWAYS revalidates
/// (cheap 304 when unchanged) but never serves a stale copy after a deploy — the old
/// `max-age=3600` was the recurring "hard-refresh after every deploy" pain. `s-maxage`
/// keeps the CDN holding the bytes; since the ETag is derived from the same baked bytes
/// the CDN and origin agree, so the CDN can answer revalidations itself.
pub fn serve_static(
    trace_id: &str,
    name: &str,
    content_type: &str,
    body: &'static str,
    if_none_match: Option<&str>,
) -> Result<Response, Error> {
    let etag = format!("\"{}\"", &crate::sigv4::sha256_hex(body.as_bytes())[..16]);
    if if_none_match == Some(etag.as_str()) {
        return Ok(Response::from_status(StatusCode::NOT_MODIFIED)
            .with_header("ETag", etag)
            .with_header("Cache-Control", "no-cache, s-maxage=3600"));
    }
    log_evt(
        trace_id,
        "dashboard",
        "static",
        "ok",
        &format!("served {}", name),
    );
    Ok(Response::from_status(StatusCode::OK)
        .with_header("Content-Type", content_type)
        .with_header("ETag", etag)
        .with_header("Cache-Control", "no-cache, s-maxage=3600")
        .with_body(body))
}

/// The dashboard HTML with the shared header spliced in (garden name baked server-
/// side, no async pop-in) and `window.GP_VIEW_ONLY=true` injected so it hides its
/// admin controls. One source file (`dashboard.html`) serves both surfaces: the
/// Pi portal injects `false` (controls) + its own header, the edge injects `true`
/// (view-only) + the header here. `nav` selects which nav link is active so `/history`
/// renders the History tab active server-side.
pub fn dashboard_view_only_html(nav: DashboardNav) -> String {
    let header = match nav {
        DashboardNav::Dashboard => dashboard_header_html(),
        DashboardNav::History => history_header_html(),
    };
    splice_dashboard(&header)
}

/// PURE: splice a rendered header into the dashboard at the `<!--PORTAL_HEADER-->`
/// sentinel and flag it view-only. No I/O -> unit-testable.
pub fn splice_dashboard(header: &str) -> String {
    DASHBOARD_HTML
        .replacen("<!--PORTAL_HEADER-->", header, 1)
        .replacen(
            "</head>",
            "<script>window.GP_VIEW_ONLY=true;</script>\n</head>",
            1,
        )
        .replace("__ASSET_VERSION__", crate::contract_gen::ASSET_VERSION)
}

/// Serves the Timelapse player page, VIEW-ONLY (export panel hidden), behind the same
/// viewer-password gate as the dashboard. The player itself is client-side and works
/// identically here (it polls the same `/api/archive*` reads) and on the Pi admin portal;
/// only the Pi adds the GIF/MP4 export controls.
pub fn serve_timelapse(req: Request, trace_id: &str) -> Result<Response, Error> {
    let gid_owned = browser_garden_id();
    let gid = resolve_safety_id(&gid_owned, trace_id, "timelapse", "garden_id");
    match viewer_gate_for(&req, gid) {
        ViewerGate::NeedLogin => {
            log_evt(
                trace_id,
                "timelapse",
                "gate",
                "ok",
                &format!("garden={} -> viewer login", gid),
            );
            Ok(Response::from_status(StatusCode::OK)
                .with_header("Content-Type", "text/html; charset=utf-8")
                .with_header("Cache-Control", HTML_CACHE_CONTROL)
                .with_body(viewer_login_html("")))
        }
        gate => {
            log_evt(
                trace_id,
                "timelapse",
                "serve",
                "ok",
                &format!("served timelapse HTML ({:?})", gate),
            );
            Ok(Response::from_status(StatusCode::OK)
                .with_header("Content-Type", "text/html; charset=utf-8")
                .with_header("Cache-Control", HTML_CACHE_CONTROL)
                .with_body(timelapse_view_only_html()))
        }
    }
}

/// The Timelapse HTML with the shared header (its nav link active) spliced in and
/// `window.GP_VIEW_ONLY=true` injected (hides the Pi-only export panel on the edge).
pub fn timelapse_view_only_html() -> String {
    splice_timelapse(&timelapse_header_html())
}

/// PURE: splice a rendered header into the timelapse page + flag it view-only.
pub fn splice_timelapse(header: &str) -> String {
    TIMELAPSE_HTML
        .replacen("<!--PORTAL_HEADER-->", header, 1)
        .replacen(
            "</head>",
            "<script>window.GP_VIEW_ONLY=true;</script>\n</head>",
            1,
        )
        .replace("__ASSET_VERSION__", crate::contract_gen::ASSET_VERSION)
}

/// Serves the single-event detail page (`/event?key=...`), VIEW-ONLY, behind the same viewer
/// gate as History/Timelapse — it renders the same archive frame a viewer can already browse.
/// It is reached by link (not in the nav), so the header marks History active for orientation.
pub fn serve_event(req: Request, trace_id: &str) -> Result<Response, Error> {
    let gid_owned = browser_garden_id();
    let gid = resolve_safety_id(&gid_owned, trace_id, "event", "garden_id");
    match viewer_gate_for(&req, gid) {
        ViewerGate::NeedLogin => {
            log_evt(
                trace_id,
                "event",
                "gate",
                "ok",
                &format!("garden={} -> viewer login", gid),
            );
            Ok(Response::from_status(StatusCode::OK)
                .with_header("Content-Type", "text/html; charset=utf-8")
                .with_header("Cache-Control", HTML_CACHE_CONTROL)
                .with_body(viewer_login_html("")))
        }
        gate => {
            log_evt(
                trace_id,
                "event",
                "serve",
                "ok",
                &format!("served event HTML ({:?})", gate),
            );
            Ok(Response::from_status(StatusCode::OK)
                .with_header("Content-Type", "text/html; charset=utf-8")
                .with_header("Cache-Control", HTML_CACHE_CONTROL)
                .with_body(event_view_only_html()))
        }
    }
}

/// The event-detail HTML with the shared header (History active) spliced in + view-only flag.
pub fn event_view_only_html() -> String {
    splice_event(&event_header_html())
}

/// PURE: splice a rendered header into the event page + flag it view-only.
pub fn splice_event(header: &str) -> String {
    EVENT_HTML
        .replacen("<!--PORTAL_HEADER-->", header, 1)
        .replacen(
            "</head>",
            "<script>window.GP_VIEW_ONLY=true;</script>\n</head>",
            1,
        )
        .replace("__ASSET_VERSION__", crate::contract_gen::ASSET_VERSION)
}

/// Serves the Alarms page (`/alarms`), VIEW-ONLY, behind the same viewer gate as the rest of
/// the public edge. It lists real/false/untagged alarms + per-species tuning recommendations;
/// tagging a NEW alarm is open to viewers, while editing/deleting prior ones + the cleanup
/// controls are gated server-side (a valid garden token), surfaced only on the admin (Pi) tier.
pub fn serve_alarms(req: Request, trace_id: &str) -> Result<Response, Error> {
    let gid_owned = browser_garden_id();
    let gid = resolve_safety_id(&gid_owned, trace_id, "alarms", "garden_id");
    match viewer_gate_for(&req, gid) {
        ViewerGate::NeedLogin => {
            log_evt(
                trace_id,
                "alarms",
                "gate",
                "ok",
                &format!("garden={} -> viewer login", gid),
            );
            Ok(Response::from_status(StatusCode::OK)
                .with_header("Content-Type", "text/html; charset=utf-8")
                .with_header("Cache-Control", HTML_CACHE_CONTROL)
                .with_body(viewer_login_html("")))
        }
        gate => {
            log_evt(
                trace_id,
                "alarms",
                "serve",
                "ok",
                &format!("served alarms HTML ({:?})", gate),
            );
            Ok(Response::from_status(StatusCode::OK)
                .with_header("Content-Type", "text/html; charset=utf-8")
                .with_header("Cache-Control", HTML_CACHE_CONTROL)
                .with_body(alarms_view_only_html()))
        }
    }
}

/// The Alarms HTML with the shared header (its nav link active) spliced in + view-only flag.
pub fn alarms_view_only_html() -> String {
    splice_alarms(&alarms_header_html())
}

/// PURE: splice a rendered header into the alarms page + flag it view-only.
pub fn splice_alarms(header: &str) -> String {
    ALARMS_HTML
        .replacen("<!--PORTAL_HEADER-->", header, 1)
        .replacen(
            "</head>",
            "<script>window.GP_VIEW_ONLY=true;</script>\n</head>",
            1,
        )
        .replace("__ASSET_VERSION__", crate::contract_gen::ASSET_VERSION)
}

/// Serves the Help page, VIEW-ONLY, behind the same viewer-password gate as the
/// dashboard. The page is static help content (no admin controls); the viewer gate
/// keeps it consistent with the rest of the public edge surface for gated gardens.
pub fn serve_help(req: Request, trace_id: &str) -> Result<Response, Error> {
    let gid_owned = browser_garden_id();
    let gid = resolve_safety_id(&gid_owned, trace_id, "help", "garden_id");
    match viewer_gate_for(&req, gid) {
        ViewerGate::NeedLogin => {
            log_evt(
                trace_id,
                "help",
                "gate",
                "ok",
                &format!("garden={} -> viewer login", gid),
            );
            Ok(Response::from_status(StatusCode::OK)
                .with_header("Content-Type", "text/html; charset=utf-8")
                .with_header("Cache-Control", HTML_CACHE_CONTROL)
                .with_body(viewer_login_html("")))
        }
        gate => {
            log_evt(
                trace_id,
                "help",
                "serve",
                "ok",
                &format!("served help HTML ({:?})", gate),
            );
            Ok(Response::from_status(StatusCode::OK)
                .with_header("Content-Type", "text/html; charset=utf-8")
                .with_header("Cache-Control", HTML_CACHE_CONTROL)
                .with_body(help_view_only_html()))
        }
    }
}

/// The Help HTML with the shared header (its nav link active) spliced in and
/// `window.GP_VIEW_ONLY=true` injected.
pub fn help_view_only_html() -> String {
    splice_help(&help_header_html())
}

/// PURE: splice a rendered header into the help page + flag it view-only.
pub fn splice_help(header: &str) -> String {
    HELP_HTML
        .replacen("<!--PORTAL_HEADER-->", header, 1)
        .replacen(
            "</head>",
            "<script>window.GP_VIEW_ONLY=true;</script>\n</head>",
            1,
        )
        .replace("__ASSET_VERSION__", crate::contract_gen::ASSET_VERSION)
}

/// `POST /api/viewer-login` — body `passcode=...` (form) or `{"passcode":"..."}`
/// (JSON). On a match against the configured viewer password, sets a signed
/// `gp_viewer` cookie and redirects to `/`. No password configured -> 404 (no
/// gate). Wrong password -> 401 with the login page. Best-effort, off any safety
/// path. Brute-force throttled by `is_ip_locked_out` (per-instance map + best-effort
/// cross-instance `garden_state` KV counter, EDGE-003) -> 429 when an IP is over budget.
pub fn handle_viewer_login(mut req: Request, trace_id: &str) -> Result<Response, Error> {
    let gid_owned = browser_garden_id();
    let gid = resolve_safety_id(&gid_owned, trace_id, "viewer_login", "garden_id");
    let client_ip = req
        .get_client_ip_addr()
        .unwrap_or(IpAddr::V4(std::net::Ipv4Addr::new(127, 0, 0, 1)));

    if is_ip_locked_out(client_ip) {
        log_evt(
            trace_id,
            "viewer_login",
            "rate_limit",
            "warn",
            &format!("garden={} ip={} is locked out -> 429", gid, client_ip),
        );
        return Ok(Response::from_status(StatusCode::TOO_MANY_REQUESTS)
            .with_header("Content-Type", "text/html; charset=utf-8")
            .with_body(viewer_login_html(
                "Too many failed attempts. Please come back later.",
            )));
    }

    let pass = match read_viewer_pass(gid) {
        Some(p) if !p.is_empty() => p,
        _ => {
            log_evt(
                trace_id,
                "viewer_login",
                "gate",
                "ok",
                "no viewer password configured -> 404",
            );
            return Ok(
                Response::from_status(StatusCode::NOT_FOUND).with_body("Viewer login not enabled")
            );
        }
    };

    let body = req.take_body().into_bytes();
    let ctype = req.get_header_str("Content-Type").unwrap_or("");
    let provided = if ctype.starts_with("application/json") {
        serde_json::from_slice::<serde_json::Value>(&body)
            .ok()
            .and_then(|v| v.get("passcode").and_then(|p| p.as_str()).map(String::from))
            .unwrap_or_default()
    } else {
        form_field(&body, "passcode")
    };

    if !provided.is_empty() && constant_time_eq(provided.as_bytes(), pass.as_bytes()) {
        reset_failed_attempts(client_ip);
        let exp = now_secs() + VIEWER_SESSION_TTL_SECS;
        let cookie = format!(
            "{}={}; HttpOnly; SameSite=Lax; Path=/; Max-Age={}",
            VIEWER_COOKIE,
            mint_viewer_cookie(pass.as_bytes(), exp),
            VIEWER_SESSION_TTL_SECS
        );
        log_evt(
            trace_id,
            "viewer_login",
            "auth",
            "ok",
            &format!("garden={} viewer authenticated", gid),
        );
        Ok(Response::from_status(StatusCode::SEE_OTHER)
            .with_header("Location", "/")
            .with_header("Set-Cookie", cookie))
    } else {
        record_failed_attempt(client_ip);
        log_evt(
            trace_id,
            "viewer_login",
            "auth",
            "warn",
            &format!("garden={} wrong viewer password -> 401", gid),
        );
        Ok(Response::from_status(StatusCode::UNAUTHORIZED)
            .with_header("Content-Type", "text/html; charset=utf-8")
            .with_body(viewer_login_html("Incorrect password.")))
    }
}
/// `GET /api/state` — full dashboard state (armed/override/continue + the garden's
/// freshest event + node health) for the REQUEST's garden (resolved via the
/// header-less fallback). Viewer-gated: a configured viewer password without a
/// valid cookie -> 401, same as the dashboard HTML + snapshot.
pub fn handle_state(req: Request, trace_id: &str) -> Result<Response, Error> {
    let gid_owned = browser_garden_id();
    let gid = resolve_safety_id(&gid_owned, trace_id, "state", "garden_id");
    if let Some(r) = viewer_block(&req, gid, trace_id, "state") {
        return Ok(r);
    }
    let state = build_state(gid);
    log_evt(
        trace_id,
        "state",
        "read",
        "ok",
        &format!(
            "garden={} armed={} override_stop={} continue={}",
            gid, state.armed, state.override_stop, state.continue_mitigation
        ),
    );
    Ok(Response::from_status(StatusCode::OK).with_body_json(&state)?)
}

/// `GET /api/snapshot` — the most recent evidence JPEG, or 404 if none yet.
/// Any store open/lookup error degrades to 404 (same as "absent"), consistent with
/// the other graceful-degrade readers — this is a dashboard image, never the Pi.
/// Behind the viewer gate: a configured viewer password without a valid cookie ->
/// 401 (the camera image is the most sensitive thing the dashboard shows).
pub fn handle_snapshot(req: Request, trace_id: &str) -> Result<Response, Error> {
    let gid_owned = browser_garden_id();
    let gid = resolve_safety_id(&gid_owned, trace_id, "snapshot", "garden_id");
    if let Some(r) = viewer_block(&req, gid, trace_id, "snapshot") {
        return Ok(r);
    }
    // `?device=<did>` selects a camera (the live gallery passes one per tile). Absent
    // -> "default": for the default garden `dev_key` ignores it (legacy flat key); for
    // a named garden the per-device key is what the gallery always asks for.
    let did_raw = req
        .get_query_parameter("device")
        .unwrap_or("default")
        .to_string();
    let did = resolve_safety_id(&did_raw, trace_id, "snapshot", "device_id");
    let bytes = match KVStore::open(STATE_STORE) {
        Ok(Some(store)) => store
            .lookup_bytes(&dev_key(gid, did, "latest_image"))
            .ok()
            .flatten(),
        _ => None,
    };

    match bytes {
        Some(b) => {
            log_evt(
                trace_id,
                "snapshot",
                "read",
                "ok",
                &format!("garden={} device={} image_bytes={}", gid, did, b.len()),
            );
            Ok(Response::from_status(StatusCode::OK)
                .with_header("Content-Type", "image/jpeg")
                .with_body(b))
        }
        None => {
            log_evt(
                trace_id,
                "snapshot",
                "read",
                "ok",
                &format!("garden={} device={} no snapshot -> 404", gid, did),
            );
            Ok(Response::from_status(StatusCode::NOT_FOUND).with_body("No snapshot available"))
        }
    }
}

/// `GET /api/cameras` — the request garden's camera devices `{cameras:[{device_id,
/// name,type}]}` for the live gallery. Viewer-gated; reads the control-plane device
/// registry (edge read-only). A garden with no registered cameras -> empty list (the
/// dashboard then keeps its single-snapshot card).
pub fn handle_cameras(req: Request, trace_id: &str) -> Result<Response, Error> {
    let gid_owned = browser_garden_id();
    let gid = resolve_safety_id(&gid_owned, trace_id, "cameras", "garden_id");
    if let Some(r) = viewer_block(&req, gid, trace_id, "cameras") {
        return Ok(r);
    }
    let cameras: Vec<serde_json::Value> = match KVStore::open(STATE_STORE) {
        Ok(Some(store)) => read_devices(&store, gid)
            .into_iter()
            .filter(|d| d.dev_type.starts_with("camera_"))
            .map(|d| serde_json::json!({"device_id": d.device_id, "name": d.name, "type": d.dev_type}))
            .collect(),
        _ => Vec::new(),
    };
    log_evt(
        trace_id,
        "cameras",
        "list",
        "ok",
        &format!("garden={} cameras={}", gid, cameras.len()),
    );
    Ok(Response::from_status(StatusCode::OK)
        .with_body_json(&serde_json::json!({"cameras": cameras}))?)
}

/// `GET /api/gadget?device=<did>` — one device's `{event, telemetry}`, for the
/// gallery's per-camera status pill + last-sighting line. Viewer-gated; resolves to
/// the request's garden. Missing/unknown device -> nulls (the tile shows "no signal").
pub fn handle_gadget(req: Request, trace_id: &str) -> Result<Response, Error> {
    let gid_owned = browser_garden_id();
    let gid = resolve_safety_id(&gid_owned, trace_id, "gadget", "garden_id");
    if let Some(r) = viewer_block(&req, gid, trace_id, "gadget") {
        return Ok(r);
    }
    let did_raw = req
        .get_query_parameter("device")
        .unwrap_or("default")
        .to_string();
    let did = resolve_safety_id(&did_raw, trace_id, "gadget", "device_id");
    let (event, telemetry) = match KVStore::open(STATE_STORE) {
        Ok(Some(store)) => (
            latest_event_from(&store, gid, did),
            latest_telemetry_from(&store, gid, did),
        ),
        _ => (serde_json::Value::Null, serde_json::Value::Null),
    };
    log_evt(
        trace_id,
        "gadget",
        "read",
        "ok",
        &format!("garden={} device={}", gid, did),
    );
    Ok(Response::from_status(StatusCode::OK)
        .with_body_json(&serde_json::json!({"event": event, "telemetry": telemetry}))?)
}

/// `GET /api/archive/days` -> `{"days":["YYYY-MM-DD",...]}` (newest first) the garden
/// has evidence for. Viewer-gated.
pub fn handle_archive_days(req: Request, trace_id: &str) -> Result<Response, Error> {
    let gid_owned = browser_garden_id();
    let gid = resolve_safety_id(&gid_owned, trace_id, "archive", "garden_id");
    if let Some(r) = viewer_block(&req, gid, trace_id, "archive") {
        return Ok(r);
    }
    let days: Vec<String> = match fos_creds() {
        Ok(creds) => evidence_days(&creds, gid, trace_id),
        Err(_) => Vec::new(),
    };
    log_evt(
        trace_id,
        "archive",
        "days",
        "ok",
        &format!("garden={} days={}", gid, days.len()),
    );
    Ok(Response::from_status(StatusCode::OK).with_body_json(&serde_json::json!({"days": days}))?)
}

/// `GET /api/archive?date=YYYY-MM-DD` -> `{"date":..,"events":[..]}` (newest first)
/// for that day across all the garden's cameras. Viewer-gated.
pub fn handle_archive_day(req: Request, trace_id: &str) -> Result<Response, Error> {
    let gid_owned = browser_garden_id();
    let gid = resolve_safety_id(&gid_owned, trace_id, "archive", "garden_id");
    if let Some(r) = viewer_block(&req, gid, trace_id, "archive") {
        return Ok(r);
    }
    let date_raw = req.get_query_parameter("date").unwrap_or("").to_string();
    let prefix = match archive_day_prefix(gid, &date_raw) {
        Some(p) => p,
        None => {
            return Ok(
                Response::from_status(StatusCode::BAD_REQUEST).with_body("date must be YYYY-MM-DD")
            );
        }
    };
    let limit = parse_limit(req.get_query_parameter("limit"));
    let mut events: Vec<serde_json::Value> = Vec::new();
    if let Ok(creds) = fos_creds() {
        // FOS doesn't honor S3 max-keys, so list the day in full (the inverted-time key
        // makes lexical-ASCENDING == newest first, and fos_list_keys' page cap keeps the
        // newest), parse, then keep the newest `limit` events.
        let mut keys = fos_list_keys(&creds, &prefix, trace_id);
        keys.sort();
        events = keys
            .iter()
            .filter_map(|k| parse_archive_key(gid, k))
            .collect();
        if let Some(n) = limit {
            events.truncate(n);
        }
    }
    log_evt(
        trace_id,
        "archive",
        "day",
        "ok",
        &format!(
            "garden={} date={} events={} limit={:?}",
            gid,
            date_raw,
            events.len(),
            limit
        ),
    );
    Ok(Response::from_status(StatusCode::OK)
        .with_body_json(&serde_json::json!({"date": date_raw, "events": events}))?)
}

/// Parse + clamp the archive `?limit=N` query param. Absent/blank/non-numeric -> `None`
/// (no cap — return the whole day). Present -> `Some(N)` clamped into S3's `max-keys`
/// range `1..=1000` (so `0` becomes `1`, huge values become `1000`). PURE + unit-tested.
pub fn parse_limit(raw: Option<&str>) -> Option<usize> {
    raw.and_then(|s| s.trim().parse::<usize>().ok())
        .map(|n| n.clamp(1, 1000))
}

/// Which source served (or denied) an archive-image read. The terminal outcome of the
/// CDN-first read plan, returned by [`archive_read_plan`] so the ordering/policy is
/// pinnable WITHOUT the fastly host ABI (which keeps the real handler out of `cargo test`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ArchiveReadOutcome {
    /// Viewer gate blocked the request (401 in the handler) — the gate is consulted FIRST,
    /// before the key check or ANY source fetch, so a gated request never touches a source.
    GateBlocked,
    /// Key failed the scope/traversal guard (`archive_image_key_ok`) -> 403. Checked AFTER
    /// the gate but BEFORE any source fetch, so a bad key never reaches CDN/FOS.
    Forbidden,
    /// The CDN read-signing service (the CDN-first source) served the bytes -> 200.
    ServedFromCdn,
    /// CDN missed/not-wired; the direct-FOS SigV4 fallback served the bytes -> 200.
    ServedFromFos,
    /// Gate + key OK but neither source yielded a 200 -> 404.
    NotFound,
}

/// PURE orchestration of the `/api/archive/image` read, extracted so the wiring ORDER
/// (viewer gate -> key scope guard -> CDN-first -> FOS fallback -> 404) is unit-testable
/// without the host ABI that the live handler needs. Behavior MUST mirror
/// [`handle_archive_image`] exactly:
///   1. `gate_allowed == false`  -> ([`ArchiveReadOutcome::GateBlocked`], `None`) — NO source fetched
///   2. `key_ok == false`        -> ([`ArchiveReadOutcome::Forbidden`],   `None`) — NO source fetched
///   3. `cdn()` returns `Some(p)` -> ([`ArchiveReadOutcome::ServedFromCdn`], `Some(p)`) — FOS NOT consulted
///   4. else `fos()` is `Some(p)` -> ([`ArchiveReadOutcome::ServedFromFos`], `Some(p)`)
///   5. else                      -> ([`ArchiveReadOutcome::NotFound`], `None`)
///
/// The two sources are `FnOnce` so the function CONSUMES them in order and short-circuits:
/// `fos` is never invoked when `cdn` already hit, which is exactly the "CDN-first, FOS only
/// on miss" policy the test pins. Each returns `Some(payload)` for "this source produced
/// servable 200 bytes" (generic `T` = `Vec<u8>` in the handler) or `None` for
/// miss/non-200/not-wired. The chosen source's payload is returned alongside the outcome so
/// the handler can serve it; a denied/missed read returns `None` (nothing to serve).
pub fn archive_read_plan<T>(
    gate_allowed: bool,
    key_ok: bool,
    cdn: impl FnOnce() -> Option<T>,
    fos: impl FnOnce() -> Option<T>,
) -> (ArchiveReadOutcome, Option<T>) {
    if !gate_allowed {
        return (ArchiveReadOutcome::GateBlocked, None);
    }
    if !key_ok {
        return (ArchiveReadOutcome::Forbidden, None);
    }
    if let Some(payload) = cdn() {
        return (ArchiveReadOutcome::ServedFromCdn, Some(payload));
    }
    if let Some(payload) = fos() {
        return (ArchiveReadOutcome::ServedFromFos, Some(payload));
    }
    (ArchiveReadOutcome::NotFound, None)
}

/// `GET /api/archive/image?key=<object-key>` -> the archived JPEG. Viewer-gated AND
/// scoped: only objects under THIS garden's `g/<gid>/evidence/` prefix may be fetched
/// (the cross-tenant guard — the browser garden is the service's own, never a header),
/// no path traversal, `.jpg` only. SigV4 GET via the FOS backend. The frontend sends
/// the key `encodeURIComponent`-encoded, and `get_query_parameter` does NOT decode, so
/// we percent-decode FIRST, then validate + sign the DECODED value.
///
/// The decision ORDER (gate -> key -> CDN-first -> FOS -> 404) lives in the pure
/// [`archive_read_plan`] so it is unit-tested without the host ABI; the closures below
/// supply the actual host I/O, and the resulting bytes are carried alongside so the
/// served outcome can emit them.
pub fn handle_archive_image(req: Request, trace_id: &str) -> Result<Response, Error> {
    let gid_owned = browser_garden_id();
    let gid = resolve_safety_id(&gid_owned, trace_id, "archive", "garden_id");
    // viewer_block both LOGS the gate event and builds the 401 Response when blocked; call
    // it ONCE and keep its Option<Response> so the gate stays the first decision and is not
    // re-evaluated (no double log) when the plan reports GateBlocked.
    let gate_block = viewer_block(&req, gid, trace_id, "archive");
    let gate_allowed = gate_block.is_none();
    let key = percent_decode(req.get_query_parameter("key").unwrap_or(""));
    let key_ok = archive_image_key_ok(gid, &key);

    // CDN-first: pull the image through the CDN read-signing service (cached, private-
    // bucket-fronting) and proxy the bytes — the viewer gate above is already enforced,
    // and the browser never sees the CDN secret. Fall back to a direct SigV4 FOS GET when
    // the CDN isn't wired (no cdn_host/secret) or returns anything but 200, so gardens
    // not yet reprovisioned with the CDN backend keep working. The plan calls the two
    // source closures lazily + in order (FOS only on a CDN miss), so this preserves the
    // exact CDN-first short-circuit of the prior `cdn().or_else(fos)` chain.
    let (outcome, bytes) = archive_read_plan(
        gate_allowed,
        key_ok,
        || {
            cdn_signed_get(&key, trace_id)
                .and_then(|(status, body)| (status == 200).then_some(body))
        },
        || {
            fos_creds().ok().and_then(|creds| {
                let canonical_uri = format!("/{}/{}", creds.bucket, key);
                // bypass_cache=false: archive images are IMMUTABLE (unique date+time+cid key),
                // so caching this direct-FOS fallback at the edge is desirable. Only the LIST
                // path needs to bypass the cache (see fos_list) — do NOT flip this to true.
                match fos_signed_get(&creds, &canonical_uri, "", false, trace_id) {
                    Some((200, body)) => Some(body),
                    _ => None,
                }
            })
        },
    );

    match outcome {
        ArchiveReadOutcome::GateBlocked => {
            // The 401 (already logged) built by the single viewer_block call above.
            return Ok(gate_block.expect("GateBlocked implies viewer_block returned Some"));
        }
        ArchiveReadOutcome::Forbidden => {
            log_evt(
                trace_id,
                "archive",
                "image",
                "warn",
                &format!("garden={} rejected out-of-scope key", gid),
            );
            return Ok(Response::from_status(StatusCode::FORBIDDEN).with_body("Forbidden"));
        }
        _ => {}
    }
    match bytes {
        Some(b) => {
            log_evt(
                trace_id,
                "archive",
                "image",
                "ok",
                &format!("garden={} bytes={}", gid, b.len()),
            );
            Ok(Response::from_status(StatusCode::OK)
                .with_header("Content-Type", "image/jpeg")
                // Archive keys are immutable (unique date+time+cid) -> cache ~forever.
                // `private` (NOT public): the image is viewer-gated, so only the end
                // user's browser may cache it, never a shared/intermediary cache.
                .with_header("Cache-Control", "private, max-age=2592000, immutable")
                .with_body(b))
        }
        None => {
            log_evt(
                trace_id,
                "archive",
                "image",
                "ok",
                &format!("garden={} not found -> 404", gid),
            );
            Ok(Response::from_status(StatusCode::NOT_FOUND).with_body("No such image"))
        }
    }
}

/// `POST /api/archive/prune?days=N` — delete archived evidence older than `N` days
/// (default 30). Since FOS has NO lifecycle/expiration, retention is an explicit sweep.
/// SECURITY: destructive, so it requires a VALID per-garden token (`X-Garden-Auth` ==
/// `Authorized`; `Tokenless`/`Rejected` are refused — prune is disabled on the tokenless
/// `default` garden) and operates ONLY on the service's OWN garden (`browser_garden_id`,
/// never a client header — not cross-tenant steerable). Deletes are per-key signed
/// DELETEs, capped per invocation so one call is bounded; `remaining=true` means run
/// again. Returns `{deleted, days_pruned, remaining, cutoff}`.
///
/// CACHE CONSISTENCY (see [`sweep_evidence_days`]): the LIST is live immediately after a
/// prune (FOS LIST is sent `with_pass(true)`, see `archive::fos_list`), so `/api/archive/days`
/// and the date browse stop reporting the deleted days at once. Individual deleted IMAGE URLs
/// may still 200 from the CDN read cache until their TTL — this is benign because the UI only
/// ever builds image URLs from the live LIST (a pruned day yields no keys, so no URL is offered).
pub fn handle_archive_prune(req: Request, trace_id: &str) -> Result<Response, Error> {
    const MAX_DAYS_PER_RUN: usize = 14;
    const MAX_DELETES_PER_RUN: usize = 150; // ATTEMPTS/call (per-object, drained); UI loops

    let gid_owned = browser_garden_id();
    let gid = resolve_safety_id(&gid_owned, trace_id, "archive", "garden_id");
    let provided = req
        .get_header_str(GARDEN_AUTH_HEADER)
        .map(|s| s.to_string());
    if garden_auth_outcome(gid, provided.as_deref()) != AuthOutcome::Authorized {
        log_evt(
            trace_id,
            "archive",
            "prune",
            "warn",
            &format!("garden={} unauthorized", gid),
        );
        return Ok(Response::from_status(StatusCode::UNAUTHORIZED)
            .with_body("Unauthorized: prune requires a valid X-Garden-Auth token"));
    }

    let days: u32 = req
        .get_query_parameter("days")
        .and_then(|s| s.trim().parse::<u32>().ok())
        .unwrap_or(30)
        .max(1); // clamp: days=0 must never wipe today's live evidence

    let creds = match fos_creds() {
        Ok(c) => c,
        Err(why) => {
            log_evt(trace_id, "archive", "prune", "skip", why);
            return Ok(Response::from_status(StatusCode::OK).with_body_json(
                &serde_json::json!({"deleted": 0, "days_pruned": 0, "remaining": false, "cutoff": serde_json::Value::Null, "skipped": why}),
            )?);
        }
    };

    let cutoff = prune_cutoff_date(now_secs(), days);
    let all_days = evidence_days(&creds, gid, trace_id);
    let expired_total = all_days
        .iter()
        .filter(|d| day_is_expired(d, &cutoff))
        .count();
    let targets = days_to_prune(&all_days, &cutoff, MAX_DAYS_PER_RUN);

    let (deleted, days_pruned, failed, hit_cap, fail_sample) = sweep_evidence_days(
        &creds,
        gid,
        &targets,
        MAX_DELETES_PER_RUN,
        trace_id,
        "prune",
    );

    // More work remains if we capped out, or there were more expired days than this run
    // processed (each subsequent run re-lists and continues).
    let remaining = hit_cap || expired_total > targets.len();
    log_evt(
        trace_id,
        "archive",
        "prune",
        if failed > 0 { "warn" } else { "ok" },
        &format!(
            "garden={} cutoff={} days={} deleted={} days_pruned={} failed={} sample={:?} remaining={}",
            gid, cutoff, days, deleted, days_pruned, failed, fail_sample, remaining
        ),
    );
    Ok(
        Response::from_status(StatusCode::OK).with_body_json(&serde_json::json!({
            "deleted": deleted,
            "days_pruned": days_pruned,
            "failed": failed,
            "fail_sample": fail_sample,
            "remaining": remaining,
            "cutoff": cutoff,
            "retention_days": days,
        }))?,
    )
}

/// PURE cross-tenant delete guard: of `keys`, keep ONLY those under this garden's
/// `g/{gid}/evidence/` prefix. This is the belt-and-suspenders that stops a prune/wipe
/// from ever deleting another garden's objects even if a listing returned something
/// unexpected. Returns owned `String`s (the caller drains them into per-object DELETEs).
/// No I/O -> unit-testable; assert that `g/other/...`, traversal-ish (`..`), and bare
/// keys are dropped while only in-scope keys survive.
pub fn keys_in_scope(gid: &str, keys: &[String]) -> Vec<String> {
    let scope = format!("g/{}/evidence/", gid);
    keys.iter()
        .filter(|k| k.starts_with(&scope))
        .cloned()
        .collect()
}

/// Delete every archived object under each of `targets` (a list of `YYYY-MM-DD` days),
/// OLDEST day first, capped at `max_deletes` ATTEMPTS per call so one invocation stays
/// bounded even when deletes are failing. Returns `(deleted, days_touched, failed, hit_cap,
/// fail_sample)` where `fail_sample` is the first failure's reason (for diagnosis). Belt-
/// and-suspenders: only ever deletes keys under this garden's `g/{gid}/evidence/` prefix
/// (via [`keys_in_scope`]).
///
/// CACHE CONSISTENCY — read this before "adding a purge after delete":
/// This sweep deletes the OBJECTS from FOS only; it intentionally issues NO Fastly cache
/// purge. There are two read caches in front of the archive and they behave differently:
///   - The LISTING (days / date-browse / this sweep's own re-list) is ALWAYS LIVE: the FOS
///     LIST send is `with_pass(true)` (see `archive::fos_list`), so a deleted day disappears
///     from `/api/archive/days` and History the instant the DELETE lands.
///   - Individual archive IMAGES are cached `immutable, max-age=2592000` by the CDN read VCL
///     (provision/vcl.py) AND by the direct-FOS fallback (handle_archive_image). So a deleted
///     image URL can keep returning 200 from cache for up to its 30-day TTL.
/// We do NOT purge those stale images here, deliberately:
///   1. It's not user-visible. The UI only ever builds an image URL from a key the LIVE LIST
///      returned; a wiped/pruned day yields no keys, so the page never offers a deleted URL.
///      Only a stale, separately-bookmarked DIRECT link can still hit a cached image.
///   2. The only purges that would clear them are unavailable / out-of-lane here:
///      * a surrogate-key purge would need the CDN VCL to tag images with a `Surrogate-Key`
///        (it does not — vcl.py is the CLOUD expert's service); adding that is a separate,
///        higher-risk infra change, not an edge code change.
///      * a per-URL or service `purge_all` API call needs a Fastly API token + an
///        `api.fastly.com` backend AT THE EDGE. The Compute service holds only SCOPED creds
///        (per-garden tokens + the FOS/CDN read secrets) by design; an account-wide purge
///        token on the public edge is a security regression, and per-URL purges would add
///        up to `max_deletes` unbounded API calls onto this destructive admin path.
/// If clearing the stale image cache ever becomes a real requirement, the right place is a
/// CLOUD-side surrogate-key purge (tag images per-garden in vcl.py, then purge the garden's
/// key after a wipe/prune) — coordinate with the cloud expert; do not bolt a purge token onto
/// the edge. Documented in docs/endpoint-contract.md (Archive read consistency).
fn sweep_evidence_days(
    creds: &FosCreds,
    gid: &str,
    targets: &[String],
    max_deletes: usize,
    trace_id: &str,
    op: &str,
) -> (usize, usize, usize, bool, Option<String>) {
    // Delete per-object (FOS has no working bulk DeleteObjects — POST ?delete hangs). The KEY
    // fix is that fos_signed_delete now DRAINS the response body so connections are reused
    // instead of pinned (undrained, the pool exhausted after ~16 and a 7044-object wipe
    // deleted only 16). The per-run ATTEMPT cap keeps one call bounded; the UI loops.
    let mut deleted = 0usize;
    let mut days_touched = 0usize;
    let mut failed = 0usize;
    let mut hit_cap = false;
    let mut fail_sample: Option<String> = None;
    'outer: for day in targets {
        let prefix = match archive_day_prefix(gid, day) {
            Some(p) => p,
            None => continue,
        };
        let keys = fos_list_keys(creds, &prefix, trace_id);
        // Belt-and-suspenders: only ever delete under this garden's evidence prefix.
        let day_keys = keys_in_scope(gid, &keys);
        let mut day_deleted = 0usize;
        let mut day_failed = 0usize;
        for key in day_keys {
            // Cap on ATTEMPTS (deleted + failed) so one call stays bounded.
            if deleted + failed >= max_deletes {
                hit_cap = true;
                break 'outer;
            }
            match fos_signed_delete(creds, &key, trace_id) {
                Ok(200) | Ok(204) => {
                    deleted += 1;
                    day_deleted += 1;
                }
                Ok(st) => {
                    failed += 1;
                    day_failed += 1;
                    if fail_sample.is_none() {
                        fail_sample = Some(format!("http {}", st));
                    }
                }
                Err(e) => {
                    failed += 1;
                    day_failed += 1;
                    if fail_sample.is_none() {
                        fail_sample = Some(format!("send: {}", e));
                    }
                }
            }
        }
        if day_deleted > 0 {
            days_touched += 1;
        }
        log_evt(
            trace_id,
            "archive",
            &format!("{}_day", op),
            if day_failed > 0 { "warn" } else { "ok" },
            &format!(
                "garden={} day={} deleted={} failed={}",
                gid, day, day_deleted, day_failed
            ),
        );
    }
    (deleted, days_touched, failed, hit_cap, fail_sample)
}

/// `POST /api/archive/wipe` — delete ALL archived evidence for this garden, regardless of
/// age (including today's). This is the operator's explicit "delete everything" action,
/// kept SEPARATE from [`handle_archive_prune`] precisely because prune clamps `days.max(1)`
/// so it can never wipe live evidence — wipe is the deliberate override of that guard.
/// SECURITY: same gate as prune — destructive, so it requires a VALID per-garden token
/// (`X-Garden-Auth` == `Authorized`) and operates ONLY on the service's OWN garden
/// (`browser_garden_id`, never a client header). The portal layers a type-the-garden-id
/// confirmation on top. Deletes are capped per invocation; `remaining=true` means run
/// again. Returns `{deleted, days_wiped, remaining}`.
///
/// CACHE CONSISTENCY (see [`sweep_evidence_days`]): after a wipe the LIST is live at once
/// (`/api/archive/days` returns empty, History shows no photos), but already-cached deleted
/// IMAGE URLs may still 200 from the CDN read cache until TTL. Benign: the UI never surfaces
/// those URLs (it lists from the live LIST), so only a stale bookmarked direct link can hit them.
pub fn handle_archive_wipe(req: Request, trace_id: &str) -> Result<Response, Error> {
    const MAX_DAYS_PER_RUN: usize = 14;
    const MAX_DELETES_PER_RUN: usize = 150; // ATTEMPTS/call (per-object, drained); UI loops

    let gid_owned = browser_garden_id();
    let gid = resolve_safety_id(&gid_owned, trace_id, "archive", "garden_id");
    let provided = req
        .get_header_str(GARDEN_AUTH_HEADER)
        .map(|s| s.to_string());
    if garden_auth_outcome(gid, provided.as_deref()) != AuthOutcome::Authorized {
        log_evt(
            trace_id,
            "archive",
            "wipe",
            "warn",
            &format!("garden={} unauthorized", gid),
        );
        return Ok(Response::from_status(StatusCode::UNAUTHORIZED)
            .with_body("Unauthorized: wipe requires a valid X-Garden-Auth token"));
    }

    let creds = match fos_creds() {
        Ok(c) => c,
        Err(why) => {
            log_evt(trace_id, "archive", "wipe", "skip", why);
            return Ok(Response::from_status(StatusCode::OK).with_body_json(
                &serde_json::json!({"deleted": 0, "days_wiped": 0, "remaining": false, "skipped": why}),
            )?);
        }
    };

    // Target EVERY day the garden has evidence for (oldest first, capped per run).
    let all_days = evidence_days(&creds, gid, trace_id);
    let total_days = all_days.len();
    let targets = days_to_wipe(&all_days, MAX_DAYS_PER_RUN);

    let (deleted, days_wiped, failed, hit_cap, fail_sample) =
        sweep_evidence_days(&creds, gid, &targets, MAX_DELETES_PER_RUN, trace_id, "wipe");

    // More work remains if we capped out mid-day, or there were more days than this run took.
    let remaining = hit_cap || total_days > targets.len();
    log_evt(
        trace_id,
        "archive",
        "wipe",
        if failed > 0 { "warn" } else { "ok" },
        &format!(
            "garden={} deleted={} days_wiped={} failed={} sample={:?} remaining={}",
            gid, deleted, days_wiped, failed, fail_sample, remaining
        ),
    );
    Ok(
        Response::from_status(StatusCode::OK).with_body_json(&serde_json::json!({
            "deleted": deleted,
            "days_wiped": days_wiped,
            "failed": failed,
            "fail_sample": fail_sample,
            "remaining": remaining,
        }))?,
    )
}

// ---------------------------------------------------------------------------
// Alarms — list / tag / manage / retention over the per-garden `g/<gid>/alarm_log` (one JSON
// doc; alarms are written at evidence time by `record_alarm_if_triggered` in main.rs). The
// per-species recommendation histograms are DERIVED from the tagged alarms on read (alarms.rs),
// so the log is the single source of truth. AUTH model: TAG a NEW (untagged) alarm is viewer-
// gated — anyone who can view may; CHANGE a tag, DELETE an alarm, and prune/wipe require a valid
// garden token (admin), so a viewer can only ever add. Read-modify-write the single doc.
// ---------------------------------------------------------------------------

/// True iff the request carries a VALID per-garden token (the admin signal: the Pi portal /
/// console proxy forwards it for an authenticated admin; a public edge viewer has none).
fn alarm_is_admin(req: &Request, gid: &str) -> bool {
    garden_auth_outcome(gid, req.get_header_str(GARDEN_AUTH_HEADER)) == AuthOutcome::Authorized
}

/// The current global confidence gate as a percent (for the recommendation baseline).
fn current_threshold_pct() -> u32 {
    (MITIGATE_THRESHOLD * 100.0_f32).round() as u32
}

/// `GET /api/alarms` — the Alarms page data: every alarm (newest first) + per-species tuning
/// recommendations + `can_manage` (true iff the request carries a valid garden token). Viewer-gated.
pub fn handle_alarms(req: Request, trace_id: &str) -> Result<Response, Error> {
    let gid_owned = browser_garden_id();
    let gid = resolve_safety_id(&gid_owned, trace_id, "alarms", "garden_id");
    if let Some(r) = viewer_block(&req, gid, trace_id, "alarms") {
        return Ok(r);
    }
    let can_manage = alarm_is_admin(&req, gid);
    let log = match KVStore::open(STATE_STORE) {
        Ok(Some(store)) => load_alarm_log(&store, gid),
        _ => AlarmLog::new(),
    };
    let current_pct = current_threshold_pct();
    let recommendations = recommendations(&stats_from_alarms(&log), current_pct, MIN_LABELS);
    let mut alarms: Vec<&AlarmRecord> = log.values().collect();
    alarms.sort_by(|a, b| b.ts.cmp(&a.ts)); // newest first
    log_evt(
        trace_id,
        "alarms",
        "read",
        "ok",
        &format!(
            "garden={} alarms={} species={}",
            gid,
            alarms.len(),
            recommendations.len()
        ),
    );
    Ok(
        Response::from_status(StatusCode::OK).with_body_json(&serde_json::json!({
            "threshold_pct": current_pct,
            "min_labels": MIN_LABELS,
            "can_manage": can_manage,
            "recommendations": recommendations,
            "alarms": alarms,
        }))?,
    )
}

/// `GET /api/alarm?key=<archive-key>` — the alarm for ONE frame (its `batch||cid`), so the event
/// page shows the determination toggle only when the frame is an alarm. Viewer-gated. Returns
/// `{alarm: {...}|null}`.
pub fn handle_alarm_get(req: Request, trace_id: &str) -> Result<Response, Error> {
    let gid_owned = browser_garden_id();
    let gid = resolve_safety_id(&gid_owned, trace_id, "alarms", "garden_id");
    if let Some(r) = viewer_block(&req, gid, trace_id, "alarms") {
        return Ok(r);
    }
    let evkey = percent_decode(req.get_query_parameter("key").unwrap_or(""));
    let alarm = parse_archive_key(gid, &evkey).and_then(|ev| {
        let batch = ev.get("batch").and_then(|v| v.as_str()).unwrap_or("");
        let cid = ev.get("cid").and_then(|v| v.as_str()).unwrap_or("");
        let id = alarm_id(batch, cid);
        match KVStore::open(STATE_STORE) {
            Ok(Some(store)) => load_alarm_log(&store, gid).get(&id).cloned(),
            _ => None,
        }
    });
    Ok(Response::from_status(StatusCode::OK)
        .with_body_json(&serde_json::json!({"alarm": alarm}))?)
}

/// `POST /api/alarm-tag` — set an alarm's determination. Body `{"id":"<alarm-id>","label":"good|neutral|bad"}`.
/// Viewer-gated. ADDING a tag to an as-yet-untagged alarm is allowed for any viewer; CHANGING an
/// existing tag requires a valid garden token (admin), so a viewer can only ever add. Returns
/// `{ok, label, edited}`.
pub fn handle_alarm_tag(mut req: Request, trace_id: &str) -> Result<Response, Error> {
    let gid_owned = browser_garden_id();
    let gid = resolve_safety_id(&gid_owned, trace_id, "alarms", "garden_id");
    if let Some(r) = viewer_block(&req, gid, trace_id, "alarms") {
        return Ok(r);
    }
    let is_admin = alarm_is_admin(&req, gid);
    let body = req.take_body().into_bytes();
    let val: serde_json::Value = match serde_json::from_slice(&body) {
        Ok(v) => v,
        Err(_) => {
            return Ok(Response::from_status(StatusCode::BAD_REQUEST)
                .with_body("Expected JSON {\"id\":\"...\",\"label\":\"good|neutral|bad\"}"));
        }
    };
    let id = val
        .get("id")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let label = val.get("label").and_then(|v| v.as_str()).unwrap_or("");
    if id.is_empty() || !label_valid(label) {
        return Ok(Response::from_status(StatusCode::BAD_REQUEST)
            .with_body("id required; label must be good|neutral|bad"));
    }
    let mut store = match KVStore::open(STATE_STORE) {
        Ok(Some(s)) => s,
        _ => {
            return Ok(Response::from_status(StatusCode::SERVICE_UNAVAILABLE)
                .with_body("State store unavailable; tag not persisted"));
        }
    };
    let mut log = load_alarm_log(&store, gid);
    let rec = match log.get_mut(&id) {
        Some(r) => r,
        None => return Ok(Response::from_status(StatusCode::NOT_FOUND).with_body("no such alarm")),
    };
    let already_tagged = rec.tag.is_some();
    // "Users can add new ones only" — changing an existing determination is an admin action.
    if already_tagged && !is_admin {
        log_evt(
            trace_id,
            "alarms",
            "tag",
            "warn",
            &format!(
                "garden={} viewer tried to change a tagged alarm -> 403",
                gid
            ),
        );
        return Ok(Response::from_status(StatusCode::FORBIDDEN)
            .with_body("This alarm is already judged — only an admin can change it."));
    }
    rec.tag = Some(label.to_string());
    if store
        .insert(&alarm_log_key(gid), serde_json::to_vec(&log)?)
        .is_err()
    {
        return Ok(Response::from_status(StatusCode::SERVICE_UNAVAILABLE)
            .with_body("State store unavailable; tag not persisted"));
    }
    log_evt(
        trace_id,
        "alarms",
        if already_tagged { "retag" } else { "tag" },
        "ok",
        &format!("garden={} id={} label={}", gid, id, label),
    );
    Ok(Response::from_status(StatusCode::OK).with_body_json(
        &serde_json::json!({"ok": true, "label": label, "edited": already_tagged}),
    )?)
}

/// `POST /api/alarm/delete` — remove ONE alarm record. Body `{"id":"<alarm-id>"}`. ADMIN-ONLY
/// (valid `X-Garden-Auth` token, same gate as prune/wipe; disabled on the tokenless `default`
/// garden). Returns `{ok, deleted}`.
pub fn handle_alarm_delete(mut req: Request, trace_id: &str) -> Result<Response, Error> {
    let gid_owned = browser_garden_id();
    let gid = resolve_safety_id(&gid_owned, trace_id, "alarms", "garden_id");
    if !alarm_is_admin(&req, gid) {
        log_evt(
            trace_id,
            "alarms",
            "delete",
            "warn",
            &format!("garden={} unauthorized", gid),
        );
        return Ok(Response::from_status(StatusCode::UNAUTHORIZED)
            .with_body("Unauthorized: deleting an alarm requires a valid X-Garden-Auth token"));
    }
    let body = req.take_body().into_bytes();
    let val: serde_json::Value = serde_json::from_slice(&body).unwrap_or(serde_json::Value::Null);
    let id = val
        .get("id")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    if id.is_empty() {
        return Ok(Response::from_status(StatusCode::BAD_REQUEST).with_body("id required"));
    }
    let mut store = match KVStore::open(STATE_STORE) {
        Ok(Some(s)) => s,
        _ => {
            return Ok(Response::from_status(StatusCode::SERVICE_UNAVAILABLE)
                .with_body("State store unavailable"))
        }
    };
    let mut log = load_alarm_log(&store, gid);
    let deleted = log.remove(&id).is_some();
    if deleted
        && store
            .insert(&alarm_log_key(gid), serde_json::to_vec(&log)?)
            .is_err()
    {
        return Ok(Response::from_status(StatusCode::SERVICE_UNAVAILABLE)
            .with_body("State store unavailable; not persisted"));
    }
    log_evt(
        trace_id,
        "alarms",
        "delete",
        "ok",
        &format!(
            "garden={} id={} deleted={} count={}",
            gid,
            id,
            deleted,
            log.len()
        ),
    );
    Ok(Response::from_status(StatusCode::OK)
        .with_body_json(&serde_json::json!({"ok": true, "deleted": deleted}))?)
}

/// `POST /api/alarms/prune?mode=days&keep=N` (or `mode=count`) — retention sweep over the alarm
/// log. ADMIN-ONLY (valid token; the portal scheduler + "Clean up now" forward it). `days` drops
/// alarms older than `keep` days; `count` keeps the newest `keep`. One KV write. Returns
/// `{ok, deleted, kept}`.
pub fn handle_alarms_prune(req: Request, trace_id: &str) -> Result<Response, Error> {
    let gid_owned = browser_garden_id();
    let gid = resolve_safety_id(&gid_owned, trace_id, "alarms", "garden_id");
    if !alarm_is_admin(&req, gid) {
        log_evt(
            trace_id,
            "alarms",
            "prune",
            "warn",
            &format!("garden={} unauthorized", gid),
        );
        return Ok(Response::from_status(StatusCode::UNAUTHORIZED)
            .with_body("Unauthorized: alarm prune requires a valid X-Garden-Auth token"));
    }
    let mode = req
        .get_query_parameter("mode")
        .unwrap_or("days")
        .to_string();
    let keep: u32 = req
        .get_query_parameter("keep")
        .and_then(|s| s.trim().parse::<u32>().ok())
        .unwrap_or(90)
        .max(0);
    let mut store = match KVStore::open(STATE_STORE) {
        Ok(Some(s)) => s,
        _ => {
            return Ok(Response::from_status(StatusCode::SERVICE_UNAVAILABLE)
                .with_body("State store unavailable"))
        }
    };
    let mut log = load_alarm_log(&store, gid);
    let deleted = if mode == "count" {
        prune_by_count(&mut log, keep as usize)
    } else {
        prune_by_days(&mut log, now_ms(), keep.max(1))
    };
    if deleted > 0
        && store
            .insert(&alarm_log_key(gid), serde_json::to_vec(&log)?)
            .is_err()
    {
        return Ok(Response::from_status(StatusCode::SERVICE_UNAVAILABLE)
            .with_body("State store unavailable; not persisted"));
    }
    log_evt(
        trace_id,
        "alarms",
        "prune",
        "ok",
        &format!(
            "garden={} mode={} keep={} deleted={} kept={}",
            gid,
            mode,
            keep,
            deleted,
            log.len()
        ),
    );
    Ok(Response::from_status(StatusCode::OK)
        .with_body_json(&serde_json::json!({"ok": true, "deleted": deleted, "kept": log.len()}))?)
}

/// `POST /api/alarms/wipe` — delete ALL alarms for this garden. ADMIN-ONLY (valid token; the
/// portal's type-the-garden-name confirm sits in front). Returns `{ok, deleted}`.
pub fn handle_alarms_wipe(req: Request, trace_id: &str) -> Result<Response, Error> {
    let gid_owned = browser_garden_id();
    let gid = resolve_safety_id(&gid_owned, trace_id, "alarms", "garden_id");
    if !alarm_is_admin(&req, gid) {
        log_evt(
            trace_id,
            "alarms",
            "wipe",
            "warn",
            &format!("garden={} unauthorized", gid),
        );
        return Ok(Response::from_status(StatusCode::UNAUTHORIZED)
            .with_body("Unauthorized: alarm wipe requires a valid X-Garden-Auth token"));
    }
    let mut store = match KVStore::open(STATE_STORE) {
        Ok(Some(s)) => s,
        _ => {
            return Ok(Response::from_status(StatusCode::SERVICE_UNAVAILABLE)
                .with_body("State store unavailable"))
        }
    };
    let deleted = load_alarm_log(&store, gid).len();
    if store.insert(&alarm_log_key(gid), b"{}".to_vec()).is_err() {
        return Ok(Response::from_status(StatusCode::SERVICE_UNAVAILABLE)
            .with_body("State store unavailable; not persisted"));
    }
    log_evt(
        trace_id,
        "alarms",
        "wipe",
        "ok",
        &format!("garden={} deleted={}", gid, deleted),
    );
    Ok(Response::from_status(StatusCode::OK)
        .with_body_json(&serde_json::json!({"ok": true, "deleted": deleted}))?)
}

#[cfg(test)]
mod tests {
    use super::{archive_read_plan, keys_in_scope, ArchiveReadOutcome};
    use std::cell::RefCell;

    // ---- TEST-007: archive_read_plan pins the CDN-first read ORDER without the host ABI ----
    //
    // The live `handle_archive_image` interleaves pure decisions (viewer gate, key scope
    // guard) with host I/O (CDN GET, FOS GET) and so won't link under `cargo test`. The
    // pure `archive_read_plan` is the SAME decision tree the handler delegates to, so these
    // tests pin the four invariants the audit called out: (1) viewer gate enforced BEFORE
    // serving, (2) CDN tried before FOS (CDN-first), (3) 403 on a bad key, (4) 404 on a miss.
    //
    // `tap()` records the ORDER each source closure fires so we can prove CDN-before-FOS and,
    // crucially, that FOS is NOT consulted once the CDN hits (the short-circuit policy).
    fn tap(
        log: &RefCell<String>,
        who: char,
        result: Option<u32>,
    ) -> impl FnOnce() -> Option<u32> + '_ {
        move || {
            log.borrow_mut().push(who);
            result
        }
    }

    #[test]
    fn test_archive_read_plan_gate_blocks_before_any_source() {
        // Invariant 1: a blocked viewer gate short-circuits to GateBlocked and NEVER touches
        // a source — even if both the CDN and FOS "would have" served. This is the fail-closed
        // viewer gate: no archived image bytes escape an un-authenticated read.
        let log = RefCell::new(String::new());
        let (outcome, bytes) = archive_read_plan(
            /* gate_allowed */ false,
            /* key_ok */ true,
            tap(&log, 'c', Some(1)),
            tap(&log, 'f', Some(2)),
        );
        assert_eq!(outcome, ArchiveReadOutcome::GateBlocked);
        assert_eq!(bytes, None, "gate-blocked read must surface no bytes");
        assert_eq!(
            log.borrow().as_str(),
            "",
            "NO source may be consulted when the gate blocks"
        );
    }

    #[test]
    fn test_archive_read_plan_bad_key_is_forbidden_before_any_source() {
        // Invariant 3: a key that fails the scope/traversal guard -> Forbidden (403), again
        // BEFORE any source fetch (the gate has already passed here). A rejected key never
        // reaches the signer, so a traversal/cross-tenant key can never be fetched.
        let log = RefCell::new(String::new());
        let (outcome, bytes) = archive_read_plan(
            /* gate_allowed */ true,
            /* key_ok */ false,
            tap(&log, 'c', Some(1)),
            tap(&log, 'f', Some(2)),
        );
        assert_eq!(outcome, ArchiveReadOutcome::Forbidden);
        assert_eq!(bytes, None);
        assert_eq!(
            log.borrow().as_str(),
            "",
            "a forbidden key must not consult any source"
        );
    }

    #[test]
    fn test_archive_read_plan_cdn_first_short_circuits_fos() {
        // Invariant 2 (CDN-first): when the CDN serves, FOS is NOT consulted. We assert both
        // the chosen source (ServedFromCdn + the CDN payload) AND that only 'c' fired.
        let log = RefCell::new(String::new());
        let (outcome, bytes) = archive_read_plan(
            true,
            true,
            tap(&log, 'c', Some(42)),
            tap(&log, 'f', Some(99)),
        );
        assert_eq!(outcome, ArchiveReadOutcome::ServedFromCdn);
        assert_eq!(bytes, Some(42), "must serve the CDN bytes, not FOS");
        assert_eq!(
            log.borrow().as_str(),
            "c",
            "FOS must NOT be consulted on a CDN hit (CDN-first)"
        );
    }

    #[test]
    fn test_archive_read_plan_falls_back_to_fos_on_cdn_miss() {
        // CDN missed (not wired / non-200) -> the plan consults FOS, in that ORDER ("cf"),
        // and serves the FOS payload. This pins the documented fallback for gardens not yet
        // reprovisioned with the CDN backend.
        let log = RefCell::new(String::new());
        let (outcome, bytes) =
            archive_read_plan(true, true, tap(&log, 'c', None), tap(&log, 'f', Some(7)));
        assert_eq!(outcome, ArchiveReadOutcome::ServedFromFos);
        assert_eq!(bytes, Some(7));
        assert_eq!(
            log.borrow().as_str(),
            "cf",
            "CDN must be tried before FOS, then FOS on the miss"
        );
    }

    #[test]
    fn test_archive_read_plan_both_miss_is_not_found() {
        // Invariant 4: gate + key OK but neither source yields 200 -> NotFound (404), with
        // both sources consulted in order and no bytes to serve.
        let log = RefCell::new(String::new());
        let (outcome, bytes) =
            archive_read_plan(true, true, tap(&log, 'c', None), tap(&log, 'f', None));
        assert_eq!(outcome, ArchiveReadOutcome::NotFound);
        assert_eq!(bytes, None);
        assert_eq!(log.borrow().as_str(), "cf");
    }

    #[test]
    fn test_archive_read_plan_gate_precedes_key_check() {
        // Ordering tie-break: when BOTH the gate is blocked AND the key is bad, the gate wins
        // (GateBlocked, not Forbidden). The gate is the outermost guard, so an un-authenticated
        // request can never learn whether its key was valid (no oracle).
        let log = RefCell::new(String::new());
        let (outcome, _) = archive_read_plan(
            /* gate_allowed */ false,
            /* key_ok */ false,
            tap(&log, 'c', Some(1)),
            tap(&log, 'f', Some(2)),
        );
        assert_eq!(outcome, ArchiveReadOutcome::GateBlocked);
        assert_eq!(log.borrow().as_str(), "");
    }

    #[test]
    fn test_keys_in_scope_drops_cross_tenant_and_traversal() {
        // The belt-and-suspenders cross-tenant DELETE guard: of a listing that mixes in
        // another garden's objects, a traversal-ish key, and a bare/short key, ONLY keys
        // under THIS garden's `g/{gid}/evidence/` prefix may survive into the delete loop.
        // This is the last line stopping a prune/wipe from deleting a neighbor's archive.
        let gid = "default";
        let in_a = "g/default/evidence/2026/06/21/120000_none_x_0_cam_a.jpg".to_string();
        let in_b = "g/default/evidence/2026/06/22/130000_mitigate_fox_91_cam_b.jpg".to_string();
        // Different garden -> MUST be dropped (the cross-tenant invariant).
        let other = "g/other/evidence/2026/06/21/120000_none_x_0_cam_c.jpg".to_string();
        // Traversal-shaped key that does NOT start with our prefix -> dropped.
        let traversal = "g/default/../other/evidence/2026/06/21/x.jpg".to_string();
        // A traversal key that LOOKS prefixed (`..` AFTER the prefix) is NOT filtered here —
        // keys_in_scope is a prefix guard, not the charset/traversal guard. That defence is
        // archive_image_key_ok on the READ path; deletes only ever come from a FOS listing
        // under the prefix, so this case documents the boundary rather than asserting it out.
        let bare = "garden_models/mobilenet_v2.onnx".to_string();
        let empty = "".to_string();

        let input = vec![
            in_a.clone(),
            other.clone(),
            in_b.clone(),
            traversal.clone(),
            bare.clone(),
            empty.clone(),
        ];
        let kept = keys_in_scope(gid, &input);

        // Only the two genuinely in-scope keys survive, ORDER preserved.
        assert_eq!(kept, vec![in_a, in_b]);
        // Explicitly: nothing from another garden, no traversal-out, no bare/empty key.
        assert!(
            !kept.contains(&other),
            "leaked a cross-tenant key into the delete set"
        );
        assert!(
            !kept.contains(&traversal),
            "leaked a traversal key into the delete set"
        );
        assert!(!kept.contains(&bare));
        assert!(!kept.contains(&empty));
    }

    #[test]
    fn test_keys_in_scope_requires_evidence_segment_not_just_garden() {
        // A key under the garden but OUTSIDE the evidence/ subtree (e.g. a config or model
        // object that shares the `g/{gid}/` root) must NOT be swept by an evidence prune.
        let gid = "g1";
        let evidence = "g/g1/evidence/2026/06/21/x.jpg".to_string();
        let non_evidence = "g/g1/config/settings.json".to_string();
        // A different garden whose id is a PREFIX of ours must not match either.
        let prefix_collision = "g/g10/evidence/2026/06/21/x.jpg".to_string();
        let kept = keys_in_scope(gid, &[evidence.clone(), non_evidence, prefix_collision]);
        assert_eq!(kept, vec![evidence]);
    }
}
