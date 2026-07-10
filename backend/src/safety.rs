//! PURE safety-decision cores — the fail-closed mitigation / heartbeat / rain-veto /
//! liveness logic, with NO I/O and NO host-ABI (only bool / Option / serde_json value
//! transforms), so it links under both wasm32 and native `cargo test`.
//!
//! These are the most important functions in the system. They were extracted from
//! main.rs as a BEHAVIOR-PRESERVING move (Phase 5): the bodies are byte-identical to
//! the originals, and the comprehensive truth-table / fail-closed / liveness / rain
//! unit tests stay in main.rs and continue to exercise them verbatim through the
//! `use safety::*;` re-export (cargo test count unchanged). The flag / state /
//! telemetry STORE I/O that feeds these decisions deliberately stays in main.rs (it is
//! tied to the host ABI + the request handlers, charter-sacred, with no clean seam to
//! move — only the pure logic is split out here, mirroring the auth decide_* pattern).
//!
//! Fail-closed contract (unchanged): an unconfirmed armed flag reads as DISARMED and an
//! unconfirmed override as STOP, so an unreadable state store can never keep the
//! sprinkler firing; the rain veto can ONLY downgrade `mitigate` -> `none`, never spray.

use crate::contract_gen::{NODE_OFFLINE_AFTER_SECS, RAIN_TELEMETRY_FRESH_SECS};

/// `true` only when the device is armed and the user hasn't issued a STOP
/// override. Shared by `/api/status` (Pi heartbeat) and `/api/state` (dashboard).
pub fn continue_mitigation(armed: bool, override_stop: bool) -> bool {
    armed && !override_stop
}

/// The three operating MODES, DERIVED from the two persisted booleans (no new
/// storage). This is the user-facing control model; `armed`/`override_stop` remain
/// the on-disk encoding so there is no KV migration:
///   OFF     = armed=false                       (nothing runs)
///   MONITOR = armed=true,  override_stop=true    (watch + alert + log, NEVER spray)
///   ACTIVE  = armed=true,  override_stop=false   (watch + spray)
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    Off,
    Monitor,
    Active,
}

impl Mode {
    /// Lower-case wire string for `/api/state` + `/api/control` JSON ("off" | "monitor" | "active").
    pub fn as_str(self) -> &'static str {
        match self {
            Mode::Off => "off",
            Mode::Monitor => "monitor",
            Mode::Active => "active",
        }
    }
}

/// PURE mode derivation from the two persisted flags. `armed=false` is OFF
/// regardless of `override_stop` (a disarmed garden is off, period); an armed
/// garden is MONITOR when stopped/held and ACTIVE when free to spray.
pub fn derive_mode(armed: bool, override_stop: bool) -> Mode {
    match (armed, override_stop) {
        (false, _) => Mode::Off,
        (true, true) => Mode::Monitor,
        (true, false) => Mode::Active,
    }
}

/// Applies a MODE-SETTING control command to the `(armed, override_stop)` state.
/// PURE. Returns the new flag tuple, or `None` for any command this fn does not
/// own — which now includes the per-event `stop`/`resume` (they mutate `abort_cid`,
/// not the mode tuple, so the handler routes them separately) AND truly unknown
/// commands (-> HTTP 400). `arm`/`disarm` are kept as ACTIVE/OFF aliases for
/// back-compat (older consoles + the gp.js transition).
pub fn apply_control(cmd: &str, _armed: bool, override_stop: bool) -> Option<(bool, bool)> {
    // `_armed` is unused in the three-mode model (every mode command sets `armed`
    // outright), but the (cmd, armed, override_stop) signature is kept stable for the
    // call sites + the truth-table tests.
    match cmd {
        // OFF: disarm. override_stop is left UNCHANGED so a later `active` restores the
        // pre-OFF spray intent; mode is OFF regardless (derive_mode ignores override when
        // disarmed), so this is observably "turn everything off".
        "off" | "disarm" => Some((false, override_stop)),
        // MONITOR ("Log mode"): armed + held — watch/alert/log but never spray.
        "monitor" => Some((true, true)),
        // ACTIVE: armed + free to spray.
        "active" | "arm" => Some((true, false)),
        // stop/resume are NOT mode changes (they touch abort_cid); the handler owns them.
        _ => None,
    }
}

