"""countUp() must count DOWN when the real number went down.

Found live: a merchant declines a paid order, Razorpay refunds it, and
today's revenue KPI should visibly drop by that amount on screen -- the
demo beat the console exists to make possible. Instead the number
flashed to zero and counted back UP to the lower figure, because
`countUp()` always animated from a hardcoded 0 regardless of what was
already on screen. A viewer watching that sees the board glitch, not
the number they just caused to change.

This runs the real function under Node rather than asserting on the
source text, because the earlier version *looked* fine by every static
check: it took a target, eased it, rendered it. The defect was only in
the relationship between two successive calls, which no regex over one
call site could see. Skipped where node is unavailable, same as
test_pages_parse.py -- a stronger check than the suite can require, not
a new dependency.
"""

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "web"

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

_FN = re.compile(r"function countUp\(.*?\n\}", re.S)

# A harness standing in for the DOM and the animation clock. Frames are
# driven by hand -- calling every queued rAF callback with an advancing
# timestamp -- so the test controls time instead of racing a real one.
HARNESS = """
const REDUCED_MOTION = false;
let queue = [];
function requestAnimationFrame(cb) { queue.push(cb); }
function runFrames(untilMs, stepMs) {
  let now = 0;
  while (now <= untilMs) {
    const due = queue; queue = [];
    for (const cb of due) cb(now);
    now += stepMs;
  }
  const due = queue; queue = [];
  for (const cb of due) cb(untilMs + 1);
}
const performance = { now: () => 0 }; // tick() reads its own `now` arg, not this
const document = { visibilityState: "visible" };

%(fn)s

function el() { return { dataset: {}, textContent: "" }; }
function numeric(text) { return Number(String(text).replace(/[^0-9.-]/g, "")); }

const target1 = 2095, target2 = 1490; // a real decline: revenue drops
const node = el();

countUp(node, target1, { duration: 200, prefix: "\\u20b9" });
runFrames(200, 16);
const afterFirst = numeric(node.textContent);

countUp(node, target2, { duration: 200, prefix: "\\u20b9" });
let sawMidpointBelowStart = false;
let touchedZero = false;
let now = 0;
while (now <= 216) {
  const due = queue; queue = [];
  for (const cb of due) cb(now);
  const v = numeric(node.textContent);
  if (v < target1 && v > target2) sawMidpointBelowStart = true;
  if (v === 0) touchedZero = true;
  now += 16;
}
const afterSecond = numeric(node.textContent);

console.log(JSON.stringify({ afterFirst, afterSecond, sawMidpointBelowStart, touchedZero }));
"""


def _extract(page: str) -> str:
    m = _FN.search(page)
    assert m, "countUp() not found"
    return m.group(0)


def _run(fn_source: str) -> dict:
    script = HARNESS % {"fn": fn_source}
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(script)
        path = fh.name
    try:
        done = subprocess.run([NODE, path], capture_output=True, text=True, timeout=10)
    finally:
        Path(path).unlink(missing_ok=True)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


@needs_node
@pytest.mark.parametrize("filename", ["merchant.html", "order.html"])
def test_a_falling_kpi_counts_down_not_back_up_from_zero(filename):
    page = (WEB / filename).read_text(encoding="utf-8")
    result = _run(_extract(page))

    assert result["afterFirst"] == 2095
    assert result["afterSecond"] == 1490
    # The whole point: somewhere mid-animation the displayed number sat
    # strictly between the old and new values -- it counted DOWN through
    # the range, rather than resetting to 0 and counting back up into it.
    assert result["sawMidpointBelowStart"], (
        f"{filename}: countUp never showed a value between the old and new "
        "totals -- it is not counting down"
    )
    assert not result["touchedZero"], (
        f"{filename}: countUp passed through 0 on the way to a lower, "
        "nonzero target -- exactly the flash-to-zero bug"
    )
