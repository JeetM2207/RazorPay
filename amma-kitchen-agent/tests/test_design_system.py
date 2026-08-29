"""The design system's non-negotiables, pinned.

Two of these are rules this project has held since the first console and
would lose silently: nothing may load from a CDN, and every animation must
have a reduced-motion answer. The third is the one the motion pass could
most easily have broken -- a terminal that types out fabricated activity
instead of what the agent actually did.

None of this replaces opening the pages in a browser. A green suite says
the files parse; it says nothing about whether the page runs. But these
are the properties a later edit would quietly undo.
"""

import pathlib
import re

import pytest

WEB = pathlib.Path(__file__).resolve().parent.parent / "web"
PAGES = sorted(WEB.glob("*.html"))
CSS = (WEB / "shared.css").read_text(encoding="utf-8")


def _all_sources():
    for page in PAGES:
        yield page.name, page.read_text(encoding="utf-8")
    yield "shared.css", CSS
    dash = WEB.parent / "dashboard.py"
    yield "dashboard.py", dash.read_text(encoding="utf-8")


# ------------------------------------------------------------- no network

@pytest.mark.parametrize("name,source", list(_all_sources()), ids=lambda v: v if isinstance(v, str) and len(v) < 30 else "")
def test_nothing_reaches_the_network(name, source):
    """The mockup pulled Inter from Google's CDN. A conference wifi that
    drops that request must not change what a judge sees, so the fonts are
    committed and served from this origin."""
    for host in ("fonts.googleapis.com", "fonts.gstatic.com", "cdn.jsdelivr.net",
                 "unpkg.com", "cdnjs.cloudflare.com", "//use.typekit"):
        assert host not in source, f"{name} loads from {host}"
    # No @import of a remote stylesheet either.
    assert not re.search(r"@import\s+url\(\s*['\"]?https?://", source), f"{name} @imports a remote sheet"


def test_the_fonts_are_actually_committed():
    """A @font-face pointing at a file nobody committed is worse than no
    @font-face: it fails only on someone else's machine."""
    fonts = WEB / "fonts"
    for declared in set(re.findall(r"url\('fonts/([^']+)'\)", CSS)):
        path = fonts / declared
        assert path.exists(), f"shared.css declares {declared}, which is not in web/fonts/"
        assert path.read_bytes()[:4] == b"wOF2", f"{declared} is not a real woff2"


def test_every_family_falls_back_to_a_system_stack():
    """If the woff2 fails to load the page must still render, not drop to
    whatever the browser feels like."""
    for token in ("--font:", "--font-mono:"):
        line = next(l for l in CSS.splitlines() if l.strip().startswith(token))
        assert line.count(",") >= 2, f"{token} has no real fallback stack: {line.strip()}"


# --------------------------------------------------------- reduced motion

def test_reduced_motion_is_answered():
    assert "prefers-reduced-motion" in CSS
    block = CSS[CSS.index("@media (prefers-reduced-motion: reduce)"):]
    for selector in (".blob", ".blur-in", ".fade-up"):
        assert selector in block, f"{selector} animates with no reduced-motion answer"


def test_the_terminal_types_instantly_under_reduced_motion():
    """The information still has to arrive; only the motion goes."""
    order = (WEB / "order.html").read_text(encoding="utf-8")
    assert "prefers-reduced-motion" in order, "order.html never reads the query"
    assert "if (REDUCED_MOTION || !total) return Promise.resolve();" in order, (
        "typeInto must print the line whole rather than animating it"
    )


# ------------------------------------------- the terminal shows real work

def test_the_mockups_canned_script_did_not_ship():
    """The design pass typed eight hardcoded lines on a loop. On this
    terminal every line is an actual step the agent took, so none of that
    script may exist here."""
    order = (WEB / "order.html").read_text(encoding="utf-8")
    for canned in ("connecting to agent-an1yp", "reading amma's live catalog",
                   "settling via razorpay", "order #114"):
        assert canned.lower() not in order.lower(), f"a canned demo line survived: {canned}"


def test_the_idle_terminal_says_it_is_idle():
    """Rather than looping fake activity, which would be fabricating agent
    behaviour that is not happening."""
    order = (WEB / "order.html").read_text(encoding="utf-8")
    assert "Agent asleep" in order


