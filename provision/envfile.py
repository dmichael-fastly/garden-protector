#!/usr/bin/env python3
"""provision/envfile.py — the ONE shared ``.env`` loader for both Python tiers.

A tiny stdlib ``KEY=VALUE`` reader (no python-dotenv dependency) that populates
``os.environ`` for keys not already set, so a real environment variable / CLI flag
always WINS over the file. On the Pi the ``.env`` holds the admin passcode and edge
config.

Lives in the ``provision`` package (the shared service library that ``hardware``
already imports) so the Pi portal (``hardware/portal.py``) and the admin console
(``provision/console.py``) load the SAME ``.env`` the SAME way — see CHARTER.md
"one shared Python service library". Both re-export it as ``<module>.load_env_file``
for existing callers/tests.
"""
import os


def load_env_file(path):
    """Best-effort ``KEY=VALUE`` loader. Skips blanks and ``#`` comments, strips a
    leading ``export``, strips surrounding quotes, and never overwrites an
    already-set environment variable. Returns the dict it applied (handy for tests).
    A missing/unreadable file is a no-op (returns ``{}``)."""
    applied = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return applied
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, val = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key and key not in os.environ:
            os.environ[key] = val
            applied[key] = val
    return applied