/// FAIL-CLOSED heartbeat decision. An unconfirmed `armed` is treated as DISARMED
/// and an unconfirmed `override_stop` as STOP, so a broken/unreadable state store
/// can never tell the Pi to keep firing. (`None` = could not be read at all.)
pub fn heartbeat_continue(armed: Option<bool>, override_stop: Option<bool>) -> bool {
    continue_mitigation(armed.unwrap_or(false), override_stop.unwrap_or(true))
}

/// FAIL-CLOSED heartbeat decision WITH the per-event smart-Stop abort layered on top.
/// The armed/override floor is exactly [`heartbeat_continue`] (unconfirmed armed ->
/// disarmed, unconfirmed override -> STOP), so a broken state store can never keep the
/// sprinkler firing. The abort is then ANDed in: spray continues only if it is NOT
/// aborted for THIS event.
///
/// `aborted` iff a non-empty `abort_cid` (the spray the user pressed Stop on) equals
/// THIS event's `event_cid` (the live correlation id the Pi echoes on every heartbeat).
/// The empty checks make it impossible for an empty/absent cid to match: an empty
/// abort never aborts, and an empty event cid never matches a real abort.
///
/// IMPORTANT — the abort is a COMFORT feature, not a safety gate (mirrors the rain
/// veto framing). It can ONLY ever turn a spray OFF, never on. Crucially the CALLER
/// must pass `abort_cid=""` on any KV read MISS (transient store error), so a flaky
/// store can never *suppress* a legitimate spray — it just degrades to "no abort", and
/// the real safety guarantees (the armed/override fail-closed floor above + the Pi's
/// own 60 s watchdog) still hold. NEVER fail-close on an unreadable abort_cid.
pub fn heartbeat_continue_abort(
    armed: Option<bool>,
    override_stop: Option<bool>,
    event_cid: &str,
    abort_cid: &str,
) -> bool {
    let aborted = !abort_cid.is_empty() && event_cid == abort_cid;
    heartbeat_continue(armed, override_stop) && !aborted
}

/// PURE liveness decision: given the last-seen epoch-ms and now, is the node online,
/// and how many whole seconds since it was last heard from? A `last_seen` in the
/// future (clock skew) is treated as "just now"; `None` (never seen) is offline.
pub fn node_liveness(last_seen_ms: Option<u64>, now_ms: u64) -> (bool, Option<u64>) {
    match last_seen_ms {
        Some(ts) if now_ms >= ts => {
            let since = (now_ms - ts) / 1000;
            (since <= NODE_OFFLINE_AFTER_SECS, Some(since))
        }
        Some(_) => (true, Some(0)), // ts in the future (skew) -> just-seen
        None => (false, None),      // never seen -> offline
    }
}

/// PURE rain veto — a COMFORT optimization, not a safety gate. If the freshest
/// telemetry says it's actively raining, a `mitigate` is suppressed (critters shelter
/// in rain, so spraying just wastes water). FAIL-SAFE BY CONSTRUCTION: it can only
/// ever downgrade `mitigate` -> `none`, never turn a spray ON, and it only trusts a
/// `raining=true` flag backed by a FRESH `last_seen_ms`. Stale/absent telemetry -> no
/// veto (worst case: you spray in the rain, which is harmless).
pub fn rain_should_suppress(action: &str, telemetry: &serde_json::Value, now_ms: u64) -> bool {
    if action != "mitigate" {
        return false;
    }
    let raining = telemetry
        .get("raining")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    if !raining {
        return false;
    }
    match telemetry.get("last_seen_ms").and_then(|v| v.as_u64()) {
        Some(ts) if now_ms >= ts => (now_ms - ts) / 1000 <= RAIN_TELEMETRY_FRESH_SECS,
        Some(_) => true, // future ts (skew) -> treat as fresh
        None => false,   // no receipt stamp -> don't trust
    }
}
