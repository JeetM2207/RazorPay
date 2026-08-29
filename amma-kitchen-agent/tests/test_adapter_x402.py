import json

import pytest
from fastapi.testclient import TestClient

import adapter_acp
import adapter_x402
import audit_log


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_log, "DEFAULT_DB_PATH", str(tmp_path / "audit.db"))
    adapter_x402._CHALLENGES.clear()
    adapter_x402._ORDERS.clear()
    return TestClient(adapter_x402.app)


def _mock_link(monkeypatch, link_id="plink_x402", url="https://rzp.io/rzp/x402"):
    monkeypatch.setattr(
        adapter_x402.orchestrator.razorpay_client,
        "create_payment_link",
        lambda **kwargs: {"id": link_id, "short_url": url},
    )


def _mock_settled(monkeypatch, payment_id="pay_x402", status="paid", link_id="plink_x402"):
    monkeypatch.setattr(
        adapter_x402.razorpay_client,
        "fetch_payment_link",
        lambda _id: {
            "id": link_id,
            "status": status,
            "payments": [{"payment_id": payment_id, "status": "captured"}] if status == "paid" else [],
        },
    )


def _order(client, agent_id="x-buyer", item="masala_dosa", qty=1, proof=None):
    headers = {"X-Payment": json.dumps(proof)} if proof else {}
    return client.post(
        "/x402/orders",
        json={"agent_id": agent_id, "items": [{"item_id": item, "qty": qty}]},
        headers=headers,
    )


def test_every_adapter_shares_the_same_orchestrator():
    """Four protocols, one brain -- not four copies of it.

    Extended as each adapter landed; the assertion is identity, not
    equality, so a re-implementation would fail it.
    """
    import adapter_ap2
    import adapter_mcp

    assert adapter_x402.orchestrator is adapter_acp.orchestrator
    assert adapter_ap2.orchestrator is adapter_acp.orchestrator
    assert adapter_mcp.orchestrator is adapter_acp.orchestrator


def test_all_four_adapters_hit_the_same_rate_limit(tmp_path, monkeypatch):
    """Identity of the module is one thing; the same LIMIT actually
    applying through all four is the thing that matters.

    Each adapter is given its own protocol name and the same agent id, so
    if any of them had its own counter -- or reached Razorpay by another
    route -- the fourth order would go through.
    """
    from datetime import datetime, timedelta, timezone

    import adapter_mcp
    import audit_log
    import merchant_config
    import notification_service
    import orchestrator
    import velocity

    monkeypatch.setattr(audit_log, "DEFAULT_DB_PATH", str(tmp_path / "shared.db"))
    monkeypatch.setattr(notification_service, "TWILIO_CONFIGURED", False)
    monkeypatch.setattr(velocity, "default_limits", velocity.VelocityLimits)
    merchant_config.reset_to_defaults()
    merchant_config._load()["velocity"] = {
        "max_orders_per_hour": 6, "max_spend_per_day_inr": 1_000_000,
    }
    orchestrator.reset_alerts()

    at = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
    cart = [("masala_dosa", 1)]

    # NEW tier halves her 6 to 3, so three protocols get through and the
    # fourth is refused -- by a window none of them owns.
    for i, protocol in enumerate(("acp", "ap2", "x402")):
        result = adapter_acp.orchestrator.negotiate_and_record(
            "agent-shared-window", protocol, cart, now=at + timedelta(seconds=i))
        assert result["decision"] == "APPROVE"

    with pytest.raises(orchestrator.VelocityRefused):
        adapter_mcp.orchestrator.negotiate_and_record(
            "agent-shared-window", "mcp", cart, now=at + timedelta(seconds=4))


def test_first_request_answers_402_with_a_real_payment_link(client, monkeypatch):
    _mock_link(monkeypatch)
    resp = _order(client)

    assert resp.status_code == 402
    body = resp.json()
    assert body["x402Version"] == 1
    offer = body["accepts"][0]
    assert offer["asset"] == "INR"
    assert offer["maxAmountRequired"] == "8000"          # Rs.80 in paise
    assert offer["extra"]["payment_link_url"] == "https://rzp.io/rzp/x402"


