# Firmware — ESP32-C3 garden node (MicroPython)

`node_c3.py` is the MicroPython firmware for the **ESP32-C3 SuperMini radar node**
(register your device id via `provision.cli register-device`). It is **not** CPython — it imports `machine` /
`network` and only runs on the board, so it is excluded from the Python test suite.

**What it does (Phase 2):**
- Reads an **RCWL-0516 radar** on `GPIO3`; on a motion trip it drives a fail-closed
  "spray" gate (`GPIO8` onboard LED on the bench; `GPIO4` + MOSFET for the real pump)
  with a hard 4-second burst cap + refractory (ported from `hardware/node_sim.py`).
- POSTs a **capture trigger** to the Pi listener (`hardware/cap_listener.py`), which
  grabs camera frames and raises an alarm.
- **Heartbeats** the Pi listener so the node shows active in the dashboard.
- Robust: clean `Ctrl-C`/exception shutdown (gate always off), timeouts on every
  socket, network-independent local deterrent.

**Flashing (MicroPython v1.28 already on the board):**
```
mpremote connect /dev/cu.usbmodem2101 fs cp firmware/node_c3.py :main.py
```
Fill in the real Wi-Fi SSID/password on the board first (the committed copy has
placeholders — never commit real credentials). The garden token is NOT in the
firmware; it lives on the Pi (`configs/secrets.json`) and the listener uses it.

See memory `esp32-c3-first-light` for the full bring-up + wiring notes.
