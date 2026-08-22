import pytest

import audit_log
import idempotency
import orchestrator
import reconcile_payments


def _fake_link(status, payment_id=None):
    link = {"status": status}
    if payment_id:
        link["payments"] = [{"payment_id": payment_id, "status": "captured"}]
    return link


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "audit.db")
    monkeypatch.setattr(audit_log, "DEFAULT_DB_PATH", path)
    return path


def _order_with_link(db_path, agent_id="buyer-r1", link_id="plink_r1"):
    detail = orchestrator.negotiate_and_record(agent_id, "acp", [("masala_dosa", 1)])
    audit_log.attach_payment_link(detail["event_id"], link_id, db_path=db_path)
    return detail["event_id"]


def test_reconcile_backfills_a_paid_payment_missed_by_webhook(db_path, monkeypatch):
    event_id = _order_with_link(db_path)
    monkeypatch.setattr(
        reconcile_payments.razorpay_sdk_client.payment_link,
        "fetch",
        lambda _id: _fake_link("paid", "pay_r1"),
    )

    stats = reconcile_payments.reconcile(db_path)

    assert stats["marked_paid"] == 1
    events = audit_log.get_events_for_agent("buyer-r1", db_path=db_path)
    assert [e for e in events if e["id"] == event_id][0]["payment_id"] == "pay_r1"


def test_reconcile_is_safe_to_run_twice(db_path, monkeypatch):
    _order_with_link(db_path, agent_id="buyer-r2", link_id="plink_r2")
    monkeypatch.setattr(
        reconcile_payments.razorpay_sdk_client.payment_link,
        "fetch",
        lambda _id: _fake_link("paid", "pay_r2"),
    )

    first = reconcile_payments.reconcile(db_path)
    second = reconcile_payments.reconcile(db_path)

    assert first["marked_paid"] == 1
    assert second["marked_paid"] == 0  # nothing left unresolved to redo


def test_reconcile_does_not_double_record_what_a_webhook_already_handled(db_path, monkeypatch):
    event_id = _order_with_link(db_path, agent_id="buyer-r3", link_id="plink_r3")
    # Simulate the webhook having already claimed this exact event.
    assert idempotency.claim_event("payment_link.paid", "plink_r3", db_path) is True

    monkeypatch.setattr(
        reconcile_payments.razorpay_sdk_client.payment_link,
        "fetch",
        lambda _id: _fake_link("paid", "pay_r3"),
    )
    stats = reconcile_payments.reconcile(db_path)

    assert stats["marked_paid"] == 0
    events = audit_log.get_events_for_agent("buyer-r3", db_path=db_path)
    assert [e for e in events if e["id"] == event_id][0]["payment_id"] is None


def test_reconcile_records_expired_link_as_not_completed(db_path, monkeypatch):
    _order_with_link(db_path, agent_id="buyer-r4", link_id="plink_r4")
    monkeypatch.setattr(
        reconcile_payments.razorpay_sdk_client.payment_link,
        "fetch",
        lambda _id: _fake_link("expired"),
    )

    stats = reconcile_payments.reconcile(db_path)

    assert stats["marked_not_completed"] == 1
    events = audit_log.get_events_for_agent("buyer-r4", db_path=db_path)
    assert any(e["decision"] == "PAYMENT_NOT_COMPLETED" for e in events)


def test_reconcile_leaves_still_created_links_alone(db_path, monkeypatch):
    event_id = _order_with_link(db_path, agent_id="buyer-r5", link_id="plink_r5")
    monkeypatch.setattr(
        reconcile_payments.razorpay_sdk_client.payment_link,
        "fetch",
        lambda _id: _fake_link("created"),
    )

    stats = reconcile_payments.reconcile(db_path)

    assert stats["still_open"] == 1
    assert stats["marked_paid"] == 0
    events = audit_log.get_events_for_agent("buyer-r5", db_path=db_path)
    assert [e for e in events if e["id"] == event_id][0]["payment_id"] is None
