# 🔌 Garden Protector — Hardware Reference Manual (Pi HAL · weatherproofing)

> **Reference manual, not the build.** The build, wiring, and full bill of materials live in
> **[hardware-architecture.md](hardware-architecture.md)**. This file holds the deep reference
> material that doc points back to: the **Pi-side HAL reference** (camera identification +
> relay polarity, for the `hardware/client.py` code the smoke test still exercises), **IR night
> operations**, and **outdoor weatherproofing**.

---

## 1. Pi-side HAL reference

> **Reference notes for `hardware/client.py`.** The garden runs an ESP32 node with radar as the
> sensor (see [hardware-architecture.md](hardware-architecture.md)); these notes are kept only
> because the Pi-side hardware-abstraction layer in `hardware/client.py` (camera capture + the
> GPIO relay deterrent) is still exercised by the smoke test and remains available for an
> optional Pi-attached deployment. They document **how that code expects the hardware to
> behave**: camera device-node identification, and fail-closed relay polarity.

### Camera software stack & identification (Raspberry Pi OS Bookworm)

The two cameras use **different software paths** — don't treat them the same:

* **CSI ribbon camera → libcamera.** Use the `rpicam-*` CLI (`rpicam-hello`,
  `rpicam-jpeg`) or `picamera2` in Python. List it with
  `rpicam-hello --list-cameras`. *(On Bookworm the old `libcamera-*` command names
  were removed; legacy `picamera` does not apply.)*
* **USB webcam → V4L2.** List devices with `v4l2-ctl --list-devices`, then confirm
  the real **capture** node with `v4l2-ctl -d /dev/videoN --list-formats-ext`.
  Open it in OpenCV by **path**: `cv2.VideoCapture("/dev/videoN", cv2.CAP_V4L2)`.

> ⚠️ **Device-node gotcha:** a CSI camera *also* registers `/dev/video*` pipeline
> nodes, and one USB webcam usually exposes both a capture node **and** a metadata
> node (which yields no frames). So `/dev/video1` is **not** guaranteed to be the
> USB camera, and `cv2.VideoCapture(1)` may open the wrong/metadata node. Run
> `python3 hardware/camera_probe.py` to resolve the correct node before wiring up
> the client. Also note V4L2 streaming is single-owner — you can't run a motion
> stream and a still-capture on the **same** node simultaneously.

### Relay polarity (fail-closed wiring)

A **low-level-trigger** relay board energizes its coil when the GPIO is driven
**LOW** and releases when driven **HIGH** — the opposite of a naive `OutputDevice(pin)`
default. For genuine fail-closed behavior the software must drive the pin to its
de-energized (HIGH) state at boot and on disarm: construct relays as
`OutputDevice(pin, active_high=False, initial_value=False)`. **Validate polarity on
the bench (LED/multimeter, deterrents disconnected) before connecting a real
sprinkler or strobe** — getting this backwards floods the garden / leaves the
strobe on exactly when the system thinks it has failed safe.

---

## 2. Optional upgrades — stealth night operations

Standard white spotlights scare animals but can disturb neighbors or draw unwanted attention to your yard. For a professional, stealthy "security guard" setup, consider these infrared (IR) alternatives:

* **Camera Upgrade: Raspberry Pi NoIR Camera Module V3**
  * *What it is:* A specialized Pi Camera with the internal Infrared Cut Filter removed. During the day, colors will appear slightly purple, but at night it can see near-infrared light perfectly.
* **Actuator Upgrade: 850nm Infrared LED Floodlight (12V)**
  * *What it is:* An array of high-intensity IR LEDs. The light is completely invisible to the human eye (and most animals), but when switched on by the relay, it illuminates the garden like daylight for the NoIR camera.
  * *Benefit:* Stealthily captures evidence and classifies pests in complete darkness without bright white strobes.

---

## 3. Outdoor weatherproofing & environmental protection

Raspberry Pis run hot and are sensitive to humidity, extreme heat, and freezing temperatures. Protect your outdoor deployment with these materials:

### A. The Enclosure
* Use an **IP66 or IP67 rated Waterproof ABS Plastic Junction Box** (approx. 200mm x 150mm x 100mm). This provides a completely sealed environment that blocks dust, heavy rain, and insects.

### B. Ingress Protection (IP68 Cable Glands)
* To run the USB-C power cable and wires for the external PIR sensor and relays out of the sealed box, drill holes and install **Nylon Compression Cable Glands (IP68)**.
* Tightening the gland compresses a rubber bushing around the wire, forming a perfectly watertight seal.

### C. Defeating Condensation (The Silent Killer)
* **The Problem:** Even inside a completely sealed plastic box, temperature swings from day to night will cause humidity in the air to condense into liquid water droplets on the cold Pi circuit board, causing corrosion and short circuits.
* **The Cure:** Place two 10-gram reusable **Silica Gel Desiccant Packets** inside the junction box. These packets absorb ambient humidity, keeping the air inside the enclosure bone-dry. Replace or reactivate them once per season.

### D. Power Supply
* Run an outdoor-rated, waterproof **12V power supply** to the box, and use a step-down buck converter to provide stable **5V/3A (USB-C)** power to the Pi. Alternatively, use a high-quality outdoor extension cord connected to a waterproof USB-C AC block.
