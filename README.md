# 🌿 Fastly Garden Protector: Edge-Native IoT Critter Deterrent System

Fastly Garden Protector is an edge-native, Internet of Things (IoT) application designed to monitor your home garden for invading critters (raccoons, deer, squirrels) and trigger physical deterrents in real-time. 

Rather than sending heavy video feeds to expensive centralized cloud platforms or managing complex machine learning servers, Fastly Garden Protector processes everything at the global edge using **Fastly Compute** and a Raspberry Pi.

> 🧭 **Contributors:** [CHARTER.md](CHARTER.md) records this repo's standing engineering decisions (one shared UI asset layer, one shared Python service library, pinned toolchains, one identity/contract source of truth) and the safety contract. It governs the codebase; read it before [AGENTS.md](AGENTS.md).

---

## 🚀 How It Works (At a Glance)

1. **Detection:** A passive infrared (PIR) motion sensor on a Raspberry Pi detects movement and captures a JPEG image.
2. **Analysis at the Edge:** The Pi POSTs the image to a Fastly Compute endpoint. Fastly Compute instantly loads a lightweight neural network (MobileNet V2 ONNX) from **Fastly KV Store** and executes ML classification entirely within a WebAssembly sandbox—zero external API accounts required!
3. **Action & Alerts:** If a critter is classified, Fastly records the event in KV Store, dispatches an SMS text notification (via Twilio API webhook) with an event deep link, and returns an HTTP response directing the Pi to activate connected physical deterrents (strobes, buzzers, or sprinklers).
4. **Admin Dashboard & Override:** A single-page **admin dashboard served by Fastly Compute itself** (`GET /`) shows live status, the latest evidence snapshot (near-live via ~3 s polling), and **Arm / Disarm / Stop / Resume** controls. Commands write to a writable **KV Store** (`garden_state`) and are fetched by the Pi on its next status heartbeat. *(True live video stays LAN-local via `hardware/camera_view.py` — Compute can't reach the Pi's LAN.)*

---

## 🛠️ The Technology Stack

