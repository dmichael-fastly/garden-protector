"""Shared pytest fixtures.

Speed: the production scrypt cost factor (n=2**14, ~16MB, memory-hard) is correct
for real passcode hashing but adds ~50-100ms per hash, and the portal/console
tests hash + verify passcodes dozens of times. Drop the cost factor for the whole
test session — the KDF roundtrip and the {algo,salt,hash} record shape are
identical at any n, so coverage of the hashing scheme is fully preserved; only the
work factor changes. Production code is untouched (these are pure monkeypatches).
"""
import pytest

from provision import auth
import hardware.telemetry as telemetry

# A small-but-real scrypt cost: still exercises the KDF end to end, ~instant.
_TEST_SCRYPT_N = 2 ** 4


@pytest.fixture(autouse=True)
def _cheap_passcode_kdf(monkeypatch):
    # The scrypt KDF now lives in ONE module (provision.auth); every tier hashes
    # through it, so a single patch lowers the cost everywhere (Phase 1).
    monkeypatch.setattr(auth, "_SCRYPT_N", _TEST_SCRYPT_N)


@pytest.fixture(autouse=True)
def _hermetic_admin_passcode_env(monkeypatch):
    # The console now resolves its passcode from GP_ADMIN_PASSCODE (env) before any
    # on-disk record (provision.console.resolve_auth). Clear it by default so a
    # developer who exports it locally can't shadow the file-based tests; a test that
    # wants the env source sets it explicitly via monkeypatch.setenv (same instance).
    monkeypatch.delenv("GP_ADMIN_PASSCODE", raising=False)


@pytest.fixture(autouse=True)
def _hermetic_telemetry_env(monkeypatch):
    # hardware/telemetry._ENABLED is read once at import time (telemetry.py:37), so
    # exporting GP_TELEMETRY=0 in the shell produces 6 false telemetry-test failures
    # (init/emit become no-ops, DB reads find nothing). Guard both sides:
    #   1. Remove GP_TELEMETRY from env so nothing else re-evaluates it as "0".
    #   2. Force _ENABLED=True so the already-imported module behaves as enabled.
    # A test that explicitly needs the disabled path sets telemetry._ENABLED = False
    # via its own monkeypatch (overrides this fixture's value within that test).
    monkeypatch.delenv("GP_TELEMETRY", raising=False)
    monkeypatch.setattr(telemetry, "_ENABLED", True)