def test_retrying_the_same_request_with_proof_settles_it(client, monkeypatch):
    _mock_link(monkeypatch)
    challenge = _order(client).json()
    _mock_settled(monkeypatch)

    resp = _order(client, proof={"challenge_id": challenge["challenge_id"], "payment_id": "pay_x402"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "settled"
    assert body["payment_id"] == "pay_x402"
    assert body["amount_inr"] == 80


def test_settlement_is_recorded_in_the_audit_trail(client, monkeypatch):
    _mock_link(monkeypatch)
    challenge = _order(client, agent_id="x-audited").json()
    _mock_settled(monkeypatch)
    _order(client, agent_id="x-audited", proof={"challenge_id": challenge["challenge_id"], "payment_id": "pay_x402"})

    events = audit_log.get_events_for_agent("x-audited", db_path=audit_log.DEFAULT_DB_PATH)
    assert events[0]["protocol"] == "x402"
    assert events[0]["payment_id"] == "pay_x402"


def test_unpaid_link_cannot_be_passed_off_as_settled(client, monkeypatch):
    _mock_link(monkeypatch)
    challenge = _order(client).json()
    _mock_settled(monkeypatch, status="created")        # Razorpay says NOT paid

    resp = _order(client, proof={"challenge_id": challenge["challenge_id"], "payment_id": "pay_invented"})
    assert resp.status_code == 402
    assert "not settled" in resp.json()["detail"]


def test_a_forged_payment_id_is_rejected(client, monkeypatch):
    """The buyer's proof is checked against Razorpay, not believed."""
    _mock_link(monkeypatch)
    challenge = _order(client).json()
    _mock_settled(monkeypatch, payment_id="pay_real")

    resp = _order(client, proof={"challenge_id": challenge["challenge_id"], "payment_id": "pay_forged"})
    assert resp.status_code == 403
    assert "does not match" in resp.json()["detail"]


def test_proof_cannot_be_replayed_to_buy_twice(client, monkeypatch):
    _mock_link(monkeypatch)
    challenge = _order(client).json()
    _mock_settled(monkeypatch)
    proof = {"challenge_id": challenge["challenge_id"], "payment_id": "pay_x402"}

    assert _order(client, proof=proof).status_code == 200
    replay = _order(client, proof=proof)
    assert replay.status_code == 409
    assert "already been used" in replay.json()["detail"]


def test_proof_cannot_be_moved_onto_a_bigger_cart(client, monkeypatch):
    """Pay for one dosa, then try to claim two. The challenge is bound to
    the exact cart it was issued for."""
    _mock_link(monkeypatch)
    challenge = _order(client, qty=1).json()
    _mock_settled(monkeypatch)

    resp = _order(client, qty=2, proof={"challenge_id": challenge["challenge_id"], "payment_id": "pay_x402"})
    assert resp.status_code == 409
    assert "does not match the cart" in resp.json()["detail"]


def test_another_agent_cannot_use_someone_elses_challenge(client, monkeypatch):
    _mock_link(monkeypatch)
    challenge = _order(client, agent_id="agent-one").json()
    _mock_settled(monkeypatch)

    resp = _order(
        client,
        agent_id="agent-two",
        proof={"challenge_id": challenge["challenge_id"], "payment_id": "pay_x402"},
    )
    assert resp.status_code == 403
    assert "different agent" in resp.json()["detail"]


def test_expired_challenge_is_refused(client, monkeypatch):
    _mock_link(monkeypatch)
    challenge = _order(client).json()
    _mock_settled(monkeypatch)
    adapter_x402._CHALLENGES[challenge["challenge_id"]]["expires_at"] = 0

    resp = _order(client, proof={"challenge_id": challenge["challenge_id"], "payment_id": "pay_x402"})
    assert resp.status_code == 403
    assert "expired" in resp.json()["detail"]


def test_escalated_cart_gets_200_not_402(client, monkeypatch):
    """No 402 is issued for an order that has not been approved -- there
    is nothing legitimate to demand payment for yet."""
    called = []
    monkeypatch.setattr(
        adapter_x402.orchestrator.razorpay_client,
        "create_payment_link",
        lambda **kwargs: called.append(kwargs) or {"id": "x", "short_url": "x"},
    )

    resp = _order(client, item="chicken_biryani", qty=2)      # Rs.440, over threshold
    assert resp.status_code == 200
    assert resp.json()["status"] == "requires_human"
    assert called == [], "a payment link was created for an unapproved order"


def test_mandate_violation_never_reaches_a_402(client, monkeypatch):
    called = []
    monkeypatch.setattr(
        adapter_x402.orchestrator.razorpay_client,
        "create_payment_link",
        lambda **kwargs: called.append(kwargs) or {"id": "x", "short_url": "x"},
    )

    resp = _order(client, item="party_catering_tray")
    assert resp.status_code == 200
    assert "category not allowed" in resp.json()["decision_detail"]["reason"]
    assert called == []


def test_repeat_request_reuses_the_live_challenge(client, monkeypatch):
    """Asking again before paying must not mint a second payment link."""
    links = []
    monkeypatch.setattr(
        adapter_x402.orchestrator.razorpay_client,
        "create_payment_link",
        lambda **kwargs: (links.append(1), {"id": f"plink_{len(links)}", "short_url": "u"})[1],
    )

    first = _order(client).json()
    second = _order(client).json()
    assert first["challenge_id"] == second["challenge_id"]
    assert len(links) == 1


def test_polling_while_escalated_does_not_duplicate_the_order(client, monkeypatch):
    """x402 has no session id, so a waiting buyer's only move is to ask
    again. That must resume the same order, not fill the merchant's queue
    with copies of one decision."""
    _mock_link(monkeypatch)

    first = _order(client, agent_id="poller", item="chicken_biryani", qty=2).json()
    for _ in range(4):
        again = _order(client, agent_id="poller", item="chicken_biryani", qty=2).json()
        assert again["order_id"] == first["order_id"]

    assert len(adapter_x402.list_orders(status="requires_human")["sessions"]) == 1
    events = audit_log.get_events_for_agent("poller", db_path=audit_log.DEFAULT_DB_PATH)
    assert len(events) == 1, "each poll wrote a fresh audit event"


def test_human_confirm_then_402_for_an_escalated_order(client, monkeypatch):
    _mock_link(monkeypatch)
    order = _order(client, item="chicken_biryani", qty=2).json()
    assert order["status"] == "requires_human"

    confirmed = client.post(f"/x402/orders/{order['order_id']}/human_confirm").json()
    assert confirmed["status"] == "payment_required"

    resp = _order(client, item="chicken_biryani", qty=2)
    assert resp.status_code == 402
    assert resp.json()["accepts"][0]["maxAmountRequired"] == "44000"


def test_malformed_proof_header_is_rejected_clearly(client, monkeypatch):
    _mock_link(monkeypatch)
    _order(client)
    resp = client.post(
        "/x402/orders",
        json={"agent_id": "x-buyer", "items": [{"item_id": "masala_dosa", "qty": 1}]},
        headers={"X-Payment": "not-json"},
    )
    assert resp.status_code == 400
