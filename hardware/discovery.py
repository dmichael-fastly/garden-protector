#!/usr/bin/env python3
"""hardware/discovery.py — find the garden's devices for the onboarding step.

Two channels, folded into one device list:

  * **mDNS** — browse ``_gpnode._tcp`` for a short bounded window (zeroconf) to find
    ESP32-C3 garden nodes that advertise themselves (the simulator does too, so the
    slice is demoable with no real firmware).
  * **Passive** — a node already pushing heartbeats to the local gateway shows up even
    if mDNS misses it (read the gateway's /state liveness).

Plus cameras from ``sysinfo.cameras()`` (camera_probe). Everything is best-effort and
never raises to the caller: zeroconf is an OPTIONAL import (absent on a Mac/CI → no
mDNS results), subprocess/HTTP probes are time-bounded, and the manual-add path needs
no network at all. Transport detail returned here is Pi-LOCAL (two-tier split): it
goes into pi-garden.json, never to the edge.
"""
import json
import socket
import time
import urllib.request

SERVICE_TYPE = "_gpnode._tcp.local."
DEFAULT_MDNS_TIMEOUT = 3.0
# Mirror the gateway's liveness window so "recently seen" agrees across the system.
PASSIVE_FRESH_SECS = 150


def _zeroconf():
    """The zeroconf module, or None when it isn't installed (dev box / CI). Optional
    import so importing discovery never fails where mDNS isn't available."""
    try:
        import zeroconf
        return zeroconf
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# mDNS browse.
# ---------------------------------------------------------------------------

def _info_to_node(name, info):
    """Map a zeroconf ServiceInfo to our node dict (decoding TXT props + addresses)."""
    props = {}
    for k, v in (getattr(info, "properties", None) or {}).items():
        try:
            kk = k.decode() if isinstance(k, (bytes, bytearray)) else str(k)
            vv = v.decode() if isinstance(v, (bytes, bytearray)) else v
        except Exception:  # noqa: BLE001
            kk, vv = str(k), v
        props[kk] = vv
    addrs = []
    for a in (getattr(info, "addresses", None) or []):
        try:
            addrs.append(socket.inet_ntoa(a))
        except (OSError, ValueError):
            pass
    node_id = props.get("node_id") or (name or "").split(".")[0] or "node"
    return {
        "node_id": node_id,
        "host": (getattr(info, "server", "") or "").rstrip("."),
        "port": getattr(info, "port", None),
        "ip": addrs[0] if addrs else None,
        "addresses": addrs,
        "props": props,
        "found_by": "mdns",
    }


def browse_mdns(timeout=DEFAULT_MDNS_TIMEOUT):
    """Browse ``_gpnode._tcp`` for ``timeout`` seconds. Returns a list of node dicts;
    [] if zeroconf isn't installed. Bounded — never blocks longer than ``timeout``."""
    zc_mod = _zeroconf()
    if zc_mod is None:
        return []
    found = {}

    class _Listener:
        def _grab(self, zc, type_, name):
            try:
                info = zc.get_service_info(type_, name, timeout=int(timeout * 1000))
            except Exception:  # noqa: BLE001 — a flaky resolve must not crash the scan
                info = None
            if info:
                found[name] = _info_to_node(name, info)

        # zeroconf calls add_service/update_service (signature varies across versions,
        # but all pass (zc, type, name)).
        def add_service(self, zc, type_, name):
            self._grab(zc, type_, name)

        def update_service(self, zc, type_, name):
            self._grab(zc, type_, name)

        def remove_service(self, zc, type_, name):
            pass

    zc = zc_mod.Zeroconf()
    try:
        zc_mod.ServiceBrowser(zc, SERVICE_TYPE, _Listener())
        time.sleep(timeout)
    finally:
        try:
            zc.close()
        except Exception:  # noqa: BLE001
            pass
    return list(found.values())


# ---------------------------------------------------------------------------
# Passive discovery via the local gateway's liveness.
# ---------------------------------------------------------------------------

def passive_nodes(gateway_url, *, timeout=2.0):
    """A node the local gateway has recently heard from (so a node already talking
    shows up even if mDNS misses it). Reads GET <gateway>/state; [] on any error or
    if the node is down. The gateway snapshot is single-node + id-less, so the id is
    best-effort (telemetry node_id if present, else 'gateway-node')."""
    if not gateway_url:
        return []
    try:
        with urllib.request.urlopen(gateway_url.rstrip("/") + "/state", timeout=timeout) as r:
            snap = json.loads(r.read().decode())
    except Exception:  # noqa: BLE001 — gateway not running / unreachable -> no passive nodes
        return []
    if not isinstance(snap, dict) or snap.get("node_down", True) or not snap.get("last_seen_ms"):
        return []
    tel = snap.get("latest_telemetry") or {}
    node_id = tel.get("node_id") or snap.get("node_id") or "gateway-node"
    return [{
        "node_id": node_id, "host": None, "port": None, "ip": None, "addresses": [],
        "props": {}, "found_by": "passive", "last_seen_ms": snap.get("last_seen_ms"),
    }]


# ---------------------------------------------------------------------------
# Aggregate scan (cameras + nodes), deduped by node_id (mDNS wins over passive).
# ---------------------------------------------------------------------------

def scan_cameras():
    """Structured cameras from sysinfo (camera_probe). [] off-Pi or on error."""
    try:
        from hardware import sysinfo
    except ImportError:  # pragma: no cover - script mode
        import sysinfo
    try:
        return sysinfo.cameras()
    except Exception:  # noqa: BLE001
        return []


def scan(*, mdns_timeout=DEFAULT_MDNS_TIMEOUT, gateway_url=None):
    """Full device scan for the onboarding step: {cameras, nodes}. mDNS first
    (authoritative for a node's id/props), then passive, deduped by node_id."""
    nodes, seen = [], set()
    for n in browse_mdns(mdns_timeout) + passive_nodes(gateway_url):
        nid = n.get("node_id")
        if nid and nid in seen:
            continue
        if nid:
            seen.add(nid)
        nodes.append(n)
    return {"cameras": scan_cameras(), "nodes": nodes}


if __name__ == "__main__":
    print(json.dumps(scan(gateway_url="http://127.0.0.1:8088"), indent=2))
