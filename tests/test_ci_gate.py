"""Pins the CI-001 fix: `make ci` compiles the wasm32 deploy artifact.

Native `cargo test` builds a host-ABI test harness; the real deploy artifact is
wasm32-wasip1 and host-ABI code is `#[cfg(target_arch="wasm32")]`-gated (main.rs),
so a wasm-only compile break passes native `cargo test` but breaks `make build`/deploy.
The fix added a `build-check` Makefile target (`cargo build --release --target
wasm32-wasip1`) and wired it into the `ci` prerequisite chain.

This test asserts the gate's STRUCTURE statically (the target exists, targets wasm,
and `ci` depends on it) — fast and deterministic. The actual wasm COMPILE is exercised
by running `make ci` itself (which these tests are part of), not duplicated here.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
MAKEFILE = (ROOT / "Makefile").read_text()


def _recipe_lines(target):
    """The indented recipe body for a Makefile `target:` (lines until the next rule)."""
    lines = MAKEFILE.splitlines()
    body, capturing = [], False
    for line in lines:
        if re.match(rf"^{re.escape(target)}:", line):
            capturing = True
            continue
        if capturing:
            # A recipe line is tab-indented; a new unindented token ends the recipe.
            if line.startswith("\t"):
                body.append(line)
            elif line.strip() == "":
                continue
            else:
                break
    return body


def _ci_prerequisites():
    """The prerequisite list on the `ci:` rule line (before any `##` doc comment)."""
    m = re.search(r"^ci:[ \t]*(.*)$", MAKEFILE, re.MULTILINE)
    assert m, "no `ci:` target found in Makefile"
    prereqs = m.group(1).split("##", 1)[0]  # strip the help comment
    return prereqs.split()


def test_build_check_target_exists_and_builds_wasm():
    body = _recipe_lines("build-check")
    assert body, "build-check target has no recipe"
    joined = "\n".join(body)
    assert "cargo build" in joined, "build-check must invoke cargo build"
    assert "--target wasm32-wasip1" in joined, (
        "build-check must compile the wasm32-wasip1 deploy artifact, not the native target"
    )
    assert "--release" in joined, "build-check should build the release artifact (matches deploy)"


def test_ci_depends_on_build_check():
    prereqs = _ci_prerequisites()
    assert "build-check" in prereqs, (
        f"`ci` must depend on build-check so the wasm artifact is compiled; prereqs were {prereqs}"
    )


def test_ci_still_runs_drift_gate_and_both_test_suites():
    # Don't let a refactor of the ci chain drop the other gates while adding build-check.
    prereqs = _ci_prerequisites()
    for required in ("gen-check", "test", "scan"):
        assert required in prereqs, f"`ci` lost its `{required}` prerequisite; prereqs were {prereqs}"


def test_build_check_is_declared_phony():
    # It must be .PHONY so `make` always re-runs it (no file named build-check to stat).
    phony = re.search(r"^\.PHONY:[ \t]*(.*)$", MAKEFILE, re.MULTILINE)
    assert phony and "build-check" in phony.group(1).split()
