# Simulation & Admin Console — running the whole system on one machine

This is the hands-on guide to running Fastly Garden Protector end-to-end **without any
physical hardware**: a software node + the indoor Pi gateway + the Fastly Compute
edge + both UIs. It exercises the full push model (docs/endpoint-contract.md) and
every optional peripheral.

```
hardware/node_sim.py   -->  hardware/gateway.py   -->  Fastly Compute edge   <--  two UIs
  (ESP32-C3 node model)  Tier-1 LAN: /motion         backend/src/main.rs         · dashboard (GET /)  — monitoring
  radar/camera/sensors   /frame /heartbeat           classify · veto · state     · provision/console — control plane
                         forwards to edge            /api/evidence /telemetry
```

Two honest design boundaries this layout reflects:

* **SSE lives in the local console, not the edge.** The Compute handler has a ~5 s
  budget, so a long-lived event stream belongs off-edge. "Configure & deploy" is a
  control-plane activity anyway, so the console (local, unconstrained) streams deploy
  progress over SSE; the edge dashboard polls.
* **Synthetic scenes classify as `none` on the real edge** (stock MobileNet has no
  raccoon synset and won't recognise a drawn animal). To exercise the *spray* branch
  deterministically, point the gateway at the **fake edge** (tests/fake_edge.py),
  which classifies by the scene's dominant colour. The real edge stays the honest ML
  path.

---

## 1. Fastest path — `make`

```bash
make serve      # terminal 1: Compute edge (Viceroy) on 0.0.0.0:7878
make gateway    # terminal 2: indoor Pi gateway on :8088 -> edge
make sim        # terminal 3: ESP32-C3 node simulator (all peripherals) -> gateway
make console    # terminal 4: admin console on :8050 (MOCK mode — no Fastly calls)
```

Then open:
* **http://localhost:7878/** — the Compute-served **monitoring dashboard**
  (status, snapshot, node health + optional-peripheral tiles, activity timeline).
* **http://localhost:8050/** — the **admin console / control plane** (multi-garden
  overview, per-garden Arm/Disarm/Stop/Resume, and modals to provision a deployment,
  register a device, rotate a token, or tear down — each with a live SSE log).

For a **narrated, no-setup walkthrough** of every decision branch (raccoon→spray,
human→veto, rain→suppress, disarm, empty-reservoir, node-down→alert) against the
deterministic fake edge:

```bash
make demo       # python3 scripts/demo.py — prints a ✓/✗ transcript, exits non-zero on surprise
```

`make help` lists every target.

---

## 2. The node simulator (`hardware/node_sim.py`)

Models the ESP32-C3 garden node: radar trips, time-lapse frames, ~60 s
heartbeats, the node-side **hard spray cap** (`min(seconds, 4)`), the **post-spray
refractory**, and fail-closed behaviour. Optional peripherals are sent only when
*fitted*:

```bash
# All optional peripherals, fast cadences for a watchable demo:
python3 hardware/node_sim.py --gateway http://localhost:8088 --fitted all \
    --heartbeat-s 2 --frame-s 5 --trip-every-s 6

# Specific peripherals + environment overrides:
python3 hardware/node_sim.py --fitted soil,reservoir,spray_confirm,presence,backup \
    --raining --reservoir-empty --on-backup-power
```

| `--fitted` token | Telemetry field | Models |
| :-- | :-- | :-- |
| `soil` | `soil_moisture_pct` | capacitive soil probe |
| `reservoir` | `reservoir_ok` | float switch (empty jug → `false`) |
| `spray_confirm` | `spray_confirmed` | INA219 current-sense (no-flow → `false`) |
| `presence` | `presence_distance_cm` | LD2410 mmWave |
| `backup` | `on_backup_power` | 18650 + TP4056 |

The trip loop cycles scenes (`raccoon`, `human`, `fox`, `empty`, `cat`); the
`human` scene is the safety veto (never sprays).

## 3. The gateway (`hardware/gateway.py`)

The indoor Pi. Implements Tier-1 (`/motion`, `/frame`, `/heartbeat`), forwards to
the edge, syncs arm-state from `/api/state`, and runs a node-down watchdog. The
spray decision is a pure, fail-closed core (`local_precheck` / `resolve_motion`)
with **local rain suppression** as the authoritative fast path. Extras:

```bash
python3 hardware/gateway.py --edge http://localhost:7878 \
    --spray-seconds 3 \
    --irrigation-window 06:00-06:30 --tz-offset-minutes -240 \
    --alert-webhook https://example/hook    # POSTed on a node_down transition
curl localhost:8088/state                   # the gateway's live view
curl -X POST localhost:8088/maintenance -d '{"on":true}'   # mute spray while gardening
```

## 4. Deterministic offline demo (no Viceroy, no Rust)

Run the **fake edge** instead of the real one to see the spray/veto branches fire:

```bash
make fake-edge                                  # terminal 1: deterministic edge on :7878
make gateway                                    # terminal 2
make sim                                         # terminal 3 — now raccoon/fox/cat -> SPRAY,
                                                 #   human -> veto, rain -> suppressed
```

## 5. The admin console (`provision/console.py`)

```bash
# Safe: the whole provisioning/registration flow with ZERO Fastly calls.
python3 -m provision.console --mock --edge http://localhost:7878

# Real control plane (drives Fastly): needs a token in the environment.
FASTLY_API_KEY=... python3 -m provision.console --edge https://<your-edge>
```

In **mock mode** the modals still produce real *local* artifacts (registry mirror,
deploy-env files, locally-minted tokens) so the UX is fully demoable offline. The
console holds the token server-side and proxies live state/control to the edge;
secrets never reach the browser.

### LAN access (passcode + session + LAN guard)

The console defaults to `127.0.0.1:8050` (localhost only, no auth — convenient for
dev). To manage your garden from any device on the house network, bind it to the LAN
**behind a passcode**:

```bash
# 1) set the admin passcode. Either source works (see precedence below):
#    a) on a Pi, just put it in .env (the SAME passcode as the portal):
#         echo 'GP_ADMIN_PASSCODE=your-passcode' >> .env
#    b) or store the console's own scrypt-hashed record (configs/console-auth.json):
#         python3 -m provision.console set-passcode

# 2) expose on the LAN (refuses to start LAN-exposed without a passcode)
python3 -m provision.console --listen 0.0.0.0:8050 --edge https://<your-edge>
```

The passcode is resolved from the first source present, **highest precedence first**:

1. **`GP_ADMIN_PASSCODE`** (env / `.env`) — shared with the Pi portal, so a freshly
   booted Pi enforces the console with no extra step. The console loads `.env`
   on startup (`--env-file`, `GP_ENV_FILE`, or `<repo>/.env`); a real env var wins.
2. **`configs/secrets.json` → `admin_passcode_hash`** — the shared hashed record the
   portal also uses, so both admin surfaces accept one credential.
3. **`configs/console-auth.json`** — the console's own record from `set-passcode`
   (legacy fallback). The startup banner prints which source is in effect.

Gating, in order, on every request:

* **LAN guard** — the peer IP must be loopback / private (RFC1918) / link-local, else
  `403`. The console must never be reachable from the public internet.
* **Passcode → session** — `/` serves a sign-in page; a correct passcode mints an
  HttpOnly `gp_session` cookie (12 h, in-memory — a console restart logs everyone out).
  All `/api/console/*` calls require the session once a passcode is set.
