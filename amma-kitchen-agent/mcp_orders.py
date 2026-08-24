"""Order lifecycle for the Claude-chat ordering path: pay first, confirm
after, tell the customer on WhatsApp.

Why this shape
--------------
The earlier design made a large order wait for Amma BEFORE taking any
money, which meant the customer had to keep the Claude conversation open,
watch for the merchant's decision, and then come back and ask Claude to
finish. Claude cannot be woken between turns, so in practice the order
just stalled -- and the customer was told it was "pending confirmation"
with no way to see it.

So payment now happens first and the confirmation runs afterwards, over
WhatsApp, completely decoupled from the chat. Claude's involvement ends
the moment it hands over a payment link.

The decision is NOT re-derived after payment. negotiation.py already said
APPROVE or ESCALATE when the cart was proposed; that verdict is stored
with the order and simply actioned later. The cap logic is evaluated
once, where it always was, and this module only routes on the answer.

The obvious objection -- "you took money for an order she might refuse"
-- is answered by making rejection refund automatically and immediately,
in the same call that records it. A declined order returns the money
without anyone having to chase it.

Everything here reuses what already exists: the same audit writer, the
same idempotency ledger, the same WhatsApp sender and the same inbound
reply webhook. No parallel machinery.
"""

import json

import audit_log
import notification_service
import razorpay_client

PROTOCOL = "mcp"

# The lifecycle. Each is written as its own append-only audit row, so the
# trail reads in the order things actually happened rather than as a
# single row mutated four times.
AWAITING_PAYMENT = "AWAITING_PAYMENT"
PAID = "PAID"
AUTO_CONFIRMED = "AUTO_CONFIRMED"
PENDING_MERCHANT_APPROVAL = "PENDING_MERCHANT_APPROVAL"
MERCHANT_ACCEPTED = "MERCHANT_ACCEPTED"
MERCHANT_REJECTED = "MERCHANT_REJECTED"
MERCHANT_TIMEOUT_REFUNDED = "MERCHANT_TIMEOUT_REFUNDED"
REFUNDED = "REFUNDED"

LIFECYCLE_STATUSES = {
    AWAITING_PAYMENT,
    PAID,
    AUTO_CONFIRMED,
    PENDING_MERCHANT_APPROVAL,
    MERCHANT_ACCEPTED,
    MERCHANT_REJECTED,
    MERCHANT_TIMEOUT_REFUNDED,
    REFUNDED,
}

# Statuses a merchant reply can still act on.
_AWAITING_MERCHANT = {PENDING_MERCHANT_APPROVAL}


# ---------------------------------------------------------------- helpers

def _db() -> str:
    return audit_log.DEFAULT_DB_PATH


def _cart_of(row: dict) -> list[dict]:
    try:
        return json.loads(row["cart_json"])
    except (json.JSONDecodeError, TypeError):
        return []


def _transition(order: dict, status: str, reason: str) -> int:
    """Record a lifecycle change as a new audit row.

    Append-only on purpose: a judge reading the trail sees payment, then
    the decision being actioned, then the merchant's answer, each
    timestamped, instead of one row whose history has been overwritten.
    """
    return audit_log.record_event(
        agent_id=order["agent_id"],
        protocol=order["protocol"],
        cart=_cart_of(order),
        decision=status,
        reason=reason,
        total_inr=order["total_inr"],
        db_path=_db(),
        order_ref=order["id"],
    )


def _tell(phone: str | None, message: str) -> None:
    """Message the customer, and never let a failure break the order. The
    status is already recorded by the time this runs.

    The number is normalised first. It arrives however the assistant
    typed what the customer told it -- "8306610707", "98765 43210",
    "+91 …" -- and Twilio needs E.164. Sending the raw string produced a
    recipient of `whatsapp:8306610707`, which is rejected outright, while
    the merchant's own number (already E.164 in config) worked fine. The
    same normaliser buyer_sms uses, rather than a second one.
    """
    if not phone:
        return
    try:
        import buyer_sms

        recipient = buyer_sms.normalise_phone(phone) or phone
        notification_service.send_sms(message, to=recipient)
    except Exception:
        pass


