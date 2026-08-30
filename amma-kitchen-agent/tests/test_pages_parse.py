"""Every inline script on every page must actually parse.

This project has now shipped a page-killing SyntaxError three times: a
duplicate `const rupee`, a duplicate `cartTotal()`, and a string literal
broken across two lines by a heredoc. Each one killed the ENTIRE script
block, so the page rendered with every JS-populated field empty and every
button dead -- while the Python suite stayed green, because nothing in it
had any opinion about whether the page runs.

The guards written after the first two matched their specific shape
(`const`/`let`/`function` declared twice) and could not have caught the
third. This one asks a JavaScript engine instead of a regex, so it covers
the whole class rather than the last instance of it.

Skipped where node is unavailable: it is a stronger check than the suite
can require, not a new dependency.
"""

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "web"

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

# <script> with no src= -- the inline blocks. Anything with a src is a
# file of its own and is served as-is.
INLINE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)


def pages():
    return sorted(WEB.glob("*.html"))


def parses(source: str):
    """Ask node whether this is valid JavaScript.

    Checked as a module as well as a script, because a page may legally
    use top-level await; either verdict passing means the syntax is fine.
    """
    problem = ""
    for suffix in (".js", ".mjs"):
        with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False,
                                         encoding="utf-8") as fh:
            fh.write(source)
            path = fh.name
        try:
            done = subprocess.run([NODE, "--check", path],
                                  capture_output=True, text=True)
            if done.returncode == 0:
                return True, ""
            problem = done.stderr.strip()
        finally:
            Path(path).unlink(missing_ok=True)
    return False, problem


@needs_node
@pytest.mark.parametrize("page", pages(), ids=lambda p: p.name)
def test_every_inline_script_parses(page):
    for i, block in enumerate(INLINE.findall(page.read_text(encoding="utf-8"))):
        if not block.strip():
            continue
        ok, problem = parses(block)
        assert ok, (
            f"{page.name}: inline script #{i + 1} does not parse, so the whole "
            f"block is dead and every handler in it with it.\n{problem}"
        )