* **Rate limit** — 5 failed logins lock that IP for 5 minutes (a correct passcode while
  locked still gets `429` — that's the brute-force defense).

### Live edge log tail (`Logs…`)

The **Logs…** toolbar button opens a live tail of the Compute edge logs over SSE
(`GET /api/console/logs?service_id=<sid>`). Two sources are merged:

* **Durable FOS poll** (history + reliable spine): lists the service's
  `telemetry/…log.gz` objects, gunzips, and emits new lines (`~7 s` cadence).
* **`fastly log-tail`** (the live edge): streamed in real time if the `fastly` CLI is
  available; otherwise it falls back to FOS-only and says so.

Lines are `[GP] trace=… component=… op=… outcome=… detail=…`; the filter box greps by
substring (e.g. `trace=` to follow one request, or `outcome=error`).

> **Pi rollout (separate work):** auto-start on boot, serve on 80/443 at
> `raspberrypi.local`, and source the passcode from the Pi's `.env` — layered on top of
> this auth in a later pass.

---

## 6. Multiple cameras wired directly to the Pi (real hardware)

The Pi is the hub. Cameras wired **straight to the Pi** (CSI ribbon and/or USB UVC) are
each their own **edge device** — no radar or ESP32 required. `hardware/camera_pusher.py`
captures a still from each on a timer and POSTs it to `/api/evidence` with that camera's
`X-Device-Id`; the console's per-garden **Cameras gallery** then shows a live still per
camera (refreshing on the same ~3 s poll). It scales to N cameras, and a camera you
unplug simply stops updating.

> Per-device snapshots only separate for a **non-default** garden, so register a named
> garden (e.g. `home`) — the `default` garden shares one snapshot slot (last-writer-wins).

**1. Probe the cameras on the Pi** to learn the real device nodes + confirm capture:

```bash
python3 hardware/camera_probe.py          # lists the CSI imx219 + the USB /dev/videoN
```

**2. Register each camera as an `observer` device** (console → **+ Add**, or the CLI):

```bash
python3 -m provision.cli register-device --service-id <sid> \
    --garden home --garden-name Home --device cam-ribbon --kind observer --type camera_csi
python3 -m provision.cli register-device --service-id <sid> \
    --garden home --device cam-usb --kind observer --type camera_usb
```

**3. Run the pusher on the Pi**, mapping each registered device id to its capture source
(`ribbon` | `usb:/dev/videoN` | `mock[:/path.jpg]`):

```bash
# Real cameras -> a local Viceroy edge running on your Mac (no live Fastly touched):
GP_GARDEN_TOKEN=$(grep -h GP_GARDEN_TOKEN configs/home-*.env | head -1 | cut -d= -f2) \
python3 hardware/camera_pusher.py --backend http://<mac-ip>:7878 --garden home \
    --camera cam-ribbon=ribbon --camera cam-usb=usb:/dev/video1 --interval 30

# Smoke it with no hardware (one round from each camera, then exit):
python3 hardware/camera_pusher.py --backend http://localhost:7878 --garden home \
    --camera cam-a=mock --camera cam-b=mock:tests/fixtures/empty_garden.jpg --once
```

USB capture needs OpenCV or `fswebcam`; the CSI ribbon needs `rpicam-apps`.

**4. Watch the gallery**: open the console, select garden **home** — one tile per camera
with its latest still and the edge's species/action label. The pusher only **monitors**:
it ignores the spray verdict and actuates nothing — the fail-closed spray decision stays
on the gateway/node, exactly as in the radar path.

---

## 7. Scenario cheat-sheet (what to watch)

| Do this | Expect |
| :-- | :-- |
| Sim trips `raccoon` (fake edge) | gateway → `spray:true`; node sprays ≤4 s; refractory |
| Sim trips `human` | `spray:false reason=human` (the safety veto) |
| `--raining` then a trip | `spray:false reason=rain` (local fast path; edge backstop too) |
| Console → garden → **Disarm** | next trip `spray:false reason=disarmed` |
| `--reservoir-empty` + a spray | node opens valve but `spray_confirmed=false` (no flow) |
| Stop the sim ~150 s | dashboard node tile → OFFLINE; gateway logs `node_down` |
| Console → **+ New Deployment** | live SSE log of every provisioning step |
| Console → **+ Add** (register) | SSE log + the minted Pi deploy env to copy |
| Console → **Logs…** → Tail | live `[GP]` edge log lines (FOS history + `fastly log-tail`) |
| LAN-exposed without a passcode | console refuses to start (set `GP_ADMIN_PASSCODE` in `.env` or run `set-passcode`) |
| 5 wrong passcodes from one IP | that IP is rate-limited (`429`) for 5 min |
| `camera_pusher.py` to garden `home` | console → home shows a **Cameras** tile per camera, refreshing |
| Unplug a camera | its tile stops updating (others keep going); no spray side-effect |

All of the above are also covered by `tests/test_sim_e2e.py` (deterministic).
