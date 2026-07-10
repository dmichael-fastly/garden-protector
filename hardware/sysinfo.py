#!/usr/bin/env python3
"""hardware/sysinfo.py — first-run environment detection for the setup wizard.

The provisioning wizard's first screen shows the operator what the Pi *is* — its
model, the Wi-Fi it's on, its LAN IP + hostname, and the cameras attached — so they
can confirm the box before naming a garden. Every helper here is best-effort and
**degrades gracefully off-Pi**: subprocess calls are time-bounded and never raise,
missing /proc files yield ``None``, and on a Mac the whole thing returns unknowns
instead of crashing (so the wizard is developable on a laptop).

Nothing here is a secret and nothing here is on the safety path — this is pure
control-plane detection. Run it standalone to eyeball the JSON:

    python3 -m hardware.sysinfo
"""
import json
import platform
import re
import shutil
import socket
import subprocess

# /proc + /sys paths are module constants so tests can repoint them at fixture files.
MODEL_PATH = "/proc/device-tree/model"
CPUINFO_PATH = "/proc/cpuinfo"
OS_RELEASE_PATH = "/etc/os-release"
MEMINFO_PATH = "/proc/meminfo"
WIRELESS_PATH = "/proc/net/wireless"
THERMAL_PATH = "/sys/class/thermal/thermal_zone0/temp"
DISK_PATH = "/"   # root filesystem; statvfs'd for free/total space

# A short, fixed budget for every probe subprocess — detection must never hang the
# wizard, and a slow/absent tool should just yield "unknown".
_CMD_TIMEOUT = 4.0


# ---------------------------------------------------------------------------
# Low-level seams (mockable): a never-raising command runner + a /proc reader.
# ---------------------------------------------------------------------------

def _run(cmd, timeout=_CMD_TIMEOUT):
    """Run ``cmd`` -> (rc, stdout, stderr). rc=127 if the binary is missing,
    rc=124 on timeout, rc=1 on any other OSError. Never raises."""
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout)
        return (p.returncode,
                p.stdout.decode("utf-8", "replace"),
                p.stderr.decode("utf-8", "replace"))
    except FileNotFoundError:
        return 127, "", f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except OSError as e:
        return 1, "", str(e)


def _read_text(path):
    """Read a small text file, or "" if it's missing/unreadable. Strips trailing
    NULs (/proc/device-tree/* are NUL-terminated)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().rstrip("\x00\n")
    except OSError:
        return ""


def _cpuinfo_field(name):
    """Pull a single ``Name : value`` field out of /proc/cpuinfo (e.g. Serial,
    Revision), or None if absent."""
    for line in _read_text(CPUINFO_PATH).splitlines():
        k, sep, v = line.partition(":")
        if sep and k.strip().lower() == name.lower():
            return v.strip() or None
    return None


# ---------------------------------------------------------------------------
# Pi identity.
# ---------------------------------------------------------------------------

def pi_model():
    """The Pi model string, e.g. 'Raspberry Pi 5 Model B Rev 1.0'. Falls back to a
    cpuinfo Revision hex, then None off-Pi."""
    model = _read_text(MODEL_PATH).strip()
    if model:
        return model
    rev = _cpuinfo_field("Revision")
    return f"rev {rev}" if rev else None


def pi_serial():
    """The Pi's CPU serial from /proc/cpuinfo, or None."""
    return _cpuinfo_field("Serial")


def os_name():
    """A human OS label: /etc/os-release PRETTY_NAME if present, else platform()."""
    for line in _read_text(OS_RELEASE_PATH).splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip().strip('"') or None
    try:
        return platform.platform()
    except Exception:  # noqa: BLE001 — platform should never fail, but never raise here
        return None


def hostname():
    """The short hostname (socket.gethostname, first label)."""
    try:
        return socket.gethostname().split(".")[0] or None
    except OSError:
        return None


def mdns_name():
    """The .local mDNS name the operator opens (e.g. 'raspberrypi.local')."""
    hn = hostname()
    return f"{hn}.local" if hn else None


