"""Every CSS variable a stylesheet USES, that stylesheet must DEFINE.

This exists because of a bug that had no error attached to it. The audit
page is server-rendered by dashboard.py and carries its own copy of the
design tokens, under some names that predate the current palette. The
wordmark's gradient was ported across from web/shared.css referring to
`var(--violet)` -- a token shared.css defines and this sheet does not.

An undefined custom property is not ignored. It makes the declaration
INVALID AT COMPUTED-VALUE TIME, so `background` fell back to its initial
value while `-webkit-text-fill-color: transparent` from the same rule
applied fine. The result was a wordmark whose second half rendered as a
blank gap, with a clean console and a green suite.

So the check is structural rather than visual: collect the var() reads,
collect the definitions, and assert the first set is inside the second.
"""

import re

import audit_log
import dashboard

# `var(--name)` with NO fallback. A read that supplies one -- `var(--mx,
# 50%)` -- stays valid whether or not the token exists, which is exactly
# how the consoles read the two properties JavaScript sets at runtime. It
# is the bare read that fails, so it is the bare read this collects.
_USES = re.compile(r"var\(\s*(--[\w-]+)\s*\)")
# `--name:` at the start of a declaration.
_DEFINES = re.compile(r"(--[\w-]+)\s*:")


def _sheet_of(html: str) -> str:
    return "\n".join(re.findall(r"<style>(.*?)</style>", html, re.S))


def _audit_sheet() -> str:
    # Rendered with no rows: the tokens live in the <style> block, which
    # does not depend on there being any data.
    return _sheet_of(dashboard._render([], audit_log.DEFAULT_DB_PATH, 0))


def test_the_audit_page_defines_every_token_it_uses():
    sheet = _audit_sheet()
    used = set(_USES.findall(sheet))
    defined = set(_DEFINES.findall(sheet))
    missing = sorted(used - defined)
    assert not missing, (
        "dashboard.py's inline stylesheet reads tokens it never defines: "
        + ", ".join(missing)
        + ". An undefined var makes the whole declaration invalid at "
        "computed-value time -- nothing errors and the rule silently "
        "does not apply."
    )


def test_shared_css_defines_every_token_it_uses():
    sheet = open("web/shared.css", encoding="utf-8").read()
    used = set(_USES.findall(sheet))
    defined = set(_DEFINES.findall(sheet))
    assert not sorted(used - defined)
