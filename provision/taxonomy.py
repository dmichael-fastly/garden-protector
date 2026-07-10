"""Device taxonomy — the `kind`/`type` model for the registry (RFC §1).

A garden's devices are not just cameras: a Raspberry Pi can host motion sensors,
heat/thermal sensors, contact switches, and other observers, plus the deterrents
it actuates. `kind` is a closed set (observer vs deterrent — the core split);
`type` is an OPEN taxonomy: known types get first-class names, but any
charset-valid custom type is accepted so "other things that plug into a Pi" work
without a code change. (Pi-side HAL drivers for non-camera observers are a Step 4
hardware concern; this is the identity/registry layer.)
"""

import re

KINDS = {"observer", "deterrent"}

# Known, first-class types (extend freely). Observers produce evidence/events;
# deterrents are actuated on/off.
OBSERVER_TYPES = {
    "camera_csi",
    "camera_usb",
    "camera_rtsp",
    "motion_pir",       # PIR motion sensor (e.g. HC-SR501)
    "heat_thermal",     # thermal / IR temperature sensor
    "ir_break_beam",    # IR break-beam tripwire
    "sound_mic",        # microphone / sound-level trigger
    "contact_switch",   # reed / contact switch
}
DETERRENT_TYPES = {
    "water_pump",
    "solenoid",
    "sound",
    "strobe",
    "shelly_switch",
    "relay_generic",
}

# A custom `type` must be lowercase snake/charset so it composes safely into
# logs, config, and (future) per-device KV keys.
_TYPE_RE = re.compile(r"[a-z0-9_]{1,32}")


def validate_device_type(kind: str, dev_type: str) -> bool:
    """Validate `(kind, type)`. `kind` must be observer|deterrent; `type` must be
    charset-valid. Returns True if `type` is a KNOWN type for the kind, False if
    it is an accepted-but-custom type. Raises ValueError on an invalid kind/type.
    """
    if kind not in KINDS:
        raise ValueError(f"invalid kind {kind!r}; must be one of {sorted(KINDS)}")
    if not isinstance(dev_type, str) or _TYPE_RE.fullmatch(dev_type) is None:
        raise ValueError(
            f"invalid device type {dev_type!r}; must match [a-z0-9_]{{1,32}}"
        )
    known = OBSERVER_TYPES if kind == "observer" else DETERRENT_TYPES
    return dev_type in known
