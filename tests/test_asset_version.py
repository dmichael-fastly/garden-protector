"""Guard: the /static/gp.{css,js} cache-bust (?v=) must come from the ONE generated
ASSET_VERSION (contract/spec.toml -> contract_gen), never a hand-typed literal.

Before this, the ?v= was copied into ~22 pages by hand and drifted (edge served v=4
while the Pi served v=3). Now every page links `?v=__ASSET_VERSION__` and each tier
stamps the real version at serve time. This test fails if any literal `?v=<number>`
sneaks back in — turning silent drift into a red build.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Files that reference the shared assets (HTML pages + the server code that serves them).
# Generated modules (contract_gen.*) and this test are intentionally excluded.
GLOBS = [
    "backend/src/dashboard.html",
    "backend/src/timelapse.html",
    "backend/src/main.rs",
    "backend/src/routes.rs",
    "hardware/portal.py",
    "hardware/*.html",
    "provision/console.py",
    "provision/console.html",
]

# A `?v=` or `&v=` followed by a digit — the hand-typed-literal shape. `?t=<digits>`
# image cache-busters (snapshot URLs) are deliberately NOT matched.
LITERAL = re.compile(r"[?&]v=\d")


def test_no_hardcoded_asset_version():
    offenders = []
    for g in GLOBS:
        for path in sorted(ROOT.glob(g)):
            for i, line in enumerate(path.read_text().splitlines(), 1):
                if LITERAL.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{i}: {line.strip()}")
    assert not offenders, (
        "hardcoded asset ?v= found — use ?v=__ASSET_VERSION__ (bump contract/spec.toml "
        "[ui].asset_version + `make gen`):\n  " + "\n  ".join(offenders)
    )


def test_token_is_used_somewhere():
    # Sanity: the token must actually appear (else the guard above is vacuously true
    # because someone removed all asset links).
    hits = sum(
        "?v=__ASSET_VERSION__" in path.read_text()
        for g in GLOBS
        for path in ROOT.glob(g)
    )
    assert hits >= 5, f"expected the asset-version token across pages, found {hits}"
