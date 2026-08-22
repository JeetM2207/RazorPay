import pytest
from fastapi.testclient import TestClient

import adapter_acp
import adapter_ap2
import adapter_x402
import app as unified
import audit_log
import escalations
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
    return TestClient(unified.app)


def _escalate(client, agent_id="sms-buyer", item="chicken_biryani", qty=2):
    return client.post(
        "/acp/checkout_sessions",
        json={"agent_id": agent_id, "items": [{"item_id": item, "qty": qty}]},
    ).json()


def _reply(client, body, sender="+919876543210"):
    return client.post("/webhook/sms-reply", data={"Body": body, "From": sender})


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
    assert "Reply '1' to APPROVE, '2' to REJECT." in text


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
    resp = _reply(client, "1")
    assert resp.status_code == 200
    assert "Nothing is waiting" in resp.text


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
    resp = _reply(client, "1 #999999")
    assert "No order #999999" in resp.text


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
