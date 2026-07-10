"""Unit tests for hardware/discovery.py + node_sim's mDNS advert.

zeroconf is mocked throughout (it isn't a hard dependency), so these run anywhere.
"""
import json
import socket

from hardware import discovery


class _FakeInfo:
    def __init__(self, properties, addresses, server, port):
        self.properties, self.addresses, self.server, self.port = properties, addresses, server, port


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# --- mDNS mapping + guarded import ----------------------------------------

def test_info_to_node_decodes_props_and_addresses():
    info = _FakeInfo(properties={b"node_id": b"node-7", b"fitted": b"soil,presence"},
                     addresses=[socket.inet_aton("10.0.0.51")], server="node-7.local.", port=80)
    n = discovery._info_to_node("node-7._gpnode._tcp.local.", info)
    assert n["node_id"] == "node-7"
    assert n["ip"] == "10.0.0.51" and n["addresses"] == ["10.0.0.51"]
    assert n["port"] == 80 and n["host"] == "node-7.local"
    assert n["props"]["fitted"] == "soil,presence" and n["found_by"] == "mdns"


def test_browse_mdns_empty_without_zeroconf(monkeypatch):
    monkeypatch.setattr(discovery, "_zeroconf", lambda: None)
    assert discovery.browse_mdns(timeout=0.01) == []


# --- passive discovery via the gateway /state -----------------------------

def test_passive_nodes_live(monkeypatch):
    monkeypatch.setattr(discovery.urllib.request, "urlopen",
                        lambda url, timeout=2.0: _Resp(
                            {"node_down": False, "last_seen_ms": 123,
                             "latest_telemetry": {"node_id": "node-7"}}))
    nodes = discovery.passive_nodes("http://gw:8088")
    assert nodes and nodes[0]["node_id"] == "node-7" and nodes[0]["found_by"] == "passive"


def test_passive_nodes_down_or_unreachable(monkeypatch):
    monkeypatch.setattr(discovery.urllib.request, "urlopen",
                        lambda url, timeout=2.0: _Resp({"node_down": True, "last_seen_ms": 1}))
    assert discovery.passive_nodes("http://gw:8088") == []

    def boom(url, timeout=2.0):
        raise OSError("gateway not running")
    monkeypatch.setattr(discovery.urllib.request, "urlopen", boom)
    assert discovery.passive_nodes("http://gw:8088") == []
    assert discovery.passive_nodes("") == []   # no gateway configured


# --- aggregate scan: dedup (mDNS wins) + cameras --------------------------

def test_scan_dedups_nodes_and_includes_cameras(monkeypatch):
    monkeypatch.setattr(discovery, "browse_mdns",
                        lambda timeout=3.0: [{"node_id": "node-7", "found_by": "mdns"}])
    monkeypatch.setattr(discovery, "passive_nodes",
                        lambda gw, **k: [{"node_id": "node-7", "found_by": "passive"},
                                         {"node_id": "node-9", "found_by": "passive"}])
    monkeypatch.setattr(discovery, "scan_cameras",
                        lambda: [{"type": "camera_usb", "transport": {"dev": "/dev/video0"}}])
    out = discovery.scan(gateway_url="http://gw:8088")
    assert [n["node_id"] for n in out["nodes"]] == ["node-7", "node-9"]   # mDNS dup wins
    assert out["nodes"][0]["found_by"] == "mdns"
    assert out["cameras"][0]["type"] == "camera_usb"


# --- node_sim mDNS advert -------------------------------------------------

def test_node_sim_advert_noop_without_zeroconf(monkeypatch):
    from hardware import node_sim
    monkeypatch.setattr(node_sim, "Zeroconf", None)
    sim = node_sim.NodeSim("http://gw:8088", node_id="node-7", advertise=True)
    sim.start_advert()        # must not raise
    assert sim._zc is None


def test_node_sim_advert_registers_node_id_and_fitted(monkeypatch):
    from hardware import node_sim
    reg = {}

    class FakeZC:
        def register_service(self, info):
            reg["info"] = info

        def unregister_service(self, info):
            reg["unreg"] = True

        def close(self):
            reg["closed"] = True

    monkeypatch.setattr(node_sim, "Zeroconf", FakeZC)
    monkeypatch.setattr(node_sim, "ServiceInfo",
                        lambda type_, name, **kw: {"type": type_, "name": name, **kw})
    sim = node_sim.NodeSim("http://gw:8088", node_id="node-7",
                           fitted=("soil", "presence"), advertise=True)
    sim.start_advert()
    assert reg["info"]["name"] == "node-7._gpnode._tcp.local."
    assert reg["info"]["properties"] == {"node_id": "node-7", "fitted": "soil,presence"}
    sim.stop()
    assert reg.get("unreg") is True and reg.get("closed") is True