def _tell_merchant(message: str) -> None:
    try:
        notification_service.send_sms(message)
    except Exception:
        pass


def status_of(order_ref: int) -> str | None:
    return audit_log.get_order_status(order_ref, db_path=_db())


def get_order(order_ref: int) -> dict | None:
    rows = audit_log.get_order_rows(order_ref, db_path=_db())
    return rows[0] if rows else None


# ------------------------------------------------------- step 1: checkout

def open_order(order_ref: int) -> int:
    """Called by the adapter once a payment link exists. From here on the
    order is in the lifecycle and the chat is no longer involved."""
    order = get_order(order_ref)
    if order is None:
        raise ValueError(f"no such order: {order_ref}")
    return _transition(
        order,
        AWAITING_PAYMENT,
        "payment link issued; awaiting the customer paying on Razorpay",
    )


# ----------------------------------------------- step 2: payment captured

def follow_up_after_capture(event: dict, payment_id: str) -> None:
    """Continue this lifecycle for an order just recorded as paid.

    EVERY path that learns a payment completed calls this, so none of
    them can quietly forget to. The webhook is the fast path; the
    reconciler is the safety net for when the webhook never arrived at
    all -- a Razorpay account whose webhook is not configured yet, a
    closed tunnel, a server that was down. The reconciler used to mark
    such an order paid and stop there, which meant the customer paid and
    heard nothing and Amma never saw it: correct in the trail, invisible
    everywhere else.

    A no-op for ACP, AP2 and x402, which finish at capture. Both callers
    have already claimed the payment through the shared idempotency
    ledger, so this runs once per payment.
    """
    if event.get("protocol") != PROTOCOL:
        return
    try:
        on_payment_captured(dict(event, payment_id=payment_id), payment_id)
    except Exception as exc:
        # The payment is recorded either way, and a follow-up failure must
        # never make Razorpay retry a capture we already have. Printed
        # rather than swallowed: an invisible failure here is the exact
        # bug this function exists to close.
        print(f"  ! post-payment follow-up failed for order {event.get('id')}: {exc}")


def on_payment_captured(order: dict, payment_id: str) -> str:
    """Razorpay says the customer paid. Record it, tell them, then action
    the decision negotiation.py already made.

    Called from the webhook handler, which has already claimed the event
    through the shared idempotency ledger -- so this runs once per
    payment even though Razorpay may deliver the webhook several times.
    """
    order_ref = order["id"]
    phone = order.get("delivery_phone")
    total = order["total_inr"]

    _transition(order, PAID, f"payment {payment_id} captured by Razorpay")
    _tell(phone, f"Payment received for order #{order_ref} (Rs.{total}). We'll confirm shortly.")

    # The verdict from when the cart was proposed. Not recomputed: the
    # cap was applied once, at decision time, and this only acts on it.
    original_decision = order["decision"]

    if original_decision == "ESCALATE":
        _transition(
            order,
            PENDING_MERCHANT_APPROVAL,
            f"over the merchant's confirmation threshold ({order['reason']}); awaiting her answer",
        )
        _tell(
            phone,
            f"Payment received for order #{order_ref}. The restaurant is confirming "
            "this one — update to follow.",
        )
        _tell_merchant(f"New order #{order_ref}, Rs.{total} — reply ACCEPT or REJECT.")
        # Register with the existing escalation ledger -- without its own
        # alert, since the message above is already worded for a paid
        # order -- so an inbound ACCEPT/REJECT reaches the same resolver
        # every other protocol uses. One inbound handler, not two.
        try:
            import escalations

            escalations.notify(
                PROTOCOL,
                str(order_ref),
                {
                    "event_id": order_ref,
                    "agent_id": order["agent_id"],
                    "total_inr": total,
                    "reason": order["reason"],
                },
                [(line["item"], line["qty"]) for line in _cart_of(order)],
                send=False,
            )
        except Exception:
            pass
        return PENDING_MERCHANT_APPROVAL

    _transition(order, AUTO_CONFIRMED, "within the merchant's limits; confirmed automatically")
    _tell(phone, f"Order #{order_ref} accepted — on its way shortly!")
    # Informational only. Nothing is being asked of her.
    _tell_merchant(f"New order #{order_ref}, Rs.{total} — paid and auto-confirmed, no action needed.")
    return AUTO_CONFIRMED


