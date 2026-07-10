# Garden Protector - Phase 2 node firmware, MicroPython on ESP32-C3.
#
# Phase-1 local radar->burst deterrent PLUS networking:
#   * on a trip, ask the Pi to snap a photo   (POST http://<pi>:8091/trigger)
#   * heartbeat the Pi so the node shows active (POST http://<pi>:8088/heartbeat)
#
# ROBUSTNESS (so the board never wedges and never sprays stuck-on):
#   * gate is FAIL-CLOSED: off at boot AND in a finally: on ANY exit incl. Ctrl-C
#   * EVERY socket has a timeout -> a dead Pi or dropped Wi-Fi can't hang the loop
#   * all network sends are best-effort: local radar->LED deterrent always runs
#
# Run live:           mpremote connect /dev/cu.usbmodem2101 run /tmp/node_phase2.py
# Install standalone: mpremote connect /dev/cu.usbmodem2101 fs cp /tmp/node_phase2.py :main.py

from machine import Pin
import network, socket, json, time

# ---- Wi-Fi (2.4GHz only) — fill these in on the board; do NOT commit real creds ----
SSID = "YOUR_WIFI_SSID"
PASSWORD = "YOUR_WIFI_PASSWORD"

# ---- Pi (raw IP; MicroPython can't resolve .local/mDNS) ----
PI_IP = "YOUR_PI_IP"
CAP_PORT = 8091  # Pi listener: /trigger (capture+alarm) AND /heartbeat (-> telemetry as this device)

# ---- pins ----
RADAR_PIN = 3
GATE_PIN = 8  # onboard LED (bench); 4 + MOSFET for the real pump
GATE_ACTIVE_LOW = True
BOOT_PIN = 9

# ---- timing ----
BURST_S = 2
MAX_BURST_S = 4  # HARD CAP - never spray longer
REFRACTORY_S = 6
HEARTBEAT_S = 30  # < the 150s node-down threshold, so we stay "active"
NET_TIMEOUT = 3  # seconds; caps EVERY socket op so nothing can hang
TZ_OFFSET_H = -6  # <-- your hours-from-UTC, for the log timestamps (adjust if wrong)

# ---- hardware ----
radar = Pin(RADAR_PIN, Pin.IN, Pin.PULL_DOWN)
gate = Pin(GATE_PIN, Pin.OUT)
boot = Pin(BOOT_PIN, Pin.IN, Pin.PULL_UP)


def gate_off():
    gate.value(1 if GATE_ACTIVE_LOW else 0)


def gate_on():
    gate.value(0 if GATE_ACTIVE_LOW else 1)


gate_off()  # FAIL-CLOSED at boot

wlan = network.WLAN(network.STA_IF)
wlan.active(True)

armed = True
trips = 0
bursts = 0
t_boot = time.ticks_ms()
ntp_ok = False


# ---- logging: [HH:MM:SS] <icon> message ----
def stamp():
    if ntp_ok:
        t = time.localtime(time.time() + TZ_OFFSET_H * 3600)
        return "%02d:%02d:%02d" % (t[3], t[4], t[5])
    return "+%ds" % (
        time.ticks_diff(time.ticks_ms(), t_boot) // 1000
    )  # boot-relative until NTP


def log(icon, msg):
    print("[%s] %s %s" % (stamp(), icon, msg))


def wifi_ok():
    """Best-effort connect; True if associated. Never blocks more than NET_TIMEOUT."""
    if wlan.isconnected():
        return True
    try:
        wlan.connect(SSID, PASSWORD)
    except Exception:
        pass
    for _ in range(int(NET_TIMEOUT / 0.2)):
        if wlan.isconnected():
            log("\U0001f4f6", "wifi up: " + wlan.ifconfig()[0])  # 📶
            return True
        time.sleep_ms(200)
    return False


def sync_time():
    """One-shot NTP sync so timestamps are real wall-clock (needs Wi-Fi)."""
    global ntp_ok
    if not wifi_ok():
        return
    try:
        import ntptime

        ntptime.settime()
        ntp_ok = True
        log("\U0001f551", "clock synced (NTP)")  # 🕑
    except Exception as e:
        log("⚠️", "NTP sync failed: %s" % e)  # ⚠️


