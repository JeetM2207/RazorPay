import pytest
from fastapi.testclient import TestClient

import adapter_acp
import adapter_ap2
import audit_log


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_log, "DEFAULT_DB_PATH", str(tmp_path / "audit.db"))
    adapter_ap2._INTENT_MANDATES.clear()
    adapter_ap2._CART_MANDATES.clear()
    return TestClient(adapter_ap2.app)


def _mock_payment_link(monkeypatch, link_id="plink_test", short_url="https://rzp.io/rzp/test"):
    fake_link = {"id": link_id, "short_url": short_url}
    monkeypatch.setattr(
        adapter_ap2.orchestrator.razorpay_client, "create_payment_link", lambda **kwargs: fake_link
    )
    return fake_link


def test_adapter_ap2_shares_the_same_orchestrator_module_as_adapter_acp():
    # The whole point of a second adapter: it reuses orchestrator.py (and
    # therefore negotiation.py) completely unchanged -- not a copy, the
    # exact same imported module object.
    assert adapter_ap2.orchestrator is adapter_acp.orchestrator


def test_full_approve_and_pay_flow(client, monkeypatch):
    fake_link = _mock_payment_link(monkeypatch)

    resp = client.post(
        "/ap2/intent-mandates",
        json={"agent_id": "buyer-b1", "intent": {"items": [{"item_id": "masala_dosa", "qty": 1}]}},
    )
    mandate = resp.json()["intent_mandate"]
    assert resp.status_code == 200
    assert mandate["status"] == "cart_ready"

    cart_mandate = client.post(f"/ap2/intent-mandates/{mandate['id']}/cart-mandate").json()["cart_mandate"]
    assert cart_mandate["total_inr"] == 80

    payment_mandate = client.post(
        f"/ap2/cart-mandates/{cart_mandate['id']}/payment-mandate"
    ).json()["payment_mandate"]
    assert payment_mandate["payment_link_url"] == fake_link["short_url"]
    assert len(payment_mandate["matched_mandate_hash"]) == 64


def test_escalated_intent_cannot_lock_a_cart(client):
    resp = client.post(
        "/ap2/intent-mandates",
        json={"agent_id": "buyer-b2", "intent": {"items": [{"item_id": "chicken_biryani", "qty": 2}]}},
    )
    mandate = resp.json()["intent_mandate"]
    assert mandate["status"] == "requires_human"

    lock_attempt = client.post(f"/ap2/intent-mandates/{mandate['id']}/cart-mandate")
    assert lock_attempt.status_code == 409


def test_counter_offer_then_accept_alternative(client, monkeypatch):
    _mock_payment_link(monkeypatch)

    resp = client.post(
        "/ap2/intent-mandates",
        json={
            "agent_id": "buyer-b3",
            "intent": {
                "items": [{"item_id": "chicken_biryani", "qty": 2}, {"item_id": "masala_dosa", "qty": 1}]
            },
        },
    )
    mandate = resp.json()["intent_mandate"]
    assert mandate["status"] == "countered"
    assert len(mandate["decision_detail"]["alternatives"]) >= 1

    accepted = client.post(
        f"/ap2/intent-mandates/{mandate['id']}/accept-alternative", json={"index": 0}
    ).json()["intent_mandate"]
    assert accepted["status"] == "cart_ready"


def test_cart_mandate_cannot_be_used_twice(client, monkeypatch):
    _mock_payment_link(monkeypatch)

    mandate = client.post(
        "/ap2/intent-mandates",
        json={"agent_id": "buyer-b4", "intent": {"items": [{"item_id": "masala_dosa", "qty": 1}]}},
    ).json()["intent_mandate"]
    cart_mandate = client.post(f"/ap2/intent-mandates/{mandate['id']}/cart-mandate").json()["cart_mandate"]

    first = client.post(f"/ap2/cart-mandates/{cart_mandate['id']}/payment-mandate")
    assert first.status_code == 200
    second = client.post(f"/ap2/cart-mandates/{cart_mandate['id']}/payment-mandate")
    assert second.status_code == 409


def test_human_confirm_allows_payment_after_threshold_escalation(client, monkeypatch):
    _mock_payment_link(monkeypatch, link_id="plink_override", short_url="https://rzp.io/rzp/override")

    mandate = client.post(
        "/ap2/intent-mandates",
        json={"agent_id": "buyer-b5", "intent": {"items": [{"item_id": "chicken_biryani", "qty": 2}]}},
    ).json()["intent_mandate"]
    assert mandate["status"] == "requires_human"

    confirmed = client.post(f"/ap2/intent-mandates/{mandate['id']}/human-confirm").json()["intent_mandate"]
    assert confirmed["status"] == "cart_ready"
    assert "human override" in confirmed["decision_detail"]["reason"]

    cart_mandate = client.post(f"/ap2/intent-mandates/{mandate['id']}/cart-mandate").json()["cart_mandate"]
    payment_mandate = client.post(
        f"/ap2/cart-mandates/{cart_mandate['id']}/payment-mandate"
    ).json()["payment_mandate"]
    assert payment_mandate["payment_link_url"] == "https://rzp.io/rzp/override"


