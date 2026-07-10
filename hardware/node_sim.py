#!/usr/bin/env python3
"""hardware/node_sim.py — a software model of the **ESP32-C3 garden node**, so the whole push-model loop can be exercised end-to-end without the
physical board (or the optional peripherals) on hand.

It speaks Tier-1 of the contract (docs/endpoint-contract.md) *to the Pi gateway*:

    POST /heartbeat   every ~60 s — liveness + environment (+ optional peripherals)
    POST /frame       every `interval_s` — a time-lapse / latest still
    POST /motion      on a radar trip — the incident, held open for the spray reply

and faithfully reproduces the board's safety behaviour:

  * **Hard spray cap.** Whatever `seconds` the gateway requests, the node sprays
    `min(seconds, 4)` — the local fail-closed backstop (a Pi crash mid-burst still
    self-terminates).
  * **Post-spray refractory.** After a burst it ignores radar for a few seconds so
    the jet's own spray (moving water = "moving mass") can't re-trigger a loop.
  * **Fail-closed.** A timeout / non-200 / missing `spray:true` -> the N/C solenoid
    stays shut (no spray).
  * **Optional peripherals** are sent only when *fitted* (matching the contract):
    soil moisture, reservoir float switch, INA219 spray-confirm, LD2410 presence,
    18650 backup power.

Pure helpers (`cap_seconds`, `refractory_active`, `build_snapshot`, `actuate`) carry
the safety logic and are unit-tested without any network.
"""
import argparse
import base64
import os
import random
import socket
import threading
import time

import requests

try:
    from hardware import scenes
except ImportError:  # plain-script execution
    import scenes

# zeroconf is OPTIONAL (only the sim's mDNS advert + the Pi's discovery need it).
# Absent on a Mac/CI -> the advert is silently skipped, the sim still runs.
try:
    from zeroconf import ServiceInfo, Zeroconf
except ImportError:  # pragma: no cover - dev box without the dep
    ServiceInfo = Zeroconf = None


# The board's non-negotiable local hard cap (seconds). See endpoint-contract.md.
HARD_CAP_SECONDS = 4
# Ignore radar during + for this long after the node's own burst.
REFRACTORY_SECS = 6


def now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# PURE SAFETY HELPERS (no I/O).
# ---------------------------------------------------------------------------

def cap_seconds(requested, hard_cap=HARD_CAP_SECONDS):
    """The node never sprays longer than its own hard cap, whatever the Pi asks."""
    try:
        s = int(requested)
    except (TypeError, ValueError):
        return 0
    return max(0, min(s, hard_cap))


def refractory_active(last_burst_end_ms, now, refractory_secs=REFRACTORY_SECS):
    """True while the node is in its post-spray refractory window."""
    if not isinstance(last_burst_end_ms, (int, float)):
        return False
    return now < last_burst_end_ms + refractory_secs * 1000


def actuate(reply, *, reservoir_ok=True, hard_cap=HARD_CAP_SECONDS):
    """Interpret a gateway /motion reply into the physical outcome the node would
    produce. Returns ``(sprayed_seconds, spray_confirmed)``.

    FAIL-CLOSED: only an explicit ``spray: true`` actuates; everything else is a
    no-op. ``spray_confirmed`` models the INA219 current-sense truth signal — the
    valve only *actually* throws water when the reservoir isn't empty, so a `true`
    command against an empty jug confirms `false` (spraying air)."""
    if not (isinstance(reply, dict) and reply.get("spray") is True):
        return 0, False
    secs = cap_seconds(reply.get("seconds", 0), hard_cap)
    if secs <= 0:
        return 0, False
    return secs, bool(reservoir_ok)


# ---------------------------------------------------------------------------
# ENVIRONMENT + PERIPHERAL MODEL.
# ---------------------------------------------------------------------------

# Optional peripherals the node can be "fitted" with. Each maps to one optional
# telemetry field from endpoint-contract.md. Only fitted ones are transmitted.
OPTIONAL_PERIPHERALS = ("soil", "reservoir", "spray_confirm", "presence", "backup")