def http_post(ip, port, path, body):
    """Minimal timed HTTP POST. Returns response bytes or None; never raises."""
    if not wifi_ok():
        log("\U0001f4f5", "wifi down, skipped " + path)  # 📵
        return None
    s = None
    try:
        s = socket.socket()
        s.settimeout(NET_TIMEOUT)  # <-- the key anti-hang guard
        s.connect(socket.getaddrinfo(ip, port)[0][-1])
        req = (
            "POST %s HTTP/1.0\r\nHost: %s\r\nContent-Type: application/json\r\n"
            "Content-Length: %d\r\n\r\n%s" % (path, ip, len(body), body)
        )
        s.send(req.encode())
        return s.recv(256)
    except Exception as e:
        log("⚠️", "POST %s failed: %s" % (path, e))  # ⚠️
        return None
    finally:
        if s:
            try:
                s.close()
            except Exception:
                pass


def trigger_capture():
    if http_post(PI_IP, CAP_PORT, "/trigger", "{}"):
        log("\U0001f4f8", "photo requested")  # 📸
    else:
        log("⚠️", "photo no ack")  # ⚠️


def send_heartbeat():
    beat = {
        "ts_ms": int(time.time()) * 1000,
        "battery_voltage": 4.12,
        "rssi": wlan.status("rssi") if wlan.isconnected() else 0,
        "uptime_s": time.ticks_diff(time.ticks_ms(), t_boot) // 1000,
        "temperature_c": 18.5,
        "humidity_pct": 64,
        "rainfall_mm": 0.0,
        "raining": False,
        "lux_level": 200.0,
    }
    if http_post(PI_IP, CAP_PORT, "/heartbeat", json.dumps(beat)):
        log("\U0001f493", "heartbeat ok")  # 💓
    else:
        log("\U0001f494", "heartbeat no ack")  # 💔


def cap_seconds(sec):
    return min(sec, MAX_BURST_S)


def burst(sec):
    global bursts
    sec = cap_seconds(sec)
    bursts += 1
    log(
        "\U0001f4a6", "burst #%d: gate ON %ds (cap %ds)" % (bursts, sec, MAX_BURST_S)
    )  # 💦
    gate_on()
    time.sleep(sec)
    gate_off()


def check_boot_toggle():
    global armed
    if boot.value() == 0:
        time.sleep_ms(30)
        if boot.value() == 0:
            armed = not armed
            if armed:
                log("\U0001f6e1️", "ARMED")  # 🛡️
            else:
                log("\U0001f6d1", "DISARMED (safe to weed)")  # 🛑
            while boot.value() == 0:
                time.sleep_ms(10)


def idle(ms):
    """Sleep in slices while still servicing the BOOT button + heartbeat schedule."""
    global next_hb
    t0 = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), t0) < ms:
        check_boot_toggle()
        if time.ticks_diff(time.ticks_ms(), next_hb) >= 0:
            send_heartbeat()
            next_hb = time.ticks_add(time.ticks_ms(), HEARTBEAT_S * 1000)
        time.sleep_ms(20)


log(
    "\U0001f331",
    "Phase-2 node up: radar=GPIO%d gate=GPIO%d pi=%s armed=%s"  # 🌱
    % (RADAR_PIN, GATE_PIN, PI_IP, armed),
)
print("       wave to trip · BOOT = arm/disarm · Ctrl-C = clean stop\n")
sync_time()  # real timestamps from here on
send_heartbeat()  # announce ourselves right away

last = 0
next_hb = time.ticks_add(time.ticks_ms(), HEARTBEAT_S * 1000)
try:
    while True:
        check_boot_toggle()
        if time.ticks_diff(time.ticks_ms(), next_hb) >= 0:
            send_heartbeat()
            next_hb = time.ticks_add(time.ticks_ms(), HEARTBEAT_S * 1000)

        level = radar.value()
        if level and not last:  # rising edge = a new motion trip
            trips += 1
            if armed:
                log("\U0001f6a8", "RADAR TRIP #%d -> spray + photo" % trips)  # 🚨
                burst(BURST_S)  # local deterrent FIRST (never network-gated)
                trigger_capture()  # then grab evidence (Pi buffer stays fresh)
                idle(REFRACTORY_S * 1000)  # stay blind after firing
                last = radar.value()
                continue
            log("\U0001f634", "RADAR TRIP #%d -> disarmed, held" % trips)  # 😴
        last = level
        time.sleep_ms(20)
except KeyboardInterrupt:
    log("⏹️", "stopped by Ctrl-C")  # ⏹️
finally:
    gate_off()  # <-- ALWAYS leave it off
    log("\U0001f512", "gate OFF (fail-closed). bye.")  # 🔒
