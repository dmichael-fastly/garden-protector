"""PI-001: the brute-force lockout must bucket by /64 for IPv6, not the raw address.

The Pi portal binds dual-stack on ``::``; a LAN attacker holding a standard SLAAC
/64 can rotate through 2**64 source addresses. Keying the lockout on the exact
address (the old behavior) hands each one a fresh failure budget, defeating the
passcode throttle. ``provision.auth.rate_limit_key`` collapses IPv6 to its /64 while
leaving IPv4 (and the IPv4-mapped form a dual-stack socket reports) per-host, so the
throttle holds. These tests pin that property end to end through ``RateLimiter`` and
keep IPv4 + the console call sites' per-host semantics intact (no regression).
"""
import ipaddress

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from provision import auth


# ---------------------------------------------------------------------------
# rate_limit_key (pure) — IPv6 -> /64, IPv4 -> /32-host
# ---------------------------------------------------------------------------

def test_rate_limit_key_collapses_ipv6_to_64():
    # Two distinct addresses in the SAME /64 map to ONE key.
    a = auth.rate_limit_key("2001:db8:abcd:1234::1")
    b = auth.rate_limit_key("2001:db8:abcd:1234:ffff:ffff:ffff:ffff")
    assert a == b == "2001:db8:abcd:1234::/64"


def test_rate_limit_key_separates_distinct_64s():
    a = auth.rate_limit_key("2001:db8:abcd:1234::1")
    b = auth.rate_limit_key("2001:db8:abcd:9999::1")  # different /64
    assert a != b


def test_rate_limit_key_ipv4_is_per_host():
    # IPv4 stays per-host (the original semantics the console relies on).
    assert auth.rate_limit_key("10.0.0.5") == "10.0.0.5"
    assert auth.rate_limit_key("10.0.0.5") != auth.rate_limit_key("10.0.0.6")


def test_rate_limit_key_ipv4_mapped_stays_per_host():
    # A dual-stack socket reports IPv4 clients as ::ffff:a.b.c.d — these must NOT be
    # collapsed to a /64 (that would lump unrelated IPv4 hosts together); they unwrap
    # to the real per-host IPv4 key, matching the bare-IPv4 form.
    assert auth.rate_limit_key("::ffff:10.0.0.5") == "10.0.0.5"
    assert auth.rate_limit_key("::ffff:10.0.0.5") != auth.rate_limit_key("::ffff:10.0.0.6")


def test_rate_limit_key_unparseable_is_stable_fallback():
    # A malformed address still gets ONE stable bucket (not a limiter bypass).
    assert auth.rate_limit_key("not-an-ip") == "not-an-ip"
    assert auth.rate_limit_key("") == ""


# ---------------------------------------------------------------------------
# RateLimiter through rate_limit_key — the throttle actually holds per /64
# ---------------------------------------------------------------------------

def _ipv6_in_64(prefix, host):
    """An address in `prefix`::/64 with the given low-64-bit host suffix."""
    net = ipaddress.ip_network(prefix + "::/64", strict=False)
    return str(net.network_address + host)


def test_ipv6_64_shares_one_lockout_budget():
    # max_fails=5; an attacker rotating IPv6 source addresses within one /64 must NOT
    # get a fresh budget per address. Five fails across FIVE distinct addresses in one
    # /64 trip the lockout for a sixth (different) address in that same /64.
    rl = auth.RateLimiter(max_fails=5, window_s=60, lockout_s=300)
    prefix = "2001:db8:abcd:1234"
    for i in range(5):
        addr = _ipv6_in_64(prefix, i + 1)
        assert rl.allowed(auth.rate_limit_key(addr), now=1000)[0] is True
        rl.record_failure(auth.rate_limit_key(addr), now=1000)
    # A SIXTH, never-before-seen address in the same /64 is already locked out.
    sixth = _ipv6_in_64(prefix, 999)
    allowed, retry = rl.allowed(auth.rate_limit_key(sixth), now=1000)
    assert allowed is False and retry > 0


def test_distinct_64s_have_independent_budgets():
    # Two different /64s (and an IPv4 host) keep separate budgets — the throttle must
    # not over-block legitimate distinct LAN links.
    rl = auth.RateLimiter(max_fails=3, window_s=60, lockout_s=300)
    a64 = auth.rate_limit_key("2001:db8:aaaa:1::1")
    b64 = auth.rate_limit_key("2001:db8:bbbb:2::1")
    v4 = auth.rate_limit_key("10.0.0.5")
    for _ in range(3):
        rl.record_failure(a64, now=1000)
    assert rl.allowed(a64, now=1000)[0] is False     # /64 A is locked
    assert rl.allowed(b64, now=1000)[0] is True       # a different /64 is unaffected
    assert rl.allowed(v4, now=1000)[0] is True        # IPv4 host is unaffected


def test_ipv4_hosts_keep_independent_budgets():
    # PI-001 must not regress IPv4: two IPv4 hosts each get their own budget.
    rl = auth.RateLimiter(max_fails=2, window_s=60, lockout_s=300)
    for _ in range(2):
        rl.record_failure(auth.rate_limit_key("10.0.0.5"), now=1000)
    assert rl.allowed(auth.rate_limit_key("10.0.0.5"), now=1000)[0] is False
    assert rl.allowed(auth.rate_limit_key("10.0.0.6"), now=1000)[0] is True


# ---------------------------------------------------------------------------
# Property: ANY two addresses in one /64 collide; cross-/64 never does
# ---------------------------------------------------------------------------

_PREFIX = "2001:db8:abcd:1234"


@settings(max_examples=200)
@given(st.integers(min_value=0, max_value=2**64 - 1),
       st.integers(min_value=0, max_value=2**64 - 1))
def test_property_same_64_always_shares_budget(host_a, host_b):
    # For arbitrary host suffixes, two addresses in the SAME /64 always key alike.
    a = auth.rate_limit_key(_ipv6_in_64(_PREFIX, host_a))
    b = auth.rate_limit_key(_ipv6_in_64(_PREFIX, host_b))
    assert a == b == _PREFIX + "::/64"


@settings(max_examples=200)
@given(st.integers(min_value=0, max_value=2**16 - 1),
       st.integers(min_value=0, max_value=2**64 - 1),
       st.integers(min_value=0, max_value=2**64 - 1))
def test_property_distinct_64s_never_collide(seg, host_a, host_b):
    # Two DIFFERENT /64s never share a bucket, regardless of host suffix. Vary the
    # 4th 16-bit segment (the last one inside the /64 boundary): pin one side to a
    # fixed segment and force the other to a guaranteed-different one.
    fixed = 0xABCD
    other = seg if seg != fixed else (fixed ^ 0x1)  # always != fixed
    a = auth.rate_limit_key(_ipv6_in_64(f"2001:db8:abcd:{fixed:x}", host_a))
    b = auth.rate_limit_key(_ipv6_in_64(f"2001:db8:abcd:{other:x}", host_b))
    assert a != b
