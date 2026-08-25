"""Idempotent handling of Razorpay's payment_link.paid / expired / cancelled
webhooks.

Razorpay delivers webhooks with AT-LEAST-ONCE semantics: the same event
can and will arrive more than once in production (retries, network
blips, dashboard replays). This file exists specifically to make sure
that never causes a double-fulfillment or a duplicated audit-trail entry.

Idempotency is enforced at the database level (see idempotency.py) via a
UNIQUE constraint -- not an application-level "have I seen this before?"
check, which would have a race window between the check and the write
under concurrent deliveries. That same ledger is shared with
reconcile_payments.py, so the webhook path and the reconciliation path
can never double-record the same fact.
"""

import json
import os

from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI, HTTPException, Request

import audit_log
import idempotency
import razorpay_client

load_dotenv()

router = APIRouter()

_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")

_HANDLED_EVENTS = (
    "payment_link.paid",
    "payment_link.expired",
    "payment_link.cancelled",
    # Issuing a refund returns a `created` refund immediately; whether the
    # money reaches the customer is settled later, and in live mode days
    # later. Without these, a refund Razorpay went on to FAIL would sit in
    # the trail as REFUNDED while the customer had nothing.
    "refund.processed",
    "refund.failed",
)


def _key_field(event_type: str, key: str) -> dict:
    """Report the id under the name it actually has. A refund event is
    keyed by the refund's own id, and calling that a payment_link_id in
    the response would be a small lie that costs someone ten minutes."""
    return {"refund_id" if event_type.startswith("refund.") else "payment_link_id": key}


def _handle_refund(refund_entity: dict, db_path: str) -> str:
    payment_id = refund_entity.get("payment_id") or ""
    original = audit_log.get_event_by_payment_id(payment_id, db_path=db_path)
    if original is None:
        return "processed_unmatched"
    if original["protocol"] != "mcp":
        # Only the Claude-chat path refunds automatically; a refund on any
        # other protocol was issued by hand and has no lifecycle to move.
        return "processed"

    import mcp_orders

    mcp_orders.on_refund_settled(dict(original, payment_id=payment_id), refund_entity)
    return "processed"

def _handle_paid(payment_link_id: str, payment_entity: dict, db_path: str) -> str:
    original = audit_log.get_event_by_payment_link(payment_link_id, db_path=db_path)
    if original is None:
        return "processed_unmatched"
    payment_id = payment_entity.get("id", "")
    audit_log.mark_paid(original["id"], payment_id, db_path=db_path)

    # The Claude-chat path continues after payment: confirm it, or ask
    # Amma and refund if she declines. The follow-up is shared with the
    # reconciler rather than written out here, so the fast path and the
    # safety net cannot drift apart -- it is a no-op for ACP, AP2 and
    # x402, which finish at capture. Already claimed through the shared
    # ledger by the caller, so it runs once however many times Razorpay
    # delivers the webhook.
    import mcp_orders

    mcp_orders.follow_up_after_capture(original, payment_id)

    return "processed"


def _handle_not_paid(payment_link_id: str, event_type: str, db_path: str) -> str:
    original = audit_log.get_event_by_payment_link(payment_link_id, db_path=db_path)
    if original is None:
        return "processed_unmatched"
    # Append-only: this is a NEW audit row describing how the story ended,
    # never a mutation of the original decision that was already recorded.
    audit_log.record_event(
        agent_id=original["agent_id"],
        protocol=original["protocol"],
        cart=json.loads(original["cart_json"]),
        decision="PAYMENT_NOT_COMPLETED",
        reason=f"payment link ended in {event_type} before payment",
        total_inr=original["total_inr"],
        db_path=db_path,
    )
    return "processed"


@router.post("/webhooks/razorpay")
async def handle_razorpay_webhook(request: Request) -> dict:
    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    if not _WEBHOOK_SECRET:
        raise HTTPException(500, "RAZORPAY_WEBHOOK_SECRET not configured")
    if not razorpay_client.verify_webhook_signature(body, signature, _WEBHOOK_SECRET):
        raise HTTPException(400, "invalid webhook signature")

    payload = json.loads(body)
    event_type = payload.get("event", "")

    if event_type not in _HANDLED_EVENTS:
        # Acknowledge anyway so Razorpay doesn't keep retrying an event
        # type we intentionally don't act on.
        return {"status": "ignored", "event": event_type}

    # A refund event carries no payment link at all, so it is keyed by the
    # refund's own id -- which is what makes the shared ledger able to
    # deduplicate it exactly as it does a capture.
    refund_entity = payload.get("payload", {}).get("refund", {}).get("entity", {})
    payment_link_entity = payload.get("payload", {}).get("payment_link", {}).get("entity", {})
    payment_link_id = refund_entity.get("id") if event_type.startswith("refund.") else (
        payment_link_entity.get("id")
    )
    if not payment_link_id:
        raise HTTPException(400, "missing payment_link or refund id in webhook payload")

    db_path = audit_log.DEFAULT_DB_PATH
    if not idempotency.claim_event(event_type, payment_link_id, db_path):
        # A duplicate delivery of an event we've already fully processed.
        # This is the case the whole file exists to get right.
        return {"status": "duplicate_ignored", "event": event_type, **_key_field(event_type, payment_link_id)}

    if event_type.startswith("refund."):
        result = _handle_refund(refund_entity, db_path)
    elif event_type == "payment_link.paid":
        payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
        result = _handle_paid(payment_link_id, payment_entity, db_path)
    else:
        result = _handle_not_paid(payment_link_id, event_type, db_path)

    return {"status": result, "event": event_type, **_key_field(event_type, payment_link_id)}


app = FastAPI(title="Amma's Kitchen -- Razorpay Webhook Handler")
app.include_router(router)
