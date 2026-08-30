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
        assert "z-index: -1" in rule.group(1), f"{name}: the field is not behind"


def test_the_field_sits_behind_everything_not_merely_early():
    """This is the one that went wrong, and it blanked the merchant
    console's headings in production.

    A `position: fixed` element at `z-index: 0` paints ABOVE every
    non-positioned block in the page. The old aurora got away with that
    because its blobs were transparent and blended; this canvas fills
    every pixel with the ground colour, so anything that had not
    explicitly opted into a layer was simply painted over. shared.css
    lifts .topbar/.page/.page-narrow/.toast -- the buyer console lives in
    .page and was fine, and the merchant console's sidebar layout was not
    on that list.

    -1 requires nothing to opt in, so a layout added later cannot
    reintroduce it."""
    for name, source in (("shared.css", CSS), ("dashboard.py", DASHBOARD)):
        rule = re.search(r"#bgCanvas\s*\{\{?(.*?)\}\}?", source, re.S)
        assert rule, f"{name} does not style #bgCanvas"
        assert "z-index: -1" in rule.group(1), (
            f"{name}: an opaque full-screen canvas at z-index 0 or above "
            "paints over every non-positioned block on the page"
        )


def test_body_is_transparent_so_the_field_can_be_seen():
    """The other half of the same fact, and it hid the field completely
    the first time round.

    A negative-z layer paints above the ROOT background but below the
    background of every in-flow block -- body included. So the ground has
    to live on <html> and body has to be transparent, or the canvas is
    painted and then covered by body.
    """
    assert re.search(r"^html\s*\{[^}]*background:", CSS, re.M), (
        "shared.css: nothing paints the ground on <html>, so a transparent "
        "body would render on whatever the browser defaults to"
    )
    body = re.search(r"^body\s*\{(.*?)^\}", CSS, re.S | re.M)
    assert body, "shared.css has no body rule"
    assert "background: transparent" in body.group(1), (
        "shared.css: an opaque body hides the field entirely"
    )

    # And page-local overrides, which is exactly how merchant.html hid it
    # again after shared.css had been fixed.
    for name in FIELD_PAGES:
        page = _without_print_rules((WEB / name).read_text(encoding="utf-8"))
        for match in re.finditer(r"^\s*body\s*\{([^}]*)\}", page, re.M):
            decl = match.group(1)
            if "background" not in decl:
                continue
            assert "transparent" in decl, (
                f"{name} sets an opaque body background, which paints over "
                f"the field: {decl.strip()!r}"
            )


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

def _without_print_rules(css: str) -> str:
    """Drop @media print blocks, matching braces rather than guessing.

    evidence.html legitimately paints a white body for its print sheet --
    Ctrl+P is this project's PDF exporter. A first version of this test
    tried to detect that by comparing rfind() positions and flagged it,
    which is the "test that cries wolf" failure this repo already has a
    note about.
    """
    out, i = [], 0
    while True:
        at = css.find("@media print", i)
        if at == -1:
            out.append(css[i:])
            return "".join(out)
        out.append(css[i:at])
        brace = css.find("{", at)
        if brace == -1:
            return "".join(out)
        depth, j = 1, brace + 1
        while j < len(css) and depth:
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
            j += 1
        i = j


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
