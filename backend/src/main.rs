use fastly::{
    http::{Method, StatusCode},
    kv_store::KVStore,
    ConfigStore, Error, Request, Response,
};
use once_cell::sync::Lazy;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::net::IpAddr;
use std::sync::Arc;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

// Generated cross-language contract (CHARTER: one identity/contract source of truth).
// The identity/key/token fns + shared constants are rendered from contract/spec.toml
// by contract/gen.py into contract_gen.rs (Rust) AND provision/contract_gen.py
// (Python); `make gen-check` (in make ci) fails the build on drift. Pure consts/fns —
// no host-ABI — so it links under both wasm32 and native cargo test.
mod contract_gen;
use contract_gen::{dev_key, is_valid_id, key};

// AWS SigV4 signing + canonical encoding for the evidence-archive reads/writes
// (Phase 5 modularization). Pure (no host-ABI). Used by `mod archive` (FOS sends) and
// `mod auth` (viewer-cookie HMAC) via `crate::sigv4::*`; main itself no longer calls it.
mod sigv4;

// Evidence-archive FOS/CDN data layer (Phase 5a extraction). Glob-imported so the
// archive handlers + their tests resolve the moved fns/consts unchanged.
mod archive;
use archive::*;

// On-edge image inference (model load + JPEG preprocess + logit->decision). The
// MODEL cache static + handle_evidence's fail-closed branch stay in main.rs.
mod inference;
use inference::*;

// Alarms — triggers become alarms, get a good/neutral/bad determination, feed a per-species
// threshold RECOMMENDATION. PURE data model + math here; the KV I/O (record/load/tag/trim) +
// route handlers (handle_alarms*/serve_alarms) live in main.rs/routes.rs.
mod alarms;

// Small pure stateless helpers (encoding/escaping/constant-time compare), Phase 5b.
mod util;
use util::*;

// Per-garden device-token auth + the optional viewer-password gate (Phase 5b). Both
// are OFF the fail-closed safety path; pure decision cores split from I/O shells.
mod auth;
use auth::*;

// PURE safety-decision cores (continue_mitigation / apply_control / heartbeat_continue /
// rain_should_suppress / node_liveness), Phase 5. Behavior-preserving move; the flag /
// state / telemetry STORE I/O that feeds them stays inline below. Glob-imported so the
// handlers + the unchanged unit tests resolve the moved fns verbatim.
mod safety;
use safety::*;

// Node-down Twilio SMS alerting (`/api/alert` + pure request builders), Phase 5. OFF every
// fail-closed path (notify-only, best-effort). Glob-imported so the dispatcher + the twilio/
// node_down unit tests resolve the moved fns unchanged.
mod alert;
use alert::*;

// Admin control-plane routes for the `/api/gardens` tree (token-gated reads + control),
// Phase 5. OFF the fail-closed path; the flag I/O it drives stays sacred in main. Glob-
// imported so the dispatcher + the parse_admin_route/AdminRoute tests resolve unchanged.
mod admin;
use admin::*;

// Browser/dashboard READ route handlers + viewer login (`/api/state|snapshot|cameras|
// gadget`, `/api/archive*`, the view-only dashboard HTML + static), Phase 5. OFF the
// fail-closed path; the Pi-facing safety handlers + `/api/control` + the dispatcher stay
// in main. Glob-imported so the dispatcher resolves the moved handlers unchanged.
mod routes;
use routes::*;
use tract_onnx::prelude::*;

// Define API JSON structures
#[derive(Serialize)]
struct EvidenceResponse {
    action: String,
    species: Option<String>,
    confidence: f32,
    /// Why a confident critter was NOT mitigated despite the model wanting to
    /// (e.g. `"rain"`). Omitted when the action stands on its own.
    #[serde(skip_serializing_if = "Option::is_none")]
    reason: Option<String>,
}

#[derive(Serialize)]
struct StatusResponse {
    continue_mitigation: bool,
}

/// Node liveness + latest environment telemetry, surfaced to the dashboard health
/// tiles. `online` is derived at read time from `last_seen_ms` vs now (see
/// [`node_liveness`]); `telemetry` is the last posted blob (or `null` before the
/// first heartbeat).
#[derive(Serialize)]
struct NodeStatus {
    online: bool,
    last_seen_ms: Option<u64>,
    seconds_since: Option<u64>,
    telemetry: serde_json::Value,
}

/// State surfaced to the admin dashboard (`GET /api/state`, `POST /api/control`).
#[derive(Serialize)]
struct StateResponse {
    /// The DERIVED operating mode: "off" | "monitor" | "active" (see `derive_mode`).
    /// New SSOT field for the three-mode control model; `armed`/`override_stop` are
    /// KEPT for back-compat (shared gp.js still reads them during the transition).
    mode: &'static str,
    armed: bool,
    override_stop: bool,
    continue_mitigation: bool,
    /// Parsed `latest_event` JSON, or `null` if no evidence has been posted yet.
    latest_event: serde_json::Value,
    /// Garden-node liveness + telemetry for the health tiles.
    node: NodeStatus,
}

/// Control command body for `POST /api/control`.
#[derive(Deserialize)]
struct ControlRequest {
    cmd: String,
}

// First-line, per-INSTANCE IP lockout state for admin + viewer login. On Fastly Compute
// every request can land on a different wasm instance, EACH with its own copy of this map,
// so by itself this only throttles an attacker pinned to one instance (see EDGE-003). It is
// kept as the cheap, always-correct first layer; `lockout_kv_*` adds a best-effort,
// fail-OPEN `garden_state` KV counter on TOP of it for a closer-to-global throttle. The KV
// layer can only ever ADD lockouts (it is never consulted to GRANT access), so it cannot
// weaken this limiter. The real auth defenses remain the constant-time compare (util.rs)
// and the HMAC-signed viewer/admin sessions (auth.rs) — the lockout is defense-in-depth.
static LOCKOUT_STATE: Lazy<Mutex<HashMap<IpAddr, (u32, i64)>>> =
    Lazy::new(|| Mutex::new(HashMap::new()));

/// Flat (garden-agnostic) `garden_state` KV key prefix for the cross-instance login fail
/// counter. An attacker's IP is the same regardless of which garden they hit, so the
/// counter is keyed per-IP, NOT per-garden (unlike the `key()` flag grammar).
const LOCKOUT_KV_PREFIX: &str = "ratelimit/login/";

/// PURE: build the KV counter key for an IP. Kept tiny + testable; the `IpAddr` Display
/// form (`a.b.c.d` / `::1`) is a stable, charset-safe key suffix.
fn lockout_kv_key(ip: IpAddr) -> String {
    format!("{}{}", LOCKOUT_KV_PREFIX, ip)
}

/// PURE: encode `(count, last_fail_ts)` as the stored counter string (`"<count>:<ts>"`).
fn lockout_kv_encode(count: u32, last_ts: i64) -> String {
    format!("{}:{}", count, last_ts)
}

/// PURE: decode a stored counter string into `(count, last_fail_ts)`. Returns `None` for
/// anything malformed so the caller treats it as "no record" (fail-OPEN — never a lockout).
fn lockout_kv_decode(raw: &str) -> Option<(u32, i64)> {
    let (c, t) = raw.split_once(':')?;
    Some((c.parse::<u32>().ok()?, t.parse::<i64>().ok()?))
}

/// PURE: is the IP locked out per the KV counter? Mirrors the in-memory window logic —
/// a counter whose window has elapsed is treated as cleared (not locked). Centralized
/// here so it is unit-tested on the native target without the host KV ABI.
fn lockout_kv_is_locked(raw: Option<&str>, now: i64, window_s: i64, max_fails: u32) -> bool {
    match raw.and_then(lockout_kv_decode) {
        Some((count, last_ts)) if now - last_ts < window_s => count >= max_fails,
        _ => false,
    }
}

/// PURE: given the current stored counter (if any), compute the NEXT counter to persist
/// after one more failed attempt. A counter whose window has elapsed restarts at 1.
fn lockout_kv_next(raw: Option<&str>, now: i64, window_s: i64) -> (u32, i64) {
    match raw.and_then(lockout_kv_decode) {
        Some((count, last_ts)) if now - last_ts < window_s => (count.saturating_add(1), now),
        _ => (1, now),
    }
}

/// Default lockout policy when neither env nor Config Store overrides it.
const RATE_LIMIT_DEFAULT_MAX_FAILS: u32 = 5;
const RATE_LIMIT_DEFAULT_WINDOW_S: i64 = 300;

/// PURE parse/merge for the lockout policy. Starting from the built-in defaults, each
/// override layer wins if PRESENT AND PARSEABLE (a missing or garbage value leaves the
/// prior layer intact — never resets to default). Precedence low->high matches the live
/// read order: defaults < env (`RATE_LIMIT_*`) < Config Store (`device_config`). The host
/// Config Store read is wasm-gated (see [`read_rate_limit_settings`]); this fn takes the
/// already-fetched raw strings so BOTH targets (native `cargo test` + wasm) can test the
/// parse/merge without the host ABI.
fn merge_rate_limit_settings(
    env_max_fails: Option<&str>,
    env_window_s: Option<&str>,
    cfg_max_fails: Option<&str>,
    cfg_window_s: Option<&str>,
) -> (u32, i64) {
    let mut max_fails = RATE_LIMIT_DEFAULT_MAX_FAILS;
    let mut window_s = RATE_LIMIT_DEFAULT_WINDOW_S;
    // env first (local/Viceroy .env override), then Config Store (live edge) wins.
    for src in [env_max_fails, cfg_max_fails] {
        if let Some(num) = src.and_then(|v| v.parse::<u32>().ok()) {
            max_fails = num;
        }
    }
    for src in [env_window_s, cfg_window_s] {
        if let Some(num) = src.and_then(|v| v.parse::<i64>().ok()) {
            window_s = num;
        }
    }
    (max_fails, window_s)
}

/// Reads rate limiting settings with overrides from environment variables and the
/// "device_config" Config Store. The host reads live here (env always, Config Store
/// wasm-only to avoid a native test link failure); the parse/merge is the pure
/// [`merge_rate_limit_settings`] so the precedence is unit-testable on the native target.
fn read_rate_limit_settings() -> (u32, i64) {
    let env_max_fails = std::env::var("RATE_LIMIT_MAX_FAILS").ok();
    let env_window_s = std::env::var("RATE_LIMIT_WINDOW_S").ok();

    // Config Store "device_config" as a secondary source (wasm32-only to prevent native
    // test link failure). Native builds see env-only — the pure merge is what tests cover.
    let (cfg_max_fails, cfg_window_s) = {
        #[cfg(target_arch = "wasm32")]
        {
            match ConfigStore::try_open("device_config") {
                Ok(config) => (
                    config.try_get("rate_limit_max_fails").ok().flatten(),
                    config.try_get("rate_limit_window_s").ok().flatten(),
                ),
                Err(_) => (None, None),
            }
        }
        #[cfg(not(target_arch = "wasm32"))]
        {
            (None::<String>, None::<String>)
        }
    };

    merge_rate_limit_settings(
        env_max_fails.as_deref(),
        env_window_s.as_deref(),
        cfg_max_fails.as_deref(),
        cfg_window_s.as_deref(),
    )
}

/// BEST-EFFORT, fail-OPEN read of the cross-instance KV fail counter. Returns `true` only
/// if the `garden_state` counter for this IP is itself over budget; ANY KV error / absence
/// / malformed value yields `false` (never a lockout from a KV problem). wasm-gated: the
/// native test target has no host KV ABI, so it always returns `false` there (the pure
/// decision in `lockout_kv_is_locked` is what the unit tests exercise).
fn lockout_kv_locked(ip: IpAddr, now: i64, window_s: i64, max_fails: u32) -> bool {
    #[cfg(target_arch = "wasm32")]
    {
        if let Ok(Some(store)) = KVStore::open(STATE_STORE) {
            if let Ok(raw) = store.lookup_str(&lockout_kv_key(ip)) {
                return lockout_kv_is_locked(raw.as_deref(), now, window_s, max_fails);
            }
        }
        false
    }
    #[cfg(not(target_arch = "wasm32"))]
    {
        let _ = (ip, now, window_s, max_fails);
        false
    }
}

/// BEST-EFFORT, fail-OPEN write of one more failure to the cross-instance KV counter. A KV
/// error is swallowed (the in-memory limiter already counted it). Read-modify-write is NOT
/// atomic across instances, so the global count can under-report under a burst — acceptable
/// for a defense-in-depth throttle. Only ever called on the (cold) FAILED-login path.
///
/// KEY GROWTH (EDGE: unbounded `ratelimit/login/<ip>` keys): the fastly 0.9.5 `KVStore` API
/// has NO per-item TTL on `insert` and NO `delete`/list, and there is no `garden_state` prune
/// cron at the edge (the only retention cron is the FOS *archive* sweep, a different store), so
/// these keys cannot be actively reaped here. Growth is instead bounded by REUSE: this writer
/// re-keys per IP (one key per distinct attacker IP, never per attempt) and `lockout_kv_next`
/// OVERWRITES an expired entry in place (restart at 1), so the live key set tracks the count of
/// distinct failing IPs, and each entry self-expires LOGICALLY by window on read
/// (`lockout_kv_is_locked`). The in-memory layer's actual GC is [`sweep_expired_lockouts`].
fn lockout_kv_record(ip: IpAddr, now: i64, window_s: i64) {
    #[cfg(target_arch = "wasm32")]
    {
        if let Ok(Some(mut store)) = KVStore::open(STATE_STORE) {
            let prev = store.lookup_str(&lockout_kv_key(ip)).ok().flatten();
            let (count, ts) = lockout_kv_next(prev.as_deref(), now, window_s);
            let _ = store.insert(&lockout_kv_key(ip), lockout_kv_encode(count, ts).as_str());
        }
    }
    #[cfg(not(target_arch = "wasm32"))]
    {
        let _ = (ip, now, window_s);
    }
}

/// Checks if the given IP address is currently locked out due to excessive failures.
/// TWO layers: the cheap per-instance map (first-line) AND a best-effort, fail-OPEN
/// `garden_state` KV counter for a closer-to-global throttle across wasm instances
/// (EDGE-003). Either layer being over budget locks the IP; a KV error never does.
fn is_ip_locked_out(ip: IpAddr) -> bool {
    let now = now_secs();
    let (max_fails, window_s) = read_rate_limit_settings();
    let in_memory_locked = if let Ok(mut state) = LOCKOUT_STATE.lock() {
        if let Some(rec) = state.get(&ip) {
            if now - rec.1 >= window_s {
                // Window has elapsed, clean up stale entry
                state.remove(&ip);
                false
            } else {
                rec.0 >= max_fails
            }
        } else {
            false
        }
    } else {
        false // Fail-open on lock contention/error
    };
    // Short-circuit: if the local instance already says locked, skip the KV round-trip.
    in_memory_locked || lockout_kv_locked(ip, now, window_s, max_fails)
}

/// PURE: drop every lockout entry whose window has elapsed (`now - last_ts >= window_s`).
/// `is_ip_locked_out`/`record_failed_attempt` only ever clean the SINGLE re-accessed IP, so
/// an attacker that rotates IPs leaves a growing pile of dead entries; this opportunistic
/// sweep bounds the map to IPs that failed within the current window. Native-testable.
fn sweep_expired_lockouts(state: &mut HashMap<IpAddr, (u32, i64)>, now: i64, window_s: i64) {
    state.retain(|_, (_, last_ts)| now - *last_ts < window_s);
}

/// Records a failed login attempt for the given IP address, in BOTH the per-instance map
/// and the best-effort cross-instance KV counter. Only the FAILED-login path touches KV —
/// the success path (`reset_failed_attempts`) stays KV-free (the counter self-expires by
/// window), so a successful login adds no KV I/O.
fn record_failed_attempt(ip: IpAddr) {
    let now = now_secs();
    let (_, window_s) = read_rate_limit_settings();
    if let Ok(mut state) = LOCKOUT_STATE.lock() {
        // Opportunistic GC: this is the COLD failed-login path (never the 3s heartbeat) and the
        // lock is already held, so sweep all expired entries here to bound map growth against an
        // IP-rotating attacker (the lazy single-IP cleanup elsewhere leaves dead entries behind).
        sweep_expired_lockouts(&mut state, now, window_s);
        if let Some(rec) = state.get_mut(&ip) {
            if now - rec.1 >= window_s {
                // Prior window elapsed: restart count
                *rec = (1, now);
            } else {
                rec.0 += 1;
            }
        } else {
            state.insert(ip, (1, now));
        }
    }
    lockout_kv_record(ip, now, window_s);
}

/// Resets the failed attempt count for the given IP address upon successful login. Clears
/// the per-instance map only; the cross-instance KV counter is left to self-expire by its
/// window (a stale KV count after a SUCCESSFUL auth is harmless — the credential is already
/// proven). This deliberately keeps the success path free of any new KV I/O.
fn reset_failed_attempts(ip: IpAddr) {
    if let Ok(mut state) = LOCKOUT_STATE.lock() {
        state.remove(&ip);
    }
}

// Global, lazily-initialized thread-safe model cache
static MODEL: Lazy<Option<Arc<TypedRunnableModel>>> = Lazy::new(|| match load_model_from_kv() {
    Ok(model) => {
        log_evt("boot", "infer", "model_init", "ok", "MODEL ready");
        Some(model)
    }
    Err(e) => {
        eprintln!("[BACKEND ERROR] Failed to load model: {}", e);
        log_evt(
            "boot",
            "infer",
            "model_init",
            "error",
            &format!("{} -> MODEL=None (fail-closed)", e),
        );
        None
    }
});

// ---------------------------------------------------------------------------
// Admin dashboard state (writable KV Store `garden_state`)
// ---------------------------------------------------------------------------

/// Name of the writable KV Store backing dashboard state. Config Store is
/// read-only at the edge, so mutable arm/override flags + the latest event and
/// snapshot live here. Seeded with defaults in `fastly.toml` for local Viceroy.
const STATE_STORE: &str = "garden_state";

/// The dashboard, baked into the Wasm binary (see `dashboard.html`). The edge
/// serves it VIEW-ONLY (controls hidden via `window.GP_VIEW_ONLY`); admins manage
/// the garden from the Pi LAN portal (hardware/portal.py), not from here.
const DASHBOARD_HTML: &str = include_str!("dashboard.html");

/// The Timelapse player page, baked into the Wasm binary (see `timelapse.html`).
/// Served VIEW-ONLY on the edge (play-only; export lives on the Pi admin portal).
/// Reachable via the "Timelapse" nav link or the History page's "Play timelapse".
const TIMELAPSE_HTML: &str = include_str!("timelapse.html");

/// The Help page, baked into the Wasm binary (see `help.html`). Served VIEW-ONLY on
/// the edge (no admin controls), behind the same viewer gate as the dashboard.
/// Reachable via the "Help" nav link (viewer-visible per the nav SSOT).
const HELP_HTML: &str = include_str!("help.html");

/// The single-event detail page (see `event.html`). Served VIEW-ONLY on the edge, behind
/// the same viewer gate as History. NOT in the nav — reached by the "View details" links the
/// dashboard / History / Timelapse surfaces render for an event (`/event?key=<archive-key>`).
const EVENT_HTML: &str = include_str!("event.html");

/// The Alarms page (see `alarms.html`). Served VIEW-ONLY on the edge (behind the viewer gate)
/// and admin on the Pi. Reachable via the "Alarms" nav link (viewer_ok). Lists real/false/
/// untagged alarms + per-species tuning recommendations; tagging a NEW alarm is open to viewers,
/// while editing/deleting prior ones + the cleanup controls are gated server-side (a valid token).
const ALARMS_HTML: &str = include_str!("alarms.html");

/// The shared header partial (brand + garden name + nav) — the SINGLE SOURCE OF
/// TRUTH for the header, baked in from `hardware/portal_header.html`. The Pi portal
/// splices the same file via Python (hardware/portal.py); here `dashboard_header_html`
/// fills it and splices it into `DASHBOARD_HTML` at the `<!--PORTAL_HEADER-->` sentinel.
const PORTAL_HEADER_HTML: &str = include_str!("../../hardware/portal_header.html");

/// The ONE shared UI asset layer (CHARTER), baked into the Wasm binary from
/// `ui/static/`. `include_str!` is the build-time presence check: a missing asset
/// fails the WHOLE build (which would also take down /api/evidence + /api/status),
/// so they must be committed alongside this file. Served at /static/gp.<ext> by
/// `serve_static`, and by the Pi portal + console from disk — one source, three tiers.
const GP_CSS: &str = include_str!("../../ui/static/gp.css");
const GP_JS: &str = include_str!("../../ui/static/gp.js");
/// The Tailwind v4 + DaisyUI compiled stylesheet (committed, minified — built by
/// `make css` on a dev/CI machine; Node never ships to Compute). Baked in like gp.css
/// and served at /static/app.css; every page links it BEFORE gp.css so the cascade is
/// app.css -> gp.css -> inline.
const APP_CSS: &str = include_str!("../../ui/static/app.css");

/// The Fastly favicon, baked into the Wasm binary from the repo-root `favicon.ico`
/// (single source of truth — the Pi console serves the same file at /favicon.ico).
/// Served by `serve_favicon` and referenced by both the dashboard and login pages.
const FAVICON_ICO: &[u8] = include_bytes!("../../favicon.ico");