# --------------------------------------------- step 3: the merchant answers

def _refund(order: dict, status: str, reason: str) -> dict:
    """Decline a paid order and return the money in the same breath.

    The refund is attempted before the terminal status is written, so an
    order can never sit marked REFUNDED without the refund having been
    called. If Razorpay refuses, the failure is recorded and the order
    stays visible as rejected-but-unrefunded rather than being quietly
    closed.
    """
    order_ref = order["id"]
    phone = order.get("delivery_phone")
    total = order["total_inr"]
    payment_id = order.get("payment_id")

    _transition(order, status, reason)

    if not payment_id:
        _transition(order, status, "no captured payment on this order; nothing to refund")
        _tell(phone, f"Order #{order_ref} couldn't be accepted.")
        return {"order_ref": order_ref, "status": status, "refunded": False}

    try:
        refund = razorpay_client.refund_payment(payment_id, total)
    except Exception as exc:
        _transition(
            order,
            status,
            f"refund of payment {payment_id} FAILED and needs manual attention: {exc}",
        )
        _tell(
            phone,
            f"Order #{order_ref} couldn't be accepted. Your refund of Rs.{total} is being "
            "processed — we'll be in touch.",
        )
        return {"order_ref": order_ref, "status": status, "refunded": False, "error": str(exc)}

    _transition(order, REFUNDED, f"refund {refund.get('id', '')} issued for payment {payment_id}")
    _tell(phone, f"Order #{order_ref} couldn't be accepted. Rs.{total} has been refunded.")
    return {"order_ref": order_ref, "status": REFUNDED, "refunded": True, "refund": refund}


def accept(order_ref: int) -> dict:
    order = get_order(order_ref)
    if order is None:
        raise ValueError(f"no such order: {order_ref}")
    if status_of(order_ref) not in _AWAITING_MERCHANT:
        raise ValueError(f"order #{order_ref} is not awaiting a decision")

    _transition(order, MERCHANT_ACCEPTED, "merchant accepted the order")
    _tell(
        order.get("delivery_phone"),
        f"Amma accepted order #{order_ref} — on its way shortly!",
    )
    return {"order_ref": order_ref, "status": MERCHANT_ACCEPTED}


def reject(order_ref: int) -> dict:
    order = get_order(order_ref)
    if order is None:
        raise ValueError(f"no such order: {order_ref}")
    if status_of(order_ref) not in _AWAITING_MERCHANT:
        raise ValueError(f"order #{order_ref} is not awaiting a decision")

    return _refund(order, MERCHANT_REJECTED, "merchant rejected the order")


def expire(order_ref: int) -> dict:
    """Treat a non-answer like a rejection, but record it as its own
    status so the trail can tell "she said no" from "she never replied".

    NOTE: nothing schedules this yet -- see CLAUDE.md. The project has no
    expiry mechanism for merchant escalations, and inventing a scheduler
    was out of scope for this change, so the capability exists and is
    tested but must currently be triggered deliberately.
    """
    order = get_order(order_ref)
    if order is None:
        raise ValueError(f"no such order: {order_ref}")
    if status_of(order_ref) not in _AWAITING_MERCHANT:
        raise ValueError(f"order #{order_ref} is not awaiting a decision")

    return _refund(
        order, MERCHANT_TIMEOUT_REFUNDED, "merchant did not answer in time; treated as declined"
    )


# ------------------------------------------------------------- the queue

def pending_orders() -> list[dict]:
    """Paid orders waiting on Amma, for her console."""
    return audit_log.get_orders_with_status(
        PENDING_MERCHANT_APPROVAL, db_path=_db(), protocol=PROTOCOL
    )
