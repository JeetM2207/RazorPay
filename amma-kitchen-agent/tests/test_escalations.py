import re

import pytest

import merchant_auth
from fastapi.testclient import TestClient

import adapter_acp
import adapter_ap2
import adapter_x402
import app as unified
import audit_log
import buyer_sms
import escalations
import reply_codes
import notification_service


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_log, "DEFAULT_DB_PATH", str(tmp_path / "audit.db"))
    monkeypatch.setattr(notification_service, "TWILIO_CONFIGURED", False)
    notification_service.clear_outbox()
    escalations.reset()
    adapter_acp._SESSIONS.clear()
    adapter_ap2._INTENT_MANDATES.clear()
    adapter_ap2._CART_MANDATES.clear()
    adapter_x402._ORDERS.clear()
    adapter_x402._CHALLENGES.clear()
    client = TestClient(unified.app)
    # These tests drive merchant surfaces, which now need a login. The
    # session is minted directly rather than posted through the form,
    # because the login itself is what tests/test_merchant_auth.py is
    # for -- and leaving these anonymous would only prove the guard
    # fires, which is already covered there.
    client.cookies.set(merchant_auth.COOKIE_NAME, merchant_auth.issue_cookie())
    return client


def _escalate(client, agent_id="sms-buyer", item="chicken_biryani", qty=2):
    return client.post(
        "/acp/checkout_sessions",
        json={"agent_id": agent_id, "items": [{"item_id": item, "qty": qty}]},
    ).json()


def _reply(client, body, sender="+919876543210", code=None):
    """Post a reply the way a console reply box does.

    Two things are supplied for the caller, because neither is what these
    tests are about:

    * /webhook/sms-reply is authenticated, so this goes through the same
      internal-token door the consoles use rather than the endpoint being
      left open for tests. Covered in tests/test_reply_auth.py.
    * A decision needs the single-use code from its message, so this
      appends the right one -- which is exactly what a person reading
      their phone does. Pass code="" to send one deliberately without it;
      the cases that are ABOUT the code live in tests/test_reply_codes.py.

    "The right one" means the escalation the reply actually names: a
    "#41" reply must carry #41's code, not the oldest one's, or targeted
    routing would silently start failing the code check instead.
    """
    import reply_auth

    if code is None:
        named = re.search(r"#(\d+)", body or "")
        if named:
            target = escalations._PENDING.get(int(named.group(1)))
        else:
            # Falls back to the most recently answered one so a REPLAY can
            # actually be sent: without it a second reply carries no code
            # and gets refused for the wrong reason, hiding whether
            # single-use works at all.
            target = escalations._oldest_unanswered() or escalations._most_recently_answered()
        code = target.code if target else ""

    if code and _CARRIES_DECISION.match(body or "") and code not in body:
        body = f"{body} {code}"

    return client.post(
        "/webhook/sms-reply",
        data={"Body": body, "From": sender},
        headers={reply_auth.INTERNAL_TOKEN_HEADER: reply_auth.internal_token()},
    )


# A reply that is a decision -- optionally with a #order on either side --
# and so the only shape a code should be appended to. Prose is left alone,
# because a test sending prose is testing that prose is not understood.
_CARRIES_DECISION = re.compile(
    r"^\s*(?:#\d+\s*[:,\-]?\s*)?"
    r"(1|2|approve[d]?|accept|yes|ok|reject[ed]?|decline[d]?|no)"
    r"\s*(?:[:,\-]?\s*#\d+)?\s*[.!]?\s*$",
    re.IGNORECASE,
)


# ------------------------------------------------------------ the parser

@pytest.mark.parametrize(
    "text,expected",
    [
        ("1", "APPROVE"),
        (" 1 ", "APPROVE"),
        ("APPROVE", "APPROVE"),
        ("approve", "APPROVE"),
        ("yes", "APPROVE"),
        ("Ok", "APPROVE"),
        ("2", "REJECT"),
        ("reject", "REJECT"),
        ("No", "REJECT"),
        ("decline", "REJECT"),
        ("1.", "APPROVE"),
    ],
)
def test_parser_understands_the_intended_replies(text, expected):
    assert escalations.parse_reply(text).action == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "12",                       # not 1, and not 2
        "3",
        "maybe",
        "1 or 2?",
        "approve 2",                # contradicts itself
        "call me",
        "1234567",
        "I would like to approve this order please",
    ],
)
def test_parser_refuses_to_guess_at_ambiguous_replies(text):
    """A wrong guess here approves or refuses somebody's money, so
    anything not unambiguous must come back as unparseable."""
    assert escalations.parse_reply(text).action is None