/// Secret-Store slot for a garden's OPTIONAL viewer password (gates the edge
/// dashboard for view-only users). Stored alongside the per-garden tokens under
/// `g.<gid>.viewer_pass` (see `token_secret_name`). Absent => dashboard is open.
const VIEWER_PASS_SLOT: &str = "viewer_pass";
/// Cookie that carries a signed edge-viewer session.
const VIEWER_COOKIE: &str = "gp_viewer";
/// Edge viewer session lifetime (seconds) — 12h, matching the Pi portal.
const VIEWER_SESSION_TTL_SECS: i64 = 43_200;

/// Minimal login page shown by the edge when a viewer password is configured and
/// the request has no valid viewer cookie. Posts to `/api/viewer-login`.
// NOTE: `r##"…"##` (double-hash) delimiter on purpose — the HTML contains `"#`
// sequences (e.g. `content="#FF282D"`) that would close a single-hash `r#"…"#`.
const VIEWER_LOGIN_HTML: &str = r##"<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fastly Garden Protector — Sign in</title>
<link rel="icon" href="/favicon.ico" sizes="any">
<meta name="theme-color" content="#FF282D">
<link rel="stylesheet" href="/static/app.css?v=__ASSET_VERSION__">
<link rel="stylesheet" href="/static/gp.css?v=__ASSET_VERSION__">
<script src="/static/gp.js?v=__ASSET_VERSION__"></script>
<style>
  /* gp.css supplies the palette + .card panel + .theme-toggle; the form field + submit
     button are DaisyUI (.input / .btn) from app.css. Only login layout + accent here. */
  body{min-height:100vh;display:flex;align-items:center;justify-content:center}
  .theme-toggle{position:fixed;top:14px;right:14px;z-index:10}
  .card{padding:28px;width:320px;max-width:92vw}
  h1{font-size:18px;margin:0 0 4px;font-weight:600}.leaf{color:var(--green)}
  p.sub{color:var(--muted);font-size:13px;margin:0 0 20px}
  label{display:block;font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px}
  .err{color:var(--red);font-size:13px;margin-top:14px;min-height:18px}
</style></head>
<body><button id="theme-toggle" class="theme-toggle" type="button" aria-label="Switch theme"></button>
<form class="card" method="POST" action="/api/viewer-login">
  <h1><span class="leaf"><svg class="gp-ic" aria-hidden="true"><use href="#gp-leaf"/></svg></span> Fastly Garden Protector</h1>
  <p class="sub">Enter the viewer password to see the dashboard.</p>
  <label for="passcode">Viewer password</label>
  <input id="passcode" name="passcode" type="password" class="input w-full" autocomplete="current-password" autofocus required>
  <button type="submit" class="btn btn-primary w-full mt-4">View dashboard</button>
  <div class="err">__ERROR__</div>
</form>
<script>GP.initTheme();</script></body></html>"##;

/// The viewer login page, with the error slot filled and the shared-asset cache-bust
/// stamped from the ONE generated `ASSET_VERSION`. Centralized so every serve point
/// (gate, lockout, wrong-password) stays in lock-step — there is no second place to
/// forget the version stamp.
pub(crate) fn viewer_login_html(error: &str) -> String {
    VIEWER_LOGIN_HTML
        .replace("__ERROR__", error)
        .replace("__ASSET_VERSION__", contract_gen::ASSET_VERSION)
}

// ---------------------------------------------------------------------------
// Multi-garden keying (forward-compat, Steps 1–2). The Pi already sends
// X-Garden-Id / X-Device-Id (default "default"); these PURE, in-memory helpers
// turn those ids into real `garden_state` KV keys with ZERO data migration.
// They are safe on the fail-closed safety paths because they do NO I/O — only
// string validation and `format!`. The SAME helpers serve reads AND writes so a
// dashboard STOP and the Pi heartbeat can never split-brain onto different keys.
// ---------------------------------------------------------------------------

// `is_valid_id`, `key`, `dev_key`, and `token_secret_name` are GENERATED from
// contract/spec.toml into contract_gen.rs (imported above) — THE single source
// shared with the Python tier. Charset [a-z0-9-], length 1..=64; excluding `/` is
// LOAD-BEARING (blocks a `device_id="../armed"` cross-tenant key collision). Pure +
// in-memory (no I/O), so they're safe on the fail-closed safety paths.

/// SAFETY-path resolution of a tenancy id: a valid id passes through; an invalid
/// one degrades to `"default"` plus a `log_evt` warn. Safety paths must NEVER
/// reject (a 400 here could break a real trip) — only admin/CRUD routes (Step 3)
/// reject with 400. The raw id is escaped (`escape_default`) before logging to
/// neutralize log-injection (newlines / control bytes).
///
/// NOTE: resolving-to-`default` on a safety path is safe ONLY while `default` is
/// the sole provisioned garden (Steps 1–2). At multi-garden (Step 3+), the
/// per-garden token keyed by the *claimed* `garden_id` (RFC §5) is what prevents
/// one garden reading another's state — not this fallback.
fn resolve_safety_id<'a>(raw: &'a str, trace_id: &str, component: &str, field: &str) -> &'a str {
    if is_valid_id(raw) {
        raw
    } else {
        log_evt(
            trace_id,
            component,
            "sanitize_id",
            "warn",
            &format!(
                "invalid {}='{}' -> fallback 'default'",
                field,
                raw.escape_default()
            ),
        );
        "default"
    }
}

// `key` / `dev_key` (the garden_state KV key grammar: default -> legacy flat keys,
// else g/<gid>/<name> and g/<gid>/dev/<did>/<name>) are generated -> contract_gen.

// ---------------------------------------------------------------------------
// Per-garden auth (Step 3, RFC §5). A per-garden bearer token in the Secret
// Store, ENFORCE-IFF-TOKEN: the `default` garden is NEVER issued a token, so it
// stays tokenless for local dev/test; any other garden is authenticated against
// the token looked up under its OWN (claimed) id, which is exactly what stops a
// token for garden A being replayed against garden B (the real tenancy boundary).
//
// FAIL-CLOSED is sacred: there is NO secret lookup on `/api/status` (the 3 s
// heartbeat) at all. `/api/evidence` does ONE once-per-trip lookup (30 s budget,
// signed-off bend) and only for NON-default gardens, so the default path adds
// zero round-trips. `/api/control` + the admin/CRUD routes enforce synchronously.
// ---------------------------------------------------------------------------

/// Name of the Secret Store holding per-garden tokens. Created + populated by
/// `gp-provision` (the control plane); the edge only reads it.
const GARDEN_TOKENS_STORE: &str = "garden_tokens";
/// Header the Pi sends its per-garden token in (`hardware/client.py`). Aliased to the
/// generated contract constant so the literal lives in ONE place (the spec SSOT) — both
/// this module and `routes.rs` (which imports this alias) follow `contract/spec.toml`.
const GARDEN_AUTH_HEADER: &str = contract_gen::HEADER_AUTH;

// `token_secret_name` (the slash-free `g.<gid>.<slot>` Secret-Store key encoding,
// shared with gp-provision) is generated -> contract_gen.

/// Hard ceiling (bytes) on a buffered request body for the unauthenticated-reachable
/// POST paths that BUFFER into the wasm heap (`/api/evidence` -> ONNX inference,
/// `/api/telemetry` -> KV write, `/api/alert`). The `default` garden is tokenless, so
/// these are reachable WITHOUT a credential — without a cap a single large POST can OOM
/// the instance and (on evidence) burn inference cost (EDGE: body-size DoS). 8 MiB is far
/// above any real garden JPEG (the Pi pushes a few hundred KB) yet bounds the blast radius.
const MAX_BODY_BYTES: usize = 8 * 1024 * 1024;

/// Hard ceiling (bytes) on a SERIALIZED telemetry record before it is written to the
/// per-device `garden_state` KV key. The body itself is already bounded by
/// [`MAX_BODY_BYTES`], but telemetry is attacker-controlled JSON that is RE-READ + parsed
/// on hot paths (`/api/state` render, the `/api/evidence` rain veto) and is reachable
/// tokenlessly on the `default` garden via `X-Garden-Id: default`. 16 KiB is generous for
/// the handful of scalar fields the dashboard renders (temp/humidity/rain/soil/...) while
/// keeping the hot-path read cheap. Oversized -> 400 (the node should send compact telemetry).
const MAX_TELEMETRY_BYTES: usize = 16 * 1024;

/// PURE: should a buffered body be REJECTED (413) given the advertised `Content-Length`
/// (if any) and a `limit`? Returns `true` (reject) only when a present, parseable
/// Content-Length EXCEEDS the limit. A missing/garbage Content-Length returns `false`
/// here — the caller still hard-checks the ACTUAL byte length after buffering, so a lying
/// or absent header cannot bypass the ceiling (this is just the cheap pre-buffer screen).
/// Kept tiny + testable on the native target.
fn body_exceeds_limit(content_length: Option<&str>, limit: usize) -> bool {
    content_length
        .and_then(|v| v.trim().parse::<usize>().ok())
        .map(|n| n > limit)
        .unwrap_or(false)
}

/// PURE: is a telemetry record over `limit` bytes once serialized? Measures the SAME bytes
/// `write_telemetry` would persist (and the hot paths would re-read). A serialization error
/// is treated as too-large (reject) — we never persist a record we can't size. Native-testable.
fn telemetry_record_too_large(record: &serde_json::Value, limit: usize) -> bool {
    match serde_json::to_vec(record) {
        Ok(bytes) => bytes.len() > limit,
        Err(_) => true,
    }
}

/// Max chars kept from an `X-Trigger` marker before it is stored in the per-garden alarm log.
const MAX_TRIGGER_MARKER_CHARS: usize = 80;

/// PURE: trim + char-cap an OPTIONAL `X-Trigger` marker, dropping it if empty after trim. The
/// marker flows verbatim into `AlarmRecord.reason` in the single `alarm_log` KV doc, so capping
/// here bounds that doc against an attacker-set header. `chars().take()` is grapheme-safe (never
/// slices mid-codepoint). Returns `Some` iff the marker is present AND non-empty (the alarm gate
/// keys on PRESENCE — capping never empties a real marker, so the gate is unchanged).
fn cap_trigger_marker(raw: Option<&str>) -> Option<String> {
    raw.map(|s| {
        s.trim()
            .chars()
            .take(MAX_TRIGGER_MARKER_CHARS)
            .collect::<String>()
    })
    .filter(|s| !s.is_empty())
}

/// Current epoch seconds (wall clock). Used only by the viewer gate (off any
/// safety path); evidence/heartbeat timing uses `now_ms()` elsewhere.
fn now_secs() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

// ---------------------------------------------------------------------------
// SAFETY-DECISION + state cluster. The PURE decision cores (continue_mitigation,
// apply_control, heartbeat_continue, rain_should_suppress, node_liveness) now live in
// `mod safety` — a BEHAVIOR-PRESERVING move (byte-identical bodies), pinned by the
// unchanged unit tests below that still exercise them through `use safety::*`. What
// DELIBERATELY STAYS INLINE here is the flag / state / telemetry STORE I/O that feeds
// those decisions (read_flags/write_flags/config_flags/flags_from + build_state +
// the telemetry read/write): it is SACRED per the charter — no behavior/latency change
// to the fail-closed/heartbeat/rain-veto paths — and unlike pure logic it is bound to
// the host ABI + the request handlers, so there is no clean, low-risk seam to move it.
// ---------------------------------------------------------------------------

/// Parses a stored flag string ("true"/"false") into a bool, falling back to
/// `default` for `None` or any unrecognized value.
fn parse_flag(s: Option<String>, default: bool) -> bool {
    match s.as_deref() {
        Some("true") => true,
        Some("false") => false,
        _ => default,
    }
}

/// Read-only Config Store holding this service's OWN identity. Created + populated
/// by `gp-provision` for a dedicated per-garden service; absent on the shared
/// multi-garden service and the legacy `default` garden.
const GARDEN_CONFIG_STORE: &str = "garden_config";

/// The service's OWN garden id, baked in at provision time. Used ONLY as the
/// fallback when a request omits `X-Garden-Id` — i.e. a BROWSER hitting a
/// dedicated per-garden service (machines always send the header). A missing
/// store/key returns `None` so the caller falls back to `"default"`, leaving the
/// shared-service and legacy-default behavior unchanged.
fn read_default_garden_id() -> Option<String> {
    let cfg = ConfigStore::try_open(GARDEN_CONFIG_STORE).ok()?;
    cfg.try_get("default_garden_id")
        .ok()
        .flatten()
        .filter(|s| !s.is_empty())
}

/// The garden's display NAME, baked into `garden_config` at create-garden time
/// (`provision/cli.py`, beside `default_garden_id`). Read here so the shared header
/// shows the name server-side on the public dashboard with no async pop-in. Comes
/// ONLY from the service's own config — never a request header — so it can't be
/// steered cross-tenant (same invariant as `browser_garden_id`). Absent on gardens
/// provisioned before this key existed -> "" (header reserves the space regardless).
fn read_default_garden_name() -> String {
    ConfigStore::try_open(GARDEN_CONFIG_STORE)
        .ok()
        .and_then(|cfg| cfg.try_get("default_garden_name").ok().flatten())
        .unwrap_or_default()
}

/// PURE: fill the shared header partial (`PORTAL_HEADER_HTML`) with a garden name
/// (escaped) and mark the dashboard nav link active. The browser-facing dashboard
/// always starts on the Dashboard tab; its own JS re-points `active` to History on
/// `/history`. Mirrors `render_header` in hardware/portal.py. No I/O -> unit-testable.
fn fill_header_active(garden_name: &str, active_id: &str, view_only: bool) -> String {
    PORTAL_HEADER_HTML
        .replace("__GARDEN_NAME__", &html_escape(garden_name))
        .replace(
            "<!--NAV_LINKS-->",
            &contract_gen::render_nav(active_id, view_only),
        )
}

/// Full (admin-style) header — every nav link present. Test-only; the live edge
/// surfaces are view-only (see `dashboard_header_html` / `timelapse_header_html`).
#[cfg(test)]
fn fill_header(garden_name: &str) -> String {
    fill_header_active(garden_name, "nav-dashboard", false)
}

/// The shared header with this service's garden name read in from `garden_config`.
/// VIEW-ONLY: admin nav links are omitted server-side (this is the public edge).
fn dashboard_header_html() -> String {
    fill_header_active(&read_default_garden_name(), "nav-dashboard", true)
}

/// The shared header for the `/history` view (its nav link marked active server-side
/// so the active tab is correct without waiting on the dashboard JS to re-point it).
/// Same VIEW-ONLY dashboard page, just a different active nav link.
fn history_header_html() -> String {
    fill_header_active(&read_default_garden_name(), "nav-history", true)
}

/// The shared header for the Timelapse page (its nav link marked active). VIEW-ONLY.
fn timelapse_header_html() -> String {
    fill_header_active(&read_default_garden_name(), "nav-timelapse", true)
}

/// The shared header for the Help page (its nav link marked active). VIEW-ONLY.
fn help_header_html() -> String {
    fill_header_active(&read_default_garden_name(), "nav-help", true)
}

/// The shared header for the single-event detail page. NOT a nav destination, so History
/// is marked active (the browse surface it belongs to) for orientation. VIEW-ONLY.
fn event_header_html() -> String {
    fill_header_active(&read_default_garden_name(), "nav-history", true)
}

/// The shared header for the Alarms page (its nav link marked active). VIEW-ONLY.
fn alarms_header_html() -> String {
    fill_header_active(&read_default_garden_name(), "nav-alarms", true)
}

/// Choose a request's garden id: the `X-Garden-Id` header wins (machines always
/// send it); a header-less request (a browser) falls back to the service's
/// configured default garden, then to the global `"default"`. `fallback` is a
/// closure so the Config Store is read LAZILY — never on the header-present hot
/// path. Pure given its inputs, so it is unit-tested directly.
fn resolve_request_garden_id<F>(header: Option<&str>, fallback: F) -> String
where
    F: FnOnce() -> Option<String>,
{
    match header {
        Some(h) => h.to_string(),
        None => fallback().unwrap_or_else(|| "default".to_string()),
    }
}

/// Garden id for BROWSER-facing surfaces (the dashboard HTML + its read APIs).
/// Unlike the machine routes (where the Pi legitimately sends `X-Garden-Id`), a
/// browser must NOT be able to STEER which garden it reads via a client-supplied
/// header — on a shared service that would be a cross-tenant read. So this IGNORES
/// the request header entirely and uses ONLY the service's own baked-in identity
/// (`garden_config`/`read_default_garden_id`), falling back to `"default"`. Other
/// gardens on a shared service are reachable only via the token-gated admin routes.
fn browser_garden_id() -> String {
    read_default_garden_id().unwrap_or_else(|| "default".to_string())
}

/// Reads `(armed, override_stop)` from the read-only `device_config` Config Store,
/// with safe defaults (`armed = true`, `override_stop = false`). This is the legacy
/// authority used when the writable `garden_state` KV Store is unprovisioned.
fn config_flags() -> (bool, bool) {
    // Use try_open/try_get: the infallible `ConfigStore::open`/`get` PANIC if the
    // store is unlinked (e.g. a cloud deploy that doesn't provision device_config),
    // which would 500 the heartbeat. A missing store/key degrades to the safe
    // defaults (armed at rest, not stopped) — identical to the absent-key behavior.
    match ConfigStore::try_open("device_config") {
        Ok(config) => {
            let armed = parse_flag(config.try_get("is_armed").ok().flatten(), true);
            let override_stop = parse_flag(config.try_get("override_stop").ok().flatten(), false);
            (armed, override_stop)
        }
        Err(_) => (true, false),
    }
}

/// Reads `(armed, override_stop)` for garden `gid` from an open `garden_state`
/// handle, using the supplied Config Store defaults when a key is simply absent.
/// Keys are derived via the shared `key` helper (default garden -> legacy flat).
fn flags_from(store: &KVStore, cfg: (bool, bool), gid: &str) -> (bool, bool) {
    let armed = parse_flag(store.lookup_str(&key(gid, "armed")).ok().flatten(), cfg.0);
    let override_stop = parse_flag(
        store.lookup_str(&key(gid, "override_stop")).ok().flatten(),
        cfg.1,
    );
    (armed, override_stop)
}