def build_snapshot(state, now, *, fitted=(), include_spray_confirm=False):
    """Build the telemetry blob the node POSTs. Always includes the base
    environment; includes each optional field only when its peripheral is fitted.

    ``state`` is the NodeSim's live dict; ``now`` lets callers stamp ts_ms."""
    snap = {
        "ts_ms": now,
        "battery_voltage": round(state["battery_voltage"], 2),
        "rssi": state["rssi"],
        "uptime_s": int(state["uptime_s"]),
        "temperature_c": round(state["temperature_c"], 1),
        "humidity_pct": round(state["humidity_pct"], 0),
        "rainfall_mm": round(state["rainfall_mm"], 1),
        "raining": bool(state["raining"]),
        "lux_level": round(state["lux_level"], 1),
    }
    if "soil" in fitted:
        snap["soil_moisture_pct"] = round(state["soil_moisture_pct"], 0)
    if "reservoir" in fitted:
        snap["reservoir_ok"] = bool(state["reservoir_ok"])
    if "presence" in fitted:
        snap["presence_distance_cm"] = state["presence_distance_cm"]
    if "backup" in fitted:
        snap["on_backup_power"] = bool(state["on_backup_power"])
    # spray_confirmed is a *post-hoc* truth signal: only meaningful right after a
    # burst, and only when the INA219 is fitted.
    if "spray_confirm" in fitted and include_spray_confirm:
        snap["spray_confirmed"] = bool(state["spray_confirmed"])
    return snap


def diurnal(now_secs, *, period_s=120.0):
    """A 0..1 'daylight' factor cycling over `period_s` (compressed day for the
    sim). Pure: drives lux + a gentle temperature swing deterministically."""
    import math
    phase = (now_secs % period_s) / period_s
    return (1 - math.cos(2 * math.pi * phase)) / 2


# ---------------------------------------------------------------------------
# THE SIMULATOR.
# ---------------------------------------------------------------------------