def test_parser_extracts_an_explicit_order_reference():
    assert escalations.parse_reply("#42 1").order_id == 42
    assert escalations.parse_reply("1 #42").order_id == 42
    assert escalations.parse_reply("1").order_id is None


# ------------------------------------------------------------- the alert

def test_escalation_sends_an_sms_in_the_specified_format(client):
    body = _escalate(client)
    assert body["status"] == "requires_human"

    messages = notification_service.outbox()
    assert len(messages) == 1
    text = messages[0]["body"]
    assert text.startswith("[Amma's Kitchen AI Alert]")
    assert f"Order #{body['decision_detail']['event_id']}" in text
    assert "sms-buyer" in text
    assert "2x Chicken Biryani (Rs.440)" in text
    # The code is the only thing standing between "anyone who learned an
    # order number" and an approved order, so the message is the one place
    # it appears and the wording is asserted rather than assumed.
    code = escalations._oldest_unanswered().code
    assert len(code) == 4 and code.isdigit()
    assert f"Reply  1 {code}  to APPROVE  or  2 {code}  to REJECT." in text


def test_an_approved_order_does_not_text_anyone(client):
    client.post(
        "/acp/checkout_sessions",
        json={"agent_id": "quiet", "items": [{"item_id": "masala_dosa", "qty": 1}]},
    )
    assert notification_service.outbox() == []


def test_repeated_polling_does_not_resend_the_alert(client):
    """A buyer waiting on a verdict must not be able to spam Amma's phone."""
    _escalate(client, agent_id="poller")
    for _ in range(4):
        adapter_x402_orders = client.post(
            "/x402/orders",
            json={"agent_id": "poller-x", "items": [{"item_id": "chicken_biryani", "qty": 2}]},
        )
        assert adapter_x402_orders.status_code == 200
    assert len(notification_service.outbox()) == 2  # one acp, one x402


# ---------------------------------------------------------- the decision

def test_reply_1_approves_and_unblocks_the_buyer(client, monkeypatch):
    body = _escalate(client)
    session_id = body["session_id"]

    resp = _reply(client, "1")
    assert resp.status_code == 200
    assert "approved" in resp.text.lower()

    session = client.get(f"/acp/checkout_sessions/{session_id}").json()
    assert session["status"] == "ready_for_payment"
    assert session["delegate_token"]


def test_reply_2_rejects_and_closes_the_order(client):
    body = _escalate(client)
    session_id = body["session_id"]

    resp = _reply(client, "2")
    assert resp.status_code == 200
    assert "rejected" in resp.text.lower()

    session = client.get(f"/acp/checkout_sessions/{session_id}").json()
    assert session["status"] == "rejected"


def test_unrecognized_reply_changes_nothing(client):
    body = _escalate(client)
    session_id = body["session_id"]

    resp = _reply(client, "what is this")
    assert resp.status_code == 200
    assert "didn't understand" in resp.text

    session = client.get(f"/acp/checkout_sessions/{session_id}").json()
    assert session["status"] == "requires_human", "an unparseable reply must not decide anything"


def test_sms_cannot_approve_what_the_console_cannot(client):
    """A disallowed category is a hard merchant rule. Replying '1' must
    not become a back door around the 403 the web console gives."""
    body = _escalate(client, agent_id="violator", item="party_catering_tray", qty=1)
    session_id = body["session_id"]

    resp = _reply(client, "1")
    assert "cannot be approved" in resp.text

    session = client.get(f"/acp/checkout_sessions/{session_id}").json()
    assert session["status"] == "requires_human"
    assert session["delegate_token"] is None


def test_replying_twice_does_not_re_decide(client):
    _escalate(client)
    first = _reply(client, "1")
    second = _reply(client, "2")

    assert "approved" in first.text.lower()
    assert "already" in second.text.lower()


def test_reply_with_no_pending_escalation_is_handled(client):
    # This used to answer "Nothing is waiting for a decision right now",
    # which is a small oracle: it tells an unknown sender whether anything
    # is pending. Every failure now reads the same.
    resp = _reply(client, "1")
    assert resp.status_code == 200
    assert resp.text == reply_codes.REASK


def test_targeted_reply_resolves_the_named_order(client):
    first = _escalate(client, agent_id="buyer-a")
    second = _escalate(client, agent_id="buyer-b")
    second_order = second["decision_detail"]["event_id"]

    _reply(client, f"2 #{second_order}")

    assert client.get(f"/acp/checkout_sessions/{second['session_id']}").json()["status"] == "rejected"
    # The older one is untouched, even though a bare reply would have hit it first.
    assert client.get(f"/acp/checkout_sessions/{first['session_id']}").json()["status"] == "requires_human"


