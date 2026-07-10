"""Unit tests for hardware/sysinfo.py — first-run environment detection.

Mocks /proc reads (repointed path constants) and the subprocess seam so the whole
thing runs off-Pi and we can assert it degrades to unknowns rather than raising.
"""
import json

from hardware import sysinfo


# --- Pi identity ----------------------------------------------------------

def test_pi_model_from_device_tree(tmp_path, monkeypatch):
    f = tmp_path / "model"
    f.write_bytes(b"Raspberry Pi 5 Model B Rev 1.0\x00")  # NUL-terminated like real /proc
    monkeypatch.setattr(sysinfo, "MODEL_PATH", str(f))
    assert sysinfo.pi_model() == "Raspberry Pi 5 Model B Rev 1.0"


def test_pi_model_and_serial_fall_back_to_cpuinfo(tmp_path, monkeypatch):
    monkeypatch.setattr(sysinfo, "MODEL_PATH", str(tmp_path / "missing"))
    cpu = tmp_path / "cpuinfo"
    cpu.write_text("processor\t: 0\nRevision\t: c03114\nSerial\t\t: 100000001abc\n")
    monkeypatch.setattr(sysinfo, "CPUINFO_PATH", str(cpu))
    assert sysinfo.pi_model() == "rev c03114"
    assert sysinfo.pi_serial() == "100000001abc"


def test_unknowns_off_pi(tmp_path, monkeypatch):
    monkeypatch.setattr(sysinfo, "MODEL_PATH", str(tmp_path / "x"))
    monkeypatch.setattr(sysinfo, "CPUINFO_PATH", str(tmp_path / "y"))
    assert sysinfo.pi_model() is None
    assert sysinfo.pi_serial() is None


def test_mdns_name_appends_local(monkeypatch):
    monkeypatch.setattr(sysinfo, "hostname", lambda: "raspberrypi")
    assert sysinfo.mdns_name() == "raspberrypi.local"


# --- Network --------------------------------------------------------------

def test_ssid_prefers_iwgetid(monkeypatch):
    monkeypatch.setattr(sysinfo, "_run",
                        lambda cmd, timeout=sysinfo._CMD_TIMEOUT:
                        (0, "HomeWiFi\n", "") if cmd[0] == "iwgetid" else (127, "", ""))
    assert sysinfo._ssid() == "HomeWiFi"


def test_ssid_falls_back_to_nmcli(monkeypatch):
    def fake(cmd, timeout=sysinfo._CMD_TIMEOUT):
        if cmd[0] == "iwgetid":
            return 127, "", "missing"
        if cmd[0] == "nmcli":
            return 0, "no:OtherNet\nyes:HomeWiFi\n", ""
        return 127, "", ""
    monkeypatch.setattr(sysinfo, "_run", fake)
    assert sysinfo._ssid() == "HomeWiFi"


def test_iface_and_mac_parses_ip_json(monkeypatch):
    payload = json.dumps([
        {"ifname": "lo", "address": "00:00:00:00:00:00",
         "addr_info": [{"local": "127.0.0.1"}]},
        {"ifname": "wlan0", "address": "dc:a6:32:11:22:33",
         "addr_info": [{"local": "10.0.0.42"}]},
    ])
    monkeypatch.setattr(sysinfo, "_run", lambda cmd, timeout=4.0: (0, payload, ""))
    assert sysinfo._iface_and_mac("10.0.0.42") == ("wlan0", "dc:a6:32:11:22:33")
    assert sysinfo._iface_and_mac(None) == (None, None)


def test_wifi_degrades_to_unknowns(tmp_path, monkeypatch):
    monkeypatch.setattr(sysinfo, "_run", lambda *a, **k: (127, "", "missing"))
    monkeypatch.setattr(sysinfo, "_primary_ip", lambda: None)
    monkeypatch.setattr(sysinfo, "WIRELESS_PATH", str(tmp_path / "missing"))
    assert sysinfo.wifi() == {"ssid": None, "ip": None, "iface": None,
                              "mac": None, "signal": None}


