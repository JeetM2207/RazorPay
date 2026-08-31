import pytest
from fastapi.testclient import TestClient

import adapter_acp
import app as unified
import audit_log
import buyer_sms
import escalations
import notification_service

BUYER_PHONE = "98765 43210"
BUYER_E164 = "+919876543210"
MERCHANT_PHONE = "+919000000001"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_log, "DEFAULT_DB_PATH", str(tmp_path / "audit.db"))
    monkeypatch.setattr(notification_service, "TWILIO_CONFIGURED", False)
    notification_service.clear_outbox()
    buyer_sms.reset()
    escalations.reset()
    adapter_acp._SESSIONS.clear()
    return TestClient(unified.app)


def _ask(client, agent_id="agent-1", phone=BUYER_PHONE, unmatched=None):
    return client.post(
        "/api/buyer-sms/ask",
        json={
            "agent_id": agent_id,
            "phone": phone,
            "original_request": "2 pizzas",
            "unmatched": unmatched if unmatched is not None else ["2 pizzas"],
        },
    )


def _inbound(client, body, sender=BUYER_E164, code=None):
    """Post a reply the way a console reply box does.

    Two things are supplied for the caller, because neither is what these
    routing tests are about:

    * /webhook/sms-reply is authenticated, so this goes through the same
      internal-token door the consoles use. See tests/test_reply_auth.py.
    * A reply needs the single-use code from its message, so this appends
      the right one -- which is what a person reading their phone does.

    "The right one" has to be chosen the same way the router chooses,
    because on this shared number BOTH a customer question and a merchant
    escalation can be open at once: a decision-shaped reply carries the
    escalation's code, anything else carries the conversation's. Getting
    that wrong here would make every routing test fail on the code check
    instead of exercising the routing.

    Pass code="" to send one deliberately without a code; the cases that
    are ABOUT the code live in tests/test_reply_codes.py.
    """
    import re

    import reply_auth

    if code is None:
        # Mirrors the router's own `prefer_buyer` condition rather than
        # guessing from the shape of the message, because the router
        # decides by RECENCY too: with both a merchant escalation and a
        # newer customer question open, a bare "1" belongs to the
        # customer. A helper that disagreed would make these tests fail
        # on the code check instead of exercising the routing they exist
        # to cover.
        named = re.search(r"#(\d+)", body or "")
        waiting = escalations._oldest_unanswered() or escalations._most_recently_answered()
        asked_at = buyer_sms.open_question_asked_at(sender)

        prefer_buyer = asked_at is not None and named is None and (
            waiting is None
            or (buyer_sms.reply_suits_open_question(sender, body)
                and asked_at > waiting.created_at)
        )

        if named:
            target = escalations._PENDING.get(int(named.group(1)))
        elif prefer_buyer:
            target = buyer_sms._find_open(sender)
        else:
            target = waiting or buyer_sms._find_open(sender)
        code = getattr(target, "code", "") or ""

    if code and code not in (body or ""):
        body = f"{body} {code}" if body else code

    return client.post(
        "/webhook/sms-reply",
        data={"Body": body, "From": sender},
        headers={reply_auth.INTERNAL_TOKEN_HEADER: reply_auth.internal_token()},
    )


# ------------------------------------------------------ phone handling

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("98765 43210", "+919876543210"),
        ("9876543210", "+919876543210"),
        ("+91 98765 43210", "+919876543210"),
        ("098765 43210", "+919876543210"),
        ("919876543210", "+919876543210"),
        ("whatsapp:+919876543210", "+919876543210"),
        ("+1 415 523 8886", "+14155238886"),
        ("", ""),
    ],
)
def test_phone_numbers_are_normalised_however_they_are_typed(raw, expected):
    assert buyer_sms.normalise_phone(raw) == expected


def test_a_reply_matches_the_number_despite_formatting(client):
    _ask(client)
    # Twilio delivers WhatsApp senders prefixed; the local form was saved.
    resp = _inbound(client, "one masala dosa", sender="whatsapp:+919876543210")
    assert "Got it" in resp.text
    assert buyer_sms.status("agent-1")["reply"] == "one masala dosa"


def test_asking_without_a_phone_number_is_refused(client):
    resp = _ask(client, phone="")
    assert resp.status_code == 400
    assert "phone number" in resp.json()["detail"]