# ---------------------------------------------------------------------------
# Network: SSID, LAN IP, interface + MAC.
# ---------------------------------------------------------------------------

def _ssid():
    """Current Wi-Fi SSID via iwgetid, falling back to nmcli, else None."""
    rc, out, _ = _run(["iwgetid", "-r"])
    if rc == 0 and out.strip():
        return out.strip()
    rc, out, _ = _run(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"])
    if rc == 0:
        for line in out.splitlines():
            active, sep, ssid = line.partition(":")
            if sep and active.strip() == "yes" and ssid.strip():
                return ssid.strip()
    return None


def _primary_ip():
    """Best-effort LAN IPv4. Prefers `hostname -I` (Pi), falls back to a routing
    UDP-socket trick that sends no packets (works on a Mac too)."""
    rc, out, _ = _run(["hostname", "-I"])
    if rc == 0:
        for tok in out.split():
            if "." in tok and not tok.startswith("127."):
                return tok
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no traffic; just picks the egress iface
        ip = s.getsockname()[0]
        return ip if ip and not ip.startswith("127.") else None
    except OSError:
        return None
    finally:
        s.close()


def _iface_and_mac(ip):
    """(iface, mac) for the interface owning ``ip``, parsed from `ip -j addr`.
    Returns (None, None) when `ip` is missing (e.g. on a Mac)."""
    if not ip:
        return None, None
    rc, out, _ = _run(["ip", "-j", "addr"])
    if rc != 0:
        return None, None
    try:
        for link in json.loads(out):
            for a in link.get("addr_info", []):
                if a.get("local") == ip:
                    return link.get("ifname"), link.get("address")
    except (ValueError, TypeError, AttributeError):
        pass
    return None, None


def _wifi_signal():
    """Wi-Fi signal as {dbm, quality_pct}, or None when not on Wi-Fi / off-Pi.

    Reads /proc/net/wireless first (no tools needed): its columns are
    ``iface: status link level noise …`` where *link* is the quality (out of ~70)
    and *level* is the RSSI in dBm. Falls back to nmcli's SIGNAL percentage."""
    for line in _read_text(WIRELESS_PATH).splitlines():
        iface, sep, rest = line.partition(":")
        if not sep:
            continue
        cols = rest.split()
        if len(cols) >= 3:
            try:
                link = float(cols[1].rstrip("."))
                dbm = int(float(cols[2].rstrip(".")))
            except ValueError:
                continue
            quality = max(0, min(100, round(link / 70 * 100)))
            return {"dbm": dbm, "quality_pct": quality}
    rc, out, _ = _run(["nmcli", "-t", "-f", "active,signal", "dev", "wifi"])
    if rc == 0:
        for line in out.splitlines():
            active, sep, sig = line.partition(":")
            if sep and active.strip() == "yes" and sig.strip().isdigit():
                return {"dbm": None, "quality_pct": int(sig.strip())}
    return None


def wifi():
    """Network snapshot: {ssid, ip, iface, mac, signal}. Any field is None if
    undetectable; ``signal`` is {dbm, quality_pct} or None (wired / off-Pi)."""
    ip = _primary_ip()
    iface, mac = _iface_and_mac(ip)
    return {"ssid": _ssid(), "ip": ip, "iface": iface, "mac": mac, "signal": _wifi_signal()}


# ---------------------------------------------------------------------------
# Resource health: disk, memory, CPU temperature, power (under-voltage/throttle).
# ---------------------------------------------------------------------------

def disk():
    """Root filesystem space as {total, free} bytes, or None if unreadable."""
    try:
        u = shutil.disk_usage(DISK_PATH)
    except OSError:
        return None
    return {"total": u.total, "free": u.free}


def memory():
    """RAM as {total, available} bytes from /proc/meminfo, or None off-Pi.
    /proc/meminfo reports kB; we normalise to bytes. ``available`` falls back to
    MemFree on older kernels that lack MemAvailable."""
    vals = {}
    for line in _read_text(MEMINFO_PATH).splitlines():
        k, sep, v = line.partition(":")
        if not sep:
            continue
        parts = v.split()
        if parts and parts[0].isdigit():
            vals[k.strip()] = int(parts[0]) * 1024
    total = vals.get("MemTotal")
    if total is None:
        return None
    return {"total": total, "available": vals.get("MemAvailable", vals.get("MemFree"))}


def memory_percent():
    """Calculate RAM usage percent, or simulated off-Pi."""
    m = memory()
    if m is None:
        import random
        return round(random.uniform(35.0, 45.0), 1)
    total = m.get("total", 0)
    available = m.get("available", 0)
    if total <= 0:
        return 0.0
    used = total - available
    return round((used / total) * 100.0, 1)


def read_cpu_times():
    """Read idle and total CPU times from /proc/stat. Returns (idle, total) or None off-Pi."""
    content = _read_text("/proc/stat")
    if not content:
        return None
    for line in content.splitlines():
        if line.startswith("cpu "):
            parts = line.split()
            try:
                values = [int(x) for x in parts[1:]]
                total = sum(values)
                # idle is the 4th value (index 3 in parts[1:])
                # iowait is the 5th value (index 4 in parts[1:])
                idle = values[3] + values[4] if len(values) > 4 else values[3]
                return idle, total
            except (ValueError, IndexError):
                return None
    return None


def cpu_percent(prev_idle=None, prev_total=None):
    """Compute CPU usage percent.
    If prev_idle and prev_total are provided, returns (cpu_pct, current_idle, current_total).
    If they are None and on-Pi, does a short sleep to measure, and returns (cpu_pct, current_idle, current_total).
    Off-Pi, returns (simulated_pct, None, None).
    """
    times = read_cpu_times()
    if times is None:
        import random
        return round(random.uniform(5.0, 15.0), 1), None, None

    idle, total = times
    if prev_idle is None or prev_total is None:
        import time
        time.sleep(0.1)
        next_times = read_cpu_times()
        if next_times is None:
            import random
            return round(random.uniform(5.0, 15.0), 1), None, None
        next_idle, next_total = next_times
        diff_idle = next_idle - idle
        diff_total = next_total - total
        if diff_total <= 0:
            pct = 0.0
        else:
            pct = round(100.0 * (1.0 - diff_idle / diff_total), 1)
        return pct, next_idle, next_total

    diff_idle = idle - prev_idle
    diff_total = total - prev_total
    if diff_total <= 0:
        pct = 0.0
    else:
        pct = round(100.0 * (1.0 - diff_idle / diff_total), 1)
    return pct, idle, total


def cpu_temp_c():
    """CPU temperature in °C (one decimal) from the thermal zone, or None off-Pi."""
    raw = _read_text(THERMAL_PATH).strip()
    try:
        return round(int(raw) / 1000.0, 1)
    except (ValueError, TypeError):
        return None


def power():
    """Pi power health from ``vcgencmd get_throttled``, or None when vcgencmd is
    absent (e.g. off-Pi). The returned flags are the under-voltage / throttling
    bits — the single best early-warning for a weak power supply or bad cable.

      under_voltage_now / throttled_now      — happening right now
      under_voltage_occurred / throttled_occurred — happened since boot
    """
    rc, out, _ = _run(["vcgencmd", "get_throttled"])
    if rc != 0:
        return None
    _, sep, hexval = out.strip().partition("=")
    if not sep:
        return None
    try:
        bits = int(hexval, 16)
    except ValueError:
        return None
    return {
        "under_voltage_now": bool(bits & 0x1),
        "throttled_now": bool(bits & 0x4),
        "under_voltage_occurred": bool(bits & 0x10000),
        "throttled_occurred": bool(bits & 0x40000),
    }


def health():
    """Pi resource snapshot for the wizard: {disk, memory, cpu_temp_c, power}.
    Every field is None when undetectable (e.g. on a Mac), so this never raises."""
    return {
        "disk": disk(),
        "memory": memory(),
        "cpu_temp_c": cpu_temp_c(),
        "power": power(),
    }


# ---------------------------------------------------------------------------
# Cameras: structured enumeration built on hardware/camera_probe.py primitives.
# ---------------------------------------------------------------------------

def _list_video_nodes():
    """Sorted /dev/video* nodes (own seam so tests can inject a fixed list)."""
    import glob
    return sorted(glob.glob("/dev/video*"))


def _csi_cameras(camera_probe):
    """Authoritative CSI ribbon cameras from `rpicam-hello --list-cameras` (one entry
    per camera the firmware reports, with the sensor model). This is the ONLY source
    of CSI cameras — the bcm2835-isp / unicam / rp1-cfe /dev/video* ISP-pipeline nodes
    are NOT separate cameras and must not be counted (that was the over-report)."""
    rc, sout, _ = camera_probe.run(["rpicam-hello", "--list-cameras"], timeout=_CMD_TIMEOUT)
    if rc == 127:
        rc, sout, _ = camera_probe.run(["libcamera-hello", "--list-cameras"], timeout=_CMD_TIMEOUT)
    if rc != 0:
        return []
    out = []
    # rpicam lists each camera as "<idx> : <sensor> [<res>] (<node>)".
    for line in sout.splitlines():
        m = re.match(r"^\s*(\d+)\s*:\s*(\S+)", line)
        if m:
            idx, sensor = m.group(1), m.group(2)
            out.append({"type": "camera_csi", "transport": {"kind": "csi", "index": int(idx)},
                        "label": f"CSI camera {sensor}", "found_by": "camera_probe"})
    # Detected but unparseable listing -> report a single CSI camera rather than none.
    if not out and ("available cameras" in sout.lower() or "imx" in sout.lower()):
        out.append({"type": "camera_csi", "transport": {"kind": "csi"},
                    "label": "CSI ribbon camera", "found_by": "camera_probe"})
    return out


def cameras():
    """Structured camera list for the wizard, reusing camera_probe's primitives
    (NON-printing). Each entry carries Pi-LOCAL transport detail (which stays in
    pi-garden.json, never on the edge): {type, transport:{kind,...}, label, found_by}.

    CSI cameras come from rpicam (authoritative); only genuine USB (uvcvideo) capture
    nodes are taken from /dev/video*, deduped by USB bus so one webcam = one entry —
    the CSI ISP-pipeline /dev/video* nodes are skipped. Degrades to [] off-Pi."""
    try:
        from hardware import camera_probe          # package mode
    except ImportError:                            # pragma: no cover - script mode
        import camera_probe

    out = _csi_cameras(camera_probe)

    seen_usb = set()
    for dev in _list_video_nodes():
        driver, bus, is_capture = camera_probe.v4l2_node_info(dev)
        if not is_capture:
            continue
        is_usb = ("usb" in (bus or "").lower()) or ((driver or "").lower() == "uvcvideo")
        if not is_usb:
            continue                               # CSI/ISP platform node -> covered by rpicam
        key = bus or dev                           # one physical webcam = one bus = one entry
        if key in seen_usb:
            continue
        seen_usb.add(key)
        out.append({
            "type": "camera_usb",
            "transport": {"kind": "usb", "dev": dev, "bus": bus or None, "driver": driver or None},
            "label": (f"{driver} " if driver else "") + f"USB camera @ {dev}",
            "found_by": "camera_probe",
        })
    return out


# ---------------------------------------------------------------------------
# Aggregate.
# ---------------------------------------------------------------------------

def detect():
    """The full first-run snapshot the wizard shows back. JSON-serializable; no
    secrets; never raises."""
    hn = hostname()
    return {
        "pi": {"model": pi_model(), "serial": pi_serial(), "hostname": hn, "os": os_name()},
        "network": wifi(),
        "health": health(),
        "hostname": hn,
        "mdns_name": mdns_name(),
        "cameras": cameras(),
    }


if __name__ == "__main__":
    print(json.dumps(detect(), indent=2))
