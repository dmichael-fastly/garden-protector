"""Pins the CI-004 and CI-005 fixes in contract/gen.py.

CI-004 — threshold float rendering can't silently drift between languages. The
generator emits MITIGATE_THRESHOLD as a deterministic decimal: a Rust `f32` literal
(`fmt_f32_rust`) and a Python literal (`fmt_float_py`). These tests assert the
CHECKED-IN generated literals parse and equal the spec value (within f32 tolerance),
so a future threshold change that one renderer mangles is caught here rather than at
deploy. The cross-language conformance test (test_contract.py) only asserts the
generated Python const equals the spec; it does NOT parse the Rust literal — that
gap is what this file closes.

CI-005 — gen.py validates the spec BEFORE rendering and fails with a clear message
(SystemExit) instead of a raw KeyError/TypeError traceback or silently rendering a
duplicate nav id. These tests feed malformed spec dicts to validate_spec() and assert
the human-readable rejection. They build the malformed spec from an in-memory deepcopy
of the parsed spec — contract/spec.toml on disk is NEVER mutated.

gen.py is a script (contract/ is not a package), so it is loaded by file path via
importlib; that keeps these tests independent of sys.path / cwd.
"""
import copy
import importlib.util
import pathlib
import re
import tomllib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC_PATH = ROOT / "contract" / "spec.toml"
RS_OUT = ROOT / "backend" / "src" / "contract_gen.rs"