* **Edge Backend:** Fastly Compute written in **Rust** running [Tract ONNX](https://github.com/sonos/tract) for edge-native machine learning.
* **Storage:** Fastly KV Store — model weights (`garden_models`) **plus a writable `garden_state`** store holding the arm/override flags, the latest event JSON, and the latest evidence JPEG — with Config Store (device defaults) and Secret Store (credentials).
* **Alert Notifications:** Outbound SMS webhook API (Twilio).
* **Hardware Client:** Raspberry Pi (Python) as the **indoor gateway**, plus a single **garden controller** (ESP32-C3 SuperMini) doing radar detection + the spray pump (cameras optional). **Optional add-ons** (all fail-closed, pick-and-choose): environment + night vision (temp/humidity, rain gauge, IR floodlight), smarter detection (PIR AND-gate, LD2410 mmWave), extra deterrents (piezo/ultrasonic), resilience (DS3231 RTC, INA219 spray-confirm, reservoir float switch, power backup), and showcase sensors (soil moisture, light, status display). See the **[Hardware Architecture & Build](docs/hardware-architecture.md)** doc for the required-vs-optional BOM. *(Relay wiring, the Pi camera HAL, and weatherproofing reference live in [docs/hardware-reference.md](docs/hardware-reference.md).)*
* **Frontend:** A single-page admin dashboard (HTML + vanilla JS + CSS) **baked into the Wasm binary** (`include_str!`) and served directly by Compute.

---

## 🖥️ Admin Dashboard & Edge API

The Compute service exposes both the Pi-facing control loop and the admin dashboard:

| Method + Path | Purpose |
| :--- | :--- |
| `GET /` (and `/admin`) | Admin dashboard HTML (polls state + snapshot every ~3 s; Arm/Disarm/Stop/Resume buttons) |
| `POST /api/evidence` | **Pi:** raw `image/jpeg` body → classify → `{action, species, confidence, reason?}`. Applies the **rain veto** (raining → suppress spray, `reason:"rain"`). Also publishes the latest snapshot + event to `garden_state` (best-effort). |
| `POST /api/telemetry` | **Pi:** node heartbeat + environment JSON → edge stamps `last_seen_ms` and persists per-device → `{ok:true}`. Feeds the dashboard health tiles. |
| `POST /api/alert` | **Gateway:** liveness transition (`node_down`) → best-effort Twilio SMS (creds stay at the edge). Fail-safe `{dispatched:false,reason:"not_configured"}` when no Twilio is set up. |
| `GET /api/status` | **Pi heartbeat:** `{continue_mitigation}`. **Fail-closed** — if the `garden_state` store is unreadable, returns `false` rather than "keep firing". |
| `GET /api/state` | **Dashboard:** `{armed, override_stop, continue_mitigation, latest_event, node}` (`node` = liveness + telemetry) |
| `GET /api/snapshot` | **Dashboard:** latest evidence JPEG (`image/jpeg`), `404` if none yet |
| `POST /api/control` | **Dashboard:** `{cmd: "arm"\|"disarm"\|"stop"\|"resume"}` → write KV → return new state |

> Full request/response shapes for every endpoint (incl. the planned node↔Pi `/motion` + `/frame`) live in **[docs/endpoint-contract.md](docs/endpoint-contract.md)**.

> ⚠️ Local Viceroy keeps KV writes **in memory for the serve session only** (not persisted across restarts). The store is seeded with safe defaults (`armed=true`, `override_stop=false`) in `backend/fastly.toml`.

---

## 📂 Project Organization

```text
├── AGENTS.md                  # Development directives, known traps, and workspace instructions
├── README.md                  # This file (Main project hub)
├── docs/                      # Architecture & reference documentation
│   ├── architecture.md        # Sequence diagrams, data structures, and edge-ML logic
│   ├── endpoint-contract.md   # Authoritative request/response contract (node↔Pi↔Fastly)
│   ├── hardware-architecture.md  # Decided garden build (Pi-indoors + ESP32-C3 node) + full BOM
│   ├── hardware-reference.md  # Reference manual: Pi HAL (camera/relay), IR & weatherproofing
│   ├── simulation-and-console.md   # Full no-hardware local simulation + admin console walkthrough
│   └── pi-setup.md            # Raspberry Pi imaging & remote SSH access
├── backend/                   # Fastly Compute backend (Rust)
│   ├── fastly.toml            # Local Viceroy config: KV stores (garden_models, garden_state), config store
│   └── src/
│       ├── main.rs            # Router, edge-ML inference, dashboard state API
│       └── dashboard.html     # Admin dashboard, baked into the Wasm binary via include_str!
├── Makefile                   # `make ci` (tests) + serve/gateway/sim/console entrypoints
├── scripts/                   # serve_backend.sh (build+serve) and sync_to_pi.sh (rsync to the Pi)
├── hardware/                  # Raspberry Pi client (client.py), camera tools, AND the
│   │                          #   software garden side: gateway.py (Tier-1 indoor gateway),
│   │                          #   node_sim.py (ESP32-C3 node + peripheral simulator), scenes.py
├── provision/                 # gp-provision control plane + console.py/console.html (admin console)
├── ui/static/                 # shared UI asset layer: gp.css (one palette) + gp.js (theme/fetch/SSE helpers), served at /static by all tiers
└── tests/                     # Fixtures, kv_store model, fake_edge.py, unit + e2e sim tests
```

---

## 📋 Prerequisites

To deploy and test this project locally, you will need:
* A **Fastly Account** with API credentials.
* The [Fastly CLI](https://github.com/fastly/cli) installed on your system.
* **Rust** (stable toolchain with target `wasm32-wasi` enabled).
* **Python 3** (and `pip` for local Pi simulation testing).
* (Optional) A physical Raspberry Pi 3/4/5 with a Camera Module and PIR sensor (see [Pi Setup Guide](docs/pi-setup.md)).

---

## 🧪 Try It With No Hardware (full local simulation)

You can run the **entire** system on one machine — a software ESP32-C3 node, the indoor
Pi gateway, the Compute edge, and both UIs — with no board, no sensors, and (in mock
mode) no Fastly account:

```bash
make serve     # Compute edge (Viceroy) on :7878        — monitoring dashboard at http://localhost:7878
make gateway   # indoor Pi gateway (Tier-1) on :8088
make sim       # ESP32-C3 node simulator (all peripherals) -> gateway
make console   # admin console (MOCK mode) on :8050     — control plane at http://localhost:8050
```

The simulator drives radar trips, time-lapse frames, ~60 s heartbeats, all the
optional peripherals, the node-side spray hard-cap + refractory, and node-down. The
console's modals (provision / register device / rotate token / teardown) stream live
progress over **SSE**. Full walkthrough + scenario cheat-sheet:
**[docs/simulation-and-console.md](docs/simulation-and-console.md)**.

**Cameras wired directly to the Pi** (no radar/ESP32 needed) push stills to the edge
via `hardware/camera_pusher.py`, and the console's per-garden **Cameras gallery** shows
a live still per camera — scales to N cameras. See the *Multiple cameras (real Pi)*
section of the runbook.

## 🏁 Getting Started & Current Stage

The project is in the **Hardware Validation & Rollout Stage**. The full Pi → Fastly → Pi loop is validated on real hardware, and the **Compute admin dashboard v1** (status + snapshot + arm/disarm/stop/resume) is built and verified locally. Next up: bench-test relay/deterrent polarity, run the fail-closed + latency benchmarks, and roll out physically in the garden — see the **[Hardware Architecture & Build](docs/hardware-architecture.md)** doc for the decided *Pi-indoors + ESP32-C3 radar node + DIY water-jet deterrent* design and full bill of materials.

### 1. Read the Architecture
Read the **[Detailed Architecture Guide](docs/architecture.md)** to understand how the Fastly edge components (KV Store, Config Store, Secret Store) and physical hardware client interface.

### 2. Setup the Raspberry Pi
If you have a physical Raspberry Pi, follow the **[Pi Setup Guide](docs/pi-setup.md)** to configure remote passwordless access. This allows you to sync and run the physical client scripts.