def test_the_countups_target_fetched_values_not_literals():
    """countUp(el, 94) with a literal is the mockup's scaffolding. Every
    call must pass something computed."""
    for name in ("order.html", "merchant.html"):
        source = (WEB / name).read_text(encoding="utf-8")
        for call in re.findall(r"countUp\(\s*\$\([^)]*\)\s*,\s*([^,)]+)", source):
            assert not call.strip().rstrip(")").isdigit(), (
                f"{name} counts up to the literal {call.strip()}"
            )


# ------------------------------------------------- the palette still maps

def test_the_four_status_colours_keep_their_meanings():
    """Which status owns which slot has not moved across any design pass.
    A restyle that quietly swapped refused and waiting would be a serious
    bug on a merchant's board."""
    assert "--ok: var(--green)" in CSS
    assert "--warn: var(--amber)" in CSS
    assert "--stop: var(--coral)" in CSS
    assert "--info: var(--sky)" in CSS


def test_the_old_variable_names_still_resolve():
    """Page-local rules across five files use these. If an alias is
    dropped, that page renders with unset colours and nothing errors."""
    declared = set(re.findall(r"^\s*(--[a-z0-9-]+)\s*:", CSS, re.MULTILINE))
    for alias in ("--paper", "--paper-card", "--paper-border", "--coffee", "--coffee-2",
                  "--gold", "--gold-deep", "--leaf", "--rust", "--brick", "--steel",
                  "--ink", "--ink-soft", "--ink-3", "--accent", "--radius-chit"):
        assert alias in declared, f"{alias} is used by page CSS but no longer defined"


def test_no_page_still_carries_a_paper_era_hex():
    """The previous palette's literals were inlined in page <style> blocks
    and, worse, in JS strings -- the terminal's state chip is coloured from
    JavaScript. A stylesheet swap cannot reach those."""
    retired = ("#F1ECDF", "#FBF8F0", "#DAD0B8", "#2B1D14", "#B8791A", "#8F5C10",
               "#4F7942", "#A85C2A", "#9B3A2C", "#2E1B0E", "#1C0F06")
    for name, source in _all_sources():
        for old in retired:
            assert old.lower() not in source.lower(), f"{name} still uses {old}"


def test_no_rule_paints_text_with_a_background_token():
    """`--paper` named the light ink on the coffee ground in the previous
    pass and names the page ground itself in this one. Every rule that used
    it as a COLOR therefore became near-black text on near-black -- an
    invisible heading, with nothing erroring anywhere.

    That is the one sharp edge of the alias layer that made this restyle
    cheap: a token whose meaning is a ROLE survives being re-pointed, and a
    token whose meaning was a VALUE does not. Found on screen, so it is
    pinned here."""
    grounds = ("--paper", "--paper-card", "--bg", "--card", "--card-2", "--coffee", "--coffee-2")
    for name, source in _all_sources():
        for ground in grounds:
            hit = re.search(rf"(?<!-)color:\s*var\(\s*{re.escape(ground)}\s*\)", source)
            assert hit is None, f"{name} paints text with the ground token {ground}"


# ----------------------------------------------------- the helper scripts

def test_every_script_can_run_from_anywhere():
    """`python scripts/foo.py` from the project root must work.

    free_payment_links.py was missing its sys.path line and died on
    `ModuleNotFoundError: No module named 'razorpay_client'` -- the same
    class of bug as the two demo scripts that pointed at ports nothing
    listened on: correct code nobody had actually invoked the documented
    way. Found by running the documented command.
    """
    scripts = (WEB.parent / "scripts").glob("*.py")
    for script in scripts:
        source = script.read_text(encoding="utf-8")
        imports_project = any(
            f"import {name}" in source
            for name in ("razorpay_client", "audit_log", "merchant_config",
                         "idempotency", "adapter_mcp", "routines", "app")
        )
        if not imports_project:
            continue
        assert "sys.path.insert" in source, (
            f"scripts/{script.name} imports a project module but never puts the "
            "project root on sys.path, so it only runs from the right directory"
        )
