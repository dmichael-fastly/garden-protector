"""Guards for the re-layered UI/UX pass (UX-001/002/004, UI-001/004/005) plus the
asset-version SSOT.

These pin OBSERVABLE, user-facing properties of the shared web surfaces so a future
refactor of the templates can't silently regress them:

  * asset_version: the generated Rust + Python contract modules carry the SAME value
    as contract/spec.toml (the cache-bust SSOT). Sister test to test_contract.py's
    spec<->python check; this one also pins the Rust generated module so a stale
    `make gen` is caught on the Rust side too.
  * UX-001: gp.css ships a `prefers-reduced-motion` fallback (WCAG 2.2 2.3.3) so the
    safety pills / spinners / blinking cursor stop animating for motion-sensitive users.
  * UX-002 / UX-004: the user-facing pages keep the gardener voice — no engineer
    jargon ("the Pi", "Secret Store", "Node ID", "FSM", "Correlation Trace",
    "camera_view.py", "ESP32", "edge registry") leaks into VISIBLE text. The check
    parses the HTML and inspects text nodes + the handful of attributes that are
    actually shown/announced (placeholder/title/alt/aria-label), so machine values
    like `value="fsm"` or `class="badge fsm"` and code in <script>/<style>/comments
    are correctly ignored.
  * UI-004: the standalone login/console/portal headers use the `#gp-leaf` sprite,
    not the leaf emoji (&#127793;).
  * UI-005: the gp-warn sprite is a single-source alias of gp-alert (one triangle).
  * UI-001: the History route marks `nav-history` active SERVER-side on both tiers
    (via the shared generated render_nav + the Pi render_header seam), so the active
    tab is right with JS disabled / before hydration.
"""
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import tomllib
from html.parser import HTMLParser

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = tomllib.loads((ROOT / "contract" / "spec.toml").read_text())


# ---------------------------------------------------------------------------
# asset_version SSOT: spec.toml == generated Python == generated Rust
# ---------------------------------------------------------------------------

def test_asset_version_synced_across_generated_modules():
    """The ?v= cache-bust lives only in contract/spec.toml; both generated modules
    must carry it verbatim. (Sister to test_contract.test_generated_ui_consts_match_spec,
    which only checks the Python side. gen-check guards drift on disk; this pins the
    actual emitted literal so a stale `make gen` is a red test, not a silent skew.)"""
    spec_v = str(SPEC["ui"]["asset_version"])

    from provision import contract_gen as cg
    assert str(cg.ASSET_VERSION) == spec_v, "provision/contract_gen.py asset_version drifted from spec.toml"

    rust = (ROOT / "backend" / "src" / "contract_gen.rs").read_text()
    m = re.search(r'ASSET_VERSION\s*:\s*&str\s*=\s*"([^"]+)"', rust)
    assert m, "ASSET_VERSION const not found in generated contract_gen.rs"
    assert m.group(1) == spec_v, (
        f"contract_gen.rs ASSET_VERSION={m.group(1)!r} != spec.toml {spec_v!r} — run `make gen`"
    )


# ---------------------------------------------------------------------------
# UX-001: reduced-motion fallback
# ---------------------------------------------------------------------------

def test_gp_css_has_reduced_motion_block():
    css = (ROOT / "ui" / "static" / "gp.css").read_text()
    assert "@media (prefers-reduced-motion: reduce)" in css, (
        "gp.css must ship a prefers-reduced-motion fallback (WCAG 2.2 2.3.3) so the "
        "safety pills / spinners / blinking cursor stop animating for motion-sensitive users"
    )


# ---------------------------------------------------------------------------
# UX-002 / UX-004: no engineer jargon in VISIBLE user-facing copy
# ---------------------------------------------------------------------------

# Substrings that must never appear in user-visible text. Lowercased for a
# case-insensitive compare. These mirror the project's banned-jargon list
# (user-facing-copy-non-technical) plus the specific leaks the audit flagged.
BANNED_JARGON = [
    "the pi",
    "secret store",
    "node id",
    "fsm",
    "correlation trace",
    "camera_view.py",
    "esp32",
    "edge registry",
]

# Pages a homeowner/gardener actually reads. (Not the bootstrap login/console pages,
# which are covered for the leaf-sprite convention below; the wizard/portal copy lives
# in these HTML templates.)
USER_FACING_PAGES = [
    "hardware/logs.html",
    "backend/src/dashboard.html",
    "hardware/devices.html",
    "hardware/storage.html",
    "hardware/costs.html",
    "hardware/settings.html",
    "hardware/wizard.html",
    "backend/src/timelapse.html",
    "provision/console.html",
]

