# Phase Plan: Raspberry Pi Hardware & Remote Access Setup

This document provides explicit instructions for setting up the Raspberry Pi client, network configuration, and how you can grant me secure, direct access through your terminal to configure, install, and test the software on the Pi.

---

## 1. Physical Hardware & OS Preparation

### Hardware Prerequisites:
* **Raspberry Pi:** Pi 3, 4, or 5 (minimum 1GB RAM, though more is better).
* **Camera Module:** Raspberry Pi Camera V2 or V3 (or a standard USB webcam).
* **Motion Detector:** HC-SR501 PIR sensor (or equivalent sensor module).
* **Actuators:** 5V Relay board (to switch sprinklers/strobes), passive buzzer, or physical LEDs.
* **MicroSD Card:** Minimum 16GB (Class 10 recommended).

### Initial OS Imaging (Using Raspberry Pi Imager):
1. Download and open **Raspberry Pi Imager** on your Mac.
2. Select **Raspberry Pi OS Lite (64-bit)** (no desktop GUI is needed; lite is fast and clean).
3. Click the gear icon to configure **Advanced Options**:
   - **Enable SSH:** Choose "Use password authentication" or paste your Mac's SSH public key (`~/.ssh/id_rsa.pub`).
   - **Set username and password:** e.g. `pi` or a custom user.
   - **Configure wireless LAN:** Enter your Wi-Fi SSID and password.
   - **Set locale settings:** Match your current timezone and keyboard layout.
4. Flash the image onto your MicroSD card and insert it into the Pi.

---

## 2. Secure Agent Access Protocol (Host Mac to Pi)

Because I run commands directly on your host Mac's terminal (with your approval), I can easily manage the Pi over your local network using standard SSH commands.

To give me seamless access, we will set up **passwordless SSH key auth** between your Mac and the Pi:

```mermaid
graph LR
    Agent[AI Agent] --> HostMac[Your Host Mac]
    HostMac -- SSH via Local Network --> Pi[Raspberry Pi]
```

### Steps to Authorize Me:
1. Turn on the Pi and wait 1–2 minutes for it to boot and connect to your Wi-Fi.
2. Find the Pi's IP address by running this command in your Mac terminal (or I can run it with your approval):
   ```bash
   arp -a | grep -i "raspberry"
   # or ping the default local hostname:
   ping -c 3 raspberrypi.local
   ```
3. Copy your Mac's SSH key to the Pi (replace `192.168.1.XX` with your Pi's actual IP):
   ```bash
   ssh-copy-id pi@192.168.1.XX
   ```
4. Confirm SSH access without a password:
   ```bash
   ssh pi@192.168.1.XX "uname -a"
   ```

Once passwordless SSH is configured, **I can handle the rest**. Simply tell me the Pi's local IP address or local hostname, and I can remotely format, configure GPIO, activate the camera, and test our controller code using standard remote commands.

---

## 3. Remote Pi Setup Checklist (To be executed by Agent)

Once connected, I will execute a series of remote setup steps on your Pi:

1. **System Diagnostics:** Confirm camera attachment and GPIO availability.
   ```bash
   # CSI ribbon camera (libcamera) — Bookworm uses the rpicam-* tools:
   rpicam-hello --list-cameras
   # USB webcam (V4L2) — list nodes, then confirm the real capture node:
   v4l2-ctl --list-devices
   v4l2-ctl -d /dev/video0 --list-formats-ext   # repeat per node; capture node lists YUYV/MJPG
   ```
   Then run the repo's probe to identify both cameras and capture a sample from each:
   ```bash
   python3 hardware/camera_probe.py
   ```
2. **Environment Bootstrap:** Create a Python virtual environment on the Pi. Use
   `--system-site-packages` so the apt-installed `picamera2`/`libcamera` bindings
   remain importable inside the venv:
   ```bash
   sudo apt-get update && sudo apt-get install -y python3-pip python3-venv \
       python3-picamera2 v4l-utils fswebcam python3-opencv
   python3 -m venv --system-site-packages ~/garden-env
   source ~/garden-env/bin/activate
   pip install --upgrade pip
   pip install requests gpiozero zeroconf
   ```
   *(`zeroconf` powers the setup wizard's mDNS device discovery + the simulator's
   `_gpnode._tcp` advert; it's pure-Python and pip-installable.)*
   *(CSI camera → `picamera2`/`rpicam-*`; USB camera → OpenCV/`fswebcam`. The legacy
   `picamera` package and `libcamera-*` command names do not exist on Bookworm.)*
   *(Camera access: `deploy/install.sh` installs a udev rule giving the `video` group
   access to `/dev/dma_heap/*` and the service user must be in `video` — otherwise
   libcamera/`rpicam` fail with "Could not open any dmaHeap device" and the wizard
   can't see the CSI camera.)*
3. **Daemon Configuration:** Set up the client application as a systemd service (`garden-protector.service`) so it runs automatically in the background when the Pi boots.
