"""PI-002: test-only deps must NOT ship to the field device.

The unattended Pi installs ``hardware/requirements.txt`` into the service venv on
every boot/restart (deploy/install.sh + deploy/update.sh). Pure CI deps
(pytest/responses/hypothesis) belong in ``hardware/requirements-dev.txt`` (installed
by CI only) so the outdoor device doesn't carry — and re-resolve on every restart —
a test runner. These tests pin the split so a stray `pytest>=…` floor can't drift
back into the runtime file.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "hardware" / "requirements.txt"
DEV = ROOT / "hardware" / "requirements-dev.txt"

# Packages that are imported ONLY by tests/* — never by the running Pi code.
TEST_ONLY = ("pytest", "responses", "hypothesis")


def _requirement_names(path):
    """The set of normalized package names pinned in a requirements file (ignoring
    blank lines, comments, and version/marker specifiers)."""
    names = set()
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Strip the version/marker tail: name[==|>=|<=|~=|!=|<|>|;|[ ]…
        name = re.split(r"[<>=!~;\[ ]", line, 1)[0].strip().lower()
        if name:
            names.add(name)
    return names


def test_runtime_requirements_carry_no_test_only_deps():
    runtime = _requirement_names(RUNTIME)
    for pkg in TEST_ONLY:
        assert pkg not in runtime, (
            f"{pkg!r} is a test-only dep but is pinned in hardware/requirements.txt — "
            "it would ship to the field Pi (PI-002). Move it to requirements-dev.txt."
        )


def test_dev_requirements_carry_the_test_only_deps():
    dev = _requirement_names(DEV)
    for pkg in TEST_ONLY:
        assert pkg in dev, f"{pkg!r} missing from hardware/requirements-dev.txt"


def test_runtime_still_lists_the_core_runtime_dep():
    # Sanity: the runtime file isn't empty / didn't lose its actual runtime dep.
    assert "requests" in _requirement_names(RUNTIME)
