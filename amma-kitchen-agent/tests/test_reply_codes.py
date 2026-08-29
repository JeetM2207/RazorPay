"""The single-use code every money-moving reply has to carry.

`reply_auth` proved the POST came from Twilio. That is not the same as
proving it came from the person we asked. A reply was authenticated by
caller ID -- spoofable -- and matched to an escalation on the last ten
digits, which is loose on purpose so a number typed in a browser matches
one Twilio delivers. Together those meant anyone who learned an order
number could approve it.

The code closes that. These tests are about the gate, not the routing:
the routing tests live in test_escalations.py and test_buyer_sms.py and
still pass unchanged, because the code gates the ACTION and does not
touch how a message is routed.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import audit_log
import buyer_sms
import escalations
import notification_service
import reply_auth
import reply_codes


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(notification_service, "TWILIO_CONFIGURED", False)
    notification_service.clear_outbox()
    escalations.reset()
    buyer_sms.reset()
    reply_codes.reset()
    import app
    return TestClient(app.app)


def _escalate(client, agent="agent-code", items=(("chicken_biryani", 2),)):
    body = client.post("/acp/checkout_sessions", json={
        "agent_id": agent,
        "items": [{"item_id": i, "qty": q} for i, q in items],
    }).json()
    assert body["status"] == "requires_human", body
    return escalations._PENDING[body["decision_detail"]["event_id"]]


def _reply(client, text, sender="+919876543210"):
    return client.post(
        "/webhook/sms-reply",
        data={"Body": text, "From": sender},
        headers={reply_auth.INTERNAL_TOKEN_HEADER: reply_auth.internal_token()},
    )


def _rows(client):
    return len(audit_log.get_all_events(db_path=audit_log.DEFAULT_DB_PATH, limit=500))


# ------------------------------------------------------ the code is real

def test_a_code_is_generated_and_carried_in_the_message(client):
    escalation = _escalate(client)
    assert len(escalation.code) == 4 and escalation.code.isdigit()

    body = notification_service.outbox()[0]["body"]
    assert f"Reply  1 {escalation.code}  to APPROVE" in body
    assert f"2 {escalation.code}  to REJECT" in body


def test_codes_differ_between_escalations(client):
    """Not proof of randomness -- `secrets.randbelow` is that -- but it
    would catch a constant or a counter."""
    seen = {_escalate(client, agent=f"agent-{n}").code for n in range(12)}
    assert len(seen) > 1


# ---------------------------------------------------------- verb + code

def test_the_right_verb_with_the_right_code_acts(client):
    escalation = _escalate(client)
    resp = _reply(client, f"1 {escalation.code}")
    assert "approved" in resp.text.lower()
    assert escalation.answered is True
    assert escalation.outcome == "APPROVED"


def test_the_right_verb_with_a_wrong_code_does_not_act(client):
    escalation = _escalate(client)
    wrong = "0000" if escalation.code != "0000" else "1111"
    before = _rows(client)

    resp = _reply(client, f"1 {wrong}")

    assert escalation.answered is False
    assert escalation.outcome is None
    assert "approved" not in resp.text.lower()
    assert _rows(client) == before, "a refused reply reached the audit log"


def test_the_right_verb_with_no_code_does_not_act(client):
    escalation = _escalate(client)
    before = _rows(client)

    resp = _reply(client, "1")

    assert escalation.answered is False
    assert resp.text == reply_codes.REASK
    assert _rows(client) == before


def test_a_code_from_one_escalation_cannot_resolve_another(client):
    """The actual attack this closes: knowing an order number is not
    enough, and neither is holding a code for a different order."""
    first = _escalate(client, agent="agent-A")
    second = _escalate(client, agent="agent-B", items=(("chicken_biryani", 2), ("gulab_jamun", 1)))
    assert first.code != second.code or True   # equality is possible; the id is what binds

    _reply(client, f"1 #{second.order_id} {first.code}")

    assert second.answered is False, "another order's code resolved this one"
    assert second.outcome is None


def test_replaying_a_used_code_does_not_act_twice(client):
    escalation = _escalate(client)
    assert "approved" in _reply(client, f"1 {escalation.code}").text.lower()

    outcome_after_first = escalation.outcome
    resp = _reply(client, f"2 {escalation.code}")

    assert escalation.outcome == outcome_after_first, "a replay changed the decision"
    assert "already" in resp.text.lower()


def test_an_expired_code_does_not_act(client):
    """The code dies with the question it belongs to -- one clock, in
    reply_codes, shared with buyer_sms rather than duplicated."""
    escalation = _escalate(client)
    stale = datetime.now(timezone.utc) - timedelta(seconds=reply_codes.TTL_SECONDS + 60)
    escalation.created_at = stale.isoformat()

    resp = _reply(client, f"1 {escalation.code}")

    assert escalation.answered is False
    assert "approved" not in resp.text.lower()


def test_an_unparseable_reply_still_asks_again_rather_than_guessing(client):
    _escalate(client)
    resp = _reply(client, "maybe later?")
    assert resp.status_code == 200
    assert "didn't understand" in resp.text


# ------------------------------------------------- no oracle to walk

def test_the_reask_is_rate_limited(client):
    """Four digits is 1 in 10,000 per guess, which is only thin if you
    are allowed 10,000 guesses."""
    escalation = _escalate(client)
    wrong = "0000" if escalation.code != "0000" else "1111"

    replies = [_reply(client, f"1 {wrong}").text for _ in range(reply_codes.MAX_REASKS + 2)]

    assert replies[0] == reply_codes.REASK
    assert replies[-1] == reply_codes.STONEWALL
    assert escalation.answered is False


def test_a_wrong_code_and_an_unknown_order_read_identically(client):
    """Answering them differently tells an attacker which order numbers
    are live, which is the thing the rate limit exists to deny."""
    escalation = _escalate(client)
    wrong = "0000" if escalation.code != "0000" else "1111"

    a = _reply(client, f"1 {wrong}").text
    reply_codes.reset()
    b = _reply(client, "1 #999999 1234").text

    assert a == b
    assert "999999" not in b


def test_a_refusal_never_names_the_order_or_the_code(client):
    escalation = _escalate(client)
    text = _reply(client, "1 0000" if escalation.code != "0000" else "1 1111").text
    assert escalation.code not in text.replace("4417", "")   # 4417 is the example in REASK
    assert str(escalation.order_id) not in text


# --------------------------------------------------- the customer's side

def _ask_approval(client):
    return buyer_sms.ask_approval(
        agent_id="agent-buyer", phone="+919876543210",
        cart_label="2x Chicken Biryani", total_inr=440, soft_cap_inr=400,
    )


def test_the_approval_question_carries_a_code(client):
    conversation = _ask_approval(client)
    body = notification_service.outbox()[-1]["body"]
    assert len(conversation.code) == 4
    assert f"Reply  YES {conversation.code}  to go ahead" in body
    assert f"NO {conversation.code}  to cancel" in body


def test_a_customer_approval_needs_its_code(client):
    conversation = _ask_approval(client)
    wrong = "0000" if conversation.code != "0000" else "1111"

    _reply(client, f"YES {wrong}")
    assert buyer_sms.status("agent-buyer")["decision"] is None

    _reply(client, f"YES {conversation.code}")
    assert buyer_sms.status("agent-buyer")["decision"] is True


def test_the_substitution_question_carries_a_code(client):
    conversation = buyer_sms.ask(
        agent_id="agent-sub", phone="+919876543210",
        original_request="2 pizzas", unmatched=["2 pizzas"],
        available=[{"id": "veg_thali", "title": "Veg Thali", "price_inr": 150}],
    )
    body = notification_service.outbox()[-1]["body"]
    assert f"starting with the code {conversation.code}" in body


def test_a_substitution_reply_is_still_a_request_and_not_an_authorisation(client):
    """The property this must not have quietly changed.

    A substitution answer re-enters the ordinary flow from the top: it is
    re-parsed against the catalog and re-checked by every gate. The code
    proves WHO is asking; it authorises nothing. What comes back is the
    customer's words, not a decision.
    """
    conversation = buyer_sms.ask(
        agent_id="agent-sub", phone="+919876543210",
        original_request="2 pizzas", unmatched=["2 pizzas"],
        available=[{"id": "veg_thali", "title": "Veg Thali", "price_inr": 150}],
    )

    result = buyer_sms.record_reply("+919876543210", f"{conversation.code} 2 veg thali")

    assert result["handled"] is True
    # The stored reply is a REQUEST -- the customer's own words, with the
    # code taken out so it is not read as a quantity -- and carries no
    # decision, no total and no approval of any kind.
    assert buyer_sms.status("agent-sub")["reply"] == "2 veg thali"
    assert buyer_sms.status("agent-sub")["decision"] is None
    for word in ("APPROVE", "APPROVED", "authorised", "confirmed"):
        assert word not in str(result)


def test_the_code_is_stripped_before_the_reply_is_read_as_an_order(client):
    """"4417 2 dosas" is an order for two dosas, not 4417 of anything."""
    conversation = buyer_sms.ask(
        agent_id="agent-sub", phone="+919876543210",
        original_request="2 pizzas", unmatched=["2 pizzas"],
        available=[{"id": "masala_dosa", "title": "Masala Dosa", "price_inr": 80}],
    )
    buyer_sms.record_reply("+919876543210", f"{conversation.code} 2 masala dosa")
    assert buyer_sms.status("agent-sub")["reply"] == "2 masala dosa"


# --------------------------------------------------- routing is unchanged

def test_an_explicit_order_number_is_still_the_first_router_branch(client):
    """The code gates the action; it does not route. A "#<order>" reply
    still names its order outright, exactly as before."""
    first = _escalate(client, agent="agent-A")
    second = _escalate(client, agent="agent-B", items=(("chicken_biryani", 2), ("gulab_jamun", 1)))

    _reply(client, f"2 #{second.order_id} {second.code}")

    assert second.outcome == "REJECTED"
    assert first.answered is False, "the named order was not the one that moved"


def test_the_parser_is_still_a_regex_with_no_model(client):
    source = open(escalations.__file__, encoding="utf-8").read()
    body = source[source.index("def parse_reply"):source.index("def _adapter_for")]
    for forbidden in ("llm_client", "openai", "anthropic", "completion", "prompt"):
        assert forbidden not in body, f"the reply parser reaches for {forbidden}"


def test_one_expiry_clock_not_two(client):
    """The code has to die with the question. A second TTL would drift."""
    assert buyer_sms.CONVERSATION_TTL_SECONDS is reply_codes.TTL_SECONDS