/// Lenient read of `(armed, override_stop)` for the CONTROL/DISPLAY path of garden
/// `gid` (a general garden, not just the default). NOT the Pi heartbeat — that path is
/// fail-closed independently (see `handle_status`). The `default` garden falls back to
/// the Config-Store authority (the system is shown ARMED at rest); a non-default garden
/// to (disarmed, not-stopped) so a fresh garden's first `active` behaves intuitively. A
/// missing/erroring store degrades to those defaults.
fn read_garden_control_flags(gid: &str) -> (bool, bool) {
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

/// Heartbeat flags from an open `garden_state` handle, biasing each *unreadable*
/// flag to its fail-closed value (`None`). A key that is merely absent uses the
/// Config Store default (a real configured value); only a lookup ERROR yields
/// `None`.
fn heartbeat_flags_from(
    store: &KVStore,
    cfg: (bool, bool),
    gid: &str,
) -> (Option<bool>, Option<bool>) {
    // In-memory key derivation only (the shared `key` helper). NO new I/O is added
    // to this fail-closed path: still exactly the two lookups it has always done.
    let armed = match store.lookup_str(&key(gid, "armed")) {
        Ok(v) => Some(parse_flag(v, cfg.0)),
        Err(_) => None,
    };
    let override_stop = match store.lookup_str(&key(gid, "override_stop")) {
        Ok(v) => Some(parse_flag(v, cfg.1)),
        Err(_) => None,
    };
    (armed, override_stop)
}

/// Persists `(armed, override_stop)` for garden `gid` to the writable
/// `garden_state` KV Store. Keys are derived via the SAME `key` helper the reads
/// use (default garden -> legacy flat), so a STOP write and the heartbeat read can
/// never target different keys. Errors if the store is missing or an insert fails.
fn write_flags(armed: bool, override_stop: bool, gid: &str) -> Result<(), Error> {
    let mut store = KVStore::open(STATE_STORE)?
        .ok_or_else(|| Error::msg("KV Store 'garden_state' not found"))?;
    store.insert(&key(gid, "armed"), if armed { "true" } else { "false" })?;
    store.insert(
        &key(gid, "override_stop"),
        if override_stop { "true" } else { "false" },
    )?;
    Ok(())
}

/// Per-event smart-Stop state (RFC: three-mode + per-event abort). Two per-garden
/// STRING fields in `garden_state`, keyed via the SAME `key` helper as the flags:
///   * `abort_cid`         — the cid of the spray the user aborted via Stop (one-shot;
///                           empty = none). Read on the heartbeat, ANDed in as a COMFORT
///                           gate (never fail-closed — a read miss degrades to "no abort").
///   * `last_mitigate_cid` — the cid of the most recent `mitigate` decision (written by
///                           the evidence handler). Stop targets THIS so the client need
///                           not pass a cid.
/// `default` garden uses the legacy flat keys ("abort_cid" / "last_mitigate_cid").

/// Reads `abort_cid` for `gid` from an open handle. IMPORTANT: this is the COMFORT
/// abort, NOT a safety gate. A lookup ERROR or absent key both degrade to `""` (no
/// abort), so a transient store miss can never SUPPRESS a legitimate spray — the
/// armed/override fail-closed floor + the Pi watchdog remain the safety guarantees.
/// (Mirrors the rain-veto "stale/absent -> no veto" framing.)
fn read_abort_cid_from(store: &KVStore, gid: &str) -> String {
    store
        .lookup_str(&key(gid, "abort_cid"))
        .ok()
        .flatten()
        .unwrap_or_default()
}

/// Reads `last_mitigate_cid` for `gid` (the live spray's cid that Stop targets). An
/// error/absent key -> `""`. Used by the control handler to resolve a `stop` to the
/// current live spray without the client passing a cid.
fn read_last_mitigate_cid(gid: &str) -> String {
    match KVStore::open(STATE_STORE) {
        Ok(Some(store)) => store
            .lookup_str(&key(gid, "last_mitigate_cid"))
            .ok()
            .flatten()
            .unwrap_or_default(),
        _ => String::new(),
    }
}

/// Persists `abort_cid` for `gid` (empty string = cleared). Used by `stop` (set to the
/// live spray cid) and `resume` (clear). Errors propagate so the control handler can
/// surface a 503 — but the heartbeat path treats an unreadable abort_cid as empty, so a
/// failed write is fail-SAFE (worst case: a Stop the user pressed doesn't take, the Pi's
/// own watchdog still bounds the spray).
fn write_abort_cid(gid: &str, cid: &str) -> Result<(), Error> {
    let mut store = KVStore::open(STATE_STORE)?
        .ok_or_else(|| Error::msg("KV Store 'garden_state' not found"))?;
    store.insert(&key(gid, "abort_cid"), cid)?;
    Ok(())
}

/// Records a NEW `mitigate` decision: writes `last_mitigate_cid = <event cid>` so a
/// later Stop can target THIS live spray server-side, and CLEARS any stale `abort_cid`
/// so an abort from a PRIOR spray can never linger and suppress this new one. Best-effort
/// (one open, two inserts): the caller swallows errors so this never affects the
/// Pi-facing evidence response or the fail-closed path.
fn record_mitigate_cid(gid: &str, cid: &str) -> Result<(), Error> {
    let mut store = KVStore::open(STATE_STORE)?
        .ok_or_else(|| Error::msg("KV Store 'garden_state' not found"))?;
    store.insert(&key(gid, "last_mitigate_cid"), cid)?;
    // Auto-resume: a fresh spray clears the one-shot abort so a stale Stop never carries over.
    store.insert(&key(gid, "abort_cid"), "")?;
    Ok(())
}

/// Reads and parses the `latest_event` for garden `gid` / device `did` from an open
/// handle (key via the shared `dev_key` helper; default garden -> legacy global key),
/// or returns JSON `null`.
fn latest_event_from(store: &KVStore, gid: &str, did: &str) -> serde_json::Value {
    store
        .lookup_str(&dev_key(gid, did, "latest_event"))
        .ok()
        .flatten()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or(serde_json::Value::Null)
}

/// Current wall-clock as epoch milliseconds (0 if the clock is before the epoch).
fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

// ---------------------------------------------------------------------------
// Node liveness + environment telemetry (heartbeat). The garden node POSTs a
// telemetry blob to `/api/telemetry`; the edge STAMPS its own `last_seen_ms`
// (receipt time, immune to a skewed device clock) and stores it per-device. The
// dashboard then derives ONLINE/OFFLINE from that stamp. This is observability +
// a comfort optimization (the rain veto), NEVER a fail-closed safety input — a
// down node already means the N/C solenoid stays shut.
// ---------------------------------------------------------------------------

// NODE_OFFLINE_AFTER_SECS (150 s; ~2.5 missed ~60 s beats -> DOWN) and
// RAIN_TELEMETRY_FRESH_SECS (600 s; rain readings older than this are ignored by the
// veto) are generated -> contract_gen (shared with the Pi gateway's thresholds).

/// Reads + parses the per-device `latest_telemetry` blob from an open handle (key via
/// the shared `dev_key` helper), or JSON `null`.
fn latest_telemetry_from(store: &KVStore, gid: &str, did: &str) -> serde_json::Value {
    store
        .lookup_str(&dev_key(gid, did, "latest_telemetry"))
        .ok()
        .flatten()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or(serde_json::Value::Null)
}

/// Best-effort open-and-read of the latest telemetry (used off the safety path by the
/// rain veto). Any store/lookup error degrades to `null` (no veto applied).
fn read_latest_telemetry(gid: &str, did: &str) -> serde_json::Value {
    match KVStore::open(STATE_STORE) {
        Ok(Some(store)) => latest_telemetry_from(&store, gid, did),
        _ => serde_json::Value::Null,
    }
}

/// Builds the dashboard [`NodeStatus`] from a telemetry blob + now. The blob's
/// edge-stamped `last_seen_ms` drives the liveness derivation.
fn build_node_status(telemetry: serde_json::Value, now_ms: u64) -> NodeStatus {
    let last_seen_ms = telemetry.get("last_seen_ms").and_then(|v| v.as_u64());
    let (online, seconds_since) = node_liveness(last_seen_ms, now_ms);
    NodeStatus {
        online,
        last_seen_ms,
        seconds_since,
        telemetry,
    }
}

/// Stamps the edge-receipt `last_seen_ms` onto an incoming telemetry body so liveness
/// is judged by when the EDGE heard from the node, not the device's own clock. A
/// non-object body is wrapped under `raw` so the stored record is always an object.
fn build_telemetry_record(incoming: serde_json::Value, now_ms: u64) -> serde_json::Value {
    let mut obj = match incoming {
        serde_json::Value::Object(m) => m,
        other => {
            let mut m = serde_json::Map::new();
            m.insert("raw".to_string(), other);
            m
        }
    };
    obj.insert("last_seen_ms".to_string(), serde_json::json!(now_ms));
    serde_json::Value::Object(obj)
}

/// Persists the stamped telemetry record per-device to `garden_state` (same
/// `dev_key` helper the reads use). Errors if the store is missing or insert fails.
fn write_telemetry(record: &serde_json::Value, gid: &str, did: &str) -> Result<(), Error> {
    let mut store = KVStore::open(STATE_STORE)?
        .ok_or_else(|| Error::msg("KV Store 'garden_state' not found"))?;
    store.insert(
        &dev_key(gid, did, "latest_telemetry"),
        serde_json::to_vec(record)?,
    )?;
    Ok(())
}

/// Builds the dashboard state snapshot, opening `garden_state` ONCE (a single host
/// round-trip serves both the flags and `latest_event`). Used by `/api/state` and
/// the `/api/control` response.
/// The control-plane-written device list for a garden (the edge only reads it).
/// Key parity with `admin_serve_registry`; absent/unparseable -> empty.
fn read_devices(store: &KVStore, gid: &str) -> Vec<Device> {
    store
        .lookup_str(&format!("index/g/{}/devices", gid))
        .ok()
        .flatten()
        .and_then(|s| serde_json::from_str::<DevicesRegistry>(&s).ok())
        .map(|r| r.devices)
        .unwrap_or_default()
}

/// Freshest `(event, telemetry)` across a garden's devices, for the single-summary
/// dashboard cards (Last Event + Node Health). The `default` garden keeps the
/// LEGACY FLAT keys (one logical device, `dev_key` ignores the device). A NAMED
/// garden scans its registry devices and picks the newest event (by `ts`) and the
/// newest telemetry (by `last_seen_ms`) — a garden-level "what last happened /
/// latest conditions" without assuming a single device. The per-camera detail is
/// the gallery's job (`/api/gadget`); this is just the at-a-glance summary.
fn garden_summary(store: &KVStore, gid: &str) -> (serde_json::Value, serde_json::Value) {
    if gid == "default" {
        return (
            latest_event_from(store, "default", "default"),
            latest_telemetry_from(store, "default", "default"),
        );
    }
    let (mut best_ev, mut best_ev_ts) = (serde_json::Value::Null, i64::MIN);
    let (mut best_tel, mut best_tel_ts) = (serde_json::Value::Null, i64::MIN);
    for d in read_devices(store, gid) {
        let ev = latest_event_from(store, gid, &d.device_id);
        if let Some(ts) = ev.get("ts").and_then(|v| v.as_i64()) {
            if ts > best_ev_ts {
                best_ev_ts = ts;
                best_ev = ev;
            }
        }
        let tel = latest_telemetry_from(store, gid, &d.device_id);
        if let Some(ts) = tel.get("last_seen_ms").and_then(|v| v.as_i64()) {
            if ts > best_tel_ts {
                best_tel_ts = ts;
                best_tel = tel;
            }
        }
    }
    (best_ev, best_tel)
}

/// Builds the dashboard state for garden `gid`: its flags + the freshest
/// event/telemetry across its devices (`garden_summary`). The `default` garden
/// resolves to the legacy flat keys, so single-tenant behavior is unchanged.
fn build_state(gid: &str) -> StateResponse {
    let cfg = config_flags();
    let (armed, override_stop, latest_event, telemetry) = match KVStore::open(STATE_STORE) {
        Ok(Some(store)) => {
            let (a, o) = flags_from(&store, cfg, gid);
            let (ev, tel) = garden_summary(&store, gid);
            (a, o, ev, tel)
        }
        _ => (
            cfg.0,
            cfg.1,
            serde_json::Value::Null,
            serde_json::Value::Null,
        ),
    };
    StateResponse {
        mode: derive_mode(armed, override_stop).as_str(),
        armed,
        override_stop,
        continue_mitigation: continue_mitigation(armed, override_stop),
        latest_event,
        node: build_node_status(telemetry, now_ms()),
    }
}

/// Best-effort publish of the latest evidence image + event JSON to `garden_state`
/// for the dashboard. The caller MUST treat failures as non-fatal so the Pi-facing
/// evidence response (and the fail-closed 503 path) is never affected by KV state.
fn publish_evidence(
    image: &[u8],
    species: &str,
    confidence: f32,
    action: &str,
    reason: Option<&str>,
    gid: &str,
    did: &str,
    archive_key: &str,
) -> Result<(), Error> {
    let ts = now_ms();
    // `key` is the durable archive object key for THIS frame (same key the FOS PUT uses),
    // so dashboard surfaces reading `latest_event` (/api/state, /api/gadget) can deep-link
    // to the event detail page (/event?key=...). It's the only field beyond the live
    // species/confidence/action/reason/ts snapshot.
    let event = serde_json::json!({
        "species": species,
        "confidence": confidence,
        "action": action,
        "reason": reason,
        "ts": ts,
        "key": archive_key,
    });

    // Per-device keys via the shared `dev_key` helper (default garden -> legacy
    // global keys, last-writer-wins as today). This whole publish is best-effort and
    // its caller swallows errors, so it never affects the Pi-facing evidence response.
    let mut store = KVStore::open(STATE_STORE)?
        .ok_or_else(|| Error::msg("KV Store 'garden_state' not found"))?;
    store.insert(&dev_key(gid, did, "latest_image"), image)?;
    store.insert(
        &dev_key(gid, did, "latest_event"),
        serde_json::to_vec(&event)?,
    )?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Alarms — a TRIGGER device's evidence push becomes an alarm (the role flag is the SOLE gate
// today; see `should_alarm`). Best-effort, mirroring `publish_evidence`: the caller swallows
// errors so the Pi-facing evidence response (+ the fail-closed path) is never affected. The
// pure model / recommendation / retention math is in `mod alarms`; only the KV I/O is here.
// ---------------------------------------------------------------------------

/// The `garden_state` key holding the per-garden alarm log (default -> legacy flat key).
pub(crate) fn alarm_log_key(gid: &str) -> String {
    key(gid, "alarm_log")
}

/// Load the alarm log for `gid` from an open handle (absent/unparseable -> empty).
pub(crate) fn load_alarm_log(store: &KVStore, gid: &str) -> alarms::AlarmLog {
    store
        .lookup_str(&alarm_log_key(gid))
        .ok()
        .flatten()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default()
}

/// Does this device create an alarm on an evidence push? TODAY: iff it can trigger (cameras
/// default to confirm-only, so nothing alarms until a device is opted in). ISOLATED on purpose
/// — future per-detector trigger conditions (radar motion-dwell, schedule windows, confidence
/// floors) slot in HERE without touching the alarm record, the page, or retention.
fn should_alarm(dev: &Device) -> bool {
    dev.can_trigger_alarm
}

/// Whether a push should be recorded as an alarm: the device must be a trigger AND the push must
/// carry a trigger marker (a routine cadence frame from a trigger camera has no marker, so it stays
/// History-only). Pure so it's unit-testable without the KV host.
fn alarm_should_record(dev: &Device, triggered: bool) -> bool {
    triggered && should_alarm(dev)
}

/// The alarm's human reason: the trigger cause first ("motion 4.2% x3"), with any action-
/// suppression note (e.g. "rain") appended so both are surfaced on the alarm. Pure.
fn compose_alarm_reason(triggered: Option<&str>, reason: Option<&str>) -> Option<String> {
    match (triggered, reason) {
        (Some(t), Some(r)) => Some(format!("{}; {}", t, r)),
        (Some(t), None) => Some(t.to_string()),
        (None, r) => r.map(String::from),
    }
}

/// If the pushing device is a TRIGGER **and** this push carries a trigger marker, upsert an alarm
/// for this capture. The marker (`triggered`) is what separates a genuine trigger EVENT (a camera's
/// confirmed motion, a radar detection) from a trigger device's routine cadence frame — without it,
/// a trigger camera's History captures would all become alarms. `alarm_id = batch||cid` so a
/// multi-angle set dedups to ONE alarm; a later push in the same batch keeps the STRONGEST read
/// (mitigate beats none, else higher confidence) and PRESERVES any human tag. Caps the log at
/// `ALARM_LOG_CAP` (oldest trimmed) as a belt to the retention cron. Best-effort; returns whether
/// an alarm was recorded (for the log line).
#[allow(clippy::too_many_arguments)]
fn record_alarm_if_triggered(
    gid: &str,
    did: &str,
    cid: &str,
    species: &str,
    confidence: f32,
    action: &str,
    reason: Option<&str>,
    triggered: Option<&str>,
    object_key: &str,
    batch: &str,
) -> Result<bool, Error> {
    // A push only becomes an alarm when it is marked as a trigger event (see HEADER_TRIGGER).
    if triggered.is_none() {
        return Ok(false);
    }
    let mut store = KVStore::open(STATE_STORE)?
        .ok_or_else(|| Error::msg("KV Store 'garden_state' not found"))?;
    let is_trigger = read_devices(&store, gid)
        .iter()
        .find(|d| d.device_id == did)
        .map(|d| alarm_should_record(d, triggered.is_some()))
        .unwrap_or(false);
    if !is_trigger {
        return Ok(false);
    }
    // The alarm's human reason = the trigger cause ("motion 4.2% x3"), with any action-suppression
    // note (e.g. "rain") appended so both are surfaced on the alarm.
    let alarm_reason: Option<String> = compose_alarm_reason(triggered, reason);
    // Derive the alarm id from the SAME values the read path (handle_alarm_get) parses out of the
    // archive key — both the batch and cid in the key are slug_for_key'd, so parsing object_key
    // here guarantees record-time and read-time ids match (don't use the raw header values).
    let id = archive::parse_archive_key(gid, object_key)
        .map(|ev| {
            alarms::alarm_id(
                ev.get("batch").and_then(|v| v.as_str()).unwrap_or(""),
                ev.get("cid").and_then(|v| v.as_str()).unwrap_or(""),
            )
        })
        .unwrap_or_else(|| alarms::alarm_id(batch, cid));
    let confpct = (confidence * 100.0).round().clamp(0.0, 100.0) as u32;
    let mut log = load_alarm_log(&store, gid);
    let prior_tag = log.get(&id).and_then(|a| a.tag.clone());
    // Within a batch, keep the existing primary unless the new read is STRONGER.
    let keep_existing = log
        .get(&id)
        .map(|cur| {
            let cur_strong = cur.action == "mitigate";
            let new_strong = action == "mitigate";
            if new_strong != cur_strong {
                !new_strong
            } else {
                confpct <= cur.confidence
            }
        })
        .unwrap_or(false);
    if !keep_existing {
        log.insert(
            id.clone(),
            alarms::AlarmRecord {
                id: id.clone(),
                ts: now_ms(),
                trigger_device: did.to_string(),
                key: object_key.to_string(),
                batch: batch.to_string(),
                species: species.to_string(),
                confidence: confpct,
                action: action.to_string(),
                reason: alarm_reason,
                tag: prior_tag,
            },
        );
    }
    if log.len() > alarms::ALARM_LOG_CAP {
        alarms::prune_by_count(&mut log, alarms::ALARM_LOG_CAP);
    }
    store.insert(&alarm_log_key(gid), serde_json::to_vec(&log)?)?;
    Ok(true)
}

// ---------------------------------------------------------------------------
// CDN-fronted evidence archive (Step 3, RFC §2) — the FOS/CDN data layer (creds,
// SigV4 GET/PUT/DELETE/LIST, CDN-first reads, object-key build/parse, retention
// math) lives in `mod archive` (extracted Phase 5a; behavior unchanged). The route
// HANDLERS (handle_archive_*) stay here in main.rs.
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Phase 0 telemetry: cross-tier trace correlation (observe-only / log-only).
// These helpers are pure and are NEVER consulted by a fail-closed branch — they
// only decide which id to echo back and log so a trip can be reconstructed with
// `grep <trace-id>` across the Pi SQLite DB and the edge log.
// ---------------------------------------------------------------------------

/// True for a plausible trace id: 8–64 chars of ASCII alphanumerics or '-'.
/// Accepts both the Pi's 16-hex id and a minted `edge-…` fallback while rejecting
/// whitespace/punctuation/control/non-ASCII bytes (avoids log-injection garbage).
/// Used only to decide whether to accept the Pi's `X-Garden-Trace-Id` or mint one.
fn is_valid_trace_id(s: &str) -> bool {
    let n = s.len();
    (8..=64).contains(&n) && s.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
}

/// Mints a log-only edge fallback id when the Pi sent none (or garbage). Derived
/// from the wall clock; uniqueness is best-effort and only matters for logs.
fn mint_edge_id() -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    format!("edge-{:016x}", nanos as u64)
}

/// Resolves the request's trace id: the Pi's header if plausible, else a minted
/// `edge-…` fallback. Pure and log-only — observe, never decide.
fn extract_trace_id(header: Option<&str>) -> String {
    match header {
        Some(s) if is_valid_trace_id(s) => s.to_string(),
        _ => mint_edge_id(),
    }
}

/// Structured, log-only action record (one line to stdout/stderr). This is the
/// edge's robust observability surface: every meaningful action calls it with the
/// request's `trace_id` so a whole trip greps out across both tiers. It is
/// OBSERVE-ONLY — it never alters control flow, never touches KV/Config/network,
/// and is safe on the safety paths (`/api/status`, `/api/evidence`) because the
/// platform batches stdout/stderr off-request. Errors/warns go to stderr (journald
/// separation); everything else to stdout. `trace_id` is "boot" for startup work.
///
/// POLICY: edge telemetry is stdout/stderr ONLY — it reaches the managed real-time
/// stream (`fastly log-tail`) + the local Viceroy console, and is NEVER persisted to
/// the cloud. The durable record lives Pi-local (SQLite). The former S3->FOS logging
/// endpoint ("Garden Protector Telemetry") was a ~1-obj/sec firehose; it's been
/// deleted (7b5f75a) and the inert `Endpoint::from_name` write removed here so the
/// name can't silently resurrect cloud logging.
fn log_evt(trace_id: &str, component: &str, op: &str, outcome: &str, detail: &str) {
    let line = format!(
        "[GP] trace={} component={} op={} outcome={} detail={}",
        trace_id, component, op, outcome, detail
    );
    // Real-time stream (`fastly log-tail`) + the local Viceroy console. No cloud sink.
    if outcome == "error" || outcome == "warn" {
        eprintln!("{}", line);
    } else {
        println!("{}", line);
    }
}

#[fastly::main]
fn main(req: Request) -> Result<Response, Error> {
    let started = SystemTime::now();

    // Read the cross-tier correlation id + forward-compat identity (default
    // "default") from the request BEFORE the router moves it. Observe-only: this
    // is echoed back and logged, never consulted by any handler or fail-closed
    // branch, and adds no KV/Config/network/inference I/O to any safety path.
    let trace_id = extract_trace_id(req.get_header_str(contract_gen::HEADER_TRACE_ID));
    // Machines (Pi gateway/pushers) always send X-Garden-Id. A header-less request
    // is a BROWSER hitting the dashboard; on a dedicated per-garden service we fall
    // back to the service's OWN baked-in garden id so the viewer gate + garden-scoped
    // reads resolve to the right garden instead of "default". The fallback is LAZY —
    // the Config Store is only opened when the header is absent, so the hot machine
    // path (3s heartbeat) never pays for it.
    let garden_id = resolve_request_garden_id(
        req.get_header_str(contract_gen::HEADER_GARDEN_ID),
        read_default_garden_id,
    );
    let device_id = req
        .get_header_str(contract_gen::HEADER_DEVICE_ID)
        .unwrap_or("default")
        .to_string();
    let node_id = req
        .get_header_str(contract_gen::HEADER_NODE_ID)
        .unwrap_or("default")
        .to_string();
    let method = req.get_method().clone();
    let path = req.get_path().to_string();

    log_evt(
        &trace_id,
        "http",
        "request",
        "begin",
        &format!(
            "{} {} garden={} device={} node={}",
            method.as_str(),
            path,
            garden_id,
            device_id,
            node_id
        ),
    );

    // Router logic. Each handler logs its own actions via `log_evt`; `req` moves
    // into the handler exactly as before, fail-closed branches untouched.
    let result = match (&method, path.as_str()) {
        (&Method::GET, "/") | (&Method::GET, "/admin") => {
            serve_dashboard(req, &trace_id, DashboardNav::Dashboard)
        }
        (&Method::GET, "/history") => serve_dashboard(req, &trace_id, DashboardNav::History),
        (&Method::GET, "/login") => serve_login(req, &trace_id),
        (&Method::GET, "/timelapse") => serve_timelapse(req, &trace_id),
        (&Method::GET, "/event") => serve_event(req, &trace_id),
        (&Method::GET, "/alarms") => serve_alarms(req, &trace_id),
        (&Method::GET, "/help") => serve_help(req, &trace_id),
        (&Method::GET, "/favicon.ico") => serve_favicon(&trace_id),
        (&Method::GET, "/static/app.css") => serve_static(
            &trace_id,
            "app.css",
            "text/css; charset=utf-8",
            APP_CSS,
            req.get_header_str("If-None-Match"),
        ),
        (&Method::GET, "/static/gp.css") => serve_static(
            &trace_id,
            "gp.css",
            "text/css; charset=utf-8",
            GP_CSS,
            req.get_header_str("If-None-Match"),
        ),
        (&Method::GET, "/static/gp.js") => serve_static(
            &trace_id,
            "gp.js",
            "application/javascript; charset=utf-8",
            GP_JS,
            req.get_header_str("If-None-Match"),
        ),
        (&Method::POST, "/api/viewer-login") => handle_viewer_login(req, &trace_id),
        (&Method::POST, "/api/evidence") => handle_evidence(req, &trace_id, &garden_id, &device_id),
        (&Method::POST, "/api/telemetry") => {
            handle_telemetry(req, &trace_id, &garden_id, &device_id)
        }
        (&Method::POST, "/api/alert") => handle_alert(req, &trace_id, &garden_id),
        (&Method::GET, "/api/status") => handle_status(req, &trace_id, &garden_id),
        (&Method::GET, "/api/state") => handle_state(req, &trace_id),
        (&Method::GET, "/api/snapshot") => handle_snapshot(req, &trace_id),
        (&Method::GET, "/api/cameras") => handle_cameras(req, &trace_id),
        (&Method::GET, "/api/gadget") => handle_gadget(req, &trace_id),
        (&Method::GET, "/api/archive") => handle_archive_day(req, &trace_id),
        (&Method::GET, "/api/archive/days") => handle_archive_days(req, &trace_id),
        (&Method::GET, "/api/archive/image") => handle_archive_image(req, &trace_id),
        (&Method::POST, "/api/archive/prune") => handle_archive_prune(req, &trace_id),
        (&Method::POST, "/api/archive/wipe") => handle_archive_wipe(req, &trace_id),
        (&Method::POST, "/api/control") => handle_control(req, &trace_id),
        // Alarms: the list + per-species recommendations (GET /api/alarms), one alarm for an
        // event key (GET /api/alarm), and tagging/management. Tagging a NEW alarm is viewer-
        // gated; CHANGING a tag, DELETING an alarm, and the retention prune/wipe require a valid
        // garden token (admin) — enforced in-handler (the Pi portal forwards the token).
        (&Method::GET, "/api/alarms") => handle_alarms(req, &trace_id),
        (&Method::GET, "/api/alarm") => handle_alarm_get(req, &trace_id),
        (&Method::POST, "/api/alarm-tag") => handle_alarm_tag(req, &trace_id),
        (&Method::POST, "/api/alarm/delete") => handle_alarm_delete(req, &trace_id),
        (&Method::POST, "/api/alarms/prune") => handle_alarms_prune(req, &trace_id),
        (&Method::POST, "/api/alarms/wipe") => handle_alarms_wipe(req, &trace_id),
        // Path-based admin/CRUD routes (RFC §3) are matched AFTER the hot exact-match
        // arms above so the Pi-facing safety paths stay a single cheap comparison.
        _ => match parse_admin_route(&method, path.as_str()) {
            Some(route) => handle_admin(req, &trace_id, route),
            None => {
                log_evt(
                    &trace_id,
                    "router",
                    "dispatch",
                    "warn",
                    &format!("no route for {} {} -> 404", method.as_str(), path),
                );
                Ok(Response::from_status(StatusCode::NOT_FOUND).with_body("Endpoint not found"))
            }
        },
    };

    // Post-decorate the successful response with the trace id and emit one
    // correlation summary line (status + duration) to stdout. An `Err` propagates
    // untouched.
    result.map(|resp| {
        let status = resp.get_status().as_u16();
        let dur_ms = started.elapsed().map(|d| d.as_secs_f64() * 1000.0).unwrap_or(0.0);
        println!(
            "[TRACE] trace_id={} garden={} device={} node={} method={} path={} status={} dur_ms={:.2}",
            trace_id, garden_id, device_id, node_id, method.as_str(), path, status, dur_ms
        );
        resp.with_header(contract_gen::HEADER_TRACE_ID, trace_id.as_str())
    })
}

fn handle_evidence(
    mut req: Request,
    trace_id: &str,
    garden_id: &str,
    device_id: &str,
) -> Result<Response, Error> {
    // Resolve tenancy ids for KV keying (in-memory only). A bad id falls back to
    // "default" + a warn — never a reject, so a malformed header can't break a trip.
    let gid = resolve_safety_id(garden_id, trace_id, "evidence", "garden_id");
    let did = resolve_safety_id(device_id, trace_id, "evidence", "device_id");

    // Per-garden auth (RFC §5, enforce-iff-token). This is the ONE sanctioned
    // secret lookup on a safety path: it runs once per trip (the evidence POST has a
    // 30 s Pi budget, NOT the 3 s heartbeat) and ONLY for non-default gardens — the
    // `default` garden is tokenless, so the local/single-tenant trip path adds ZERO
    // round-trips and is byte-for-byte unchanged. A bad/absent token on a provisioned
    // garden -> 401, which the Pi treats as fail-closed (disarm). Done BEFORE
    // inference so an unauthenticated caller can't even spend the model.
    let auth_header = req
        .get_header_str(GARDEN_AUTH_HEADER)
        .map(|s| s.to_string());

    // Multi-angle correlation (both OPTIONAL; absent -> edge clock + ungrouped, so older
    // Pis keep working). The Pi stamps every camera it pushes in one tick with the SAME
    // capture timestamp + batch id, so the archive groups them as one moment instead of
    // drifting apart at the edge's per-frame receive time. Consumed by archive_evidence.
    let capture_ts = req
        .get_header_str(contract_gen::HEADER_CAPTURE_TS)
        .and_then(|s| s.trim().parse::<i64>().ok());
    let capture_batch = req
        .get_header_str(contract_gen::HEADER_CAPTURE_BATCH)
        .map(|s| s.to_string());
    // Alarm trigger marker (OPTIONAL): present only when this push is a genuine trigger EVENT
    // (e.g. a camera's confirmed motion). Its value is a short human reason. A trigger device's
    // routine cadence frames carry no marker, so they archive to History without alarming.
    // Cap the marker at capture so an attacker-set header can't bloat the single per-garden
    // `alarm_log` KV doc (it flows verbatim into AlarmRecord.reason, re-serialized in full on
    // every record/read/tag/prune). Presence still gates the alarm (role can_trigger_alarm AND
    // marker) — capping never empties a real marker, so the gate is unchanged. See
    // [`cap_trigger_marker`].
    let trigger_marker = cap_trigger_marker(req.get_header_str(contract_gen::HEADER_TRIGGER));

    if garden_auth_outcome(gid, auth_header.as_deref()) == AuthOutcome::Rejected {
        log_evt(
            trace_id,
            "evidence",
            "auth",
            "warn",
            &format!("garden={} unauthorized -> 401 (fail-closed)", gid),
        );
        return Ok(Response::from_status(StatusCode::UNAUTHORIZED)
            .with_body("Unauthorized: invalid or missing X-Garden-Auth"));
    }

    // Body-size ceiling (EDGE: body-size DoS). Screen the advertised Content-Length BEFORE
    // buffering so an oversized POST is rejected without ever reaching the heap or the model.
    if body_exceeds_limit(req.get_header_str("content-length"), MAX_BODY_BYTES) {
        log_evt(
            trace_id,
            "evidence",
            "body_limit",
            "warn",
            &format!("content-length over {} bytes -> 413", MAX_BODY_BYTES),
        );
        return Ok(Response::from_status(StatusCode::PAYLOAD_TOO_LARGE)
            .with_body("Evidence payload too large"));
    }

    // Read the raw body bytes containing image
    let body_bytes = req.take_body().into_bytes();

    // Hard cap on the ACTUAL buffered length — defends a missing/lying Content-Length that
    // slipped the pre-screen above. Done BEFORE preprocess/inference so an oversized payload
    // can never spend the model.
    if body_bytes.len() > MAX_BODY_BYTES {
        log_evt(
            trace_id,
            "evidence",
            "body_limit",
            "warn",
            &format!(
                "buffered {} bytes over {} -> 413",
                body_bytes.len(),
                MAX_BODY_BYTES
            ),
        );
        return Ok(Response::from_status(StatusCode::PAYLOAD_TOO_LARGE)
            .with_body("Evidence payload too large"));
    }

    // For proof of concept / testing under simulation:
    // Extract image bytes from multipart/form-data boundary or treat body as raw JPEG bytes.
    // In our client, we post raw JPEG binary bytes or structured form. Here we'll treat raw bytes.
    let image_bytes = &body_bytes;
    log_evt(
        trace_id,
        "evidence",
        "received",
        "ok",
        &format!("payload_bytes={}", image_bytes.len()),
    );

    if image_bytes.is_empty() {
        log_evt(
            trace_id,
            "evidence",
            "validate",
            "error",
            "empty payload -> 400",
        );
        return Ok(
            Response::from_status(StatusCode::BAD_REQUEST).with_body("Empty evidence payload")
        );
    }

    // Preprocess the JPEG image
    let input_tensor = match preprocess_image(image_bytes) {
        Ok(t) => t,
        Err(e) => {
            log_evt(
                trace_id,
                "evidence",
                "preprocess",
                "error",
                &format!("{} -> 422", e),
            );
            return Ok(Response::from_status(StatusCode::UNPROCESSABLE_ENTITY)
                .with_body(format!("Image preprocessing failed: {}", e)));
        }
    };
    log_evt(
        trace_id,
        "evidence",
        "preprocess",
        "ok",
        "tensor [1,3,224,224]",
    );

    // Require a loaded model. A missing/corrupt model must NOT masquerade as a
    // real classification (the old fallback returned "raccoon"/mitigate for ANY
    // JPEG) — fail loudly so a misconfigured KV Store is caught immediately.
    let model = match *MODEL {
        Some(ref m) => m,
        None => {
            eprintln!("[BACKEND ERROR] Inference model not loaded; returning 503.");
            log_evt(
                trace_id,
                "infer",
                "model",
                "error",
                "model not loaded -> 503 (fail-closed)",
            );
            return Ok(Response::from_status(StatusCode::SERVICE_UNAVAILABLE)
                .with_body("Inference model not loaded"));
        }
    };

    let infer_start = SystemTime::now();
    let results = match model.run(tvec!(input_tensor.into())) {
        Ok(r) => r,
        Err(e) => {
            eprintln!("[BACKEND ERROR] Inference failed: {}", e);
            log_evt(trace_id, "infer", "run", "error", &format!("{} -> 500", e));
            return Ok(Response::from_status(StatusCode::INTERNAL_SERVER_ERROR)
                .with_body("Inference failed"));
        }
    };
    let infer_ms = infer_start
        .elapsed()
        .map(|d| d.as_secs_f64() * 1000.0)
        .unwrap_or(0.0);

    // Convert raw logits -> softmax probability -> critter-gated action.
    let logits_view = results[0].to_plain_array_view::<f32>()?;
    let logits: Vec<f32> = logits_view.iter().copied().collect();
    let (species, confidence, action) = classify_logits(&logits);
    log_evt(
        trace_id,
        "infer",
        "classify",
        "ok",
        &format!(
            "species={} confidence={:.4} action={} infer_ms={:.2}",
            species, confidence, action, infer_ms
        ),
    );

    // Rain veto (comfort optimization, FAIL-SAFE — can only ever suppress a spray).
    // If the node's freshest telemetry says it's actively raining, a `mitigate` is
    // downgraded to `none`: critters shelter in the rain, so spraying just wastes
    // water. Reading telemetry here is best-effort and OFF the fail-closed heartbeat
    // path (the evidence POST carries the 30 s budget). The authoritative fast path
    // is still LOCAL on the node (it owns the rain gauge) — this edge veto is the
    // backstop + what makes the dashboard show "held: rain".
    let telemetry = read_latest_telemetry(gid, did);
    let (action, reason) = if rain_should_suppress(action, &telemetry, now_ms()) {
        log_evt(
            trace_id,
            "evidence",
            "rain_veto",
            "ok",
            "raining (fresh telemetry) -> suppress mitigate, action=none",
        );
        ("none", Some("rain".to_string()))
    } else {
        (action, None)
    };

    // Per-event smart-Stop bookkeeping (RFC: three-mode + per-event abort). On a NEW
    // `mitigate` decision (post-veto), stamp this event's cid as `last_mitigate_cid` so a
    // dashboard Stop can target the LIVE spray server-side, and clear any stale `abort_cid`
    // (auto-resume — an abort from a prior spray never carries over). Best-effort + OFF the
    // fail-closed path: a KV error here is logged and swallowed, never altering the Pi
    // response. The cid is this request's trace id (the Pi's X-Garden-Trace-Id).
    if action == "mitigate" {
        if let Err(e) = record_mitigate_cid(gid, trace_id) {
            log_evt(
                trace_id,
                "kv",
                "record_mitigate_cid",
                "warn",
                &format!("{} (best-effort, ignored)", e),
            );
        } else {
            log_evt(
                trace_id,
                "evidence",
                "record_mitigate_cid",
                "ok",
                &format!("garden={} last_mitigate_cid set, abort_cid cleared", gid),
            );
        }
    }

    // Build the durable archive object key ONCE so the SAME key is both embedded in the
    // dashboard `latest_event` (for the /event detail deep-link) and used for the FOS PUT.
    // `cid` is the request trace id (X-Garden-Trace-Id); batch/capture_ts are the optional
    // multi-angle correlation headers ("" / None -> ungrouped, edge-clock behavior).
    let object_key = archive::evidence_key_for(
        gid,
        did,
        action,
        &species,
        confidence,
        trace_id,
        capture_batch.as_deref().unwrap_or(""),
        capture_ts,
    );

    // Best-effort: publish the latest snapshot + event for the admin dashboard.
    // A KV failure here must NEVER alter the Pi-facing response or the fail-closed
    // 503 path, so errors are logged and swallowed.
    if let Err(e) = publish_evidence(
        image_bytes,
        &species,
        confidence,
        action,
        reason.as_deref(),
        gid,
        did,
        &object_key,
    ) {
        eprintln!("[BACKEND WARN] Failed to publish dashboard state: {}", e);
        log_evt(
            trace_id,
            "kv",
            "publish_evidence",
            "warn",
            &format!("{} (best-effort, ignored)", e),
        );
    } else {
        log_evt(
            trace_id,
            "kv",
            "publish_evidence",
            "ok",
            "latest_image+latest_event written",
        );
    }

    // Best-effort ALARM record: if THIS device is a trigger (default cameras are confirm-only,
    // so this no-ops until a device is opted in), upsert an alarm keyed by batch||cid using the
    // post-veto species/confidence/action + the same object_key. Off the fail-closed path; a
    // failure is logged and swallowed (never affects the Pi-facing response).
    match record_alarm_if_triggered(
        gid,
        did,
        trace_id,
        &species,
        confidence,
        action,
        reason.as_deref(),
        trigger_marker.as_deref(),
        &object_key,
        capture_batch.as_deref().unwrap_or(""),
    ) {
        Ok(true) => log_evt(
            trace_id,
            "alarm",
            "record",
            "ok",
            &format!("device={} triggered an alarm", did),
        ),
        Ok(false) => {}
        Err(e) => log_evt(
            trace_id,
            "alarm",
            "record",
            "warn",
            &format!("{} (best-effort, ignored)", e),
        ),
    }

    // Best-effort durable archive to Fastly Object Storage (RFC §2). AFTER the decision
    // and dispatched FIRE-AND-FORGET inside archive_evidence (send_async, never awaited),
    // so it adds NO latency to this Pi-timed response — the FOS PUT keeps sending in the
    // background even after this program exits. A missing fos_config/backend or a dispatch
    // error is swallowed and never touches the response. Uses the SAME `object_key` already
    // published in `latest_event` above (built by `evidence_key_for`).
    archive_evidence(image_bytes, &object_key, trace_id);

    let response_payload = EvidenceResponse {
        action: action.to_string(),
        species: Some(species),
        confidence,
        reason,
    };

    Ok(Response::from_status(StatusCode::OK).with_body_json(&response_payload)?)
}

/// `POST /api/telemetry` — the garden node's heartbeat + environment telemetry. The
/// edge stamps `last_seen_ms` (receipt time) and persists the blob per-device so the
/// dashboard can render liveness + temp/humidity/rain. Enforce-iff-token like
/// `/api/evidence` (once-per-call, non-default gardens only). Best-effort persistence:
/// a store outage returns 503 (so the gap is visible) but never affects spraying.
fn handle_telemetry(
    mut req: Request,
    trace_id: &str,
    garden_id: &str,
    device_id: &str,
) -> Result<Response, Error> {
    let gid = resolve_safety_id(garden_id, trace_id, "telemetry", "garden_id");
    let did = resolve_safety_id(device_id, trace_id, "telemetry", "device_id");

    let auth_header = req
        .get_header_str(GARDEN_AUTH_HEADER)
        .map(|s| s.to_string());
    if garden_auth_outcome(gid, auth_header.as_deref()) == AuthOutcome::Rejected {
        log_evt(
            trace_id,
            "telemetry",
            "auth",
            "warn",
            &format!("garden={} unauthorized -> 401", gid),
        );
        return Ok(Response::from_status(StatusCode::UNAUTHORIZED)
            .with_body("Unauthorized: invalid or missing X-Garden-Auth"));
    }

    // Body-size ceiling (EDGE: body-size DoS) — same guard as `/api/evidence`. Screen the
    // advertised Content-Length BEFORE buffering, then hard-check the actual bytes after.
    if body_exceeds_limit(req.get_header_str("content-length"), MAX_BODY_BYTES) {
        log_evt(
            trace_id,
            "telemetry",
            "body_limit",
            "warn",
            &format!("content-length over {} bytes -> 413", MAX_BODY_BYTES),
        );
        return Ok(Response::from_status(StatusCode::PAYLOAD_TOO_LARGE)
            .with_body("Telemetry payload too large"));
    }

    let body = req.take_body().into_bytes();
    if body.len() > MAX_BODY_BYTES {
        log_evt(
            trace_id,
            "telemetry",
            "body_limit",
            "warn",
            &format!(
                "buffered {} bytes over {} -> 413",
                body.len(),
                MAX_BODY_BYTES
            ),
        );
        return Ok(Response::from_status(StatusCode::PAYLOAD_TOO_LARGE)
            .with_body("Telemetry payload too large"));
    }

    let incoming: serde_json::Value = match serde_json::from_slice(&body) {
        Ok(v) => v,
        Err(_) => {
            log_evt(
                trace_id,
                "telemetry",
                "parse",
                "error",
                "malformed JSON body -> 400",
            );
            return Ok(Response::from_status(StatusCode::BAD_REQUEST)
                .with_body("Expected a JSON telemetry object"));
        }
    };

    let record = build_telemetry_record(incoming, now_ms());
    // Size-cap the SERIALIZED record before persisting it (finding 5): it is re-read +
    // parsed on hot paths and is reachable tokenlessly on the `default` garden, so a bloated
    // blob would tax every `/api/state` render + rain-veto read. Oversized -> 400 (the
    // stamped record only ADDS `last_seen_ms`, so this is essentially the incoming JSON size).
    if telemetry_record_too_large(&record, MAX_TELEMETRY_BYTES) {
        log_evt(
            trace_id,
            "telemetry",
            "size_limit",
            "warn",
            &format!("record over {} bytes -> 400", MAX_TELEMETRY_BYTES),
        );
        return Ok(
            Response::from_status(StatusCode::BAD_REQUEST).with_body("Telemetry record too large")
        );
    }
    match write_telemetry(&record, gid, did) {
        Ok(_) => {
            log_evt(
                trace_id,
                "telemetry",
                "write",
                "ok",
                &format!("garden={} device={} bytes={}", gid, did, body.len()),
            );
            Ok(Response::from_status(StatusCode::OK)
                .with_body_json(&serde_json::json!({"ok": true}))?)
        }
        Err(e) => {
            eprintln!("[BACKEND ERROR] Failed to persist telemetry: {}", e);
            log_evt(
                trace_id,
                "telemetry",
                "write",
                "error",
                &format!("{} -> 503", e),
            );
            Ok(Response::from_status(StatusCode::SERVICE_UNAVAILABLE)
                .with_body("State store unavailable; telemetry not persisted"))
        }
    }
}

fn handle_status(req: Request, trace_id: &str, garden_id: &str) -> Result<Response, Error> {
    // The Pi polls this during MITIGATING to decide whether to KEEP deterrents
    // energized, so this path is FAIL-CLOSED: if the authoritative garden_state
    // store cannot be read, we must not answer "keep firing".
    //   - Ok(Some): read flags, biasing any unreadable flag to its safe value
    //     (unconfirmed armed -> disarmed, unconfirmed override -> STOP).
    //   - Ok(None): store unprovisioned -> fall back to the read-only Config Store,
    //     itself a real configured authority (preserves pre-dashboard deploys).
    //   - Err: the store should be reachable but isn't -> stop firing.
    //
    // Tenancy id resolution is in-memory ONLY (no I/O): the gid just selects which
    // KV keys to read via the shared `key` helper. This adds NO new round-trip to
    // the heartbeat — still the one `KVStore::open` + the two flag lookups, plus one
    // cheap in-store `abort_cid` lookup for the per-event smart Stop (read off the
    // SAME open handle — no extra round-trip).
    let gid = resolve_safety_id(garden_id, trace_id, "status", "garden_id");
    // The LIVE event's cid the Pi echoes on every heartbeat (X-Garden-Trace-Id). Use the
    // RAW header (empty if absent), NOT the minted edge-fallback `trace_id`: an empty
    // event cid can never match a non-empty abort_cid, so a header-less heartbeat is never
    // aborted. Evidence stamps `last_mitigate_cid`/`abort_cid` with this same id.
    let event_cid = req
        .get_header_str(contract_gen::HEADER_TRACE_ID)
        .unwrap_or("")
        .to_string();
    let cfg = config_flags();
    // Default selection per garden. The Config Store (armed=true) is the legacy
    // authority ONLY for the `default` garden (preserves pre-dashboard single-tenant
    // deploys). A NON-default garden has no Config-Store authority, so an
    // absent/unprovisioned key must FAIL CLOSED (armed=false, override=true ->
    // continue=false) rather than inherit the default's `armed=true`. This satisfies
    // "unknown garden on /api/status -> continue=false" (RFC §3) and adds NO I/O —
    // it only chooses which default tuple feeds `heartbeat_flags_from`.
    let defaults = if gid == "default" { cfg } else { (false, true) };
    let continue_mitigation = match KVStore::open(STATE_STORE) {
        Ok(Some(store)) => {
            let (armed, override_stop) = heartbeat_flags_from(&store, defaults, gid);
            // Per-event smart Stop, ANDed on TOP of the fail-closed armed/override floor.
            // COMFORT gate, never a safety gate: an unreadable abort_cid degrades to ""
            // (no abort) so a flaky store can only ever fail-SAFE (it never suppresses a
            // legitimate spray). Read off the SAME open handle — no extra round-trip.
            let abort_cid = read_abort_cid_from(&store, gid);
            let decision = heartbeat_continue_abort(armed, override_stop, &event_cid, &abort_cid);
            log_evt(
                trace_id,
                "status",
                "heartbeat",
                "ok",
                &format!(
                    "source=kv armed={:?} override_stop={:?} event_cid={} abort_cid={} continue={}",
                    armed, override_stop, event_cid, abort_cid, decision
                ),
            );
            decision
        }
        Ok(None) => {
            // Store unprovisioned -> use the per-garden default tuple: the `default`
            // garden falls back to the real Config-Store authority; a non-default
            // garden fails closed (it has no Config-Store authority).
            let decision = continue_mitigation(defaults.0, defaults.1);
            log_evt(
                trace_id,
                "status",
                "heartbeat",
                "ok",
                &format!(
                    "source=config garden={} armed={} override_stop={} continue={}",
                    gid, defaults.0, defaults.1, decision
                ),
            );
            decision
        }
        Err(e) => {
            eprintln!(
                "[BACKEND ERROR] garden_state unreadable ({}); failing closed (continue_mitigation=false).",
                e
            );
            log_evt(
                trace_id,
                "status",
                "heartbeat",
                "error",
                &format!(
                    "garden_state unreadable ({}) -> continue=false (fail-closed)",
                    e
                ),
            );
            false
        }
    };

    let response_payload = StatusResponse {
        continue_mitigation,
    };

    Ok(Response::from_status(StatusCode::OK).with_body_json(&response_payload)?)
}

/// `POST /api/control` — body `{cmd}` -> write KV -> return the new dashboard state
/// (the `/api/state` shape, incl. the derived `mode`). THREE-MODE + per-event smart Stop:
///   off / disarm  -> mode OFF      (armed=false)
///   monitor       -> mode MONITOR  (armed=true, override_stop=true — watch+alert+log, never spray)
///   active / arm  -> mode ACTIVE   (armed=true, override_stop=false — watch+spray)
///   stop          -> abort the LIVE spray (abort_cid = last_mitigate_cid); does NOT change mode;
///                    no-op (still 200) when no spray is live (last_mitigate_cid empty)
///   resume        -> clear abort_cid (manual un-abort)
/// Unknown/malformed command -> 400; store write failure -> 503.
///
/// AUTH (defense-in-depth, RFC §5 enforce-iff-token): this is the PUBLIC edge control
/// surface and the header ARMED pill is becoming a clickable toggle, so an anonymous
/// viewer must NOT be able to drive it. It gates on the SAME per-garden `X-Garden-Auth`
/// token as the other protected mutations (the Pi portal proxies it through its admin
/// gate and forwards the token). The `default` garden is TOKENLESS — `garden_auth_outcome`
/// returns `Tokenless` (allowed) for it AND for any non-default garden with no token
/// configured — so local dev + the single-tenant default path keep working unauthenticated.
/// A configured token with an absent/wrong credential -> 403.
fn handle_control(mut req: Request, trace_id: &str) -> Result<Response, Error> {
    // The garden this control affects = the service's own garden (header-less browser
    // resolution), matching `/api/state`. On the default service this is "default"
    // (tokenless, unchanged local behavior); on a dedicated service it's that garden.
    let gid_owned = browser_garden_id();
    let gid = resolve_safety_id(&gid_owned, trace_id, "control", "garden_id");

    // Auth gate. Rate-limit + per-garden token, mirroring the protected admin mutations.
    let client_ip = req
        .get_client_ip_addr()
        .unwrap_or(IpAddr::V4(std::net::Ipv4Addr::new(127, 0, 0, 1)));
    if is_ip_locked_out(client_ip) {
        log_evt(
            trace_id,
            "control",
            "rate_limit",
            "warn",
            &format!("garden={} ip={} locked out -> 429", gid, client_ip),
        );
        return Ok(Response::from_status(StatusCode::TOO_MANY_REQUESTS)
            .with_body("Too many failed attempts. Please come back later."));
    }
    let auth_header = req
        .get_header_str(GARDEN_AUTH_HEADER)
        .map(|s| s.to_string());
    if garden_auth_outcome(gid, auth_header.as_deref()) == AuthOutcome::Rejected {
        record_failed_attempt(client_ip);
        log_evt(
            trace_id,
            "control",
            "auth",
            "warn",
            &format!("garden={} unauthorized -> 403", gid),
        );
        return Ok(Response::from_status(StatusCode::FORBIDDEN)
            .with_body("Forbidden: invalid or missing X-Garden-Auth"));
    }
    reset_failed_attempts(client_ip);

    let body = req.take_body().into_bytes();
    let cmd_req: ControlRequest = match serde_json::from_slice(&body) {
        Ok(c) => c,
        Err(_) => {
            log_evt(
                trace_id,
                "control",
                "parse",
                "error",
                "malformed JSON body -> 400",
            );
            return Ok(Response::from_status(StatusCode::BAD_REQUEST)
                .with_body("Expected JSON body {\"cmd\": \"off|monitor|active|stop|resume\"}"));
        }
    };

    // stop / resume mutate the per-event abort_cid, NOT the mode tuple — handle them here
    // (apply_control owns only the mode-setting commands).
    match cmd_req.cmd.as_str() {
        "stop" => {
            // Target the LIVE spray's cid. Empty (no spray live) -> no-op, still 200.
            let target = read_last_mitigate_cid(gid);
            if let Err(e) = write_abort_cid(gid, &target) {
                eprintln!("[BACKEND ERROR] Failed to write abort_cid: {}", e);
                log_evt(
                    trace_id,
                    "control",
                    "write",
                    "error",
                    &format!("garden={} stop -> 503 (abort not persisted: {})", gid, e),
                );
                return Ok(Response::from_status(StatusCode::SERVICE_UNAVAILABLE)
                    .with_body("State store unavailable; command not persisted"));
            }
            log_evt(
                trace_id,
                "control",
                "apply",
                "ok",
                &format!("garden={} cmd=stop abort_cid='{}'", gid, target),
            );
            return Ok(Response::from_status(StatusCode::OK).with_body_json(&build_state(gid))?);
        }
        "resume" => {
            if let Err(e) = write_abort_cid(gid, "") {
                eprintln!("[BACKEND ERROR] Failed to clear abort_cid: {}", e);
                log_evt(
                    trace_id,
                    "control",
                    "write",
                    "error",
                    &format!("garden={} resume -> 503 (abort not cleared: {})", gid, e),
                );
                return Ok(Response::from_status(StatusCode::SERVICE_UNAVAILABLE)
                    .with_body("State store unavailable; command not persisted"));
            }
            log_evt(
                trace_id,
                "control",
                "apply",
                "ok",
                &format!("garden={} cmd=resume abort_cid cleared", gid),
            );
            return Ok(Response::from_status(StatusCode::OK).with_body_json(&build_state(gid))?);
        }
        _ => {}
    }

    // Mode-setting commands (off/disarm/monitor/active/arm). read the garden's current
    // flags so `off` can preserve override_stop, then apply.
    let (armed, override_stop) = read_garden_control_flags(gid);
    let (new_armed, new_override) = match apply_control(&cmd_req.cmd, armed, override_stop) {
        Some(state) => state,
        None => {
            log_evt(
                trace_id,
                "control",
                "apply",
                "error",
                &format!("unknown cmd '{}' -> 400", cmd_req.cmd.escape_default()),
            );
            return Ok(Response::from_status(StatusCode::BAD_REQUEST)
                .with_body(format!("Unknown command: {}", cmd_req.cmd)));
        }
    };

    if let Err(e) = write_flags(new_armed, new_override, gid) {
        // Surface store-unavailable distinctly so the operator knows the command
        // (e.g. switching to OFF) did NOT take effect. The heartbeat path itself
        // fails closed independently (see handle_status).
        eprintln!("[BACKEND ERROR] Failed to write control state: {}", e);
        log_evt(
            trace_id,
            "control",
            "write",
            "error",
            &format!("{} -> 503 (not persisted)", e),
        );
        return Ok(Response::from_status(StatusCode::SERVICE_UNAVAILABLE)
            .with_body("State store unavailable; command not persisted"));
    }

    log_evt(
        trace_id,
        "control",
        "apply",
        "ok",
        &format!(
            "garden={} cmd={} armed={} override_stop={} mode={}",
            gid,
            cmd_req.cmd,
            new_armed,
            new_override,
            derive_mode(new_armed, new_override).as_str()
        ),
    );
    Ok(Response::from_status(StatusCode::OK).with_body_json(&build_state(gid))?)
}

// ---------------------------------------------------------------------------
// Admin / CRUD routes (Step 3, RFC §3). Path-based REST, matched AFTER the hot
// exact-match arms so the Pi-facing safety paths are never slowed. These are
// NON-safety routes: a malformed id REJECTS with 400 (unlike the safety paths,
// which fall back to `default`). The registry index docs are CONTROL-PLANE-
// WRITE-ONLY (RFC §4): the edge READS them; the two mutating POSTs return 405
// pointing at `gp-provision` (Fastly KV has no CAS, so a single Python writer
// owns the registry). Garden-scoped routes enforce the per-garden token.
// ---------------------------------------------------------------------------

/// Registry doc shapes (RFC §2). Every doc carries `v` so the reader can evolve
/// shapes without migration. These are the edge's READ models for the index docs
/// `gp-provision` writes.
// NOTE: the gardens-index read models (Location / Garden / GardensRegistry) were
// removed — the edge serves `index/gardens` as a raw passthrough (admin_serve_registry)
// and never parsed through them, so they were dead in the wasm build. gp-provision
// (Python) is the authoritative writer of that schema; the `Device` read model below
// IS used (read_devices parses Vec<Device>).
#[derive(Serialize, Deserialize, Debug, PartialEq)]
struct Device {
    device_id: String,
    node_id: String,
    /// `observer` | `deterrent` (RFC §1).
    kind: String,
    /// Type within the kind, e.g. `camera_usb`, `motion_pir`, `heat_thermal`,
    /// `solenoid`. Renamed because `type` is a Rust keyword.
    #[serde(rename = "type")]
    dev_type: String,
    name: String,
    status: String,
    /// Alarm roles (RFC: triggers create alarms; confirms corroborate them). `#[serde(default)]`
    /// => existing registry entries (written before these fields) read as `false`, no migration.
    /// `can_trigger_alarm` is the device-level gate on alarm creation; a push ALSO has to carry the
    /// `X-Trigger` marker (a genuine trigger EVENT, e.g. a camera's confirmed motion) — see
    /// `alarm_should_record`. Richer per-detector trigger conditions (e.g. a radar's motion-dwell
    /// time) will live in a future per-device `trigger_config` blob alongside this (the registry
    /// already stores per-device config dicts like `transport`), with no schema change here.
    #[serde(default)]
    can_trigger_alarm: bool,
    #[serde(default)]
    can_confirm_alarm: bool,
}

#[derive(Serialize, Deserialize, Debug, PartialEq)]
struct DevicesRegistry {
    v: u32,
    garden_id: String,
    updated_ts: u64,
    devices: Vec<Device>,
}

#[cfg(test)]
mod tests {
    use super::*;
    // These thresholds are consumed by `mod safety` (node_liveness / rain_should_suppress)
    // in non-test builds, so main.rs imports them ONLY here for the safety unit tests.
    use crate::contract_gen::{NODE_OFFLINE_AFTER_SECS, RAIN_TELEMETRY_FRESH_SECS};

    #[test]
    fn test_html_escape_minimal() {
        assert_eq!(
            html_escape("a & b <x> \"q\""),
            "a &amp; b &lt;x&gt; &quot;q&quot;"
        );
    }

    #[test]
    fn test_fill_header_bakes_name_marks_active_keeps_ids() {
        let h = fill_header("Backyard <Patch> & Co");
        // `/` and `/admin` start on the Dashboard tab; `/history` now marks History active
        // SERVER-side (see test_dashboard_nav_selects_active_link below) — no JS re-point.
        assert!(h.contains("id=\"nav-dashboard\" class=\"active\""));
        // Name baked in + HTML-escaped; placeholder consumed.
        assert!(h.contains("Backyard &lt;Patch&gt; &amp; Co"));
        assert!(!h.contains("__GARDEN_NAME__"));
        // Every id the dashboard JS depends on must survive into the shared header.
        for id in [
            "nav-dashboard",
            "nav-history",
            "nav-timelapse",
            "nav-devices",
            "nav-settings",
            "nav-costs",
            "nav-logs",
            "nav-storage",
            "portal-nav",
            "garden-name",
        ] {
            assert!(
                h.contains(&format!("id=\"{}\"", id)),
                "shared header missing id {}",
                id
            );
        }
    }

    #[test]
    fn test_empty_name_keeps_element_for_reserved_space() {
        // Absent garden name (e.g. legacy garden) -> empty slot, element still present.
        let h = fill_header("");
        assert!(h.contains("id=\"garden-name\""));
        assert!(!h.contains("__GARDEN_NAME__"));
    }

    #[test]
    fn test_splice_dashboard_one_header_and_flag() {
        let out = splice_dashboard(&fill_header("Test Garden"));
        assert!(out.contains("window.GP_VIEW_ONLY=true"));
        assert!(
            !out.contains("<!--PORTAL_HEADER-->"),
            "sentinel should be consumed by the splice"
        );
        // Exactly one header injected (id="portal-nav" appears only in the header markup;
        // the CSS uses #portal-nav and the JS uses "portal-nav").
        assert_eq!(out.matches("id=\"portal-nav\"").count(), 1);
        assert!(out.contains("Test Garden"));
    }

    #[test]
    fn test_dashboard_nav_selects_active_link() {
        // The dashboard route serves the SAME page for `/`, `/admin`, and `/history`;
        // only the active nav link differs, and it is now chosen SERVER-side (the
        // `/history` JS no longer has to re-point it). `dashboard_view_only_html(nav)`
        // selects the header via `dashboard_header_html` / `history_header_html`, which
        // differ ONLY by their active nav id — both read the garden name from the config
        // store (a host ABI that won't link under native `cargo test`), so here we pin the
        // SAME path->nav mapping through the pure header layer those wrappers delegate to.
        let dash = splice_dashboard(&fill_header_active("Test Garden", "nav-dashboard", true));
        assert!(dash.contains("id=\"nav-dashboard\" class=\"active\""));
        assert!(!dash.contains("id=\"nav-history\" class=\"active\""));

        let hist = splice_dashboard(&fill_header_active("Test Garden", "nav-history", true));
        assert!(hist.contains("id=\"nav-history\" class=\"active\""));
        assert!(!hist.contains("id=\"nav-dashboard\" class=\"active\""));
        // Both render the same view-only dashboard page (same hooks + view-only flag).
        assert!(hist.contains("window.GP_VIEW_ONLY=true"));
        assert_eq!(hist.matches("id=\"portal-nav\"").count(), 1);
    }

    #[test]
    fn test_fill_header_active_marks_timelapse() {
        // The Timelapse page marks its own nav link active (not Dashboard).
        let h = fill_header_active("Test Garden", "nav-timelapse", false);
        assert!(h.contains("id=\"nav-timelapse\" class=\"active\""));
        assert!(!h.contains("id=\"nav-dashboard\" class=\"active\""));
    }

    #[test]
    fn test_view_only_header_omits_admin_nav() {
        // THE security boundary (defect B): the view-only header the edge serves must
        // OMIT every admin nav link from the HTML source — not merely hide it client-side.
        let view = fill_header_active("Test Garden", "nav-dashboard", true);
        for id in [
            "nav-devices",
            "nav-settings",
            "nav-costs",
            "nav-logs",
            "nav-storage",
        ] {
            assert!(
                !view.contains(&format!("id=\"{}\"", id)),
                "view-only header leaked admin link {}",
                id
            );
        }
        for id in ["nav-dashboard", "nav-history", "nav-timelapse"] {
            assert!(
                view.contains(&format!("id=\"{}\"", id)),
                "missing viewer link {}",
                id
            );
        }
        // The admin (Pi) header still carries all eight.
        let admin = fill_header_active("Test Garden", "nav-dashboard", false);
        assert!(admin.contains("id=\"nav-storage\"") && admin.contains("id=\"nav-costs\""));
    }

    #[test]
    fn test_splice_timelapse_one_header_and_flag() {
        let out = splice_timelapse(&fill_header_active("Test Garden", "nav-timelapse", true));
        assert!(out.contains("window.GP_VIEW_ONLY=true"));
        assert!(
            !out.contains("<!--PORTAL_HEADER-->"),
            "sentinel should be consumed by the splice"
        );
        assert_eq!(out.matches("id=\"portal-nav\"").count(), 1);
        // The baked timelapse page must carry its player hooks + the shared asset bump
        // (stamped from the generated ASSET_VERSION, not a hand-typed literal).
        assert!(out.contains("id=\"tl-grid\""));
        assert!(out.contains("id=\"tl-transport\""));
        assert!(out.contains(&format!("/static/gp.js?v={}", contract_gen::ASSET_VERSION)));
        assert!(!out.contains("__ASSET_VERSION__"));
    }

    #[test]
    fn test_splice_event_one_header_and_flag() {
        // The single-event detail page is reached by link, so it marks HISTORY active (its
        // browse home) rather than a nav item of its own.
        let out = splice_event(&fill_header_active("Test Garden", "nav-history", true));
        assert!(out.contains("window.GP_VIEW_ONLY=true"));
        assert!(
            !out.contains("<!--PORTAL_HEADER-->"),
            "sentinel should be consumed by the splice"
        );
        assert_eq!(out.matches("id=\"portal-nav\"").count(), 1);
        assert!(out.contains("id=\"nav-history\" class=\"active\""));
        // The baked event page must carry its detail hooks + the shared asset bump.
        assert!(out.contains("id=\"ev-article\""));
        assert!(out.contains("id=\"ev-facts\""));
        assert!(out.contains("id=\"ev-tl\""));
        assert!(out.contains(&format!("/static/gp.js?v={}", contract_gen::ASSET_VERSION)));
        assert!(!out.contains("__ASSET_VERSION__"));
    }

    #[test]
    fn test_evidence_key_for_matches_object_key_on_capture_ts() {
        // Locks the refactor: when the Pi-provided capture_ts is plausible (close to now) it is
        // used verbatim, so the key handed to publish_evidence (for the /event deep-link) is
        // byte-identical to the one archive_evidence PUTs. Pinning to capture_ts keeps this
        // deterministic regardless of the clock ticking between the two calls.
        let ts = now_secs();
        let from_helper = archive::evidence_key_for(
            "g1",
            "cam-a",
            "none",
            "raccoon",
            0.5,
            "cid1",
            "b1",
            Some(ts),
        );
        let direct = evidence_object_key("g1", "cam-a", "none", "raccoon", 0.5, "cid1", "b1", ts);
        assert_eq!(from_helper, direct);
        // And it round-trips through the archive parser (the field the detail page reads).
        let ev = parse_archive_key("g1", &from_helper).expect("helper key parses");
        assert_eq!(ev["device"], "cam-a");
        assert_eq!(ev["batch"], "b1");
        assert_eq!(ev["cid"], "cid1");
    }

    #[test]
    fn test_in_memory_rate_limiting() {
        let test_ip = IpAddr::V4(std::net::Ipv4Addr::new(10, 0, 0, 1));

        // 1. Initially NOT locked out
        reset_failed_attempts(test_ip);
        assert!(
            !is_ip_locked_out(test_ip),
            "IP should not be locked out initially"
        );

        // 2. Increment failures
        let (max_fails, _) = read_rate_limit_settings();
        for _ in 0..max_fails {
            assert!(!is_ip_locked_out(test_ip));
            record_failed_attempt(test_ip);
        }

        // 3. Exceeded max fails -> locked out
        assert!(
            is_ip_locked_out(test_ip),
            "IP should be locked out after exceeding max_fails"
        );

        // 4. Reset failures -> unlocked
        reset_failed_attempts(test_ip);
        assert!(
            !is_ip_locked_out(test_ip),
            "IP should be unlocked after reset"
        );
    }

    #[test]
    fn test_lockout_kv_key_is_flat_and_per_ip() {
        // Garden-agnostic, per-IP namespace (NOT the garden-scoped `key()` grammar).
        let v4 = IpAddr::V4(std::net::Ipv4Addr::new(203, 0, 113, 7));
        assert_eq!(lockout_kv_key(v4), "ratelimit/login/203.0.113.7");
        let v6 = IpAddr::V6(std::net::Ipv6Addr::LOCALHOST);
        assert_eq!(lockout_kv_key(v6), "ratelimit/login/::1");
    }

    #[test]
    fn test_lockout_kv_encode_decode_roundtrip() {
        assert_eq!(lockout_kv_encode(3, 1_700_000_000), "3:1700000000");
        assert_eq!(lockout_kv_decode("3:1700000000"), Some((3, 1_700_000_000)));
        // Malformed values decode to None (caller treats as "no record" -> fail-open).
        assert_eq!(lockout_kv_decode(""), None);
        assert_eq!(lockout_kv_decode("garbage"), None);
        assert_eq!(lockout_kv_decode("x:5"), None);
        assert_eq!(lockout_kv_decode("5:y"), None);
        assert_eq!(lockout_kv_decode("5"), None);
    }

    #[test]
    fn test_lockout_kv_is_locked_window_and_threshold() {
        let (now, window, max) = (1_000_000i64, 300i64, 5u32);
        // Under threshold within window -> not locked.
        assert!(!lockout_kv_is_locked(Some("4:999900"), now, window, max));
        // At/over threshold within window -> locked.
        assert!(lockout_kv_is_locked(Some("5:999900"), now, window, max));
        assert!(lockout_kv_is_locked(Some("9:999900"), now, window, max));
        // Over threshold but window elapsed -> treated as cleared (not locked).
        assert!(!lockout_kv_is_locked(Some("9:999000"), now, window, max));
        // Fail-OPEN: no record / malformed -> never locked.
        assert!(!lockout_kv_is_locked(None, now, window, max));
        assert!(!lockout_kv_is_locked(Some("garbage"), now, window, max));
    }

    #[test]
    fn test_lockout_kv_next_increments_then_restarts() {
        let (now, window) = (1_000_000i64, 300i64);
        // No prior record -> first failure.
        assert_eq!(lockout_kv_next(None, now, window), (1, now));
        // Within window -> increment, stamp now.
        assert_eq!(lockout_kv_next(Some("4:999900"), now, window), (5, now));
        // Window elapsed -> restart at 1.
        assert_eq!(lockout_kv_next(Some("9:999000"), now, window), (1, now));
        // Malformed prior -> treated as fresh start.
        assert_eq!(lockout_kv_next(Some("junk"), now, window), (1, now));
    }

    #[test]
    fn test_sweep_expired_lockouts_prunes_only_elapsed() {
        let (now, window) = (1_000_000i64, 300i64);
        let fresh = IpAddr::V4(std::net::Ipv4Addr::new(10, 0, 0, 1)); // within window
        let stale = IpAddr::V4(std::net::Ipv4Addr::new(10, 0, 0, 2)); // window elapsed
        let edge = IpAddr::V4(std::net::Ipv4Addr::new(10, 0, 0, 3)); // exactly at window -> elapsed
        let mut state: HashMap<IpAddr, (u32, i64)> = HashMap::new();
        state.insert(fresh, (3, now - 10));
        state.insert(stale, (9, now - window - 1));
        state.insert(edge, (4, now - window));
        sweep_expired_lockouts(&mut state, now, window);
        // Only the in-window entry survives; the IP-rotating attacker's dead entries are reaped.
        assert!(state.contains_key(&fresh));
        assert!(!state.contains_key(&stale));
        assert!(!state.contains_key(&edge));
        assert_eq!(state.len(), 1);
    }

    #[test]
    fn test_body_exceeds_limit_pre_screen() {
        // Present + over the limit -> reject (true).
        assert!(body_exceeds_limit(Some("9000000"), MAX_BODY_BYTES));
        // Present + at/under the limit -> accept (false).
        assert!(!body_exceeds_limit(
            Some(&MAX_BODY_BYTES.to_string()),
            MAX_BODY_BYTES
        ));
        assert!(!body_exceeds_limit(Some("1024"), MAX_BODY_BYTES));
        // Whitespace is tolerated around a real value.
        assert!(body_exceeds_limit(Some("  9000000  "), MAX_BODY_BYTES));
        // Absent / non-numeric Content-Length -> the pre-screen does NOT reject (false); the
        // caller's hard byte-length check is what actually bounds a lying/absent header.
        assert!(!body_exceeds_limit(None, MAX_BODY_BYTES));
        assert!(!body_exceeds_limit(Some("not-a-number"), MAX_BODY_BYTES));
        assert!(!body_exceeds_limit(Some(""), MAX_BODY_BYTES));
    }

    #[test]
    fn test_telemetry_record_too_large() {
        // A normal stamped telemetry record is well under the ceiling.
        let small = build_telemetry_record(
            serde_json::json!({"temp": 21.5, "humidity": 60, "rain": false}),
            1_700_000_000_000,
        );
        assert!(!telemetry_record_too_large(&small, MAX_TELEMETRY_BYTES));
        // A bloated attacker blob (one huge string field) exceeds the ceiling -> reject.
        let big_val = "z".repeat(MAX_TELEMETRY_BYTES + 1);
        let big = build_telemetry_record(serde_json::json!({"junk": big_val}), 1_700_000_000_000);
        assert!(telemetry_record_too_large(&big, MAX_TELEMETRY_BYTES));
        // Boundary: a record serializing to exactly the limit is accepted; one byte over rejects.
        // Build a record whose serialized length we tune via a padding string.
        let base_len = serde_json::to_vec(&serde_json::json!({"p": ""}))
            .unwrap()
            .len();
        let pad = "a".repeat(MAX_TELEMETRY_BYTES - base_len);
        let at_limit = serde_json::json!({"p": pad});
        assert_eq!(
            serde_json::to_vec(&at_limit).unwrap().len(),
            MAX_TELEMETRY_BYTES
        );
        assert!(!telemetry_record_too_large(&at_limit, MAX_TELEMETRY_BYTES));
        let over = serde_json::json!({"p": "a".repeat(MAX_TELEMETRY_BYTES - base_len + 1)});
        assert!(telemetry_record_too_large(&over, MAX_TELEMETRY_BYTES));
    }

    #[test]
    fn test_lockout_kv_layer_never_grants_on_error() {
        // The cross-instance KV layer can only ADD lockouts; a missing/malformed/expired
        // record must always resolve to "not locked" so a KV problem cannot lock out
        // (fail-OPEN) AND cannot be relied on to grant access (it is only consulted to deny).
        let (now, window, max) = (1_000_000i64, 300i64, 5u32);
        for raw in [None, Some(""), Some("garbage"), Some("5:1")] {
            assert!(
                !lockout_kv_is_locked(raw, now, window, max),
                "fail-open expected for {:?}",
                raw
            );
        }
    }

    #[test]
    fn test_rate_limiting_env_overrides() {
        // Clear env vars to be clean
        std::env::remove_var("RATE_LIMIT_MAX_FAILS");
        std::env::remove_var("RATE_LIMIT_WINDOW_S");

        // Baseline defaults
        let (max_fails, window_s) = read_rate_limit_settings();
        assert_eq!(max_fails, 5);
        assert_eq!(window_s, 300);

        // Set overrides
        std::env::set_var("RATE_LIMIT_MAX_FAILS", "10");
        std::env::set_var("RATE_LIMIT_WINDOW_S", "60");

        let (max_fails_overridden, window_s_overridden) = read_rate_limit_settings();
        assert_eq!(max_fails_overridden, 10);
        assert_eq!(window_s_overridden, 60);

        // Clean up
        std::env::remove_var("RATE_LIMIT_MAX_FAILS");
        std::env::remove_var("RATE_LIMIT_WINDOW_S");
    }

    #[test]
    fn test_merge_rate_limit_settings_precedence() {
        // PURE precedence proof for the lockout policy: defaults < env < Config Store, with
        // each layer winning ONLY when present AND parseable. This is the env-free, host-ABI-free
        // core of read_rate_limit_settings, so it pins the merge on the native target where the
        // Config Store can't be read.

        // 1. No overrides anywhere -> built-in defaults.
        assert_eq!(merge_rate_limit_settings(None, None, None, None), (5, 300));

        // 2. Env only -> env wins over defaults.
        assert_eq!(
            merge_rate_limit_settings(Some("10"), Some("60"), None, None),
            (10, 60)
        );

        // 3. Config Store only -> Config Store wins over defaults.
        assert_eq!(
            merge_rate_limit_settings(None, None, Some("3"), Some("900")),
            (3, 900)
        );

        // 4. Both present -> Config Store (the live edge source) wins over env.
        assert_eq!(
            merge_rate_limit_settings(Some("10"), Some("60"), Some("3"), Some("900")),
            (3, 900)
        );

        // 5. Garbage NEVER resets a lower layer to default: an unparseable value is ignored
        //    and the prior (env, then default) layer stays intact. Fail-safe: a typo in the
        //    Config Store can't silently widen the lockout window.
        assert_eq!(
            merge_rate_limit_settings(Some("10"), Some("60"), Some("oops"), Some("")),
            (10, 60),
            "unparseable Config Store values must not clobber the env layer"
        );
        assert_eq!(
            merge_rate_limit_settings(Some("nope"), Some("x"), None, None),
            (5, 300),
            "unparseable env values must leave the defaults intact"
        );

        // 6. Partial override: only one field set at a layer leaves the other at its prior value.
        assert_eq!(
            merge_rate_limit_settings(None, Some("45"), Some("7"), None),
            (7, 45)
        );
    }

    #[test]
    fn test_image_preprocessing_pipeline() {
        // Load our minimal red 1x1 test JPEG fixture
        let jpeg_bytes = include_bytes!("../../tests/fixtures/raccoon.jpg");

        // Assert preprocessing compiles and runs on macOS unit tests
        let tensor_res = preprocess_image(jpeg_bytes);
        assert!(
            tensor_res.is_ok(),
            "JPEG preprocessing failed: {:?}",
            tensor_res.err()
        );

        let tensor = tensor_res.unwrap();
        assert_eq!(tensor.shape(), &[1, 3, 224, 224]);
    }

    #[test]
    fn test_classify_logits_critter_high_confidence_mitigates() {
        // Top class is 'red fox' (277) with a dominant logit -> high softmax prob.
        let mut logits = vec![0.0f32; 1000];
        logits[277] = 12.0;
        let (species, conf, action) = classify_logits(&logits);
        assert_eq!(species, "red fox");
        assert!(conf > 0.9, "expected high confidence, got {}", conf);
        assert_eq!(action, "mitigate");
    }

    #[test]
    fn test_classify_logits_non_critter_never_mitigates() {
        // A confident but non-allowlisted class must not trigger a deterrent.
        let mut logits = vec![0.0f32; 1000];
        logits[0] = 12.0;
        let (species, _conf, action) = classify_logits(&logits);
        assert_eq!(species, "class_0");
        assert_eq!(action, "none");
    }

    #[test]
    fn test_classify_logits_low_confidence_critter_does_not_mitigate() {
        // Critter class on top but flat distribution -> probability below threshold.
        let mut logits = vec![0.0f32; 1000];
        logits[277] = 0.2;
        let (_species, conf, action) = classify_logits(&logits);
        assert!(
            conf < crate::contract_gen::MITIGATE_THRESHOLD,
            "expected low confidence, got {}",
            conf
        );
        assert_eq!(action, "none");
    }

    #[test]
    fn test_classify_logits_empty_resolves_to_safe_none() {
        // A corrupt/degenerate model that emits NO logits must NEVER fire a deterrent.
        // The loop never runs (best_class stays 0, sum_exp stays 0 -> confidence 0), so the
        // safe default is the only acceptable outcome.
        let (_species, conf, action) = classify_logits(&[]);
        assert_eq!(action, "none", "empty logits must fail-safe to no-fire");
        assert_eq!(conf, 0.0, "empty logits must yield zero confidence");
    }

    #[test]
    fn test_classify_logits_nan_logits_resolve_to_safe_none() {
        // NaN logits (degenerate/corrupt model output): every `l > best_logit` compare is
        // false for NaN, so best_class stays 0 (a non-critter). A garden owner would far
        // rather we no-fire on garbage than spray on it -> action must be "none".
        let logits = vec![f32::NAN; 1000];
        let (_species, _conf, action) = classify_logits(&logits);
        assert_eq!(action, "none", "all-NaN logits must fail-safe to no-fire");

        // Mixed: a NaN sitting on a critter class index must still not manufacture a
        // confident mitigate (NaN never wins the `>` compare, NaN confidence is never >= threshold).
        let mut mixed = vec![0.0f32; 1000];
        mixed[277] = f32::NAN; // 'red fox' slot, but NaN
        let (_s, _c, action_mixed) = classify_logits(&mixed);
        assert_eq!(
            action_mixed, "none",
            "a NaN on a critter class must not mitigate"
        );
    }

    #[test]
    fn test_continue_mitigation_truth_table() {
        assert!(
            continue_mitigation(true, false),
            "armed & not stopped -> continue"
        );
        assert!(!continue_mitigation(false, false), "disarmed -> stop");
        assert!(!continue_mitigation(true, true), "stop override -> stop");
        assert!(
            !continue_mitigation(false, true),
            "disarmed & stopped -> stop"
        );
    }

    #[test]
    fn test_apply_control_commands() {
        // THREE-MODE model. The mode-setting commands own the (armed, override_stop) tuple.
        // OFF / disarm: armed=false; override_stop LEFT UNCHANGED (so a later `active`
        // restores the pre-OFF spray intent). Mode is OFF either way (derive_mode).
        assert_eq!(apply_control("off", true, false), Some((false, false)));
        assert_eq!(apply_control("off", true, true), Some((false, true)));
        assert_eq!(apply_control("disarm", true, false), Some((false, false))); // alias of off
                                                                                // MONITOR ("Log mode"): armed + held -> never spray.
        assert_eq!(apply_control("monitor", false, false), Some((true, true)));
        assert_eq!(apply_control("monitor", true, false), Some((true, true)));
        // ACTIVE / arm: armed + free to spray.
        assert_eq!(apply_control("active", false, true), Some((true, false)));
        assert_eq!(apply_control("arm", false, true), Some((true, false))); // alias of active
                                                                            // Idempotent re-application is a no-op, not an error.
        assert_eq!(apply_control("active", true, false), Some((true, false)));
        assert_eq!(apply_control("monitor", true, true), Some((true, true)));
        // stop/resume are NOT mode changes (they mutate abort_cid) -> apply_control declines
        // them so the handler routes them separately. NOT a 400.
        assert_eq!(apply_control("stop", true, false), None);
        assert_eq!(apply_control("resume", true, true), None);
        // Truly unknown commands are rejected (caller returns HTTP 400).
        assert_eq!(apply_control("bogus", true, false), None);
        assert_eq!(apply_control("", true, false), None);
    }

    #[test]
    fn test_derive_mode_truth_table() {
        // armed=false -> OFF regardless of override_stop.
        assert_eq!(derive_mode(false, false), Mode::Off);
        assert_eq!(derive_mode(false, true), Mode::Off);
        // armed + held -> MONITOR; armed + free -> ACTIVE.
        assert_eq!(derive_mode(true, true), Mode::Monitor);
        assert_eq!(derive_mode(true, false), Mode::Active);
        // Wire strings the dashboard / contract consume.
        assert_eq!(Mode::Off.as_str(), "off");
        assert_eq!(Mode::Monitor.as_str(), "monitor");
        assert_eq!(Mode::Active.as_str(), "active");
    }

    #[test]
    fn test_heartbeat_continue_abort_truth_table() {
        // Floor is exactly heartbeat_continue: with NO abort, behavior is identical
        // (incl. fail-closed armed/override).
        assert!(heartbeat_continue_abort(
            Some(true),
            Some(false),
            "cid1",
            ""
        ));
        assert!(!heartbeat_continue_abort(
            Some(false),
            Some(false),
            "cid1",
            ""
        ));
        assert!(!heartbeat_continue_abort(
            Some(true),
            Some(true),
            "cid1",
            ""
        ));
        assert!(
            !heartbeat_continue_abort(None, Some(false), "cid1", ""),
            "unconfirmed armed -> disarmed even with no abort"
        );
        assert!(
            !heartbeat_continue_abort(Some(true), None, "cid1", ""),
            "unconfirmed override -> STOP even with no abort"
        );
        assert!(!heartbeat_continue_abort(None, None, "cid1", ""));

        // Abort matches THIS event -> suppress (even though the floor would spray).
        assert!(
            !heartbeat_continue_abort(Some(true), Some(false), "cid1", "cid1"),
            "abort_cid == event_cid -> aborted"
        );
        // Abort for a DIFFERENT event -> floor stands (keep spraying).
        assert!(
            heartbeat_continue_abort(Some(true), Some(false), "cid2", "cid1"),
            "abort for another spray does not suppress this one"
        );

        // Empty-cid edge cases: an empty abort never aborts; an empty event_cid never matches.
        assert!(
            heartbeat_continue_abort(Some(true), Some(false), "", "cid1"),
            "empty event_cid never matches a real abort"
        );
        assert!(
            heartbeat_continue_abort(Some(true), Some(false), "cid1", ""),
            "empty abort_cid never aborts"
        );
        assert!(
            heartbeat_continue_abort(Some(true), Some(false), "", ""),
            "both empty -> no abort (NOT an empty==empty match)"
        );

        // FAIL-SAFE: the abort can only ever turn a spray OFF, never on. A matching abort
        // on a NON-firing floor (disarmed) still yields no-spray.
        assert!(!heartbeat_continue_abort(
            Some(false),
            Some(false),
            "cid1",
            "cid1"
        ));
        // COMFORT-not-safety: a store-MISS must arrive here as abort_cid="" (the caller's
        // contract), so it degrades to "no abort" and never suppresses a legitimate spray.
        assert!(heartbeat_continue_abort(
            Some(true),
            Some(false),
            "cid1",
            ""
        ));
    }

    #[test]
    fn test_parse_flag_fallbacks() {
        assert!(parse_flag(Some("true".to_string()), false));
        assert!(!parse_flag(Some("false".to_string()), true));
        // Missing or unrecognized values fall back to the provided default.
        assert!(parse_flag(None, true));
        assert!(!parse_flag(None, false));
        assert!(parse_flag(Some("yes".to_string()), true));
        assert!(!parse_flag(Some("YES".to_string()), false));
    }

    #[test]
    fn test_heartbeat_continue_fails_closed_on_unreadable_flags() {
        // Both flags affirmatively read: normal truth table.
        assert!(
            heartbeat_continue(Some(true), Some(false)),
            "armed & not stopped -> keep firing"
        );
        assert!(
            !heartbeat_continue(Some(false), Some(false)),
            "confirmed disarmed -> stop"
        );
        assert!(
            !heartbeat_continue(Some(true), Some(true)),
            "confirmed stop -> stop"
        );
        // Fail-closed: an unreadable flag must never keep deterrents energized.
        assert!(
            !heartbeat_continue(None, Some(false)),
            "unconfirmed armed -> disarmed"
        );
        assert!(
            !heartbeat_continue(Some(true), None),
            "unconfirmed override -> STOP"
        );
        assert!(!heartbeat_continue(None, None), "nothing readable -> stop");
    }

    #[test]
    fn test_is_valid_trace_id() {
        assert!(is_valid_trace_id("0123456789abcdef"), "16-hex Pi id");
        assert!(
            is_valid_trace_id("edge-0123456789abcdef"),
            "edge fallback re-validates"
        );
        assert!(!is_valid_trace_id("short"), "< 8 chars rejected");
        assert!(
            !is_valid_trace_id("xyz!@#nothex1234"),
            "non-hex chars rejected"
        );
        assert!(!is_valid_trace_id(&"a".repeat(65)), "> 64 chars rejected");
        assert!(!is_valid_trace_id(""), "empty rejected");
    }

    #[test]
    fn test_extract_trace_id_passthrough_and_fallback() {
        // A plausible Pi id passes through unchanged.
        assert_eq!(
            extract_trace_id(Some("0123456789abcdef")),
            "0123456789abcdef"
        );
        // Absent or garbage -> a minted edge id that itself re-validates.
        let minted = extract_trace_id(None);
        assert!(
            minted.starts_with("edge-"),
            "absent header -> edge id, got {}",
            minted
        );
        assert!(is_valid_trace_id(&minted), "minted id must re-validate");
        assert!(
            extract_trace_id(Some("nope!")).starts_with("edge-"),
            "garbage -> edge id"
        );
    }

    #[test]
    fn test_mint_edge_id_format() {
        let id = mint_edge_id();
        assert!(id.starts_with("edge-"), "got {}", id);
        // "edge-" (5) + 16 hex chars = 21.
        assert_eq!(id.len(), 21, "got {}", id);
        assert!(
            id["edge-".len()..].bytes().all(|b| b.is_ascii_hexdigit()),
            "suffix must be hex"
        );
    }

    // --- Multi-garden keying (forward-compat, Steps 1–2) ---

    #[test]
    fn test_is_valid_id_charset_and_length() {
        // Accepted: the charset [a-z0-9-], length 1..=64.
        assert!(is_valid_id("default"));
        assert!(is_valid_id("g1"));
        assert!(is_valid_id("cam-front-01"));
        assert!(is_valid_id("a"), "min length 1");
        assert!(is_valid_id(&"a".repeat(64)), "max length 64");
        // Rejected: empty, too long, uppercase, underscore, and crucially `/`
        // (the cross-tenant key-injection guard) + non-ascii + whitespace.
        assert!(!is_valid_id(""), "empty rejected");
        assert!(!is_valid_id(&"a".repeat(65)), "> 64 rejected");
        assert!(!is_valid_id("Cam1"), "uppercase rejected");
        assert!(!is_valid_id("cam_1"), "underscore rejected");
        assert!(
            !is_valid_id("../armed"),
            "slash rejected (cross-tenant guard)"
        );
        assert!(!is_valid_id("g/1"), "slash rejected");
        assert!(!is_valid_id("café"), "non-ascii rejected");
        assert!(!is_valid_id("a b"), "space rejected");
    }

    #[test]
    fn test_resolve_safety_id_passthrough_and_fallback() {
        // A valid id passes through unchanged.
        assert_eq!(resolve_safety_id("g1", "t", "test", "garden_id"), "g1");
        assert_eq!(
            resolve_safety_id("default", "t", "test", "garden_id"),
            "default"
        );
        // Invalid ids degrade to "default" (a safety path NEVER rejects).
        assert_eq!(
            resolve_safety_id("../armed", "t", "test", "garden_id"),
            "default"
        );
        assert_eq!(resolve_safety_id("", "t", "test", "device_id"), "default");
        assert_eq!(
            resolve_safety_id("BAD", "t", "test", "garden_id"),
            "default"
        );
    }

    #[test]
    fn test_resolve_request_garden_id_header_wins_lazily() {
        // Header present -> used verbatim, and the fallback is NEVER evaluated
        // (the panic proves the Config Store isn't read on the machine hot path).
        assert_eq!(
            resolve_request_garden_id(Some("my-garden"), || panic!("fallback must not run")),
            "my-garden"
        );
        // Header-less (browser) -> the service's configured default garden.
        assert_eq!(
            resolve_request_garden_id(None, || Some("backyard".to_string())),
            "backyard"
        );
        // Header-less AND no configured default (shared svc / legacy) -> "default".
        assert_eq!(resolve_request_garden_id(None, || None), "default");
    }

    #[test]
    fn test_key_default_is_legacy_flat() {
        // Zero-migration: the default garden keeps the legacy flat keys.
        assert_eq!(key("default", "armed"), "armed");
        assert_eq!(key("default", "override_stop"), "override_stop");
    }

    #[test]
    fn test_key_namespaced_for_real_garden() {
        assert_eq!(key("g1", "armed"), "g/g1/armed");
        assert_eq!(key("backyard", "override_stop"), "g/backyard/override_stop");
    }

    #[test]
    fn test_dev_key_default_is_legacy_global() {
        // Default garden: per-device keys resolve to the legacy GLOBAL keys, and the
        // device id is intentionally IGNORED (a NON-default did still maps to flat).
        assert_eq!(
            dev_key("default", "cam-front", "latest_image"),
            "latest_image"
        );
        assert_eq!(
            dev_key("default", "cam-front", "latest_event"),
            "latest_event"
        );
    }

    #[test]
    fn test_dev_key_namespaced_for_real_garden() {
        assert_eq!(
            dev_key("g1", "cam1", "latest_image"),
            "g/g1/dev/cam1/latest_image"
        );
        assert_eq!(
            dev_key("g1", "cam1", "latest_event"),
            "g/g1/dev/cam1/latest_event"
        );
    }

    // --- Per-garden auth (Step 3, RFC §5) ---

    #[test]
    fn test_constant_time_eq() {
        assert!(
            constant_time_eq(b"s3cr3t-token", b"s3cr3t-token"),
            "equal bytes match"
        );
        assert!(
            !constant_time_eq(b"s3cr3t-token", b"s3cr3t-tokeN"),
            "one byte differs"
        );
        assert!(
            !constant_time_eq(b"short", b"longer-value"),
            "length mismatch -> false"
        );
        assert!(constant_time_eq(b"", b""), "empty == empty");
    }

    // --- viewer gate (PURE) ------------------------------------------------

    #[test]
    fn test_decide_viewer_gate() {
        // no password configured -> open (today's behavior)
        assert_eq!(decide_viewer_gate(None, false), ViewerGate::Open);
        // empty password treated as not configured
        assert_eq!(decide_viewer_gate(Some(""), false), ViewerGate::Open);
        // configured + valid cookie -> allowed
        assert_eq!(
            decide_viewer_gate(Some("hunter2"), true),
            ViewerGate::Allowed
        );
        // configured + no/invalid cookie -> login
        assert_eq!(
            decide_viewer_gate(Some("hunter2"), false),
            ViewerGate::NeedLogin
        );
    }

    #[test]
    fn test_browser_read_allowed_token_bypasses_viewer_gate() {
        use AuthOutcome::*;
        use ViewerGate::*;
        // A valid per-garden token (the trusted LAN portal / console proxy) serves
        // regardless of the viewer gate — the LAN admin already authenticated.
        assert!(
            browser_read_allowed(Authorized, NeedLogin),
            "valid token bypasses the viewer gate"
        );
        assert!(browser_read_allowed(Authorized, Open));
        assert!(browser_read_allowed(Authorized, Allowed));
        // No / wrong token -> the public viewer gate decides.
        assert!(
            !browser_read_allowed(Tokenless, NeedLogin),
            "public browser, viewer pass set, no cookie -> blocked"
        );
        assert!(
            !browser_read_allowed(Rejected, NeedLogin),
            "wrong token falls through to the gate -> blocked"
        );
        assert!(
            browser_read_allowed(Tokenless, Open),
            "no viewer password -> open (legacy)"
        );
        assert!(
            browser_read_allowed(Tokenless, Allowed),
            "valid viewer cookie -> served"
        );
    }

    #[test]
    fn test_viewer_cookie_roundtrip_expiry_tamper() {
        let pass = b"garden-view";
        let cookie = mint_viewer_cookie(pass, 1000);
        assert!(
            verify_viewer_cookie(&cookie, pass, 999),
            "valid before expiry"
        );
        assert!(!verify_viewer_cookie(&cookie, pass, 1000), "expired at exp");
        assert!(
            !verify_viewer_cookie(&cookie, pass, 1001),
            "expired after exp"
        );
        assert!(
            !verify_viewer_cookie(&cookie, b"wrong-pass", 999),
            "wrong password -> false"
        );
        // tamper with the MAC
        let bad = format!("{}0", &cookie[..cookie.len() - 1]);
        assert!(
            !verify_viewer_cookie(&bad, pass, 999),
            "tampered MAC -> false"
        );
        // malformed shapes
        assert!(!verify_viewer_cookie("nodot", pass, 999));
        assert!(!verify_viewer_cookie("notanumber.deadbeef", pass, 999));
    }

    #[test]
    fn test_cookie_from_header() {
        let h = "foo=1; gp_viewer=abc.def; bar=2";
        assert_eq!(
            cookie_from_header(Some(h), "gp_viewer").as_deref(),
            Some("abc.def")
        );
        assert_eq!(cookie_from_header(Some(h), "missing"), None);
        assert_eq!(cookie_from_header(None, "gp_viewer"), None);
        assert_eq!(
            cookie_from_header(Some("gp_viewer=solo"), "gp_viewer").as_deref(),
            Some("solo")
        );
    }

    #[test]
    fn test_form_field_and_percent_decode() {
        assert_eq!(form_field(b"passcode=hunter2", "passcode"), "hunter2");
        assert_eq!(
            form_field(b"a=1&passcode=p%40ss+word&b=2", "passcode"),
            "p@ss word"
        );
        assert_eq!(form_field(b"other=x", "passcode"), "");
        assert_eq!(percent_decode("a%2Bb"), "a+b");
    }

    #[test]
    fn test_token_secret_name_is_slash_free() {
        use crate::contract_gen::token_secret_name;
        // Fastly secret names forbid `/`; the encoding uses `.` and is shared with
        // the Python provisioner. gid charset [a-z0-9-] never contains `.`.
        assert_eq!(
            token_secret_name("backyard", "token_current"),
            "g.backyard.token_current"
        );
        assert_eq!(
            token_secret_name("g1", "token_previous"),
            "g.g1.token_previous"
        );
        assert!(
            !token_secret_name("g1", "token_current").contains('/'),
            "must be slash-free"
        );
    }

    #[test]
    fn test_decide_auth_default_is_always_tokenless() {
        // The `default` garden is NEVER issued a token; enforcement is skipped even
        // if (somehow) secrets are present and a credential is missing.
        assert_eq!(decide_auth(true, None, None, None), AuthOutcome::Tokenless);
        assert_eq!(
            decide_auth(true, None, Some("tok"), None),
            AuthOutcome::Tokenless
        );
        assert_eq!(
            decide_auth(true, Some("wrong"), Some("tok"), None),
            AuthOutcome::Tokenless
        );
    }

    #[test]
    fn test_decide_auth_enforce_iff_token() {
        // No token configured for a non-default garden -> not enforced (tokenless).
        assert_eq!(decide_auth(false, None, None, None), AuthOutcome::Tokenless);
        assert_eq!(
            decide_auth(false, Some("anything"), None, None),
            AuthOutcome::Tokenless
        );
    }

    #[test]
    fn test_decide_auth_current_and_previous_match() {
        // Current matches.
        assert_eq!(
            decide_auth(false, Some("cur"), Some("cur"), None),
            AuthOutcome::Authorized
        );
        // Previous matches (rotation window).
        assert_eq!(
            decide_auth(false, Some("prev"), Some("cur"), Some("prev")),
            AuthOutcome::Authorized
        );
    }

    #[test]
    fn test_decide_auth_rejects_bad_or_absent_credential() {
        // Wrong token.
        assert_eq!(
            decide_auth(false, Some("nope"), Some("cur"), Some("prev")),
            AuthOutcome::Rejected
        );
        // Absent / empty header when a token IS configured.
        assert_eq!(
            decide_auth(false, None, Some("cur"), None),
            AuthOutcome::Rejected
        );
        assert_eq!(
            decide_auth(false, Some(""), Some("cur"), None),
            AuthOutcome::Rejected
        );
    }

    #[test]
    fn test_decide_auth_cross_garden_replay_guard() {
        // The REAL tenancy boundary: garden A's token presented for garden B. The
        // caller resolves the secrets under B's CLAIMED id, so B's `current` is B's
        // token, not A's — A's token therefore never matches. Modeled here by passing
        // B's secret while presenting A's token.
        let token_a = "token-for-garden-a";
        let secret_b_current = Some("token-for-garden-b");
        assert_eq!(
            decide_auth(false, Some(token_a), secret_b_current, None),
            AuthOutcome::Rejected,
            "a token minted for garden A must be rejected against garden B"
        );
    }

    #[test]
    fn test_heartbeat_default_tuple_selection() {
        // The `default` garden keeps the Config-Store authority (armed=true here);
        // a non-default garden gets the fail-closed tuple so absent keys -> stop.
        let cfg = (true, false);
        let default_defaults = if "default" == "default" {
            cfg
        } else {
            (false, true)
        };
        assert_eq!(
            default_defaults,
            (true, false),
            "default garden keeps config authority"
        );
        // For a non-default gid the tuple is the fail-closed (false, true) ->
        // heartbeat_continue(false, true) == false.
        assert!(
            !heartbeat_continue(Some(false), Some(true)),
            "non-default absent keys -> stop"
        );
    }

    // --- Admin / CRUD routes (Step 3, RFC §3) ---

    #[test]
    fn test_parse_admin_route_all_shapes() {
        use AdminRoute::*;
        assert_eq!(
            parse_admin_route(&Method::GET, "/api/gardens"),
            Some(ListGardens)
        );
        assert_eq!(
            parse_admin_route(&Method::GET, "/api/gardens/"),
            Some(ListGardens)
        );
        assert_eq!(
            parse_admin_route(&Method::POST, "/api/gardens"),
            Some(CreateGarden)
        );
        assert_eq!(
            parse_admin_route(&Method::GET, "/api/gardens/g1"),
            Some(GetGarden("g1".into()))
        );
        assert_eq!(
            parse_admin_route(&Method::GET, "/api/gardens/g1/devices"),
            Some(ListDevices("g1".into()))
        );
        assert_eq!(
            parse_admin_route(&Method::POST, "/api/gardens/g1/devices"),
            Some(RegisterDevice("g1".into()))
        );
        assert_eq!(
            parse_admin_route(&Method::GET, "/api/gardens/g1/devices/cam1/event"),
            Some(DeviceEvent("g1".into(), "cam1".into()))
        );
        assert_eq!(
            parse_admin_route(&Method::GET, "/api/gardens/g1/devices/cam1/snapshot"),
            Some(DeviceSnapshot("g1".into(), "cam1".into()))
        );
        assert_eq!(
            parse_admin_route(&Method::POST, "/api/gardens/g1/control"),
            Some(GardenControl("g1".into()))
        );
    }

    #[test]
    fn test_parse_admin_route_rejects_non_tree_and_bad_shapes() {
        // Outside the tree -> None (handled by the 404 fallback).
        assert_eq!(parse_admin_route(&Method::GET, "/api/state"), None);
        assert_eq!(parse_admin_route(&Method::GET, "/"), None);
        // Prefix must be followed by `/` or end — not a substring match.
        assert_eq!(parse_admin_route(&Method::GET, "/api/gardensXYZ"), None);
        // Unknown method/shape combos -> None.
        assert_eq!(parse_admin_route(&Method::DELETE, "/api/gardens/g1"), None);
        assert_eq!(
            parse_admin_route(&Method::GET, "/api/gardens/g1/devices/cam1/bogus"),
            None
        );
        assert_eq!(
            parse_admin_route(&Method::PUT, "/api/gardens/g1/control"),
            None
        );
    }

    #[test]
    fn test_registry_serde_round_trip_with_type_rename_and_version() {
        let reg = DevicesRegistry {
            v: 1,
            garden_id: "g1".into(),
            updated_ts: 1234,
            devices: vec![
                Device {
                    device_id: "cam-front".into(),
                    node_id: "pi-01".into(),
                    kind: "observer".into(),
                    dev_type: "camera_usb".into(),
                    name: "Front cam".into(),
                    status: "active".into(),
                    can_trigger_alarm: false,
                    can_confirm_alarm: true,
                },
                Device {
                    device_id: "pir-1".into(),
                    node_id: "pi-01".into(),
                    kind: "observer".into(),
                    dev_type: "motion_pir".into(),
                    name: "Motion".into(),
                    status: "active".into(),
                    can_trigger_alarm: true,
                    can_confirm_alarm: false,
                },
            ],
        };
        let json = serde_json::to_string(&reg).unwrap();
        // Existing registry JSON written BEFORE the role fields must still parse (serde default
        // => false), so an upgrade reads old devices without a migration.
        let legacy = r#"{"v":1,"garden_id":"g1","updated_ts":1,"devices":[{"device_id":"d","node_id":"n","kind":"observer","type":"camera_usb","name":"C","status":"active"}]}"#;
        let legacy_reg: DevicesRegistry = serde_json::from_str(legacy).unwrap();
        assert!(
            !legacy_reg.devices[0].can_trigger_alarm && !legacy_reg.devices[0].can_confirm_alarm
        );
        // `type` is serialized (not `dev_type`); `v` is present.
        assert!(
            json.contains("\"type\":\"camera_usb\""),
            "type rename in output: {}",
            json
        );
        assert!(
            json.contains("\"type\":\"motion_pir\""),
            "non-camera device type serialized: {}",
            json
        );
        assert!(json.contains("\"v\":1"));
        let back: DevicesRegistry = serde_json::from_str(&json).unwrap();
        assert_eq!(back, reg, "round-trips losslessly");
    }

    fn dev_with_roles(can_trigger: bool, can_confirm: bool) -> Device {
        Device {
            device_id: "cam-front".into(),
            node_id: "pi-01".into(),
            kind: "observer".into(),
            dev_type: "camera_usb".into(),
            name: "Front".into(),
            status: "active".into(),
            can_trigger_alarm: can_trigger,
            can_confirm_alarm: can_confirm,
        }
    }

    #[test]
    fn test_alarm_gate_needs_role_and_marker() {
        let trigger_cam = dev_with_roles(true, true);
        let confirm_cam = dev_with_roles(false, true);
        // A trigger device records an alarm ONLY when the push carries a marker (a real motion
        // event); its routine cadence frames (no marker) stay History-only.
        assert!(alarm_should_record(&trigger_cam, true));
        assert!(!alarm_should_record(&trigger_cam, false));
        // A confirm-only device never alarms, marker or not.
        assert!(!alarm_should_record(&confirm_cam, true));
        assert!(!alarm_should_record(&confirm_cam, false));
    }

    #[test]
    fn test_compose_alarm_reason() {
        // Trigger cause leads; an action-suppression note (rain) is appended.
        assert_eq!(
            compose_alarm_reason(Some("motion 4.2% x3"), Some("rain")).as_deref(),
            Some("motion 4.2% x3; rain")
        );
        assert_eq!(
            compose_alarm_reason(Some("motion 4.2% x3"), None).as_deref(),
            Some("motion 4.2% x3")
        );
        // No marker => falls back to whatever reason existed (record path is gated off anyway).
        assert_eq!(
            compose_alarm_reason(None, Some("rain")).as_deref(),
            Some("rain")
        );
        assert_eq!(compose_alarm_reason(None, None), None);
    }

    #[test]
    fn test_cap_trigger_marker_bounds_and_preserves_presence() {
        // Short real markers pass through verbatim (the alarm gate keys on PRESENCE).
        assert_eq!(
            cap_trigger_marker(Some("motion 4.2% x3")).as_deref(),
            Some("motion 4.2% x3")
        );
        // Surrounding whitespace is trimmed.
        assert_eq!(
            cap_trigger_marker(Some("  motion  ")).as_deref(),
            Some("motion")
        );
        // An oversized attacker marker is capped to MAX_TRIGGER_MARKER_CHARS chars (still Some,
        // so capping never disables the alarm gate — presence is preserved).
        let huge = "x".repeat(10_000);
        let capped = cap_trigger_marker(Some(&huge)).expect("non-empty stays Some");
        assert_eq!(capped.chars().count(), MAX_TRIGGER_MARKER_CHARS);
        // A marker EXACTLY at the cap is unchanged.
        let exact = "y".repeat(MAX_TRIGGER_MARKER_CHARS);
        assert_eq!(
            cap_trigger_marker(Some(&exact)).as_deref(),
            Some(exact.as_str())
        );
        // Multi-byte chars are counted by char, not byte, and never sliced mid-codepoint.
        let emoji = "🌱".repeat(MAX_TRIGGER_MARKER_CHARS + 5);
        let emoji_capped = cap_trigger_marker(Some(&emoji)).expect("non-empty");
        assert_eq!(emoji_capped.chars().count(), MAX_TRIGGER_MARKER_CHARS);
        // Absent / empty / whitespace-only markers drop to None (no alarm marker present).
        assert_eq!(cap_trigger_marker(None), None);
        assert_eq!(cap_trigger_marker(Some("")), None);
        assert_eq!(cap_trigger_marker(Some("   ")), None);
    }

    // NOTE: the admin id->400 / auth->401 / 405 RESPONSE behaviors construct a
    // `fastly::Response`, whose host ABI symbol is unavailable in the native
    // `cargo test` link. They are covered by the Viceroy curl smoke tests instead.
    // The pure predicate behind id->400 is `is_valid_id` (tested above).

    // --- Evidence archive (Step 3, RFC §2) ---

    #[test]
    fn test_invert_seconds_of_day() {
        assert_eq!(invert_seconds_of_day(0, 0, 0), "86400"); // earliest -> largest -> sorts LAST
        assert_eq!(invert_seconds_of_day(23, 59, 59), "00001"); // latest -> smallest -> sorts FIRST
        assert_eq!(invert_seconds_of_day(12, 0, 0), "43200");
        // 5-digit zero-padded; later in the day sorts lexically before earlier (newest first).
        assert!(invert_seconds_of_day(10, 0, 0) > invert_seconds_of_day(11, 0, 0));
        assert_eq!(invert_seconds_of_day(0, 0, 1).len(), 5);
    }

    #[test]
    fn test_evidence_object_key_format() {
        // Date-first, INV-prefixed within-day, metadata embedded, garden ALWAYS present.
        // ts=0 -> 1970/01/01 00:00:00 -> INV 86400. The <batch> segment sits before <cid>.
        assert_eq!(
            evidence_object_key(
                "default",
                "cam-front",
                "none",
                "raccoon",
                0.87,
                "abc123",
                "b7",
                0
            ),
            "g/default/evidence/1970/01/01/86400_000000_none_raccoon_87_cam-front_b7_abc123.jpg"
        );
        // 2021-01-02T03:04:05Z (11045s -> INV 75355); space slugs to red-fox; conf rounds.
        // An empty batch slugs to "none" (the ungrouped marker the UI ignores).
        assert_eq!(
            evidence_object_key(
                "backyard",
                "pir-1",
                "mitigate",
                "red fox",
                0.5,
                "cid9",
                "",
                1_609_556_645
            ),
            "g/backyard/evidence/2021/01/02/75355_030405_mitigate_red-fox_50_pir-1_none_cid9.jpg"
        );
    }

    #[test]
    fn test_archive_key_round_trip_and_legacy_skip() {
        // A device id with hyphens must survive the `_`-delimited filename intact, and the
        // shared batch id round-trips into its own field (multi-angle grouping key).
        let key = evidence_object_key(
            "g1",
            "csi-camera-imx219",
            "mitigate",
            "class_42",
            0.91,
            "edge-abc123",
            "tick-9f2a",
            1_609_556_645,
        );
        let ev = parse_archive_key("g1", &key).expect("parses its own key");
        assert_eq!(ev["date"], "2021-01-02");
        assert_eq!(ev["time"], "03:04:05");
        assert_eq!(ev["action"], "mitigate");
        assert_eq!(ev["species"], "class-42"); // '_' slugged out
        assert_eq!(ev["confidence"], 91);
        assert_eq!(ev["device"], "csi-camera-imx219");
        assert_eq!(ev["batch"], "tick-9f2a");
        assert_eq!(ev["cid"], "edge-abc123");
        assert_eq!(ev["key"], key);
        // BACKWARD COMPAT: a legacy 7-field key (no <batch>) still parses, batch == "".
        let legacy = "g/g1/evidence/2021/01/02/75355_030405_mitigate_red-fox_50_pir-1_cid9.jpg";
        let lev = parse_archive_key("g1", legacy).expect("legacy key still parses");
        assert_eq!(lev["device"], "pir-1");
        assert_eq!(lev["cid"], "cid9");
        assert_eq!(lev["batch"], "");
        // Legacy device-first keys (and other gardens) are not in this scheme -> None.
        assert!(parse_archive_key("g1", "g/g1/csi-cam/evidence/2026/06/21/120000-x.jpg").is_none());
        assert!(parse_archive_key(
            "g1",
            "g/other/evidence/2026/06/21/75355_120000_none_none_0_d_c.jpg"
        )
        .is_none());
        // Prior date-first key WITHOUT the INV prefix (6 fields) -> skipped (None).
        assert!(
            parse_archive_key("g1", "g/g1/evidence/2026/06/21/120000_none_none_0_d_c.jpg")
                .is_none()
        );
    }

    #[test]
    fn test_parse_limit() {
        assert_eq!(parse_limit(None), None);
        assert_eq!(parse_limit(Some("")), None);
        assert_eq!(parse_limit(Some("abc")), None);
        assert_eq!(parse_limit(Some("0")), Some(1)); // clamped up
        assert_eq!(parse_limit(Some("12")), Some(12));
        assert_eq!(parse_limit(Some(" 5 ")), Some(5));
        assert_eq!(parse_limit(Some("99999")), Some(1000)); // clamped to max-keys
    }

    #[test]
    fn test_prune_cutoff_and_expiry() {
        // 2021-01-02T03:04:05Z. days=1 -> cutoff 2021-01-01; days=2 crosses the year.
        assert_eq!(prune_cutoff_date(1_609_556_645, 1), "2021-01-01");
        assert_eq!(prune_cutoff_date(1_609_556_645, 2), "2020-12-31"); // year rollover
        assert!(day_is_expired("2020-12-31", "2021-01-01"));
        assert!(!day_is_expired("2021-01-01", "2021-01-01")); // cutoff day is kept
        assert!(!day_is_expired("2021-01-02", "2021-01-01"));
    }

    #[test]
    fn test_days_to_prune_selection() {
        // evidence_days returns newest-first; selection filters expired + sorts oldest-first.
        let all = vec![
            "2021-01-05".to_string(),
            "2021-01-04".to_string(),
            "2021-01-01".to_string(),
            "2020-12-31".to_string(),
        ];
        assert_eq!(
            days_to_prune(&all, "2021-01-03", 10),
            vec!["2020-12-31".to_string(), "2021-01-01".to_string()]
        );
        // Capped per run (oldest first).
        assert_eq!(
            days_to_prune(&all, "2021-01-03", 1),
            vec!["2020-12-31".to_string()]
        );
        // Nothing expired.
        assert!(days_to_prune(&all, "2020-01-01", 10).is_empty());
    }

    #[test]
    fn test_days_to_wipe_selection() {
        // Wipe targets EVERY day (no cutoff), oldest-first, capped per run.
        let all = vec![
            "2021-01-05".to_string(),
            "2021-01-04".to_string(),
            "2021-01-01".to_string(),
            "2020-12-31".to_string(),
        ];
        assert_eq!(
            days_to_wipe(&all, 10),
            vec![
                "2020-12-31".to_string(),
                "2021-01-01".to_string(),
                "2021-01-04".to_string(),
                "2021-01-05".to_string()
            ]
        );
        // Capped per run (oldest first) so one invocation stays bounded.
        assert_eq!(
            days_to_wipe(&all, 2),
            vec!["2020-12-31".to_string(), "2021-01-01".to_string()]
        );
        // No days -> nothing to wipe.
        assert!(days_to_wipe(&[], 10).is_empty());
    }

    #[test]
    fn test_slug_for_key() {
        assert_eq!(slug_for_key("class_42"), "class-42");
        assert_eq!(slug_for_key("Red Fox!"), "red-fox");
        assert_eq!(slug_for_key("csi-camera-imx219"), "csi-camera-imx219");
        assert_eq!(slug_for_key(""), "none");
        assert_eq!(slug_for_key("  -- "), "none");
        assert_eq!(slug_for_key("MITIGATE"), "mitigate");
    }

    #[test]
    fn test_xml_inner_all_keys_and_prefixes() {
        let xml = "<ListBucketResult><Contents><Key>g/x/a.jpg</Key></Contents>\
            <Contents><Key>g/x/b.jpg</Key></Contents>\
            <CommonPrefixes><Prefix>g/x/2026/</Prefix></CommonPrefixes>\
            <IsTruncated>false</IsTruncated></ListBucketResult>";
        assert_eq!(
            xml_inner_all(xml, "<Key>", "</Key>"),
            vec!["g/x/a.jpg", "g/x/b.jpg"]
        );
        let cps = xml_inner_all(xml, "<CommonPrefixes>", "</CommonPrefixes>");
        assert_eq!(cps.len(), 1);
        assert_eq!(
            xml_inner_all(&cps[0], "<Prefix>", "</Prefix>"),
            vec!["g/x/2026/"]
        );
    }

    #[test]
    fn test_archive_image_key_ok_scope_and_traversal() {
        let good = "g/default/evidence/2026/06/21/120000_none_raccoon_87_cam-front_abc.jpg";
        assert!(archive_image_key_ok("default", good));
        assert!(archive_image_key_ok("my-garden", "g/my-garden/evidence/2026/06/21/120000_mitigate_raccoon_90_csi-camera-imx219_edge-abc.jpg"));
        // Traversal (the decoded form of %2e%2e) -> rejected.
        assert!(!archive_image_key_ok(
            "default",
            "g/default/evidence/../../other/evidence/x.jpg"
        ));
        // Cross-tenant prefix -> rejected.
        assert!(!archive_image_key_ok(
            "default",
            "g/other/evidence/2026/06/21/x.jpg"
        ));
        // Non-jpg -> rejected.
        assert!(!archive_image_key_ok(
            "default",
            "g/default/evidence/2026/06/21/x.txt"
        ));
        // Leftover '%' (double-encoding), whitespace, empty segment, uppercase -> rejected.
        assert!(!archive_image_key_ok(
            "default",
            "g/default/evidence/2026/%2e/x.jpg"
        ));
        assert!(!archive_image_key_ok(
            "default",
            "g/default/evidence/2026/ /x.jpg"
        ));
        assert!(!archive_image_key_ok(
            "default",
            "g/default/evidence//x.jpg"
        ));
        assert!(!archive_image_key_ok(
            "default",
            "g/default/evidence/2026/06/21/X.jpg"
        ));
    }

    #[test]
    fn test_cdn_img_url_shape() {
        // Bucket-relative key (no bucket prefix) under the /img/ vanity prefix the CDN
        // VCL strips; the secret is NOT in the URL (it rides the x-fastly-key header).
        assert_eq!(
            cdn_img_url(
                "svc-cdn.global.ssl.fastly.net",
                "g/default/evidence/2026/06/21/120000_none_raccoon_87_cam_abc.jpg"
            ),
            "https://svc-cdn.global.ssl.fastly.net/img/g/default/evidence/2026/06/21/120000_none_raccoon_87_cam_abc.jpg"
        );
        assert!(!cdn_img_url("h", "g/x/evidence/a.jpg").contains("key="));
    }

    #[test]
    fn test_archive_date_prefix_helpers() {
        assert_eq!(
            archive_day_prefix("g1", "2026-06-21"),
            Some("g/g1/evidence/2026/06/21/".into())
        );
        assert_eq!(archive_day_prefix("g1", "2026-6-21"), None); // not zero-padded
        assert_eq!(archive_day_prefix("g1", "2026/06/21"), None);
        assert_eq!(archive_day_prefix("g1", "../etc"), None);
    }

    #[test]
    fn test_day_from_key() {
        // Works for the current INV-prefixed key...
        assert_eq!(
            day_from_key(
                "g1",
                "g/g1/evidence/2026/06/21/75355_030405_none_x_0_cam_cid.jpg"
            ),
            Some("2026-06-21".into())
        );
        // ...AND legacy/old-format filenames under a YYYY/MM/DD/ path (so prune finds them).
        assert_eq!(
            day_from_key("g1", "g/g1/evidence/2020/01/01/anything-at-all.jpg"),
            Some("2020-01-01".into())
        );
        assert_eq!(day_from_key("g1", "g/g1/evidence/2026/06/file.jpg"), None); // too shallow
        assert_eq!(
            day_from_key("g1", "g/other/evidence/2026/06/21/x.jpg"),
            None
        ); // other garden
        assert_eq!(day_from_key("g1", "g/g1/evidence/20x6/06/21/x.jpg"), None); // non-digit year
    }

    // --- Node liveness + telemetry + rain veto ---

    #[test]
    fn test_node_liveness_online_offline_and_unseen() {
        let now = 1_000_000u64; // ms
                                // Seen 10 s ago -> online, age 10 s.
        assert_eq!(node_liveness(Some(now - 10_000), now), (true, Some(10)));
        // Exactly at the threshold -> still online.
        assert_eq!(
            node_liveness(Some(now - NODE_OFFLINE_AFTER_SECS * 1000), now),
            (true, Some(NODE_OFFLINE_AFTER_SECS))
        );
        // One second past the threshold -> offline.
        assert_eq!(
            node_liveness(Some(now - (NODE_OFFLINE_AFTER_SECS + 1) * 1000), now),
            (false, Some(NODE_OFFLINE_AFTER_SECS + 1))
        );
        // Never seen -> offline, no age.
        assert_eq!(node_liveness(None, now), (false, None));
        // Future timestamp (clock skew) -> treated as just-seen.
        assert_eq!(node_liveness(Some(now + 5_000), now), (true, Some(0)));
    }

    #[test]
    fn test_build_telemetry_record_stamps_and_wraps() {
        let rec = build_telemetry_record(
            serde_json::json!({"temperature_c": 18.5, "raining": true}),
            12345,
        );
        assert_eq!(
            rec.get("temperature_c").and_then(|v| v.as_f64()),
            Some(18.5)
        );
        assert_eq!(rec.get("raining").and_then(|v| v.as_bool()), Some(true));
        // The edge stamps its own receipt time regardless of the device clock.
        assert_eq!(
            rec.get("last_seen_ms").and_then(|v| v.as_u64()),
            Some(12345)
        );
        // A non-object body is wrapped under `raw` and still stamped.
        let wrapped = build_telemetry_record(serde_json::json!("hello"), 7);
        assert_eq!(wrapped.get("raw").and_then(|v| v.as_str()), Some("hello"));
        assert_eq!(
            wrapped.get("last_seen_ms").and_then(|v| v.as_u64()),
            Some(7)
        );
    }

    #[test]
    fn test_rain_should_suppress_only_fresh_raining_mitigate() {
        let now = 1_000_000u64;
        let fresh_rain = serde_json::json!({"raining": true, "last_seen_ms": now - 5_000});
        // Fresh rain + mitigate -> suppress.
        assert!(rain_should_suppress("mitigate", &fresh_rain, now));
        // FAIL-SAFE: never acts on a non-mitigate action (can't turn a spray ON).
        assert!(!rain_should_suppress("none", &fresh_rain, now));
        // Not raining -> no veto.
        let dry = serde_json::json!({"raining": false, "last_seen_ms": now});
        assert!(!rain_should_suppress("mitigate", &dry, now));
        // Stale rain telemetry -> ignored (node may be down).
        let stale = serde_json::json!({"raining": true, "last_seen_ms": now - (RAIN_TELEMETRY_FRESH_SECS + 5) * 1000});
        assert!(!rain_should_suppress("mitigate", &stale, now));
        // Raining but no receipt stamp -> not trusted.
        assert!(!rain_should_suppress(
            "mitigate",
            &serde_json::json!({"raining": true}),
            now
        ));
        // Null telemetry (no node yet) -> no veto.
        assert!(!rain_should_suppress(
            "mitigate",
            &serde_json::Value::Null,
            now
        ));
    }

    #[test]
    fn test_rain_veto_is_monotone_and_idempotent() {
        // SAFETY INVARIANT (fail-safe by construction): the rain veto can ONLY ever
        // downgrade `mitigate` -> `none`, NEVER the reverse, and re-applying it is a
        // no-op. This pins the property handle_evidence relies on: a comfort
        // optimization that can never turn a spray ON. Demonstrably fails if the
        // `action == "mitigate"` guard is removed from `rain_should_suppress`.
        let now = 1_000_000u64;
        let fresh_rain = serde_json::json!({"raining": true, "last_seen_ms": now - 5_000});
        let dry = serde_json::json!({"raining": false, "last_seen_ms": now});
        let stale = serde_json::json!(
            {"raining": true, "last_seen_ms": now - (RAIN_TELEMETRY_FRESH_SECS + 5) * 1000});
        let null = serde_json::Value::Null;
        let telemetries = [&fresh_rain, &dry, &stale, &null];

        for t in telemetries {
            for action in ["mitigate", "none", "", "spray", "MITIGATE"] {
                // MONOTONE: a suppression may fire ONLY for a `mitigate` action, so the
                // veto can never fabricate a spray from a non-spray action.
                if rain_should_suppress(action, t, now) {
                    assert_eq!(
                        action, "mitigate",
                        "rain veto fired on non-mitigate action {:?} -> not fail-safe",
                        action
                    );
                }
            }
            // IDEMPOTENT: `none` is the post-veto action; the veto must be a fixed point
            // there, so applying it twice equals applying it once.
            assert!(
                !rain_should_suppress("none", t, now),
                "rain veto must be a no-op on an already-suppressed (none) action"
            );
        }
    }

    // --- Property-based fail-closed / traversal-guard proofs (proptest, native-only) ---
    //
    // The example tests above pin specific truth-table rows; these PROVE the two
    // load-bearing invariants over arbitrary input, the "fail-closed must be PROVEN"
    // standard the product holds itself to (the Python side has hypothesis property
    // tests in tests/test_safety_properties.py — this is the Rust counterpart).
    proptest::proptest! {
        /// INVARIANT: the rain veto can NEVER fire for any action other than "mitigate".
        /// It is a comfort optimization that may only ever downgrade mitigate -> none, so
        /// for ANY non-"mitigate" action, over arbitrary telemetry + raining flag + clocks,
        /// rain_should_suppress MUST be false (it can never fabricate or affect a spray).
        #[test]
        fn prop_rain_never_suppresses_non_mitigate(
            action in "[a-zA-Z]{0,12}",
            raining in proptest::bool::ANY,
            last_seen_ms in proptest::option::of(0u64..2_000_000_000_000u64),
            now_ms in 0u64..2_000_000_000_000u64,
        ) {
            // Skip the one action the veto is allowed to act on; everything else is fair game.
            proptest::prop_assume!(action != "mitigate");
            let mut telemetry = serde_json::json!({ "raining": raining });
            if let Some(ts) = last_seen_ms {
                telemetry["last_seen_ms"] = serde_json::json!(ts);
            }
            proptest::prop_assert!(
                !rain_should_suppress(&action, &telemetry, now_ms),
                "rain veto fired on non-mitigate action {:?} -> NOT fail-safe", action
            );
        }

        /// INVARIANT: the archive traversal/charset guard NEVER accepts a key containing
        /// `..`, `//`, or a byte outside the `[a-z0-9/_.-]` charset, regardless of the
        /// garden id and regardless of how the rest of the key is shaped. (Acceptance is
        /// additionally gated on the `g/{gid}/evidence/` prefix + `.jpg` suffix; this
        /// property asserts the NEGATIVE half — the SECURITY-bearing rejections — over
        /// arbitrary strings, so no byte sequence with a traversal/illegal char can pass.)
        #[test]
        fn prop_archive_key_rejects_traversal_and_bad_bytes(
            gid in "[a-z0-9-]{1,16}",
            // Arbitrary keys, biased to include the dangerous substrings & bytes.
            key in proptest::string::string_regex("[a-zA-Z0-9/._%. -]{0,64}").unwrap(),
        ) {
            let bad_charset = key
                .bytes()
                .any(|b| !matches!(b, b'a'..=b'z' | b'0'..=b'9' | b'/' | b'_' | b'.' | b'-'));
            if key.contains("..") || key.contains("//") || bad_charset {
                proptest::prop_assert!(
                    !archive_image_key_ok(&gid, &key),
                    "traversal/illegal-byte key was ACCEPTED: gid={:?} key={:?}", gid, key
                );
            }
        }

        /// INVARIANT (stronger, generative): explicitly INJECT `..` into an otherwise
        /// well-formed in-scope `.jpg` key and assert it is still rejected — the traversal
        /// guard must hold even when the prefix + suffix would otherwise qualify.
        #[test]
        fn prop_archive_key_rejects_injected_dotdot(
            gid in "[a-z0-9-]{1,16}",
            seg in "[a-z0-9_-]{1,8}",
        ) {
            let key = format!("g/{}/evidence/2026/06/21/../{}.jpg", gid, seg);
            proptest::prop_assert!(
                !archive_image_key_ok(&gid, &key),
                "injected `..` traversal accepted: {:?}", key
            );
        }
    }

    #[test]
    fn test_parse_admin_route_device_telemetry() {
        assert_eq!(
            parse_admin_route(&Method::GET, "/api/gardens/g1/devices/cam1/telemetry"),
            Some(AdminRoute::DeviceTelemetry("g1".into(), "cam1".into()))
        );
    }

    // --- Node-down alert (edge -> Twilio) pure builders ---

    #[test]
    fn test_base64_encode_rfc4648_vectors() {
        assert_eq!(base64_encode(b""), "");
        assert_eq!(base64_encode(b"f"), "Zg==");
        assert_eq!(base64_encode(b"fo"), "Zm8=");
        assert_eq!(base64_encode(b"foo"), "Zm9v");
        assert_eq!(base64_encode(b"foob"), "Zm9vYg==");
        assert_eq!(base64_encode(b"fooba"), "Zm9vYmE=");
        assert_eq!(base64_encode(b"foobar"), "Zm9vYmFy");
        assert_eq!(base64_encode(b"Man"), "TWFu");
        assert_eq!(base64_encode(b"Hello, World!"), "SGVsbG8sIFdvcmxkIQ==");
    }

    #[test]
    fn test_twilio_basic_auth() {
        // base64("u:p") == "dTpw"
        assert_eq!(twilio_basic_auth("u", "p"), "Basic dTpw");
        assert!(twilio_basic_auth("AC123", "tok").starts_with("Basic "));
    }

    #[test]
    fn test_form_urlencode() {
        assert_eq!(form_urlencode("a b+c"), "a%20b%2Bc");
        assert_eq!(form_urlencode("+15551234567"), "%2B15551234567");
        assert_eq!(form_urlencode("plain.Text-1_~"), "plain.Text-1_~");
    }

    #[test]
    fn test_twilio_form_body() {
        assert_eq!(
            twilio_form_body("+1555", "+1444", "hi there"),
            "From=%2B1555&To=%2B1444&Body=hi%20there"
        );
    }

    #[test]
    fn test_node_down_message() {
        let m = node_down_message("default", "node-1", None);
        assert!(m.contains("'node-1'") && m.contains("'default'") && m.contains("OFFLINE"));
        let m2 = node_down_message("backyard", "n2", Some("last seen 200s ago"));
        assert!(m2.ends_with("last seen 200s ago"));
    }
}