class NodeSim:
    def __init__(self, gateway_url, *, fitted=(), raining=False, reservoir_ok=True,
                 on_backup_power=False, day_period_s=120.0, http_timeout=4.0,
                 rng=None, node_id="node-sim-01", advertise=True):
        self.gateway_url = gateway_url.rstrip("/")
        self.fitted = tuple(fitted)
        self.node_id = node_id
        self.advertise = advertise
        self._zc = None
        self._svc_info = None
        self.http_timeout = http_timeout
        self.rng = rng or random.Random(1234)
        self._start = time.time()
        self.last_burst_end_ms = None
        self.interval_s = 300
        self.armed = False
        self.maintenance = False
        self.day_period_s = day_period_s
        # Live physical state.
        self.state = {
            "battery_voltage": 4.12,
            "rssi": -61,
            "uptime_s": 0,
            "temperature_c": 18.5,
            "humidity_pct": 64.0,
            "rainfall_mm": 0.0,
            "raining": raining,
            "lux_level": 200.0,
            "soil_moisture_pct": 38.0,
            "reservoir_ok": reservoir_ok,
            "presence_distance_cm": 400,
            "on_backup_power": on_backup_power,
            "spray_confirmed": False,
        }
        self._stop = threading.Event()
        self.counters = {"heartbeats": 0, "frames": 0, "motions": 0, "sprays": 0}

    # -- environment evolution --------------------------------------------
    def _advance_env(self, now):
        self.state["uptime_s"] = now - self._start
        light = diurnal(now, period_s=self.day_period_s)
        self.state["lux_level"] = round(2.0 + light * 800.0, 1)
        self.state["temperature_c"] = round(14.0 + light * 10.0, 1)
        self.state["humidity_pct"] = round(80.0 - light * 25.0 + (10.0 if self.state["raining"] else 0.0), 0)
        # Battery sags slowly; faster while on backup power.
        drain = 0.02 if self.state["on_backup_power"] else 0.004
        self.state["battery_voltage"] = max(3.3, self.state["battery_voltage"] - drain)
        self.state["rssi"] = -55 - self.rng.randint(0, 20)
        if self.state["raining"]:
            self.state["rainfall_mm"] = round(self.state["rainfall_mm"] + 0.2, 1)
        return light

    def set_rain(self, on):
        self.state["raining"] = bool(on)

    # -- one-shot network actions (return the parsed reply) ---------------
    def send_heartbeat(self, *, include_spray_confirm=False):
        now = now_ms()
        self._advance_env(time.time())
        snap = build_snapshot(self.state, now, fitted=self.fitted,
                              include_spray_confirm=include_spray_confirm)
        reply = self._post("/heartbeat", snap)
        self.counters["heartbeats"] += 1
        self._apply_params(reply)
        return reply

    def send_frame(self, scene="empty"):
        now = now_ms()
        self._advance_env(time.time())
        jpeg = scenes.render_scene(scene, seed=self.counters["frames"])
        body = {
            "ts_ms": now,
            "jpeg_b64": base64.b64encode(jpeg).decode(),
            "battery_voltage": round(self.state["battery_voltage"], 2),
            "rssi": self.state["rssi"],
        }
        reply = self._post("/frame", body)
        self.counters["frames"] += 1
        self._apply_params(reply)
        return reply

    def radar_trip(self, scene="raccoon", bed=1):
        """Model a radar trip: capture a scene, POST /motion, and actuate per the
        reply (honouring the hard cap + refractory + fail-closed)."""
        now = now_ms()
        self.counters["motions"] += 1
        if refractory_active(self.last_burst_end_ms, now):
            # The board ignores radar during its own refractory.
            return {"spray": False, "reason": "refractory", "local": True}

        self._advance_env(time.time())
        jpeg = scenes.render_scene(scene, seed=self.counters["motions"])
        body = {
            "bed": bed,
            "ts_ms": now,
            "jpeg_b64": base64.b64encode(jpeg).decode(),
            "telemetry": {"raining": self.state["raining"], "lux_level": self.state["lux_level"]},
        }
        try:
            reply = self._post("/motion", body)
        except Exception as e:
            # Fail-closed: the node does nothing on any error talking to the Pi.
            print(f"[node] /motion failed: {e} -> no spray (fail-closed)", flush=True)
            return {"spray": False, "reason": "gateway_unreachable", "local": True}

        secs, confirmed = actuate(reply, reservoir_ok=self.state["reservoir_ok"])
        if secs > 0:
            self.counters["sprays"] += 1
            self.state["spray_confirmed"] = confirmed
            self.last_burst_end_ms = now_ms() + secs * 1000
            tag = "CONFIRMED" if confirmed else "NO-FLOW(empty reservoir?)"
            print(f"[node] SPRAY {secs}s ({tag}) bed={bed} scene={scene}", flush=True)
        return reply

    # -- helpers -----------------------------------------------------------
    def _apply_params(self, reply):
        if isinstance(reply, dict):
            self.interval_s = reply.get("interval_s", self.interval_s)
            self.armed = reply.get("armed", self.armed)
            self.maintenance = reply.get("maintenance", self.maintenance)

    def _post(self, route, body):
        r = requests.post(f"{self.gateway_url}{route}", json=body, timeout=self.http_timeout)
        r.raise_for_status()
        return r.json() if r.content else {}

    # -- mDNS self-advertisement (so the wizard's scan finds this node) ----
    def _local_ip(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))  # no packets sent; picks the egress iface
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"
        finally:
            s.close()

    def start_advert(self):
        """Register a ``_gpnode._tcp`` service (name=node_id, TXT carries node_id +
        fitted peripherals) so hardware/discovery.py finds this node. No-op if
        zeroconf is missing or advertising is disabled. The node is push-only, so the
        advertised port is synthetic (80)."""
        if not self.advertise or Zeroconf is None:
            if self.advertise and Zeroconf is None:
                print("[node] zeroconf not installed — mDNS advert disabled", flush=True)
            return
        ip = self._local_ip()
        info = ServiceInfo(
            "_gpnode._tcp.local.",
            f"{self.node_id}._gpnode._tcp.local.",
            addresses=[socket.inet_aton(ip)],
            port=80,
            properties={"node_id": self.node_id, "fitted": ",".join(self.fitted)},
            server=f"{self.node_id}.local.",
        )
        try:
            self._zc = Zeroconf()
            self._zc.register_service(info)
            self._svc_info = info
            print(f"[node] mDNS: advertising {self.node_id} (_gpnode._tcp) at {ip}", flush=True)
        except Exception as e:  # noqa: BLE001 — advert is best-effort, never fatal
            print(f"[node] mDNS advert failed: {e}", flush=True)
            self._zc = None

    def _stop_advert(self):
        if self._zc is not None:
            try:
                if self._svc_info:
                    self._zc.unregister_service(self._svc_info)
                self._zc.close()
            except Exception:  # noqa: BLE001
                pass
            self._zc = None

    # -- live run loops ----------------------------------------------------
    def run(self, *, heartbeat_s=2.0, frame_s=6.0, trip_every_s=8.0, scenes_cycle=None):
        """Run the three loops until stop(). Cadences are compressed for a watchable
        demo; real firmware uses ~60 s heartbeats."""
        self.start_advert()
        scenes_cycle = scenes_cycle or ["raccoon", "human", "fox", "empty", "cat"]
        threads = [
            threading.Thread(target=self._loop, args=(heartbeat_s, lambda: self.send_heartbeat()), daemon=True),
            threading.Thread(target=self._loop, args=(frame_s, lambda: self.send_frame("empty")), daemon=True),
        ]

        def trip_loop():
            i = 0
            while not self._stop.wait(trip_every_s):
                scene = scenes_cycle[i % len(scenes_cycle)]
                i += 1
                try:
                    self.radar_trip(scene)
                except Exception as e:
                    print(f"[node] trip error: {e}", flush=True)

        threads.append(threading.Thread(target=trip_loop, daemon=True))
        for t in threads:
            t.start()
        try:
            while not self._stop.wait(1.0):
                pass
        except KeyboardInterrupt:
            pass

    def _loop(self, interval, fn):
        while not self._stop.wait(interval):
            try:
                fn()
            except Exception as e:
                print(f"[node] loop error: {e}", flush=True)

    def stop(self):
        self._stop.set()
        self._stop_advert()