def test_human_confirm_rejects_hard_merchant_rule_escalation(client):
    mandate = client.post(
        "/ap2/intent-mandates",
        json={"agent_id": "buyer-b6", "intent": {"items": [{"item_id": "not_a_real_item", "qty": 1}]}},
    ).json()["intent_mandate"]
    assert mandate["status"] == "requires_human"

    resp = client.post(f"/ap2/intent-mandates/{mandate['id']}/human-confirm")
    assert resp.status_code == 403


def test_human_confirm_with_reduced_cart_approves_smaller_order(client, monkeypatch):
    _mock_payment_link(monkeypatch)

    mandate = client.post(
        "/ap2/intent-mandates",
        json={"agent_id": "buyer-b7", "intent": {"items": [{"item_id": "chicken_biryani", "qty": 2}]}},
    ).json()["intent_mandate"]

    reduced = client.post(
        f"/ap2/intent-mandates/{mandate['id']}/human-confirm",
        json={"items": [{"item_id": "chicken_biryani", "qty": 1}]},
    ).json()["intent_mandate"]
    assert reduced["status"] == "cart_ready"
    assert reduced["decision_detail"]["total_inr"] == 220
    assert "override" not in reduced["decision_detail"]["reason"]


def test_human_confirm_with_reduced_cart_that_still_escalates_is_rejected(client):
    mandate = client.post(
        "/ap2/intent-mandates",
        json={"agent_id": "buyer-b8", "intent": {"items": [{"item_id": "chicken_biryani", "qty": 2}]}},
    ).json()["intent_mandate"]

    resp = client.post(
        f"/ap2/intent-mandates/{mandate['id']}/human-confirm",
        json={"items": [{"item_id": "chicken_biryani", "qty": 2}, {"item_id": "veg_thali", "qty": 1}]},
    )
    assert resp.status_code == 409


def test_human_reject_closes_intent_without_payment(client):
    mandate = client.post(
        "/ap2/intent-mandates",
        json={"agent_id": "buyer-b9", "intent": {"items": [{"item_id": "chicken_biryani", "qty": 2}]}},
    ).json()["intent_mandate"]

    rejected = client.post(f"/ap2/intent-mandates/{mandate['id']}/human-reject").json()["intent_mandate"]
    assert rejected["status"] == "rejected"
    assert "human rejected" in rejected["decision_detail"]["reason"]

    confirm_after_reject = client.post(f"/ap2/intent-mandates/{mandate['id']}/human-confirm")
    assert confirm_after_reject.status_code == 409
    lock_after_reject = client.post(f"/ap2/intent-mandates/{mandate['id']}/cart-mandate")
    assert lock_after_reject.status_code == 409


def test_accept_upsell_extends_cart_and_stays_ready(client, monkeypatch):
    _mock_payment_link(monkeypatch)

    mandate = client.post(
        "/ap2/intent-mandates",
        json={"agent_id": "buyer-b10", "intent": {"items": [{"item_id": "masala_dosa", "qty": 1}]}},
    ).json()["intent_mandate"]
    upsell = mandate["decision_detail"].get("upsell_suggestion")
    assert upsell is not None

    updated = client.post(f"/ap2/intent-mandates/{mandate['id']}/accept-upsell").json()["intent_mandate"]
    assert updated["status"] == "cart_ready"
    assert updated["decision_detail"]["total_inr"] > mandate["decision_detail"]["total_inr"]


# ------------------------------------------- pay-first on the buyer console

def _escalating(client, agent="pf-1"):
    return client.post("/ap2/intent-mandates", json={
        "agent_id": agent, "intent": {"items": [{"item_id": "chicken_biryani", "qty": 2}]},
    }).json()["intent_mandate"]


def test_pay_first_takes_payment_before_she_answers(client, monkeypatch):
    """The old flow made the customer sit and wait for a cook to look at
    her phone, which is a sale that quietly dies. Payment goes through
    now; her verdict is actioned afterwards, exactly as on the Claude
    path. Nothing is re-decided -- negotiation.py already said ESCALATE."""
    mandate = _escalating(client)
    assert mandate["status"] == "requires_human"
    assert mandate["decision_detail"]["decision"] == "ESCALATE"

    out = client.post(f"/ap2/intent-mandates/{mandate['id']}/settle-pending-confirmation")
    assert out.status_code == 200
    assert out.json()["intent_mandate"]["status"] == "cart_ready"


def test_a_hard_rule_is_still_refused_before_any_money_moves(client):
    """A disallowed category cannot be waved through by anybody, so
    charging for it would guarantee a refund."""
    mandate = client.post("/ap2/intent-mandates", json={
        "agent_id": "pf-hard",
        "intent": {"items": [{"item_id": "party_catering_tray", "qty": 1}]},
    }).json()["intent_mandate"]

    out = client.post(f"/ap2/intent-mandates/{mandate['id']}/settle-pending-confirmation")
    assert out.status_code == 403
    assert "hard merchant rule" in out.json()["detail"]


def test_pay_first_is_not_recorded_as_a_human_override(client, monkeypatch):
    """Nobody approved anything. What happened is that payment was taken
    first, and the trail has to say that rather than inventing a yes."""
    import audit_log

    mandate = _escalating(client, "pf-noforge")
    client.post(f"/ap2/intent-mandates/{mandate['id']}/settle-pending-confirmation")

    reasons = [e["reason"] for e in
               audit_log.get_events_for_agent("pf-noforge", db_path=audit_log.DEFAULT_DB_PATH)]
    assert not any("human override" in r for r in reasons), reasons