# Attributes whose VALUES are shown to or announced for the user. Everything else
# (value/id/class/data-*/href/name/for/...) is a machine identifier and is ignored,
# so `<option value="fsm">` or `class="badge fsm"` do NOT count as visible jargon.
_VISIBLE_ATTRS = {
    "placeholder", "title", "alt",
    "aria-label", "aria-description", "aria-roledescription", "aria-placeholder",
}
_SKIP_TAGS = {"script", "style"}


class _VisibleTextExtractor(HTMLParser):
    """Collect text nodes + visible attribute values, skipping <script>/<style>
    bodies. convert_charrefs decodes entities (so &#127793; etc. surface as text).
    HTML comments are dropped by HTMLParser (handle_comment is not overridden)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.chunks = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        for key, val in attrs:
            if key in _VISIBLE_ATTRS and val:
                self.chunks.append(val)

    def handle_startendtag(self, tag, attrs):
        for key, val in attrs:
            if key in _VISIBLE_ATTRS and val:
                self.chunks.append(val)

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            self.chunks.append(data)


def _visible_text(html: str) -> str:
    p = _VisibleTextExtractor()
    p.feed(html)
    p.close()
    return " ".join(p.chunks)


def test_no_jargon_in_user_facing_visible_copy():
    offenders = []
    for rel in USER_FACING_PAGES:
        path = ROOT / rel
        assert path.exists(), f"expected user-facing page missing: {rel}"
        text = _visible_text(path.read_text()).lower()
        for term in BANNED_JARGON:
            if term in text:
                offenders.append(f"{rel}: visible copy contains banned jargon {term!r}")
    assert not offenders, (
        "user-facing copy must stay in the gardener voice (no engineer jargon):\n  "
        + "\n  ".join(offenders)
    )


def test_visible_text_extractor_ignores_code_and_attrs():
    """Pin the extractor's contract so the jargon guard above can't go vacuously green:
    machine attribute values + <script>/<style>/comments are NOT treated as visible copy,
    but real text nodes and visible attrs ARE."""
    sample = (
        '<!-- the Pi comment -->'
        '<style>.x{content:"the Pi"}</style>'
        '<script>var x = "the Pi";</script>'
        '<option value="fsm">Protection status</option>'
        '<input placeholder="say hello">'
        '<p>Hello gardener</p>'
    )
    vis = _visible_text(sample).lower()
    assert "the pi" not in vis          # comment + script + style stripped
    assert "fsm" not in vis             # value="" is a machine attr
    assert "say hello" in vis           # placeholder is visible
    assert "hello gardener" in vis      # text node
    assert "protection status" in vis   # option label text


# ---------------------------------------------------------------------------
# UI-004: standalone headers use the leaf sprite, not the emoji
# ---------------------------------------------------------------------------

# The three server emitters of the standalone login/console/portal header.
_LEAF_HEADER_EMITTERS = [
    "backend/src/main.rs",
    "provision/console.py",
    "hardware/portal.py",
]
_LEAF_EMOJI = "\U0001f33f"  # 🌿 — also written &#127793; in HTML


def test_login_headers_use_leaf_sprite_not_emoji():
    for rel in _LEAF_HEADER_EMITTERS:
        src = (ROOT / rel).read_text()
        # Locate the header line and assert it references the sprite, not the emoji.
        header_lines = [l for l in src.splitlines() if "Fastly Garden Protector</h1>" in l]
        assert header_lines, f"no login/header H1 found in {rel}"
        for line in header_lines:
            assert "#gp-leaf" in line, f"{rel} header should use the #gp-leaf sprite: {line.strip()}"
            assert _LEAF_EMOJI not in line, f"{rel} header still ships the leaf emoji: {line.strip()}"
            assert "&#127793;" not in line, f"{rel} header still ships the leaf emoji entity: {line.strip()}"


# ---------------------------------------------------------------------------
# UI-005: gp-warn is a single-source alias of gp-alert
# ---------------------------------------------------------------------------

def test_gp_warn_is_alias_of_gp_alert():
    """One triangle path, two names. gp-warn must reference #gp-alert (an alias),
    not duplicate the triangle <path> bytes."""
    js = (ROOT / "ui" / "static" / "gp.js").read_text()

    def _sprite_body(name):
        m = re.search(r"\['" + re.escape(name) + r"',\s*'([^']*)'\]", js)
        assert m, f"sprite {name!r} not found in gp.js SPRITE array"
        return m.group(1)

    alert_body = _sprite_body("gp-alert")
    warn_body = _sprite_body("gp-warn")
    assert "<path" in alert_body, "gp-alert should define the triangle path"
    assert warn_body == '<use href="#gp-alert"/>', (
        f"gp-warn must be a <use> alias of gp-alert, got: {warn_body!r}"
    )
    # Defensive: the alias must not re-inline the triangle path (the duplication UI-005 fixed).
    assert "<path" not in warn_body, "gp-warn re-inlined a path — it should alias gp-alert"


# ---------------------------------------------------------------------------
# UI-001: History marks nav-history active SERVER-side on both tiers
# ---------------------------------------------------------------------------

def test_render_nav_marks_history_active_server_side():
    """The shared generated render_nav (rendered by BOTH tiers) marks the History tab
    active when given nav-history — no JS active-class patch needed. This is the SSOT
    both the edge history_header_html and the Pi /history route feed."""
    from provision import contract_gen as cg
    nav = cg.render_nav("nav-history", False)
    assert 'id="nav-history" class="active"' in nav, "render_nav did not mark History active"
    assert 'id="nav-dashboard" class="active"' not in nav, "Dashboard wrongly marked active on History"


def test_pi_history_route_renders_history_active():
    """The Pi render_header seam splices nav-history as active for the /history route,
    so the server-rendered active tab is correct without client JS."""
    from hardware import portal as pt
    partial = '<nav id="portal-nav" class="portal"><!--NAV_LINKS--></nav>'
    out = pt.render_header(partial, "nav-history", "G", view_only=True)
    assert 'id="nav-history" class="active"' in out
    assert 'id="nav-dashboard" class="active"' not in out


# ---------------------------------------------------------------------------
# Inline-JS syntax guard: smart quotes as string delimiters break the WHOLE page
# ---------------------------------------------------------------------------
#
# A friendly-copy pass once curly-quoted JS string DELIMITERS inside an inline
# <script> — `off: “Off …”,` (dashboard MODE_HELP) and `let s = “Garden hub” …`
# (devices describeConnection). U+201C/U+201D are not valid JS delimiters, so the
# ENTIRE inline script is a SyntaxError and silently never runs. The dashboard is an
# SPA (/, /admin, /history are one page; JS show/hides sections by URL), so the dead
# script made "/history" render the default Dashboard sections — a routing-looking bug
# with a typographic cause. The jargon/sprite guards above parse text, not JS, so this
# slipped past CI. These two tests close that gap.

# Every template that BOTH tiers serve — globbed so a new page is covered automatically.
_TEMPLATES = sorted(
    list((ROOT / "backend" / "src").glob("*.html"))
    + list((ROOT / "hardware").glob("*.html"))
    + list((ROOT / "provision").glob("*.html"))
)
# Inline <script> bodies only (skip <script src=...> references).
_INLINE_SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)
_CURLY = "“”‘’"  # “ ” ‘ ’
# A `/` that follows one of these (or starts the input) begins a REGEX literal, not a
# division — the standard regex-vs-divide disambiguation by previous significant token.
# Covers the realistic inline-script cases (`.replace(/…/)`, `= /…/`, `(/…/)`); a regex
# right after a keyword like `return` is not handled, but those carry no quotes here.
_REGEX_PRECEDES = set("(,=:[!&|?{};<>+-*/%^~")


def _curly_quotes_in_code(js):
    """Return [(script_relative_line, char)] for every curly quote that sits in CODE
    position — i.e. used as / where a token goes, NOT inside a string literal, template
    literal, regex literal, or comment. Those are the ones that stand in for a straight
    " ' or ` and blow up the parse. Curly quotes INSIDE a straight-delimited string
    (display typography like `" “x” "`), a regex, or a comment are legitimate and ignored.

    A small hand-rolled scanner, deliberately dependency-free so it runs in CI even where
    `node` isn't provisioned. It DOES skip regex literals (incl. their char classes) so a
    quote inside one — e.g. `.replace(/[&<>"]/g, …)` — can't desync the string tracking.
    Curly quotes strictly inside a `${…}` template interpolation are treated as template
    content (an accepted blind spot); the real-world bug lives in plain code position."""
    offenders, i, n, line = [], 0, len(js), 1
    state = "code"  # code | dq | sq | tpl | line_comment | block_comment | regex | regex_class
    prev = ""        # last significant (non-space) char seen in code — drives regex-vs-divide
    while i < n:
        c = js[i]
        nxt = js[i + 1] if i + 1 < n else ""
        if c == "\n":
            line += 1
            if state == "line_comment":
                state = "code"
            i += 1
            continue
        if state == "code":
            if c == "/" and nxt == "/":
                state = "line_comment"; i += 2; continue
            if c == "/" and nxt == "*":
                state = "block_comment"; i += 2; continue
            if c == "/" and (prev == "" or prev in _REGEX_PRECEDES):
                state = "regex"; i += 1; continue
            if c == '"':
                state = "dq"; i += 1; continue
            if c == "'":
                state = "sq"; i += 1; continue
            if c == "`":
                state = "tpl"; i += 1; continue
            if c in _CURLY:
                offenders.append((line, c))
            if not c.isspace():
                prev = c
            i += 1
            continue
        if state == "line_comment":
            i += 1; continue
        if state == "block_comment":
            if c == "*" and nxt == "/":
                state = "code"; i += 2; continue
            i += 1; continue
        if state == "regex":
            if c == "\\":
                i += 2; continue
            if c == "[":
                state = "regex_class"; i += 1; continue
            if c == "/":
                state = "code"; prev = "/"; i += 1; continue
            i += 1; continue
        if state == "regex_class":          # inside [...]; a / here is literal, ] ends the class
            if c == "\\":
                i += 2; continue
            if c == "]":
                state = "regex"
            i += 1
            continue
        # string-ish states (dq / sq / tpl): curly quotes here are valid content.
        if c == "\\":  # escape — skip the next char (a `\` + delimiter stays in-string)
            i += 2; continue
        if (state == "dq" and c == '"') or (state == "sq" and c == "'") or (state == "tpl" and c == "`"):
            state = "code"; prev = c
        i += 1
    return offenders


def test_inline_scripts_have_no_curly_quote_delimiters():
    """No curly/smart quote may stand in CODE position inside an inline <script> — that
    is the SyntaxError class that silently killed the dashboard/devices scripts. Pure
    Python (no node dependency) so it ALWAYS runs in CI, not just where node happens to
    be installed."""
    offenders = []
    for path in _TEMPLATES:
        html = path.read_text(encoding="utf-8")
        for m in _INLINE_SCRIPT_RE.finditer(html):
            base = html.count("\n", 0, m.start(1)) + 1  # file line of the script body start
            for rel_line, ch in _curly_quotes_in_code(m.group(1)):
                offenders.append(
                    f"{path.relative_to(ROOT)}:{base + rel_line - 1}: curly quote {ch!r} in JS code position"
                )
    assert not offenders, (
        "curly/smart quotes used as JS string delimiters inside an inline <script> are a "
        "SyntaxError that silently kills the whole page script (this broke the dashboard "
        "MODE_HELP + devices describeConnection). Use straight \" ' ` delimiters; curly "
        "quotes are only OK as display content INSIDE a string:\n  " + "\n  ".join(offenders)
    )


def test_curly_quote_code_scanner_contract():
    """Pin the scanner so the guard above can't go vacuously green: a curly DELIMITER is
    caught; curly quotes inside straight strings / templates / comments are NOT."""
    assert _curly_quotes_in_code("var a = “x”;")          # curly used as delimiters -> caught
    assert _curly_quotes_in_code("let b = ‘y’;")
    assert _curly_quotes_in_code("o = { k: “v” };")       # the exact MODE_HELP shape
    assert not _curly_quotes_in_code('a = " “x” ";')      # display quotes in a "string"
    assert not _curly_quotes_in_code("a = ' “x” ';")      # ... in a 'string'
    assert not _curly_quotes_in_code("a = `t “x” `;")     # ... in a `template`
    assert not _curly_quotes_in_code("// “x”")            # line comment
    assert not _curly_quotes_in_code("/* “x” */")         # block comment
    assert not _curly_quotes_in_code('s = "isn\'t";')              # apostrophe in "string", no curly
    # A regex literal containing a quote must NOT desync the trailing string (the storage.html
    # `.replace(/[&<>"]/g, …)` case that made a downstream "Couldn’t…" look like code).
    assert not _curly_quotes_in_code('x.replace(/[&<>"]/g, "&"); var y = "Couldn’t";')
    assert _curly_quotes_in_code('x.replace(/[&<>"]/g, "&"); var y = “z”;')  # real delimiter still caught after a regex


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available (pure-Python guard above is the CI check)")
def test_inline_scripts_parse_with_node():
    """Bonus broad-coverage guard: every inline <script> must pass `node --check` (catches
    ANY syntax error, not just curly quotes). Skips where node isn't installed — CI doesn't
    provision it, so test_inline_scripts_have_no_curly_quote_delimiters is the guaranteed
    guard; this adds a real-parser pass wherever node IS present (dev machines, runner
    images that ship it)."""
    node = shutil.which("node")
    broken = []
    for path in _TEMPLATES:
        html = path.read_text(encoding="utf-8")
        blocks = _INLINE_SCRIPT_RE.findall(html)
        if not blocks:
            continue
        js = "\n;\n".join(blocks)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tf:
            tf.write(js)
            tmp = tf.name
        try:
            r = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
        finally:
            os.unlink(tmp)
        if r.returncode != 0:
            broken.append(f"{path.relative_to(ROOT)}: {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else 'syntax error'}")
    assert not broken, "inline <script> failed node --check:\n  " + "\n  ".join(broken)