def main(argv=None):
    p = argparse.ArgumentParser(description="ESP32-C3 garden-node simulator")
    p.add_argument("--gateway", default=os.environ.get("GP_GATEWAY", "http://localhost:8088"))
    p.add_argument("--node-id", default=os.environ.get("GP_NODE_ID") or socket.gethostname() or "node-sim-01",
                   help="node id advertised over mDNS + shown in the wizard scan")
    p.add_argument("--advertise", dest="advertise", action="store_true", default=True,
                   help="advertise _gpnode._tcp over mDNS (default on)")
    p.add_argument("--no-advertise", dest="advertise", action="store_false",
                   help="disable the mDNS advert (e.g. when real firmware is present)")
    p.add_argument("--fitted", default="", help="comma list of optional peripherals: "
                   + ",".join(OPTIONAL_PERIPHERALS) + " (or 'all')")
    p.add_argument("--raining", action="store_true")
    p.add_argument("--reservoir-empty", action="store_true")
    p.add_argument("--on-backup-power", action="store_true")
    p.add_argument("--heartbeat-s", type=float, default=2.0)
    p.add_argument("--frame-s", type=float, default=6.0)
    p.add_argument("--trip-every-s", type=float, default=8.0)
    p.add_argument("--day-period-s", type=float, default=120.0)
    p.add_argument("--seed", type=int, default=1234)
    args = p.parse_args(argv)

    fitted = OPTIONAL_PERIPHERALS if args.fitted == "all" else tuple(
        x.strip() for x in args.fitted.split(",") if x.strip())
    sim = NodeSim(args.gateway, fitted=fitted, raining=args.raining,
                  reservoir_ok=not args.reservoir_empty, on_backup_power=args.on_backup_power,
                  day_period_s=args.day_period_s, rng=random.Random(args.seed),
                  node_id=args.node_id, advertise=args.advertise)
    print(f"[node] simulating ESP32-C3 node '{args.node_id}' -> {args.gateway} "
          f"fitted={fitted or '(base only)'} advert={'on' if args.advertise else 'off'}", flush=True)
    try:
        sim.run(heartbeat_s=args.heartbeat_s, frame_s=args.frame_s, trip_every_s=args.trip_every_s)
    finally:
        sim.stop()
        print(f"[node] counters: {sim.counters}", flush=True)


if __name__ == "__main__":
    main()
