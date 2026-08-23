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


def _inbound(client, body, sender=BUYER_E164):
    return client.post("/webhook/sms-reply", data={"Body": body, "From": sender})


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


def test_status_is_404_when_nothing_was_asked(client):
    assert client.get("/api/buyer-sms/status/never-asked").status_code == 404


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
