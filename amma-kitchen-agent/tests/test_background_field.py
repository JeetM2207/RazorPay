"""The violet field behind every console page.

Ported from design/reference_background.html ("Mono"). Purely decorative:
it reads nothing, decides nothing, and reacts to nothing. These tests pin
the handful of properties that would each turn it from a background into
a bug.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
BG = WEB / "bg.js"
CSS = (WEB / "shared.css").read_text(encoding="utf-8")
DASHBOARD = (ROOT / "dashboard.py").read_text(encoding="utf-8")

# The pages somebody actually works on. index.html and login.html keep the
# older three-blob aurora deliberately -- they are arrival screens.
FIELD_PAGES = ["order.html", "merchant.html", "profile.html", "shop.html",
               "evidence.html"]


# ------------------------------------------------------- the effect itself

def test_the_additive_blend_is_present():
    """'lighter' IS the effect. Without it the five gradients paint over
    each other and this is flat blobs rather than blending light."""
    src = BG.read_text(encoding="utf-8")
    assert '"lighter"' in src or "'lighter'" in src, "the additive blend is gone"
    assert "source-over" in src, (
        "the composite mode is never reset, so everything drawn afterwards "
        "on this canvas would blend too"
    )


def test_one_hue_at_five_depths():
    """A single violet is what keeps amber/coral/green the only non-violet
    things on screen, so a status still catches the eye. Five colours that
    are all violet -- not five different hues."""
    src = BG.read_text(encoding="utf-8")
    block = re.search(r"COLORS\s*=\s*\[(.*?)\]", src, re.S)
    assert block, "the palette is not where the test expects it"
    triples = re.findall(r'"(\d+),(\d+),(\d+)"', block.group(1))
    assert len(triples) == 5, f"expected five depths, found {len(triples)}"
    for r, g, b in triples:
        r, g, b = int(r), int(g), int(b)
        assert b > r > g, f"rgb({r},{g},{b}) is not a violet"


# ------------------------------------------------------------- placement

def test_the_canvas_never_swallows_a_click():
    """A fixed full-screen canvas without this is a worse regression than
    having no background at all: every button on the site stops working
    and nothing errors."""
    for name, source in (("shared.css", CSS), ("dashboard.py", DASHBOARD)):
        rule = re.search(r"#bgCanvas\s*\{\{?(.*?)\}\}?", source, re.S)
        assert rule, f"{name} does not style #bgCanvas"
        assert "pointer-events: none" in rule.group(1), (
            f"{name}: #bgCanvas would swallow every click on the page"
        )
        assert "z-index: 0" in rule.group(1), f"{name}: the field is not behind"


def test_every_console_page_carries_it():
    """Consistency is the point -- on some pages and not others reads as a
    bug rather than a choice."""
    for name in FIELD_PAGES:
        page = (WEB / name).read_text(encoding="utf-8")
        assert 'id="bgCanvas"' in page, f"{name} has no canvas"
        assert "/static/bg.js" in page, f"{name} never loads the field"
    assert 'id="bgCanvas"' in DASHBOARD, "the audit page has no canvas"
    assert "/static/bg.js" in DASHBOARD, "the audit page never loads the field"


def test_a_page_never_runs_both_backgrounds():
    """Two violet gradient systems at different blur scales fight each
    other -- it reads as mud, and doubles a full-screen composite on pages
    that have to hold frame rate during a live demo."""
    for name in FIELD_PAGES:
        page = (WEB / name).read_text(encoding="utf-8")
        assert 'class="aurora' not in page, (
            f"{name} runs the old three-blob aurora as well as the field"
        )


# ------------------------------------------------- what it must NOT be

def _code_only(src: str) -> str:
    """Strip comments before scanning for forbidden calls.

    The first version of the test below matched the word "audit" inside
    this file's own comment explaining that it never reads the audit
    trail. A test that fires on prose is a test people learn to ignore.
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"^\s*//.*$", "", src, flags=re.M)


def test_it_is_decorative_and_reads_nothing():
    """A background driven by live data is a background people watch. The
    surface that deserves watching here is the agent's terminal."""
    src = _code_only(BG.read_text(encoding="utf-8"))
    for forbidden in ("fetch(", "XMLHttpRequest", "/api/", "audit",
                      "mousemove", "pointermove", "clientX"):
        assert forbidden not in src, (
            f"the background reaches for {forbidden!r} -- it is meant to be "
            "inert decoration, not a live or interactive surface"
        )


def test_reduced_motion_keeps_the_background_and_drops_the_motion():
    src = BG.read_text(encoding="utf-8")
    assert "prefers-reduced-motion" in src, "the query is never read"
    # A single frame is still painted; only the loop stops.
    assert re.search(r"function render\(\)\s*\{[^}]*frame\(t\)", src, re.S), (
        "render() must paint a frame unconditionally -- resize() clears the "
        "canvas, so anything that leaves the repaint to a future frame is "
        "black under reduced motion and in a hidden tab"
    )


def test_the_reference_it_was_ported_from_is_committed():
    ref = ROOT / "design" / "reference_background.html"
    assert ref.exists(), "the reference is not in the repo"
    assert "flowMono" in ref.read_text(encoding="utf-8"), (
        "the reference no longer contains the mode this was ported from"
    )
