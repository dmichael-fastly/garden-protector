#!/usr/bin/env python3
"""provision/auth.py — the ONE shared LAN-admin auth library for both Python tiers.

Single source of truth for: the scrypt passcode KDF + ``{algo,salt,hash}`` auth
record, the LAN-address gate, the in-memory ``SessionStore``, and the per-IP
brute-force ``RateLimiter`` (pure ``rate_decision`` core + a thread-safe shell).

Imported by the Pi portal (``hardware/portal.py``), the admin console
(``provision/console.py``), and the Pi config store (``hardware/pi_config.py``).
Before this module those three carried near-verbatim copies of the KDF, two
different ``RateLimiter`` classes, and two copies of ``is_lan_addr`` — see
CHARTER.md "one shared Python service library".

Pure cores take an injected clock so expiry/lockout stay unit-testable. Nothing
here does network or file I/O (callers own persistence).
"""
import hashlib
import hmac
import ipaddress
import secrets
import threading
import time

# ---------------------------------------------------------------------------
# scrypt passcode KDF + {algo,salt,hash} record
# ---------------------------------------------------------------------------

# scrypt cost factor. The production value is intentionally memory-hard (~16MB,
# ~50-100ms/hash). The test suite lowers it by monkeypatching THIS module's
# ``_SCRYPT_N`` in one place (tests/conftest.py) — every caller hashes through the
# functions below, which read this global at call time, so the patch is global.
_SCRYPT_N = 2 ** 14


def hash_passcode(passcode, salt):
    """scrypt KDF (stdlib, memory-hard). Deterministic given the salt, so the same
    function both creates and verifies a record. Reads ``_SCRYPT_N`` at call time."""
    return hashlib.scrypt(passcode.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=8, p=1,
                          dklen=32, maxmem=128 * 1024 * 1024)


def make_auth_record(passcode):
    """Build the ``{algo,salt,hash}`` record persisted at rest (console-auth.json on
    the console; ``admin_passcode_hash`` in the Pi's secrets.json)."""
    salt = secrets.token_bytes(16)
    return {"algo": "scrypt", "salt": salt.hex(), "hash": hash_passcode(passcode, salt).hex()}


def verify_passcode(passcode, rec):
    """Constant-time verify of a passcode against a stored ``{algo,salt,hash}``
    record. Any malformed record / non-scrypt algo / bad hex -> False."""
    if not isinstance(rec, dict) or rec.get("algo") != "scrypt" or passcode is None:
        return False
    try:
        salt = bytes.fromhex(rec["salt"])
        expected = bytes.fromhex(rec["hash"])
    except (KeyError, ValueError, TypeError):
        return False
    return hmac.compare_digest(hash_passcode(passcode, salt), expected)


def check_passcode(provided, expected):
    """Constant-time compare of a submitted passcode against an expected PLAINTEXT
    passcode (e.g. one supplied via the ``GP_ADMIN_PASSCODE`` env var / .env, which
    isn't pre-hashed). Empty ``expected`` never matches, so an unconfigured passcode
    can't be satisfied by an empty submission. Mirrors the hashed-record verifier
    above for the plaintext source."""
    if not expected or provided is None:
        return False
    return hmac.compare_digest(str(provided), str(expected))


# ---------------------------------------------------------------------------
# LAN gate
# ---------------------------------------------------------------------------