# --------------------------------------------------------- the message

def test_the_customer_is_told_what_is_missing_and_what_exists(client):
    _ask(client, unmatched=["2 pizzas", "a coke"])
    body = notification_service.outbox()[0]["body"]

    assert "don't have 2 pizzas, a coke" in body
    assert "Veg Thali" in body
    assert "Reply with what you'd like instead" in body


def test_the_message_goes_only_to_the_number_on_file(client):
    _ask(client, phone="99999 11111")
    assert notification_service.outbox()[0]["to"] == "+919999911111"


def test_in_person_only_dishes_are_not_offered_as_alternatives(client):
    """Suggesting something the agent then can't buy would send the
    customer straight into a refusal."""
    _ask(client)
    assert "Party Catering Tray" not in notification_service.outbox()[0]["body"]


# ------------------------------------------------------------ the reply

def test_the_reply_is_captured_and_handed_over_once(client):
    _ask(client)
    _inbound(client, "one masala dosa and a coffee")

    state = client.get("/api/buyer-sms/status/agent-1").json()
    assert state["answered"] is True
    assert state["consumed"] is False

    first = client.post("/api/buyer-sms/consume/agent-1")
    assert first.json()["reply"] == "one masala dosa and a coffee"

    second = client.post("/api/buyer-sms/consume/agent-1")
    assert second.status_code == 409, "a reply must not be reusable on a later run"


def test_an_empty_reply_asks_again_rather_than_ordering_nothing(client):
    _ask(client)
    resp = _inbound(client, "   ")
    assert "didn't catch that" in resp.text
    assert buyer_sms.status("agent-1")["answered"] is False


def test_status_reports_no_open_question_rather_than_erroring(client):
    """"Nothing to answer" is the NORMAL state, not a failure.

    The buyer console polls this from page load now, so that it can
    surface a question raised by a standing order while nobody was
    watching. Answering the idle case with a 404 filled the browser
    console with red on a page where nothing was wrong -- and that is
    exactly where somebody looks for a real problem mid-demo.
    """
    body = client.get("/api/buyer-sms/status/nobody").json()
    assert body["open"] is False
    assert body.get("code") in (None, "")

def test_a_reply_from_an_unrelated_number_is_not_taken_as_the_answer(client):
    _ask(client)
    _inbound(client, "one masala dosa", sender="+919111122223")
    assert buyer_sms.status("agent-1")["answered"] is False


# ------------------------------------ routing when one number does both

def _escalate(client):
    return client.post(
        "/acp/checkout_sessions",
        json={"agent_id": "esc-buyer", "items": [{"item_id": "chicken_biryani", "qty": 2}]},
    ).json()


def test_a_bare_1_still_decides_a_waiting_escalation(client):
    """Even with a customer question open on the same number, '1' means
    the merchant deciding -- getting that wrong would move money."""
    session = _escalate(client)
    _ask(client)

    resp = _inbound(client, "1")

    assert "approved" in resp.text.lower()
    assert client.get(f"/acp/checkout_sessions/{session['session_id']}").json()["status"] == "ready_for_payment"
    assert buyer_sms.status("agent-1")["answered"] is False, "the customer question was consumed by mistake"


def test_free_text_goes_to_the_customer_question_not_the_escalation(client):
    session = _escalate(client)
    _ask(client)

    resp = _inbound(client, "make it one dosa instead")

    assert "Got it" in resp.text
    assert buyer_sms.status("agent-1")["reply"] == "make it one dosa instead"
    assert client.get(f"/acp/checkout_sessions/{session['session_id']}").json()["status"] == "requires_human"


def test_a_bare_1_with_no_escalation_waiting_is_a_customer_reply(client):
    """Nothing is waiting on a merchant decision, so '1' can only be the
    customer answering -- perhaps picking the first thing on the list."""
    _ask(client)
    resp = _inbound(client, "1")
    assert "Got it" in resp.text
    assert buyer_sms.status("agent-1")["reply"] == "1"


def test_an_unrecognised_message_with_nothing_open_explains_itself(client):
    resp = _inbound(client, "hello?")
    assert "didn't understand" in resp.text


# -------------------------------------------- soft-cap approval by SMS

