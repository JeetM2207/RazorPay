"""Her board must not show work she does not owe.

The messages sent to Amma are a mix of questions and notices, and the
console badges them. The badge used to be chosen by looking for an
*answer* -- an escalation whose outcome was recorded -- and calling
everything else "awaiting reply". So a velocity alert reading "Nothing
was charged. No action needed" sat on her board in amber forever, as did
an auto-confirmed order and a failed refund.

That is the same fault as a message that reaches nobody, in the other
direction: a queue that shows a decision she does not have to make is a
queue she stops trusting.

The rule is now taken from the message itself, so this test is built from
the real formatters rather than from strings written to agree with it --
those would pass no matter what the code sent.
"""

import re
from pathlib import Path

import notification_service

MERCHANT = Path(__file__).resolve().parents[1] / "web" / "merchant.html"


def asks_for_an_answer(body: str) -> bool:
    """The console's own predicate, read out of the page it lives on.

    Read rather than restated, because a copy here could agree with a
    test while disagreeing with the page.
    """
    match = re.search(r"const asks = /(?P<re>[^/]+)/(?P<flags>[a-z]*)\.test\(m\.body\);",
                      MERCHANT.read_text(encoding="utf-8"))
    assert match, "the console no longer decides this the way the test expects"
    pattern = match.group("re").replace("\\\\b", "\\b")
    flags = re.IGNORECASE if "i" in match.group("flags") else 0
    return re.search(pattern, body, flags) is not None


# ------------------------------------------------- the ones that ask

def test_an_escalation_asks_for_an_answer():
    body = notification_service.format_escalation_alert(
        order_id=40, agent_id="Jeet's Agent", cart=[("veg_thali", 2)],
        total_inr=440, reason="total Rs.440 at/above human confirmation "
                              "threshold Rs.400", code="4417",
    )
    assert asks_for_an_answer(body), body


def test_an_escalation_with_no_code_still_asks():
    """The codeless wording is the fallback, and it is still a question."""
    body = notification_service.format_escalation_alert(
        order_id=40, agent_id="Jeet's Agent", cart=[("veg_thali", 2)],
        total_inr=440, reason="over the threshold", code="",
    )
    assert asks_for_an_answer(body), body


def test_the_pay_first_order_asks_even_with_no_digits_in_the_instruction():
    """mcp_orders words its own: "reply ACCEPT or REJECT", with no code in
    it at all. A first version of this predicate looked for a digit after
    the word and marked this one as needing nothing."""
    body = "New order #145, Rs.450 — reply ACCEPT or REJECT."
    assert asks_for_an_answer(body), body


# ---------------------------------------------- the ones that do not

def test_an_auto_confirmed_order_is_a_notice():
    body = "New order #145, Rs.450 — paid and auto-confirmed, no action needed."
    assert not asks_for_an_answer(body), body


def test_a_failed_refund_is_a_notice():
    body = ("Refund for order #145 (Rs.450) FAILED at Razorpay -- "
            "the customer is still owed this money.")
    assert not asks_for_an_answer(body), body


def test_a_velocity_alert_is_a_notice():
    """The one that made this visible: it says "No action needed" in as
    many words, under a badge saying she owed a reply."""
    body = ("[Amma's Kitchen AI Alert]\n"
            "Agent Jeet's Agent is ordering unusually fast and has been stopped.\n"
            "agent daily spend limit reached: Rs.1940 spent in the last 24h, "
            "this order Rs.110, limit Rs.2000.\n"
            "Nothing was charged. No action needed unless you expected this.")
    assert not asks_for_an_answer(body), body


def test_the_console_offers_a_quiet_badge_for_a_notice():
    page = MERCHANT.read_text(encoding="utf-8")
    assert "no reply needed" in page, "a notice has no badge of its own"
    assert "badge-mute" in page, "a notice must not be styled as work outstanding"
