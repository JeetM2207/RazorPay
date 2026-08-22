import pytest

import audit_log
import orchestrator


def test_negotiate_and_record_approves_and_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_log, "DEFAULT_DB_PATH", str(tmp_path / "audit.db"))

    detail = orchestrator.negotiate_and_record("agent-x", "acp", [("masala_dosa", 1)])

    assert detail["decision"] == "APPROVE"
    assert detail["trust_tier"] == "NEW"
    events = audit_log.get_events_for_agent("agent-x", db_path=str(tmp_path / "audit.db"))
    assert len(events) == 1
    assert events[0]["decision"] == "APPROVE"


def test_negotiate_and_record_includes_upsell_only_on_approve(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_log, "DEFAULT_DB_PATH", str(tmp_path / "audit.db"))

    approved = orchestrator.negotiate_and_record("agent-x", "acp", [("masala_dosa", 1)])
    assert "upsell_suggestion" in approved

    escalated = orchestrator.negotiate_and_record("agent-x", "acp", [("chicken_biryani", 2)])
    assert "upsell_suggestion" not in escalated


def test_upsell_becomes_predictive_once_history_exists(tmp_path, monkeypatch):
    """End to end: the orchestrator reads co-purchase history and the
    suggestion changes from best-value to what people actually buy."""
    db_path = str(tmp_path / "audit.db")
    monkeypatch.setattr(audit_log, "DEFAULT_DB_PATH", db_path)

    cold = orchestrator.negotiate_and_record("pred-1", "acp", [("masala_dosa", 1)])
    assert cold["upsell_suggestion"]["item"] == "chicken_biryani"
    assert cold["upsell_suggestion"]["basis"] == "best value that fits"

    # Three paid orders where masala_dosa went out with filter_coffee.
    for _ in range(3):
        event_id = audit_log.record_event(
            agent_id="past-buyer",
            protocol="acp",
            cart=[{"item": "masala_dosa", "qty": 1}, {"item": "filter_coffee", "qty": 1}],
            decision="APPROVE",
            reason="within budget",
            total_inr=110,
            db_path=db_path,
        )
        audit_log.mark_paid(event_id, f"pay_{event_id}", db_path=db_path)

    warm = orchestrator.negotiate_and_record("pred-2", "acp", [("masala_dosa", 1)])
    assert warm["upsell_suggestion"]["item"] == "filter_coffee"
    assert warm["upsell_suggestion"]["basis"] == "bought together before"


def test_trust_tier_improves_after_a_completed_order(tmp_path, monkeypatch):
    db_path = str(tmp_path / "audit.db")
    monkeypatch.setattr(audit_log, "DEFAULT_DB_PATH", db_path)

    first = orchestrator.negotiate_and_record("agent-y", "acp", [("masala_dosa", 1)])
    assert first["trust_tier"] == "NEW"
    audit_log.mark_paid(first["event_id"], "pay_test123", db_path=db_path)

    second = orchestrator.negotiate_and_record("agent-y", "acp", [("masala_dosa", 1)])
    assert second["trust_tier"] == "STANDARD"


def test_create_payment_for_cart_rejects_carts_that_no_longer_approve(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_log, "DEFAULT_DB_PATH", str(tmp_path / "audit.db"))
    detail = orchestrator.negotiate_and_record("agent-z", "acp", [("chicken_biryani", 2)])
    assert detail["decision"] == "ESCALATE"

    with pytest.raises(ValueError):
        orchestrator.create_payment_for_cart("agent-z", detail["event_id"], [("chicken_biryani", 2)])


def test_create_payment_for_cart_calls_razorpay_and_attaches_link(tmp_path, monkeypatch):
    db_path = str(tmp_path / "audit.db")
    monkeypatch.setattr(audit_log, "DEFAULT_DB_PATH", db_path)

    fake_link = {"id": "plink_fake123", "short_url": "https://rzp.io/rzp/fake123"}
    monkeypatch.setattr(orchestrator.razorpay_client, "create_payment_link", lambda **kwargs: fake_link)

    detail = orchestrator.negotiate_and_record("agent-w", "acp", [("masala_dosa", 1)])
    link = orchestrator.create_payment_for_cart("agent-w", detail["event_id"], [("masala_dosa", 1)])

    assert link == fake_link
    events = audit_log.get_events_for_agent("agent-w", db_path=db_path)
    assert events[0]["payment_link_id"] == "plink_fake123"