def _load_gen():
    """Import contract/gen.py by path (it is a script, not an installed package)."""
    spec = importlib.util.spec_from_file_location("contract_gen_script", ROOT / "contract" / "gen.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GEN = _load_gen()
SPEC = tomllib.loads(SPEC_PATH.read_text())

# f32 has ~7 significant decimal digits; a threshold compared at 1e-6 is well inside that.
_F32_TOL = 1e-6


def _rust_threshold_literal():
    """Pull the raw RHS of the generated `pub const MITIGATE_THRESHOLD: f32 = <lit>;`."""
    text = RS_OUT.read_text()
    m = re.search(r"pub const MITIGATE_THRESHOLD: f32 = (.+?);", text)
    assert m, "MITIGATE_THRESHOLD const not found in generated contract_gen.rs"
    return m.group(1).strip()


def _parse_rust_f32_literal(lit):
    """Parse a Rust f32 literal back to a Python float.

    Rust literals permit `_` digit separators and a trailing `_f32` / `f32` type suffix
    (the generator emits e.g. `0.3_f32`); strip both so float() can read the value.
    """
    body = re.sub(r"_?f32$", "", lit)  # drop type suffix (with or without leading '_')
    body = body.replace("_", "")        # drop digit separators
    return float(body)


# ---------------------------------------------------------------------------
# CI-004: the generated threshold literals equal the spec value in BOTH languages
# ---------------------------------------------------------------------------

def test_generated_rust_threshold_literal_parses_and_matches_spec():
    spec_value = float(SPEC["thresholds"]["mitigate_threshold"])
    lit = _rust_threshold_literal()
    # The generator must emit an explicit f32-typed literal (not a bare 0.3 the compiler
    # would infer as f64 in a different context, and not str()'s scientific notation).
    assert lit.endswith("f32"), f"Rust threshold literal must carry an f32 suffix, got {lit!r}"
    parsed = _parse_rust_f32_literal(lit)
    assert abs(parsed - spec_value) < _F32_TOL, (
        f"Rust MITIGATE_THRESHOLD literal {lit!r} -> {parsed} drifted from spec {spec_value}"
    )


def test_generated_python_threshold_const_matches_spec():
    # Import the generated Python module and compare its const to the spec value. The
    # rendered Python literal must round-trip exactly to the spec float.
    from provision import contract_gen as cg

    spec_value = float(SPEC["thresholds"]["mitigate_threshold"])
    assert abs(cg.MITIGATE_THRESHOLD - spec_value) < _F32_TOL


def test_both_languages_render_the_same_threshold_value():
    # Belt-and-braces: the Rust literal and the Python const must agree with each other,
    # not merely each with the spec. A future change that drifts one renderer is caught.
    from provision import contract_gen as cg

    rust_value = _parse_rust_f32_literal(_rust_threshold_literal())
    assert abs(rust_value - cg.MITIGATE_THRESHOLD) < _F32_TOL


@pytest.mark.parametrize(
    "value, expected_rust, expected_py",
    [
        (0.30, "0.3_f32", "0.3"),
        (0.3, "0.3_f32", "0.3"),
        (0.05, "0.05_f32", "0.05"),
        (0.123456789, "0.123456789_f32", "0.123456789"),
        (1, "1.0_f32", "1.0"),          # int spec value still renders as a float literal
        (1e-05, "1e-05_f32", "1e-05"),  # small value: deterministic, both languages agree
    ],
)
def test_float_render_helpers_are_deterministic(value, expected_rust, expected_py):
    # The helpers must render a stable, round-trippable form for representative threshold
    # values so a spec change to mitigate_threshold can't make the two languages diverge.
    assert GEN.fmt_f32_rust(value) == expected_rust
    assert GEN.fmt_float_py(value) == expected_py
    # And both forms must round-trip back to the input value.
    assert abs(_parse_rust_f32_literal(GEN.fmt_f32_rust(value)) - float(value)) < _F32_TOL
    assert abs(float(GEN.fmt_float_py(value)) - float(value)) < _F32_TOL


# ---------------------------------------------------------------------------
# CI-005: validate_spec rejects malformed specs with a clear message (no traceback)
# ---------------------------------------------------------------------------

def _good_spec():
    """A fresh deepcopy of the real, valid spec — mutate the COPY, never the file."""
    return copy.deepcopy(SPEC)


def test_validate_spec_accepts_the_real_spec():
    # Guard the negative tests: the real spec must pass, so a failure below is the spec
    # being malformed, not validate_spec being broken.
    GEN.validate_spec(_good_spec())  # must not raise


def test_validate_spec_rejects_missing_required_key():
    s = _good_spec()
    del s["identity"]["default_garden"]
    with pytest.raises(SystemExit) as ei:
        GEN.validate_spec(s)
    msg = str(ei.value)
    assert "spec validation failed" in msg
    assert "identity].default_garden" in msg  # names the exact missing key


def test_validate_spec_rejects_missing_required_table():
    s = _good_spec()
    del s["thresholds"]
    with pytest.raises(SystemExit) as ei:
        GEN.validate_spec(s)
    assert "missing required table [thresholds]" in str(ei.value)


def test_validate_spec_rejects_duplicate_nav_id():
    s = _good_spec()
    s["ui"]["nav"].append(copy.deepcopy(s["ui"]["nav"][0]))  # duplicate the first nav entry
    with pytest.raises(SystemExit) as ei:
        GEN.validate_spec(s)
    msg = str(ei.value)
    assert "duplicate nav id" in msg
    assert repr(s["ui"]["nav"][0]["id"]) in msg  # names which id collided


def test_validate_spec_rejects_id_len_misordering():
    s = _good_spec()
    s["identity"]["id_min_len"] = 100
    s["identity"]["id_max_len"] = 5
    with pytest.raises(SystemExit) as ei:
        GEN.validate_spec(s)
    assert "id_min_len" in str(ei.value) and "id_max_len" in str(ei.value)


def test_validate_spec_rejects_non_numeric_threshold():
    s = _good_spec()
    s["thresholds"]["mitigate_threshold"] = "oops"
    with pytest.raises(SystemExit) as ei:
        GEN.validate_spec(s)
    assert "mitigate_threshold must be numeric" in str(ei.value)


def test_validate_spec_does_not_mutate_the_input():
    # Validation is read-only: it must not normalize/strip the dict it inspects.
    before = _good_spec()
    after = copy.deepcopy(before)
    GEN.validate_spec(after)
    assert after == before
