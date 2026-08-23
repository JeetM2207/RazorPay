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
    assert "Priya Sharma" in body and "Indiranagar" in body


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