def test_bare_reply_answers_the_oldest_waiting_order(client):
    first = _escalate(client, agent_id="buyer-a")
    second = _escalate(client, agent_id="buyer-b")

    _reply(client, "1")

    assert client.get(f"/acp/checkout_sessions/{first['session_id']}").json()["status"] == "ready_for_payment"
    assert client.get(f"/acp/checkout_sessions/{second['session_id']}").json()["status"] == "requires_human"


def test_reply_for_an_unknown_order_is_reported(client):
    _escalate(client)
    # Deliberately no longer "No order #999999": confirming which order
    # numbers exist is exactly the oracle the code is here to close.
    resp = _reply(client, "1 #999999")
    assert resp.text in (reply_codes.REASK, reply_codes.STONEWALL)
    assert "999999" not in resp.text


# ----------------------------------------------------- across protocols

@pytest.mark.parametrize("protocol", ["acp", "ap2", "x402"])
def test_sms_resolves_escalations_from_every_protocol(client, protocol):
    if protocol == "acp":
        client.post(
            "/acp/checkout_sessions",
            json={"agent_id": "multi", "items": [{"item_id": "chicken_biryani", "qty": 2}]},
        )
    elif protocol == "ap2":
        client.post(
            "/ap2/intent-mandates",
            json={"agent_id": "multi", "intent": {"items": [{"item_id": "chicken_biryani", "qty": 2}]}},
        )
    else:
        client.post(
            "/x402/orders",
            json={"agent_id": "multi", "items": [{"item_id": "chicken_biryani", "qty": 2}]},
        )

    assert len(escalations.pending()) == 1
    resp = _reply(client, "1")
    assert "approved" in resp.text.lower()
    assert escalations.pending()[0]["outcome"] == "APPROVED"


def test_sms_state_endpoint_reports_transport_and_queue(client):
    _escalate(client)
    body = client.get("/api/sms").json()
    assert body["transport"] == "mock"
    assert len(body["outbox"]) == 1
    assert body["escalations"][0]["answered"] is False


def test_a_transport_failure_never_breaks_the_order(client, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("carrier down")

    monkeypatch.setattr(notification_service, "send_sms", explode)

    body = _escalate(client, agent_id="resilient")
    assert body["status"] == "requires_human", "the order must still be recorded and resolvable"


# ------------------------- who a message was written for, and its buttons

def test_the_outbox_records_which_side_each_message_asks(monkeypatch):
    """In a demo both parties are usually the same phone number, so the
    recipient cannot tell an escalation from a customer question. The two
    are asked in deliberately different vocabularies -- 1/2 for the
    merchant, YES/NO for the customer -- and a console that shows one
    side's message beside the other side's buttons invites a reply that
    answers nobody."""
    monkeypatch.setattr(notification_service, "TWILIO_CONFIGURED", False)
    notification_service.clear_outbox()
    escalations.reset()

    escalations.notify("acp", "s1", {
        "event_id": 42, "agent_id": "agent-x", "total_inr": 440,
        "reason": "total Rs.440 at/above human confirmation threshold Rs.400",
    }, [("chicken_biryani", 2)])
    buyer_sms.ask_approval(agent_id="agent-x", phone="8306610707",
                           cart_label="2x Paneer Bhurji", total_inr=300, soft_cap_inr=300)

    outbox = notification_service.outbox()
    assert [m["audience"] for m in outbox] == ["customer", "merchant"]
    # Same number on both, which is exactly why the label is needed.
    assert outbox[0]["to"].endswith("8306610707")
    assert outbox[1]["to"].endswith("8306610707")


def test_an_unlabelled_send_is_treated_as_the_merchants(monkeypatch):
    """The default recipient is hers -- a send with no `to` goes to
    MERCHANT_PHONE -- so the default audience matches."""
    monkeypatch.setattr(notification_service, "TWILIO_CONFIGURED", False)
    notification_service.clear_outbox()

    notification_service.send_sms("kitchen, an order needs you")
    assert notification_service.outbox()[0]["audience"] == "merchant"


def test_an_order_under_her_threshold_never_asks_the_merchant(monkeypatch):
    """The bug this came from: a Rs.300 order sat under her Rs.400
    threshold, so only the CUSTOMER was ever asked. The merchant console
    showed that message on 'Amma's phone' and offered her 1/2 buttons for
    a question nobody had asked her."""
    monkeypatch.setattr(notification_service, "TWILIO_CONFIGURED", False)
    notification_service.clear_outbox()
    escalations.reset()

    buyer_sms.ask_approval(agent_id="agent-x", phone="8306610707",
                           cart_label="2x Paneer Bhurji", total_inr=300, soft_cap_inr=300)

    assert escalations.pending() == [], "the merchant was never asked"
    assert [m["audience"] for m in notification_service.outbox()] == ["customer"]
