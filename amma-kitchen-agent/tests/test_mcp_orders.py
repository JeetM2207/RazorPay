"""The pay-first lifecycle: payment lands, then the decision that was
already made gets actioned, then Amma answers if it was a large one.

The cases that matter are the ones where money is at stake: a webhook
delivered twice, and a rejection that has to give the money back.
"""

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

import adapter_mcp
import audit_log
import escalations
import mcp_orders
import notification_service
import webhook_handler
from adapter_mcp import CartItem

WEBHOOK_SECRET = "test_webhook_secret"
WHY = "Friends over for dinner."
DELIVERY = {
    "delivery_name": "Priya Sharma",
    "delivery_phone": "9876543210",
    "delivery_address": "Flat 402, Indiranagar, Bengaluru",
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "audit.db")
    monkeypatch.setattr(audit_log, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(notification_service, "TWILIO_CONFIGURED", False)
    monkeypatch.setattr(webhook_handler, "_WEBHOOK_SECRET", WEBHOOK_SECRET)
    notification_service.clear_outbox()
    escalations.reset()

    links = []

    def fake_link(**kwargs):
        links.append(kwargs)
        return {"id": f"plink_{len(links)}", "short_url": f"https://rzp.io/rzp/{len(links)}"}

    monkeypatch.setattr(adapter_mcp.orchestrator.razorpay_client, "create_payment_link", fake_link)

    refunds = []

    def fake_refund(payment_id, amount_inr=None):
        refunds.append({"payment_id": payment_id, "amount_inr": amount_inr})
        return {"id": f"rfnd_{len(refunds)}", "status": "processed"}

    monkeypatch.setattr(mcp_orders.razorpay_client, "refund_payment", fake_refund)

    return {"db": db_path, "links": links, "refunds": refunds,
            "client": TestClient(webhook_handler.app)}


def cart(*pairs):
    return [CartItem(item_id=i, qty=q) for i, q in pairs]


def checkout(*pairs):
    return adapter_mcp.checkout_impl(cart(*pairs), **DELIVERY)


def pay(env, payment_link_id, payment_id="pay_abc"):
    body = json.dumps({
        "entity": "event", "event": "payment_link.paid",
        "payload": {
            "payment_link": {"entity": {"id": payment_link_id, "status": "paid"}},
            "payment": {"entity": {"id": payment_id, "status": "captured"}},
        },
    }).encode()
    sig = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return env["client"].post("/webhooks/razorpay", content=body,
                              headers={"X-Razorpay-Signature": sig})


def statuses(order_ref, db):
    return [r["decision"] for r in audit_log.get_order_rows(order_ref, db_path=db)]


def messages():
    return [m["body"] for m in notification_service.outbox()]


# ---------------------------------------------------------- checkout only

def test_checkout_returns_a_link_and_nothing_more(env):
    placed = checkout(("masala_dosa", 1))

    assert placed["status"] == "awaiting_payment"
    assert placed["payment_url"].startswith("https://")
    assert "payment_id" not in placed, "checkout must not report money as moved"
    assert mcp_orders.status_of(placed["order_id"]) == mcp_orders.AWAITING_PAYMENT


def test_an_over_cap_cart_is_no_longer_refused_at_checkout(env):
    """Pay-first: the escalation is carried forward, not used to block."""
    placed = checkout(("chicken_biryani", 2))      # Rs.440, over the Rs.400 line

    assert placed["status"] == "awaiting_payment"
    assert len(env["links"]) == 1
    order = mcp_orders.get_order(placed["order_id"])
    assert order["decision"] == "ESCALATE", "the verdict must be carried, not discarded"


def test_a_cart_no_human_could_accept_is_refused_before_any_money_moves(env):
    """Pay-first only makes sense when a human COULD say yes. A
    disallowed category cannot be waved through by anyone, so charging
    for one would guarantee a refund -- refuse it up front instead."""
    placed = checkout(("party_catering_tray", 1))

    assert placed["status"] == "refused"
    assert "category not allowed" in placed["reason"]
    assert env["links"] == [], "a payment link was created for an unacceptable cart"
    assert mcp_orders.status_of(placed["order_id"]) is None


def test_nobody_is_told_anything_before_payment(env):
    """Neither proposing nor issuing a link should reach Amma. She hears
    about an order when the money has actually arrived -- alerting her at
    propose time meant she was pinged about carts nobody ever paid for,
    and then pinged again after payment."""
    adapter_mcp.propose_cart_impl(cart(("chicken_biryani", 2)), WHY)
    assert messages() == [], "proposing a cart alerted the merchant"

    checkout(("chicken_biryani", 2))
    assert messages() == [], "issuing a payment link alerted the merchant"


# ------------------------------------------------- within cap: auto-confirm

def test_within_cap_order_auto_confirms_on_payment(env):
    placed = checkout(("masala_dosa", 1))
    assert pay(env, placed["payment_link_id"]).json()["status"] == "processed"

    assert mcp_orders.status_of(placed["order_id"]) == mcp_orders.AUTO_CONFIRMED
    assert mcp_orders.PAID in statuses(placed["order_id"], env["db"])

    sent = messages()
    assert any(f"Payment received for order #{placed['order_id']}" in m for m in sent)
    assert any(f"Order #{placed['order_id']} accepted" in m for m in sent)
    assert any("no action needed" in m for m in sent), "merchant should be told, not asked"
    assert not any("reply ACCEPT or REJECT" in m for m in sent)


# --------------------------------------------- over cap: merchant decides

def test_over_cap_order_waits_for_the_merchant_after_payment(env):
    placed = checkout(("chicken_biryani", 2))
    pay(env, placed["payment_link_id"])

    assert mcp_orders.status_of(placed["order_id"]) == mcp_orders.PENDING_MERCHANT_APPROVAL

    sent = messages()
    assert any("restaurant is confirming" in m for m in sent)
    assert any("reply ACCEPT or REJECT" in m for m in sent)
    assert not any("accepted —" in m for m in sent), "nothing was accepted yet"


def test_merchant_accepting_confirms_and_tells_the_customer(env):
    placed = checkout(("chicken_biryani", 2))
    pay(env, placed["payment_link_id"])
    notification_service.clear_outbox()

    mcp_orders.accept(placed["order_id"])

    assert mcp_orders.status_of(placed["order_id"]) == mcp_orders.MERCHANT_ACCEPTED
    assert any(f"Amma accepted order #{placed['order_id']}" in m for m in messages())
    assert env["refunds"] == [], "an accepted order must not refund"


def test_merchant_rejecting_refunds_the_actual_payment(env):
    placed = checkout(("chicken_biryani", 2))
    pay(env, placed["payment_link_id"], payment_id="pay_realone")
    notification_service.clear_outbox()

    result = mcp_orders.reject(placed["order_id"])

    assert result["refunded"] is True
    assert env["refunds"] == [{"payment_id": "pay_realone", "amount_inr": 440}]
    assert mcp_orders.status_of(placed["order_id"]) == mcp_orders.REFUNDED
    assert mcp_orders.MERCHANT_REJECTED in statuses(placed["order_id"], env["db"])
    assert any("Rs.440 has been refunded" in m for m in messages())


def test_a_timeout_refunds_but_is_recorded_differently(env):
    """The trail should tell "she said no" from "she never replied"."""
    placed = checkout(("chicken_biryani", 2))
    pay(env, placed["payment_link_id"], payment_id="pay_timeout")

    mcp_orders.expire(placed["order_id"])

    assert mcp_orders.status_of(placed["order_id"]) == mcp_orders.REFUNDED
    trail = statuses(placed["order_id"], env["db"])
    assert mcp_orders.MERCHANT_TIMEOUT_REFUNDED in trail
    assert mcp_orders.MERCHANT_REJECTED not in trail
    assert env["refunds"][0]["payment_id"] == "pay_timeout"


def test_a_failed_refund_is_recorded_rather_than_silently_closed(env, monkeypatch):
    placed = checkout(("chicken_biryani", 2))
    pay(env, placed["payment_link_id"], payment_id="pay_stuck")

    def explode(*a, **k):
        raise RuntimeError("razorpay down")

    monkeypatch.setattr(mcp_orders.razorpay_client, "refund_payment", explode)
    result = mcp_orders.reject(placed["order_id"])

    assert result["refunded"] is False
    assert mcp_orders.status_of(placed["order_id"]) != mcp_orders.REFUNDED
    assert any("FAILED and needs manual attention" in r["reason"]
               for r in audit_log.get_order_rows(placed["order_id"], db_path=env["db"]))


def test_an_order_cannot_be_decided_twice(env):
    placed = checkout(("chicken_biryani", 2))
    pay(env, placed["payment_link_id"])
    mcp_orders.accept(placed["order_id"])

    with pytest.raises(ValueError):
        mcp_orders.reject(placed["order_id"])


def test_an_unpaid_order_cannot_be_accepted(env):
    placed = checkout(("chicken_biryani", 2))
    with pytest.raises(ValueError):
        mcp_orders.accept(placed["order_id"])


# ------------------------------------------------------------ idempotency

def test_the_same_payment_webhook_twice_changes_nothing_twice(env):
    placed = checkout(("masala_dosa", 1))

    first = pay(env, placed["payment_link_id"])
    before = len(audit_log.get_order_rows(placed["order_id"], db_path=env["db"]))
    sent_before = len(messages())

    second = pay(env, placed["payment_link_id"])

    assert first.json()["status"] == "processed"
    assert second.json()["status"] == "duplicate_ignored"
    assert len(audit_log.get_order_rows(placed["order_id"], db_path=env["db"])) == before
    assert len(messages()) == sent_before, "a redelivery messaged the customer again"
    assert mcp_orders.status_of(placed["order_id"]) == mcp_orders.AUTO_CONFIRMED


# ---------------------------------------------------- merchant surfaces

def test_a_paid_pending_order_appears_in_the_merchant_queue(env):
    placed = checkout(("chicken_biryani", 2))
    pay(env, placed["payment_link_id"])

    queue = adapter_mcp.list_pending()["sessions"]
    assert len(queue) == 1
    entry = queue[0]["decision_detail"]
    assert entry["event_id"] == placed["order_id"]
    assert entry["already_paid"] is True
    assert "declining refunds the customer" in entry["reason"]


def test_an_accept_reply_over_whatsapp_resolves_it(env):
    """One inbound handler: the same webhook every other protocol uses."""
    placed = checkout(("chicken_biryani", 2))
    pay(env, placed["payment_link_id"])

    parsed = escalations.parse_reply("ACCEPT")
    assert parsed.action == "APPROVE"
    escalations.resolve(parsed.action, placed["order_id"])

    assert mcp_orders.status_of(placed["order_id"]) == mcp_orders.MERCHANT_ACCEPTED


def test_a_reject_reply_over_whatsapp_refunds(env):
    placed = checkout(("chicken_biryani", 2))
    pay(env, placed["payment_link_id"], payment_id="pay_sms")

    parsed = escalations.parse_reply("REJECT")
    escalations.resolve(parsed.action, placed["order_id"])

    assert env["refunds"][0]["payment_id"] == "pay_sms"
    assert mcp_orders.status_of(placed["order_id"]) == mcp_orders.REFUNDED


# ----------------------------------------------------------- the trail

def test_the_trail_reads_in_the_order_things_happened(env):
    """The explainability bar: payment, decision, merchant action,
    outcome -- top to bottom, each timestamped."""
    placed = checkout(("chicken_biryani", 2))
    pay(env, placed["payment_link_id"], payment_id="pay_trail")
    mcp_orders.reject(placed["order_id"])

    trail = statuses(placed["order_id"], env["db"])
    assert trail == [
        "ESCALATE",                              # the decision, made once
        mcp_orders.AWAITING_PAYMENT,
        mcp_orders.PAID,
        mcp_orders.PENDING_MERCHANT_APPROVAL,
        mcp_orders.MERCHANT_REJECTED,
        mcp_orders.REFUNDED,
    ]
    rows = audit_log.get_order_rows(placed["order_id"], db_path=env["db"])
    assert all(r["ts"] for r in rows), "every transition is timestamped"


def test_other_protocols_are_untouched_by_the_new_flow(env):
    """ACP/AP2/x402 still finish at capture -- no lifecycle, no messages."""
    detail = adapter_mcp.orchestrator.negotiate_and_record(
        "buyer-acp", "acp", [("masala_dosa", 1)]
    )
    audit_log.attach_payment_link(detail["event_id"], "plink_acp", db_path=env["db"])
    notification_service.clear_outbox()

    pay(env, "plink_acp", payment_id="pay_acp")

    assert mcp_orders.status_of(detail["event_id"]) is None, "an ACP order entered the lifecycle"
    assert messages() == [], "an ACP payment sent a customer message"
