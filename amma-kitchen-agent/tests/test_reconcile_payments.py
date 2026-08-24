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


# ------------------------- the safety net has to be a net for MCP too

def _paid_mcp_order(db_path, link_id="plink_mcp_r"):
    """A Claude-chat order over the confirmation threshold, checked out
    and waiting on Razorpay."""
    import mcp_orders

    detail = orchestrator.negotiate_and_record("mcp:claude", "mcp", [("chicken_biryani", 2)])
    audit_log.attach_payment_link(detail["event_id"], link_id, db_path=db_path)
    audit_log.attach_delivery(
        detail["event_id"], "Jeet", "9876543210", "12 MG Road", db_path=db_path
    )
    mcp_orders.open_order(detail["event_id"])
    return detail["event_id"]


def test_reconcile_finishes_an_mcp_order_the_webhook_never_reported(db_path, monkeypatch):
    """Marking it paid is only half the job. If no webhook ever arrived --
    a Razorpay account with none configured, a closed tunnel, a server
    that was down -- this is the only path left that can confirm the
    order to the customer and put it in front of Amma. It used to stop at
    the payment, which is how an order could be perfectly recorded and
    still reach nobody."""
    import mcp_orders
    import notification_service

    monkeypatch.setattr(notification_service, "TWILIO_CONFIGURED", False)
    notification_service.clear_outbox()

    event_id = _paid_mcp_order(db_path)
    monkeypatch.setattr(
        reconcile_payments.razorpay_sdk_client.payment_link,
        "fetch",
        lambda _id: _fake_link("paid", "pay_mcp_r"),
    )

    assert reconcile_payments.reconcile(db_path)["marked_paid"] == 1

    assert mcp_orders.status_of(event_id) == mcp_orders.PENDING_MERCHANT_APPROVAL
    assert notification_service.outbox(), "the customer was told nothing"

    import adapter_mcp

    assert len(adapter_mcp.list_pending()["sessions"]) == 1, "Amma never saw it"


def test_reconcile_leaves_the_other_protocols_alone(db_path, monkeypatch):
    """ACP, AP2 and x402 finish at capture. The follow-up must be a no-op
    for them, not merely harmless."""
    import notification_service

    monkeypatch.setattr(notification_service, "TWILIO_CONFIGURED", False)
    notification_service.clear_outbox()

    _order_with_link(db_path, agent_id="buyer-acp-r", link_id="plink_acp_r")
    monkeypatch.setattr(
        reconcile_payments.razorpay_sdk_client.payment_link,
        "fetch",
        lambda _id: _fake_link("paid", "pay_acp_r"),
    )

    assert reconcile_payments.reconcile(db_path)["marked_paid"] == 1
    assert notification_service.outbox() == []


def test_the_webhook_and_the_reconciler_run_the_same_follow_up(db_path):
    """One function, two callers. The bug this guards against is the two
    drifting apart again -- the fast path gaining a step the safety net
    never gets."""
    import inspect

    import webhook_handler

    for module in (webhook_handler, reconcile_payments):
        source = inspect.getsource(module)
        assert "follow_up_after_capture" in source, module.__name__
        assert "on_payment_captured" not in source, (
            f"{module.__name__} reaches into the lifecycle directly instead of "
            "through the shared follow-up"
        )
