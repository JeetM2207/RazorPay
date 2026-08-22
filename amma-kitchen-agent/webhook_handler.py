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
from fastapi import FastAPI, HTTPException, Request

import audit_log
import idempotency
import razorpay_client

load_dotenv()

app = FastAPI(title="Amma's Kitchen -- Razorpay Webhook Handler")

_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")

_HANDLED_EVENTS = ("payment_link.paid", "payment_link.expired", "payment_link.cancelled")

def _handle_paid(payment_link_id: str, payment_entity: dict, db_path: str) -> str:
    original = audit_log.get_event_by_payment_link(payment_link_id, db_path=db_path)
    if original is None:
        return "processed_unmatched"
    audit_log.mark_paid(original["id"], payment_entity.get("id", ""), db_path=db_path)
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


@app.post("/webhooks/razorpay")
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

    payment_link_entity = payload.get("payload", {}).get("payment_link", {}).get("entity", {})
    payment_link_id = payment_link_entity.get("id")
    if not payment_link_id:
        raise HTTPException(400, "missing payment_link id in webhook payload")

    db_path = audit_log.DEFAULT_DB_PATH
    if not idempotency.claim_event(event_type, payment_link_id, db_path):
        # A duplicate delivery of an event we've already fully processed.
        # This is the case the whole file exists to get right.
        return {"status": "duplicate_ignored", "event": event_type, "payment_link_id": payment_link_id}

    if event_type == "payment_link.paid":
        payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
        result = _handle_paid(payment_link_id, payment_entity, db_path)
    else:
        result = _handle_not_paid(payment_link_id, event_type, db_path)

    return {"status": result, "event": event_type, "payment_link_id": payment_link_id}
