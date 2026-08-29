import pytest
from fastapi.testclient import TestClient

import audit_log
import dashboard
import orchestrator


@pytest.fixture
def client_and_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "audit.db")
    monkeypatch.setattr(audit_log, "DEFAULT_DB_PATH", db_path)
    return TestClient(dashboard.app), db_path


def test_empty_dashboard_renders(client_and_db):
    client, _ = client_and_db
    resp = client.get("/")
    assert resp.status_code == 200
    assert "No decisions recorded yet" in resp.text


def test_approved_and_paid_order_shows_as_paid(client_and_db):
    client, db_path = client_and_db
    detail = orchestrator.negotiate_and_record("buyer-d1", "acp", [("masala_dosa", 1)])
    audit_log.attach_payment_link(detail["event_id"], "plink_d1", db_path=db_path)
    audit_log.mark_paid(detail["event_id"], "pay_d1", db_path=db_path)

    body = client.get("/").text
    assert "buyer-d1" in body
    assert "pay_d1" in body
    assert "PAID" in body


def test_escalated_order_shows_no_razorpay_call_made(client_and_db):
    client, _ = client_and_db
    orchestrator.negotiate_and_record("buyer-d2", "acp", [("chicken_biryani", 2)])

    body = client.get("/").text
    assert "ESCALATE" in body
    assert "no Razorpay call made" in body


def test_both_protocols_appear_in_the_same_log(client_and_db):
    client, _ = client_and_db
    orchestrator.negotiate_and_record("buyer-d3", "acp", [("masala_dosa", 1)])
    orchestrator.negotiate_and_record("buyer-d4", "ap2", [("veg_thali", 1)])

    body = client.get("/").text
    assert ">ACP<" in body
    assert ">AP2<" in body


def test_trust_tier_is_shown_and_rises_with_completed_orders(client_and_db):
    client, db_path = client_and_db
    detail = orchestrator.negotiate_and_record("buyer-d5", "acp", [("masala_dosa", 1)])
    assert "NEW" in client.get("/").text

    audit_log.mark_paid(detail["event_id"], "pay_d5", db_path=db_path)
    assert "STANDARD" in client.get("/").text


def test_both_reasons_render_side_by_side(client_and_db):
    """What the system decided, and the human context behind it."""
    client, db_path = client_and_db
    detail = orchestrator.negotiate_and_record("mcp:claude", "mcp", [("masala_dosa", 1)])
    audit_log.attach_buyer_reasoning(
        detail["event_id"], "Working late, wants something light.", db_path=db_path
    )
    audit_log.attach_delivery(
        detail["event_id"], "Priya Sharma", "9876543210", "Flat 402, Indiranagar",
        db_path=db_path,
    )

    body = client.get("/").text
    assert "within budget and below human confirm threshold" in body   # system's reason
    assert "Working late, wants something light." in body            # the human context
    assert "customer wanted:" in body

    # The delivery details are REDACTED here, deliberately. This page has
    # no login -- its whole purpose is being checkable by someone without
    # an account -- so the customer's name, phone and address are taken
    # out of it rather than the page being closed. The full record is at
    # /evidence/<id>, which does need a merchant login.
    assert "Priya Sharma" not in body
    assert "Indiranagar" not in body
    assert "9876543210" not in body
    assert "deliver to:" in body and "needs a merchant login" in body


def test_buyer_reasoning_is_html_escaped(client_and_db):
    """It is free text written by someone else's model."""
    client, db_path = client_and_db
    detail = orchestrator.negotiate_and_record("mcp:claude", "mcp", [("masala_dosa", 1)])
    audit_log.attach_buyer_reasoning(
        detail["event_id"], "<script>alert('x')</script>", db_path=db_path
    )

    body = client.get("/").text
    assert "<script>alert" not in body
    assert "&lt;script&gt;" in body


def test_refresh_can_be_disabled(client_and_db):
    client, _ = client_and_db
    assert "http-equiv='refresh'" in client.get("/").text
    assert "http-equiv='refresh'" not in client.get("/?refresh=0").text


def test_reason_text_is_html_escaped(client_and_db):
    client, db_path = client_and_db
    audit_log.record_event(
        agent_id="buyer-d6",
        protocol="acp",
        cart=[{"item": "masala_dosa", "qty": 1}],
        decision="ESCALATE",
        reason="<script>alert('xss')</script>",
        total_inr=80,
        db_path=db_path,
    )
    body = client.get("/").text
    assert "<script>alert" not in body
    assert "&lt;script&gt;" in body


def test_a_stage_is_shown_under_its_order_not_beside_it(client_and_db):
    """One order, one block.

    The trail is append-only, so a paid order that gets confirmed writes
    AWAITING_PAYMENT, PAID and AUTO_CONFIRMED as separate rows. That stays.
    What changed is that the page used to render them exactly like
    decisions, at the same level, so one order looked like four separate
    orders on screen.
    """
    import dashboard

    decision = {
        "id": 100, "ts": "2026-08-29T19:13:00+00:00", "agent_id": "Jeet's Agent",
        "protocol": "ap2", "decision": "APPROVE", "reason": "within budget",
        "cart_json": '[{"item": "veg_thali", "qty": 1}]', "total_inr": 380,
        "payment_id": "pay_x", "payment_link_id": None, "order_ref": None,
        "buyer_reasoning": None, "delivery_name": None, "delivery_phone": None,
        "delivery_address": None,
    }
    stages = [
        dict(decision, id=100 + n, order_ref=100, payment_id=None, decision=state,
             reason=state)
        for n, state in enumerate(("AWAITING_PAYMENT", "PAID", "AUTO_CONFIRMED"), start=1)
    ]

    rendered = dashboard._event_rows([stages[2], stages[1], stages[0], decision])

    # Nothing is dropped: the record must stay complete.
    assert rendered.count("<tr") == 4

    # Exactly one of them is an order; the other three are its stages.
    assert rendered.count("<tr class='stage'>") == 3
    assert rendered.count("order #100") == 3

    # And the stages come after the decision they belong to.
    assert rendered.index("class='stage'") > rendered.index("Jeet&#x27;s Agent")


def test_an_orphaned_stage_is_still_shown(client_and_db):
    """A stage whose decision is off the end of the page must not vanish.
    Dropping it would make the trail incomplete, which is the one thing it
    may never be."""
    import dashboard

    orphan = {
        "id": 501, "ts": "2026-08-29T19:13:03+00:00", "agent_id": "a", "protocol": "ap2",
        "decision": "PAID", "reason": "captured", "cart_json": "[]", "total_inr": 380,
        "payment_id": None, "payment_link_id": None, "order_ref": 999,
        "buyer_reasoning": None, "delivery_name": None, "delivery_phone": None,
        "delivery_address": None,
    }
    rendered = dashboard._event_rows([orphan])
    assert "order #999" in rendered
    assert rendered.count("<tr") == 1