def test_wifi_signal_from_proc_net_wireless(tmp_path, monkeypatch):
    w = tmp_path / "wireless"
    w.write_text(
        "Inter-| sta-|   Quality        |   Discarded packets\n"
        " face | tus | link level noise |  nwid  crypt   misc\n"
        " wlan0: 0000   63.   -58.  -256        0      0      0\n")
    monkeypatch.setattr(sysinfo, "WIRELESS_PATH", str(w))
    assert sysinfo._wifi_signal() == {"dbm": -58, "quality_pct": 90}


def test_wifi_signal_falls_back_to_nmcli(tmp_path, monkeypatch):
    monkeypatch.setattr(sysinfo, "WIRELESS_PATH", str(tmp_path / "missing"))
    monkeypatch.setattr(sysinfo, "_run",
                        lambda cmd, timeout=sysinfo._CMD_TIMEOUT:
                        (0, "no:40\nyes:72\n", "") if cmd[0] == "nmcli" else (127, "", ""))
    assert sysinfo._wifi_signal() == {"dbm": None, "quality_pct": 72}


# --- Resource health: disk / memory / temp / power ------------------------

def test_memory_parses_meminfo(tmp_path, monkeypatch):
    m = tmp_path / "meminfo"
    m.write_text("MemTotal:        4000000 kB\nMemFree:          500000 kB\n"
                 "MemAvailable:    3000000 kB\n")
    monkeypatch.setattr(sysinfo, "MEMINFO_PATH", str(m))
    assert sysinfo.memory() == {"total": 4000000 * 1024, "available": 3000000 * 1024}


def test_memory_none_off_pi(tmp_path, monkeypatch):
    monkeypatch.setattr(sysinfo, "MEMINFO_PATH", str(tmp_path / "missing"))
    assert sysinfo.memory() is None


def test_cpu_temp_from_thermal_zone(tmp_path, monkeypatch):
    t = tmp_path / "temp"
    t.write_text("48312\n")
    monkeypatch.setattr(sysinfo, "THERMAL_PATH", str(t))
    assert sysinfo.cpu_temp_c() == 48.3


def test_cpu_temp_none_off_pi(tmp_path, monkeypatch):
    monkeypatch.setattr(sysinfo, "THERMAL_PATH", str(tmp_path / "missing"))
    assert sysinfo.cpu_temp_c() is None


def test_power_parses_vcgencmd_throttled(monkeypatch):
    # 0x50005 = under_voltage_now|throttled_now (low nibble) + both *_occurred bits
    monkeypatch.setattr(sysinfo, "_run",
                        lambda cmd, timeout=sysinfo._CMD_TIMEOUT: (0, "throttled=0x50005\n", ""))
    assert sysinfo.power() == {
        "under_voltage_now": True, "throttled_now": True,
        "under_voltage_occurred": True, "throttled_occurred": True,
    }


def test_power_healthy(monkeypatch):
    monkeypatch.setattr(sysinfo, "_run",
                        lambda cmd, timeout=sysinfo._CMD_TIMEOUT: (0, "throttled=0x0\n", ""))
    assert sysinfo.power() == {
        "under_voltage_now": False, "throttled_now": False,
        "under_voltage_occurred": False, "throttled_occurred": False,
    }


def test_power_none_when_vcgencmd_missing(monkeypatch):
    monkeypatch.setattr(sysinfo, "_run", lambda *a, **k: (127, "", "missing"))
    assert sysinfo.power() is None


def test_disk_returns_total_and_free(tmp_path, monkeypatch):
    monkeypatch.setattr(sysinfo, "DISK_PATH", str(tmp_path))
    d = sysinfo.disk()
    assert set(d) == {"total", "free"} and d["total"] > 0 and d["free"] >= 0


# --- Cameras (structured wrapper over camera_probe) -----------------------

