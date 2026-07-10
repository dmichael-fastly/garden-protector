# Endpoint Contract (push model)

This is the **authoritative request/response contract** for Fastly Garden Protector's two
network hops. Where the older sketches in [architecture.md](architecture.md) differ, this
document wins.

There are two tiers, and the data flows **push-first** — no RTSP pull, no streaming:

```
[ESP32-C3 node]  --LAN-->  [Raspberry Pi (indoor gateway)]  --WAN-->  [Fastly Compute (edge)]
   radar/camera/sensors     aggregates + buffers + decides         classify · veto · state · alert
```

| Tier | Hop | Transport | Status |
| :--- | :--- | :--- | :--- |
| **1** | Node ↔ Pi | HTTP on the LAN | **Implemented** in the Pi gateway (`hardware/gateway.py`) + exercised by a software node (`hardware/node_sim.py`); the physical ESP32 *firmware* is still TODO |
| **2** | Pi ↔ Fastly | HTTPS to the Compute service | **Implemented** (`backend/src/main.rs`) |

---

## Conventions (both tiers)

**Identity + tracing headers.** Every Pi→Fastly request carries these (the node mirrors the
first three to the Pi). All default to `default`, so a single-garden deploy sends nothing
special.

| Header | Meaning | Charset |
| :--- | :--- | :--- |
| `X-Garden-Id` | Tenancy (which garden) | `[a-z0-9-]`, 1–64 |
| `X-Device-Id` | Which device within the garden (the ESP32-C3 node = sensors + optional camera) | `[a-z0-9-]`, 1–64 |
| `X-Node-Id` | Physical board id (logging only) | `[a-z0-9-]`, 1–64 |
| `X-Garden-Trace-Id` | Cross-tier correlation id; echoed back. Minted at the edge if absent/invalid | `[A-Za-z0-9-]`, 8–64 |
| `X-Garden-Auth` | Per-garden bearer token (**non-`default` gardens only**; enforce-iff-token) | secret |
| `X-Capture-Ts` | *(optional, `/api/evidence` only)* Unix **seconds** the Pi captured the frame. The edge stamps the archive object key with this instead of its own receipt clock, so all cameras pushed in one tick share one timeline second. Absent / >1 day from the edge clock → edge clock | digits |
| `X-Capture-Batch` | *(optional, `/api/evidence` only)* Id shared by every camera pushed in one capture tick, so the History/timelapse UI groups them as one multi-angle set. Absent → ungrouped | `[a-z0-9-]` |
| `X-Trigger` | *(optional, `/api/evidence` only)* Marks this push as a genuine **trigger event** (e.g. a camera's confirmed motion), value = a short human reason ("motion 4.2% x3"). The edge records an alarm only when the device `can_trigger_alarm` **and** this marker is present, so a trigger camera's routine cadence frames (no marker) stay History-only. Absent → no alarm | text |

**Auth (enforce-iff-token).** The `default` garden is **tokenless** (local/single-tenant dev
stays open). Any provisioned garden is authenticated against the token stored under *its own
claimed id*, which is what blocks garden A's token being replayed against garden B. A bad or
absent token on a provisioned garden → **401** on `/api/evidence`, `/api/telemetry`, and the
garden-scoped admin routes (→ **403** on the public `/api/control` toggle). The fail-closed
heartbeat (`GET /api/status`) does **no** secret lookup at all (the per-event smart Stop reads
`abort_cid` from `garden_state` KV, not the Secret Store).

**Timestamps.** All `*_ms` fields are epoch **milliseconds**. The edge stamps its own
**`last_seen_ms`** on telemetry from its receipt clock — device clocks are never trusted for
liveness.

**Fail-closed is sacred.** A timeout, non-2xx, or unreadable state must always resolve to
*don't spray* / *stand down*. Nothing in this contract may turn a deterrent **on** as a
failure mode.

---

## Tier 1 — Node ↔ Pi (LAN)  ·  *planned*

The node never talks to Fastly directly. It pushes to the Pi, which aggregates, buffers
time-lapse frames, applies the local fast-path decision (incl. **local rain suppression**),
and forwards to the edge. The Pi's reply tells the node what to do **right now**.

### `POST /motion` — incident report (held open)

Fired by the node on a radar trip. The Pi holds the connection open (~3 s) while it round-
trips the edge, then replies with the spray directive. A timeout → the node does **nothing**
(fail-closed).

Request (`multipart/form-data` or JSON + binary):
```json
{
  "bed": 1,
  "ts_ms": 1782012034000,
  "jpeg": "<binary or base64 still captured at the trip>",
  "telemetry": { "raining": false, "lux_level": 12.0 }
}
```
Response:
```json
{ "spray": true, "seconds": 3 }
```
```json
{ "spray": false, "reason": "human" | "rain" | "disarmed" | "stop" | "maintenance" }
```
- `seconds` is the **requested** burst; the node still applies its own **hard cap**
  (`min(seconds, 4)`) regardless of what the Pi asks for.

### `POST /frame` — time-lapse / liveness still

Scheduled capture (user-configurable interval: 1m / 5m / 15m / 1h) and the on-demand
"latest still". Doubles as a liveness beat when it carries no incident.

Request:
```json
{ "ts_ms": 1782012034000, "jpeg": "<bytes>", "battery_voltage": 4.12, "rssi": -61 }
```
Response (the Pi tells the node its current operating params):
```json
{ "interval_s": 300, "armed": true, "maintenance": false }
```

### `POST /heartbeat` — liveness + environment (no image)

The cheap ~60 s beat when there's no frame to send. Body is the node's sensor snapshot;
the Pi updates `last_seen`, folds these readings into the telemetry it forwards to Fastly,
and returns the same operating params as `/frame`.

Request:
```json
{
  "ts_ms": 1782012034000,
  "battery_voltage": 4.12,
  "rssi": -61,
  "uptime_s": 84230,
  "temperature_c": 18.5,
  "humidity_pct": 64.0,
  "rainfall_mm": 0.0,
  "raining": false,
  "lux_level": 12.0
}
```
Response:
```json
{ "interval_s": 300, "armed": true, "maintenance": false }
```

---

## Tier 2 — Pi ↔ Fastly (edge)  ·  *implemented*

Served by the Compute service. Exact-match safety routes are matched before the admin tree.

### `POST /api/evidence` — classify + verdict (the "veto")

The Pi POSTs the raw JPEG (`image/jpeg` body). The edge runs Tract ONNX, then applies the
**rain veto** (below) before replying.

The POST may carry the optional **`X-Capture-Ts`** + **`X-Capture-Batch`** headers (see the
identity table). When present, the edge dates the durable archive object on the Pi's capture
second (not its own receipt time) and tags it with the batch id — so every camera the Pi pushed
in one tick lands on a single timeline moment and the UI renders them as one multi-angle set.
SigV4 signing still uses the edge clock; a capture time implausibly far from now falls back to
it. Both are best-effort and never affect the verdict, the response, or the fail-closed path.

Response:
```json
{ "action": "mitigate", "species": "red fox", "confidence": 0.941 }
```
```json
{ "action": "none", "species": "raccoon", "confidence": 0.92, "reason": "rain" }
```
| Field | Meaning |
| :--- | :--- |
| `action` | `mitigate` (spray) or `none` (stand down) |
| `species` | Best-guess label, or `class_<n>` when not in the critter allowlist. **Logged, never gating** |
| `confidence` | Top-1 softmax probability (0–1) |
| `reason` | *(optional)* why a would-be `mitigate` was withheld — currently `"rain"`. Omitted otherwise |

Side effects (all best-effort, never block the response): publishes the snapshot + event to
`garden_state` for the dashboard, and archives the JPEG to Object Storage.

**Archive read consistency (after wipe/prune).** The edge sends the FOS `ListObjectsV2`
LIST with cache bypass (`Request::with_pass(true)`), so `GET /api/archive/days` / `?date=`
reflect a wipe or prune **immediately** (no edge-cache TTL wait). One known, benign residual:
an individual *image* URL (`/api/archive/image`) that was already fetched can still `200`
from the CDN read cache (objects are served `immutable, max-age=2592000`) until its TTL
expires. This is not user-visible because the History UI only ever builds image URLs from the
live LIST, which no longer references the deleted objects. The correct future hardening is a
cloud-side Surrogate-Key on the CDN read VCL so a wipe can purge by key (an edge-issued purge
is intentionally avoided — it would require an account-wide Fastly API credential on the
public edge).

### `POST /api/telemetry` — node heartbeat + environment

The Pi forwards the node's health + environment. The edge **stamps `last_seen_ms`** and
persists the blob per-device (`garden_state`). Any JSON object is accepted; the dashboard
reads these well-known keys when present:

Request:
```json
{
  "battery_voltage": 4.12,
  "temperature_c": 18.5,
  "humidity_pct": 64.0,
  "rainfall_mm": 0.0,
  "raining": false,
  "lux_level": 12.0
}
```
Response:
```json
{ "ok": true }
```
- **503** if the state store is unavailable (the liveness gap is surfaced rather than hidden) —
  but telemetry is never on the spray path, so this never affects deterrence.

**Optional telemetry fields** (sent only when the matching [optional peripheral](hardware-architecture.md#optional-add-ons)
is fitted; the edge stores any JSON object verbatim and the dashboard renders the keys it
recognizes, so adding a sensor needs **no contract change**):

| Field | Type | Source peripheral | Meaning |
| :--- | :--- | :--- | :--- |
| `soil_moisture_pct` | number | capacitive soil probe (G5) | Bed soil moisture, 0–100 |
| `reservoir_ok` | bool | float switch (G4) | `false` → jug empty / spraying air |
| `spray_confirmed` | bool | INA219 (G4) | `true` → current confirmed the jet actually fired (vs. a stuck/dead valve) |
| `presence_distance_cm` | number | LD2410 mmWave (G2) | Distance to the detected presence |
| `on_backup_power` | bool | 18650+TP4056 (G4) | `true` while riding battery through a mains/solar dropout |

These are **observe-only**: like all telemetry they never gate a spray. (`spray_confirmed`
is a *post-hoc* truth signal for the dashboard/alerts, not an input to the decision.)

### `POST /api/alert` — liveness-transition notification (edge → Twilio SMS)

Posted by the gateway on a node ONLINE→DOWN transition so the edge can SMS while the
credentials stay at the edge. **Notify-only, best-effort, never on a safety path.**
Enforce-iff-token like the other Pi routes.

Request:
```json
{ "event": "node_down", "node_id": "pi-01", "last_seen_ms": 1782011890000, "detail": "last seen 200s ago" }
```
Response (always 200 so a missing config is visible, never an error):
```json
{ "dispatched": true, "reason": "sent", "event": "node_down" }
```
```json
{ "dispatched": false, "reason": "not_configured", "event": "node_down" }
```
- Twilio creds: `twilio_config` Config Store (`account_sid`, `from`, `to`) + the
  `twilio.auth_token` secret in `garden_tokens` + a `twilio_api` backend. Absent any of
  these → `not_configured` (no-op). Bad JSON → 400.

### Control model — three modes + per-event smart Stop

The control surface is a **three-mode** model **derived** from the two persisted booleans
(`armed`, `override_stop`) — there is **no storage migration**; the booleans stay the on-disk
encoding:

| `mode`    | `armed` | `override_stop` | Behavior |
| :-------- | :------ | :-------------- | :------- |
| `off`     | false   | (any)           | Nothing runs. |
| `monitor` | true    | true            | Watch + alert + log, **never spray** ("Log mode"). |
| `active`  | true    | false           | Watch + spray. |

**Smart Stop** is a *per-event* abort, layered on top, backed by two per-garden
`garden_state` KV string fields (keyed via the shared `key` helper; `default` garden = legacy
flat keys):

- `last_mitigate_cid` — the trace id (`X-Garden-Trace-Id`) of the most recent **`mitigate`**
  decision, written by the evidence handler. Pressing Stop targets *this* so the dashboard
  never has to pass a cid. Writing it also **clears** `abort_cid` (auto-resume — a stale Stop
  from a prior spray never carries over to a new one).
- `abort_cid` — the cid of the spray the user aborted via Stop (empty = none). One-shot:
  it suppresses only the *one* live spray whose cid it equals.

> **Smart Stop is a COMFORT feature, not a safety gate** (same framing as the rain veto). It
> can only ever turn a spray **off**, never on. An unreadable `abort_cid` is treated as empty
> (no abort) — a transient store miss must never *suppress* a legitimate spray. The
> fail-closed armed/override floor + the Pi's 60 s watchdog remain the safety guarantees.

### `GET /api/status` — fail-closed heartbeat

The Pi polls this every ~3 s **during active mitigation**, echoing the live event's
`X-Garden-Trace-Id` on every beat. No body, no secret/auth lookup.
```json
{ "continue_mitigation": true }
```
Returns `true` only when **armed AND not override-stopped AND not aborted-for-this-event**.
`continue_mitigation = heartbeat_continue(armed, override_stop) && !(abort_cid != "" && X-Garden-Trace-Id == abort_cid)`.
If `garden_state` is unreadable, or the armed/override flags can't be confirmed, this returns
`false` (stop) — never "keep firing". An unreadable `abort_cid` degrades to "no abort" (it can
never *cause* a stop).

### `GET /api/state` — dashboard state

Read by the admin dashboard (~3 s poll). Resolves to the **service's own garden** (header-less
browser resolution). Viewer-gated.
```json
{
  "mode": "active",
  "armed": true,
  "override_stop": false,
  "continue_mitigation": true,
  "latest_event": { "species": "red fox", "confidence": 0.94, "action": "mitigate", "reason": null, "ts": 1782012034000 },
  "node": {
    "online": true,
    "last_seen_ms": 1782012090000,
    "seconds_since": 12,
    "telemetry": {
      "battery_voltage": 4.12,
      "temperature_c": 18.5,
      "humidity_pct": 64.0,
      "rainfall_mm": 0.0,
      "raining": false,
      "lux_level": 12.0,
      "last_seen_ms": 1782012090000
    }
  }
}
```
- `mode` is the derived three-mode string (`off` | `monitor` | `active`). `armed` /
  `override_stop` are **kept for back-compat** (the shared `gp.js` still reads them during the
  transition).
- `node.online` is derived: `true` iff a telemetry beat landed within **150 s**
  (`NODE_OFFLINE_AFTER_SECS`). `latest_event` / `node.telemetry` are `null` before the first
  evidence / telemetry post.

### `POST /api/control` — dashboard control (**auth-gated**)

Body `{ "cmd": ... }` → returns the new `/api/state` body (incl. `mode`).

| `cmd` | Effect |
| :---- | :----- |
| `off` (alias `disarm`)  | `armed=false` (mode OFF; `override_stop` unchanged). |
| `monitor`               | `armed=true, override_stop=true` (mode MONITOR). |
| `active` (alias `arm`)  | `armed=true, override_stop=false` (mode ACTIVE). |
| `stop`                  | `abort_cid = last_mitigate_cid` — one-shot abort of the live spray. **Does not change mode.** No-op (still **200**) when no spray is live. |
| `resume`                | clear `abort_cid` (manual un-abort; usually unnecessary — a new mitigate auto-resumes). |

Unknown command → **400**; store write failure → **503** (so an emergency STOP that didn't
persist is visible).

**Garden targeted (service's own, never a header):** `/api/control` resolves the garden via the
same header-less rule as `/api/state` — the service's **own** garden (`browser_garden_id`), never a
client-supplied `X-Garden-Id`. On the shared default service that is the tokenless `default` garden;
on a dedicated service it is that service's garden. So a browser can only ever toggle the service it
is hitting — it is **not** cross-tenant steerable. Per-garden control of an *arbitrary* garden is the
separate token-gated `POST /api/gardens/{gid}/control`.

**Auth (defense-in-depth):** this is the public edge control surface and the header ARMED pill
is becoming a clickable toggle, so an anonymous viewer must not be able to drive it. It gates on
the same per-garden `X-Garden-Auth` token as the other protected mutations (the Pi portal
proxies it through its admin gate and **forwards the token**). Enforce-iff-token: the `default`
garden — and any garden with **no token configured** — is **tokenless** (open), so local dev and
the single-tenant default path keep working. A configured token with an absent/wrong credential
→ **403**. Rate-limited like the admin tree (repeated 403s → **429**).

### Alarms (`/api/alarms`, `/api/alarm*`)

An **alarm** is a potential intruder: a *trigger* device's evidence push, double-checked by camera
evidence, given a **determination** (`good`=real / `neutral`=unsure / `bad`=false). Routine captures
stay in History; only triggers become alarms. Created at evidence time by the edge when the pushing
device `can_trigger_alarm` (see device roles below) **and** the push carries the `X-Trigger` marker
(`alarm_should_record` = role && marker) — so a trigger camera's routine cadence frames (no marker)
stay History-only and can't flood the log. The role + marker are the gate today (isolated in
`should_alarm`/`alarm_should_record`, ready for richer per-detector trigger conditions later). All routes
resolve the service's **own** garden (`browser_garden_id`, never a client header). Storage is one
per-garden JSON doc `g/<gid>/alarm_log` (`alarm_id → {id, ts, trigger_device, key, batch, species,
confidence, action, reason, tag}`, where `alarm_id` = the capture batch when present else the trace
id, so a multi-angle set dedups to one alarm). `species`/`confidence`/`action` come from the
classifier — never trusted from the client. The advisory per-species threshold recommendations are
derived from the *tagged* alarms on read (collect + recommend only; the spray model is a fixed ONNX).

| Method + Path | Auth | Purpose |
| :--- | :--- | :--- |
| `GET /api/alarms` | viewer-gated | `{threshold_pct, min_labels, can_manage, recommendations:[{species,good,neutral,bad,recommended_pct,note}], alarms:[…newest-first]}`. `can_manage` = request carried a valid token (drives the admin edit/delete + cleanup affordance). |
| `GET /api/alarm?key=` | viewer-gated | The alarm for one frame (its `batch||cid`), or null — drives the event-page determination toggle. → `{alarm}`. |
| `POST /api/alarm-tag` | viewer-gated (**add**); token (**edit**) | Body `{id,label}`. Tag an untagged alarm (any viewer); **changing** an existing tag requires a valid `X-Garden-Auth` token (admin). → `{ok,label,edited}`. |
| `POST /api/alarm/delete` | token (admin) | Body `{id}`. Delete one alarm. Requires `X-Garden-Auth == Authorized`. → `{ok,deleted}`. |
| `POST /api/alarms/prune?mode=days&keep=N` (or `mode=count`) | token (admin) | Retention sweep: `days` drops alarms older than `keep` days; `count` keeps the newest `keep`. One KV write. → `{ok,deleted,kept}`. |
| `POST /api/alarms/wipe` | token (admin) | Delete ALL alarms for this garden. → `{ok,deleted}`. |

**Auth split:** "users add new ones only; admins manage all prior ones." On the public edge a viewer
(viewer-cookie) can only *tag a new alarm*; the Pi admin portal forwards the per-garden token, so the
edge sees `Authorized` and may *retag / delete / prune / wipe*. `label` ∉ {good,neutral,bad} → **400**;
store write failure → **503**. Page: `GET /alarms` (dual-served like `/event`, viewer-gated on the
edge / admin on the Pi; nav id `alarms`, `viewer_ok`); the per-alarm determination toggle also appears
on `/event` when that frame is an alarm.

**Device alarm roles** (registry, written by `gp-provision`; edge reads via `#[serde(default)]`, so
older entries default to `false`): each device carries `can_trigger_alarm` + `can_confirm_alarm`.
Cameras default to **confirm-only** (trigger off) so nothing alarms until a device is opted in; edit
per device on the Gadgets page (→ `gp-provision edit-device --can-trigger-alarm/--can-confirm-alarm`).

**Camera motion trigger** (Pi-local; how a camera *becomes* a sparse trigger instead of "every photo
is an alarm"): the camera daemon watches an enabled camera's live feed — a running-average background +
sensitivity + `confirm_frames` consecutive samples + cooldown, confined to a normalized monitor-zone
ROI — and on a confirmed detection escalates ONE frame with `X-Trigger` set (→ alarm). The per-sample
frames are never uploaded (a local pre-roll is kept under `/var/lib/garden-protector/motion/<did>/`).
Per-camera config lives Pi-local in `pi-garden.json` `motion.<device_id> = {enabled, cadence_s,
confirm_frames, sensitivity, cooldown_s, roi}` (validated by `pi_config.normalize_motion`; hot-reloaded
each tick). Portal routes (admin): `GET /api/gadget/motion-settings?device=<id>`,
`POST /api/gadget/motion-settings`. Enabling motion in the UI also sets the `can_trigger_alarm` role
(via the device-edit path) so the marker actually creates an alarm. Inference still happens on the
edge (no local model); the escalation step is isolated as a seam for a local pre-filter later.

**Alarm retention** (Pi portal, separate from image/History retention): `pi-garden.json`
`maintenance.alarm_retention = {mode, keep_days, keep_count, prune_hour, last_alarm_prune_date}`. A
daily `alarm-prune-scheduler` thread (idempotent via `last_alarm_prune_date`) calls
`POST /api/alarms/prune`. Portal routes (admin): `GET /api/alarms/settings`,
`POST /api/settings/alarm-retention`, `POST /api/alarms/run-now`, `POST /api/alarms/wipe-all`
(type-the-garden-name confirm). Surfaced in the Alarms page's "Alarm cleanup" card.

### Admin / CRUD tree (`/api/gardens/...`)  ·  multi-garden

Garden-scoped routes enforce the per-garden token. The registry is control-plane-write-only
(the two mutating POSTs return **405**, pointing at `gp-provision`).

| Method + Path | Purpose |
| :--- | :--- |
| `GET /api/gardens` | List gardens (open overview) |
| `GET /api/gardens/{gid}` | Garden flags + derived continue |
| `GET /api/gardens/{gid}/devices` | Device registry for a garden |
| `GET /api/gardens/{gid}/devices/{did}/event` | Latest event for a device |
| `GET /api/gardens/{gid}/devices/{did}/snapshot` | Latest JPEG for a device |
| `GET /api/gardens/{gid}/devices/{did}/telemetry` | Latest telemetry blob for a device |
| `POST /api/gardens/{gid}/control` | Per-garden arm/disarm/stop/resume |

### Setup wizard (Pi-local, LAN only)

First-run-only routes served by `hardware/portal.py` under `/api/wizard/*` (LAN-guarded;
open in bootstrap mode, else an admin session). One is worth contract-noting:

#### `POST /api/wizard/geocode` — resolve an address to coordinates

The wizard proxies OpenStreetMap **Nominatim** so the operator never types lat/lon by hand.
Body is either structured or free-form:

```json
{ "street": "1 Main St", "city": "Springfield", "state": "IL", "postalcode": "62701", "country": "US" }
```
```json
{ "q": "1 Main St, Springfield IL" }
```

Response: `{ "ok": true, "lat": 39.7817, "lon": -89.6501, "display_name": "…" }`. No address →
**400**; no match → **404**; provider error → **502**. Failures are *soft* — the wizard still
lets the operator enter lat/lon manually or skip them (garden creation is never blocked).

---

## Cost calculator (control plane)

The cost model is a **shared pure core** (`provision/cost_rates.py` — rate constants + actual /
estimated-monthly math) over **live usage fetchers** (`provision/usage_stats.py` — Fastly
Historical Stats API + FOS object listing). Two surfaces expose it: the admin **console**
(operator detail) and the Pi **portal** (homeowner summary). Every measured resource degrades to
*unavailable* on any failure (mock mode, no token, missing boto3, network) — the page always
renders, never 500s.

**Data sources per resource:** FOS image count + bytes = exact via S3 `list_objects_v2`
(optionally scoped to one garden by the `g/<gid>/` key prefix); FOS Class A/B ops =
`GET /stats/aggregate` (**account-wide**, not per-service); CDN bandwidth/requests + Compute
requests = `GET /stats/service/{id}` (per-service, exact); KV/Secret/Config ops = **modeled
estimate** from edge request volume (no Stats field exists), `$`-weighted only if a rate is set.

#### `GET /api/console/cost` — measured usage + cost (console, admin/LAN)

Query: `service_id` (**required**), `window` (token `1h`/`24h`/`7d`/`30d`, or a legacy bare number =
days, 1–90; default `7d`), `garden` (optional; `all`/`default` = whole bucket, else `g/<gid>/`
storage scope). Missing `service_id` → **400**; unknown service → **404**. The Stats rollup scales
with the window (minute for the 1h view, hourly for a day or two, daily beyond) so short views stay
accurate and long ones stay light. Response (`window`/`window_label`/`window_hours` alongside the
legacy `window_days`):

```json
{ "service_id": "S1", "window": "7d", "window_label": "7 days", "window_hours": 168, "window_days": 7, "garden": null,
  "usage": { "window": "7d", "window_label": "7 days", "window_hours": 168, "window_days": 7, "from": 0, "to": 0,
             "fos": {"objects": 0, "bytes": 0} | null, "fos_ops": {"class_a": 0, "class_b": 0} | null,
             "cdn": {"requests": 0, "bandwidth_bytes": 0} | null, "compute": {"requests": 0} | null,
             "store_ops": {"kv": 0, "secret": 0, "config": 0, "estimated": true} },
  "actual":  { "lines": [{"key","label","qty","unit","cost","estimated"}], "total": 0.0 },
  "monthly": { "lines": [ … ], "total": 0.0 },
  "rates": { "class_a_rate_per_1k": 0.005, … }, "mock": false }
```

#### `POST /api/console/cost-rates` — persist edited rates (console, admin/LAN)

Body: any subset of the rate keys; unknown keys + non-numeric values are dropped. Persists to
`configs/cost-rates.json` (gitignored). Response: `{ "ok": true, "rates": { … } }`.

#### `GET /api/cost` — friendly cost summary (portal, admin/LAN)

Query: `window` (token `1h`/`24h`/`7d`/`30d`, or a legacy bare number = days; default `30d`). Maps
the shared model to **exactly three** plain-language line items so `costs.html` stays jargon-free.
The gather is cached ~120s per (service, window). Short windows (1h/24h) carry a "bounces around
more" caveat in `note`. An unprovisioned/offline Pi returns `available:false` (200), never an error.

```json
{ "available": true, "window": "30d", "window_label": "30 days", "window_hours": 720, "window_days": 30, "monthly_total": 1.84, "monthly_total_str": "$1.84",
  "items": [ {"label": "Photos kept safe", "icon": "📸",
              "detail": "1.2K kept · 2.10 GB · 3.4M saved/mo",
              "blurb": "Storing your security photos …", "help": "Every photo your cameras …",
              "monthly": "$0.04", "available": true}, … ],
  "rates": { "class_a_rate_per_1k": 0.005, … },
  "note": "Estimated from the last 30 days …", "mock": false }
```

Each item carries `blurb` (one-line, always shown) and `help` (paragraph, shown in a per-item
modal behind an `i` icon). The `rates` block pre-fills the page's custom-pricing editor.
**Cost↔count bucketing** (every line's headline number drives its dollars — no "0 X for $Y"):
*Photos kept safe* = FOS storage **+ Class A** (saves); *Photo deliveries* = CDN egress **+ Class
B** (fetches); *Always-on guarding* = Compute **+** the modelled KV/Secret/Config look-ups.
Zero/sub-cent costs render as `$0.00` / `less than 1¢`.

#### `POST /api/cost-rates` — persist custom pricing (portal, admin/LAN)

Lets a homeowner/operator on a negotiated contract enter their own Fastly prices instead of list
rates, right from the Costs page. Body: any subset of the rate keys; the shared `cost_rates`
core drops unknown keys + non-numeric values and coerces types. Persists to the **same**
`configs/cost-rates.json` the console writes (one cost model, one source of truth). `409` if the Pi
has no config store yet. Response: `{ "ok": true, "rates": { … } }`. The cost cache holds *raw*
usage, so the next `GET /api/cost` reprices instantly with the new rates.

> **Storage 30-day minimum.** `min_billed_days` (default 30) floors the monthly storage estimate:
> Fastly bills each object for at least that many days even if deleted sooner, so a freshly-started
> garden is already charged a full month. Editable like any other rate. (Evidence objects are
> write-only in normal operation — no lifecycle/expiry — so there's no churn term to model.)

> **Trap — FOS ops are account-wide.** Fastly's Stats API reports Object Storage Class A/B
> operation counts across the **whole account**, with no per-service breakdown (confirmed live:
> `/stats/service/{id}` exposes no `object_storage_*` fields). Accurate when the account runs only
> garden-protector; on a shared account it over-counts and the friendly per-garden total is
> dominated by other services' storage activity. Labelled in the UI; revisit if a dedicated account
> is used.

---

## Decision / veto semantics

The spray rule is **presumption-of-critter** (see
[architecture.md](architecture.md#decision-logic-presumption-of-critter)):

```
spray = armed AND radar-tripped AND not vetoed
```

Veto inputs, in order of authority:

1. **Human / benign veto** — ML proving a person (or known pet / confidently empty scene) is
   in frame. The one case that must always fail *safe*.
2. **Rain suppression (environmental veto)** — if the rain sensor reports **active rain**,
   suppress the spray: critters shelter in rain, so there's nothing to deter and the water is
   wasted.
3. **Schedule / mode** — `off` (disarmed), `monitor` (the "Log mode" hold = `override_stop`),
   maintenance window, or the scheduled irrigation-suppression window.
4. **Per-event smart Stop** — a one-shot abort of the *live* spray (the dashboard Stop button),
   keyed on the event's correlation id (`abort_cid`). A COMFORT gate, fail-safe like the rain
   veto: it can only downgrade the live spray to off, auto-resumes on the next `mitigate`, and an
   unreadable `abort_cid` is treated as "no abort". See [the control model](#control-model--three-modes--per-event-smart-stop).

### Rain veto specifics

- **Fail-safe by construction:** the rain veto can only ever downgrade `mitigate` → `none`,
  **never** the reverse.
- **Authoritative copy is LOCAL** on the node/Pi (it owns the rain gauge → suppresses with
  zero network dependency). The **edge mirrors it** as a backstop and for visibility:
  `/api/evidence` reads the node's freshest telemetry and, when `raining` is true **and** the
  reading is fresh (`last_seen_ms` within `RAIN_TELEMETRY_FRESH_SECS` = 600 s), returns
  `action:"none"`, `reason:"rain"`.
- **Stale / absent telemetry → no veto** (worst case: a harmless spray in the rain). The edge
  never withholds a spray on the strength of an old reading.

---

## Liveness / board-down

Because a dead board fails *closed* (no spray), it looks identical to a quiet garden — so
loss of protection is silent unless monitored.

- Node → Pi beat every **~60 s**; Pi → Fastly via `POST /api/telemetry`.
- The edge derives **DOWN** after **~150 s** of silence (≈2.5 missed beats;
  `NODE_OFFLINE_AFTER_SECS`).
- The dashboard renders a **Garden Node** tile (ONLINE/OFFLINE + "seen Ns ago") plus battery,
  temperature, humidity, and a rain tile that doubles as the **spray-suppressed** indicator.
- A DOWN transition fires `POST /api/alert` (gateway → edge), and the edge dispatches an
  SMS via Twilio (`node_down`). Built; the live SMS send needs Twilio creds + a `twilio_api`
  backend (absent → fail-safe `not_configured` no-op).

---

## Implemented vs planned

| Item | State |
| :--- | :--- |
| `POST /api/evidence` (classify + rain veto + `reason`) | ✅ implemented |
| `POST /api/telemetry` (+ edge `last_seen_ms` stamp) | ✅ implemented |
| `GET /api/state` `node` block + dashboard health tiles | ✅ implemented |
| `GET /api/gardens/{gid}/devices/{did}/telemetry` | ✅ implemented |
| `GET /api/status`, `/api/state`, `POST /api/control`, admin tree | ✅ implemented (pre-existing) |
| Cost calculator (`/api/console/cost`, `/api/console/cost-rates`, portal `/api/cost`) | ✅ implemented — shared core (`cost_rates.py` + `usage_stats.py`), console + portal pages |
| Node↔Pi `/motion`, `/frame`, `/heartbeat` | ✅ gateway (`hardware/gateway.py`) + software node (`hardware/node_sim.py`); physical ESP32 firmware ⏳ |
| `node_down` alert (gateway watchdog → `POST /api/alert` → edge→Twilio SMS) | ✅ built end-to-end; live SMS send needs Twilio creds + a `twilio_api` backend |
| Per-garden rain telemetry (garden-scoped, not per-device) | ⏳ future refinement |
| Optional-peripheral telemetry (`soil_moisture_pct`, `reservoir_ok`, `spray_confirmed`, …) | ⏳ accepted+stored today (any JSON); dedicated dashboard tiles planned with the hardware |