def _ask_approval(client, agent_id="agent-1", phone=BUYER_PHONE, total=440):
    return client.post(
        "/api/buyer-sms/approve",
        json={
            "agent_id": agent_id,
            "phone": phone,
            "cart_label": "2x Chicken Biryani",
            "total_inr": total,
            "soft_cap_inr": 400,
        },
    )


def test_a_soft_cap_order_asks_the_customer_on_whatsapp(client):
    _ask_approval(client)
    body = notification_service.outbox()[0]["body"]

    assert "2x Chicken Biryani for Rs.440" in body
    assert "above the Rs.400 you asked to be checked on" in body
    # The code is the only thing proving this reply came from the person
    # we asked, so it is in the message and asserted here.
    code = buyer_sms._find_open(BUYER_E164).code
    assert len(code) == 4 and code.isdigit()
    assert f"Reply  YES {code}  to go ahead, or  NO {code}  to cancel." in body


@pytest.mark.parametrize("word", ["YES", "yes", "y", "ok", "approve", "sure", "1"])
def test_approval_words_are_understood(client, word):
    _ask_approval(client)
    resp = _inbound(client, word)
    assert "going ahead" in resp.text
    assert buyer_sms.status("agent-1")["decision"] is True


@pytest.mark.parametrize("word", ["NO", "no", "n", "cancel", "stop", "2"])
def test_refusal_words_are_understood(client, word):
    _ask_approval(client)
    resp = _inbound(client, word)
    assert "Cancelled" in resp.text
    assert buyer_sms.status("agent-1")["decision"] is False


@pytest.mark.parametrize("word", ["maybe", "what?", "yes no", "", "later"])
def test_an_ambiguous_approval_reply_decides_nothing(client, word):
    """This answer authorises a charge, so anything unclear must be asked
    again rather than guessed at."""
    _ask_approval(client)
    resp = _inbound(client, word)
    assert "didn't understand" in resp.text
    assert buyer_sms.status("agent-1")["decision"] is None
    assert buyer_sms.status("agent-1")["answered"] is False


def test_approval_asks_are_refused_without_a_phone(client):
    resp = _ask_approval(client, phone="")
    assert resp.status_code == 400


# ------------------------- two open questions on one number, by recency

def test_the_newest_question_gets_the_answer(client):
    """A person replying to their phone is answering the message they
    just received, not one from earlier."""
    session = _escalate(client)                     # merchant asked first
    _ask_approval(client)                           # customer asked second

    resp = _inbound(client, "1")

    # "1" reads as approval of the customer's own order, the newer ask.
    assert buyer_sms.status("agent-1")["decision"] is True
    assert client.get(f"/acp/checkout_sessions/{session['session_id']}").json()["status"] == "requires_human"
    assert "going ahead" in resp.text


def test_an_explicit_order_number_still_reaches_the_merchant_queue(client):
    """Naming an order overrides recency -- it says exactly what is meant."""
    session = _escalate(client)
    order_id = session["decision_detail"]["event_id"]
    _ask_approval(client)

    resp = _inbound(client, f"1 #{order_id}")

    assert "approved" in resp.text.lower()
    assert client.get(f"/acp/checkout_sessions/{session['session_id']}").json()["status"] == "ready_for_payment"
    assert buyer_sms.status("agent-1")["answered"] is False


def test_an_older_customer_question_yields_to_a_newer_escalation(client):
    _ask_approval(client)                           # customer asked first
    session = _escalate(client)                     # merchant asked second

    _inbound(client, "1")

    assert client.get(f"/acp/checkout_sessions/{session['session_id']}").json()["status"] == "ready_for_payment"
    assert buyer_sms.status("agent-1")["answered"] is False


# ---------------------------------------------------- end to end shape

def test_the_whole_loop_leaves_an_orderable_request(client):
    _ask(client, unmatched=["2 pizzas"])
    _inbound(client, "one masala dosa")
    reply = client.post("/api/buyer-sms/consume/agent-1").json()["reply"]

    # What comes back is a REQUEST, never an authorisation -- it still has
    # to clear the buyer's own mandate before any merchant sees it.
    gate = client.post(
        "/api/buyer-check",
        json={
            "items": [{"item_id": "masala_dosa", "qty": 1}],
            "spend_cap_inr": 600,
            "confirm_above_inr": 300,
        },
    ).json()
    assert reply == "one masala dosa"
    assert gate["decision"] == "PROCEED"