def test_cameras_structured_with_local_transport(monkeypatch):
    from hardware import camera_probe
    monkeypatch.setattr(sysinfo, "_list_video_nodes", lambda: ["/dev/video0", "/dev/video1"])

    def fake_v4l2(dev):
        if dev == "/dev/video0":
            return "uvcvideo", "usb-0000:01:00.0-1.2", True   # real capture node
        return "uvcvideo", "usb-0000:01:00.0-1.3", False      # metadata node (skipped)
    monkeypatch.setattr(camera_probe, "v4l2_node_info", fake_v4l2)
    monkeypatch.setattr(camera_probe, "run",
                        lambda cmd, timeout=4.0: (0, "Available cameras\n0 : imx708 [4608x2592]", ""))

    cams = sysinfo.cameras()
    assert [c["type"] for c in cams].count("camera_csi") == 1   # CSI from rpicam (one)
    usb = [c for c in cams if c["type"] == "camera_usb"]
    assert len(usb) == 1                                        # metadata node skipped
    assert usb[0]["transport"] == {"kind": "usb", "dev": "/dev/video0",
                                   "bus": "usb-0000:01:00.0-1.2", "driver": "uvcvideo"}


def test_cameras_no_overreport_from_csi_isp_pipeline(monkeypatch):
    """Regression: a Pi CSI camera exposes ~12 /dev/video* ISP-pipeline nodes that all
    enumerate formats — they must NOT each become a camera. Exactly 1 CSI (from rpicam)
    + 1 USB (deduped by bus), not 13."""
    from hardware import camera_probe
    nodes = [f"/dev/video{i}" for i in range(14)]
    monkeypatch.setattr(sysinfo, "_list_video_nodes", lambda: nodes)

    def fake_v4l2(dev):
        n = int(dev.rsplit("video", 1)[1])
        if n in (8, 9):   # the single USB webcam exposes a capture + metadata node, same bus
            return "uvcvideo", "usb-0000:01:00.0-1.2", (n == 8)
        # everything else is the CSI/ISP platform pipeline (capture-capable but NOT a camera)
        return "bcm2835-isp", "platform:bcm2835-isp", True
    monkeypatch.setattr(camera_probe, "v4l2_node_info", fake_v4l2)
    monkeypatch.setattr(camera_probe, "run",
                        lambda cmd, timeout=4.0: (0, "Available cameras\n-----\n0 : imx708 [4608x2592] (/base/soc)", ""))

    cams = sysinfo.cameras()
    types = [c["type"] for c in cams]
    assert types.count("camera_csi") == 1            # NOT 12 phantom CSI nodes
    assert types.count("camera_usb") == 1            # USB deduped by bus (capture node only)
    assert len(cams) == 2
    usb = next(c for c in cams if c["type"] == "camera_usb")
    assert usb["transport"]["dev"] == "/dev/video8"


def test_cameras_empty_off_pi(monkeypatch):
    from hardware import camera_probe
    monkeypatch.setattr(sysinfo, "_list_video_nodes", lambda: [])
    monkeypatch.setattr(camera_probe, "run", lambda cmd, timeout=4.0: (127, "", "missing"))
    assert sysinfo.cameras() == []


# --- Aggregate ------------------------------------------------------------

def test_detect_is_json_serializable_and_never_raises(monkeypatch):
    from hardware import camera_probe
    monkeypatch.setattr(sysinfo, "_run", lambda *a, **k: (127, "", "missing"))
    monkeypatch.setattr(sysinfo, "_list_video_nodes", lambda: [])
    monkeypatch.setattr(camera_probe, "run", lambda cmd, timeout=4.0: (127, "", ""))
    d = sysinfo.detect()
    json.dumps(d)  # must not raise
    assert set(d) >= {"pi", "network", "health", "hostname", "mdns_name", "cameras"}
    assert d["cameras"] == []
    assert set(d["network"]) == {"ssid", "ip", "iface", "mac", "signal"}
    assert set(d["health"]) == {"disk", "memory", "cpu_temp_c", "power"}