def _as_lan_ip(ip):
    """Parse ``ip`` to an ``ip_address``, unwrapping the IPv4-mapped IPv6 form
    (``::ffff:10.0.0.5``) a dual-stack listener reports for IPv4 clients so the LAN
    check sees the real IPv4. Returns None if ``ip`` doesn't parse."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        return addr.ipv4_mapped
    return addr


def is_lan_addr(ip, server_ip=None):
    """True iff ``ip`` is on the house LAN: loopback, private (RFC1918 / IPv6 ULA),
    or link-local. Any globally-routable address is normally rejected — the admin
    surfaces must never be manageable from the public internet, only from the LAN.

    IPv6 caveat: home networks routinely number every host with a *globally-routable*
    address (a GUA) out of the ISP-delegated prefix — those aren't "private", yet the
    client genuinely is on the LAN. So when ``server_ip`` (the local address THIS
    connection arrived on) is itself a global IPv6, a global-IPv6 client that shares
    its ``/64`` is accepted: same /64 == same link == the LAN. A spoofed off-LAN
    source can't complete the TCP handshake and is dropped by ISP ingress filtering,
    so this stays LAN-only. Without ``server_ip`` a global IPv6 is rejected (fail
    closed), preserving the original single-argument behavior for IPv4-only callers."""
    addr = _as_lan_ip(ip)
    if addr is None:
        return False
    if addr.is_loopback or addr.is_private or addr.is_link_local:
        return True
    # Global IPv6: accept only if it shares the /64 of the server's own global IPv6.
    if isinstance(addr, ipaddress.IPv6Address) and server_ip is not None:
        saddr = _as_lan_ip(server_ip)
        if (isinstance(saddr, ipaddress.IPv6Address)
                and not (saddr.is_loopback or saddr.is_private or saddr.is_link_local)):
            return addr.packed[:8] == saddr.packed[:8]
    return False


def rate_limit_key(ip):
    """Collapse a client address into the key the brute-force ``RateLimiter`` should
    bucket it under, so one attacker can't get a fresh failure budget per address.

    IPv6 hosts on a LAN routinely hold an ENTIRE /64 (the standard SLAAC prefix), so
    an attacker behind a dual-stack listener can rotate through 2**64 source addresses
    — keying the lockout on the exact address (the old behavior) defeats the throttle.
    We therefore bucket any IPv6 address by its /64 network. IPv4 (and the IPv4-mapped
    form a dual-stack socket reports, ``::ffff:a.b.c.d``) stays per-host /32, matching
    the original semantics for IPv4 clients. Reuses ``_as_lan_ip`` so the unwrap of the
    mapped form is the same one ``is_lan_addr`` uses.

    Unparseable input falls back to the raw string so a malformed address still gets a
    (single) stable bucket rather than slipping the limiter entirely."""
    addr = _as_lan_ip(ip)
    if addr is None:
        return ip
    if isinstance(addr, ipaddress.IPv6Address):
        # /64 network as the bucket: same link == same budget.
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


# ---------------------------------------------------------------------------
# In-memory sessions
# ---------------------------------------------------------------------------

SESSION_TTL_SECONDS = 12 * 3600  # 12h sessions (cleared on restart)


class SessionStore:
    """In-memory session tokens with a TTL. Cleared on restart (a restart =
    everyone re-authenticates), which is the right trade-off for a home admin tool.
    The clock is injectable so expiry is unit-testable."""

    def __init__(self, ttl=SESSION_TTL_SECONDS, clock=time.time):
        self._sessions = {}
        self._ttl = ttl
        self._clock = clock

    def mint(self):
        tok = secrets.token_urlsafe(32)
        self._sessions[tok] = self._clock() + self._ttl
        return tok

    def valid(self, tok):
        if not tok:
            return False
        exp = self._sessions.get(tok)
        if exp is None:
            return False
        if self._clock() >= exp:
            self._sessions.pop(tok, None)
            return False
        return True

    def drop(self, tok):
        self._sessions.pop(tok, None)


# ---------------------------------------------------------------------------
# Brute-force rate limiter — one pure decision core + a thread-safe shell with
# two equivalent method facades so both tiers keep their existing call sites.
# ---------------------------------------------------------------------------

def rate_decision(history, now, max_fails, window_s, lockout_s):
    """Pure brute-force decision. ``history`` is the list of this IP's failed-login
    epoch timestamps. If at least ``max_fails`` failures fall within the trailing
    ``window_s``, the IP is locked for ``lockout_s`` measured from its LAST failure.

    Returns ``(allowed, retry_after_secs)``; ``retry_after`` is 0 when allowed."""
    if not history:
        return True, 0
    recent = [t for t in history if 0 <= now - t <= window_s]
    if len(recent) >= max_fails:
        last = max(history)
        retry = int(last + lockout_s - now)
        if retry > 0:
            return False, retry
    return True, 0


class RateLimiter:
    """Per-IP failed-login lockout over the pure ``rate_decision`` core (thread-safe;
    mirrors the VCL penaltybox guarding the CDN read service).

    Two equivalent method facades over ONE algorithm so neither tier's call sites
    change:
      * portal-style — ``allowed(ip, now)`` / ``record_failure(ip, now)`` /
        ``record_success(ip)``, with an explicit ``now``.
      * console-style — ``locked(ip)`` / ``record_fail(ip)`` / ``reset(ip)``, using
        the injected ``clock``.
    A single ``window=`` kwarg (console style) sets ``window_s == lockout_s``."""

    def __init__(self, max_fails=5, window_s=60, lockout_s=300, *, window=None, clock=time.time):
        if window is not None:
            window_s = window
            lockout_s = window
        self.max_fails = max_fails
        self.window_s = window_s
        self.lockout_s = lockout_s
        self._fails = {}
        self._lock = threading.Lock()
        self._clock = clock

    # -- portal-style API (explicit now) ------------------------------------
    def allowed(self, ip, now):
        with self._lock:
            return rate_decision(self._fails.get(ip, []), now,
                                 self.max_fails, self.window_s, self.lockout_s)

    def record_failure(self, ip, now):
        horizon = max(self.window_s, self.lockout_s)
        with self._lock:
            hist = [t for t in self._fails.get(ip, []) if now - t <= horizon]
            hist.append(now)
            self._fails[ip] = hist

    def record_success(self, ip):
        with self._lock:
            self._fails.pop(ip, None)

    # -- console-style API (injected clock) ---------------------------------
    def locked(self, ip):
        allowed, _ = self.allowed(ip, self._clock())
        return not allowed

    def record_fail(self, ip):
        self.record_failure(ip, self._clock())

    def reset(self, ip):
        self.record_success(ip)
