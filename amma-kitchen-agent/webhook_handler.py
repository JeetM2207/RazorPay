"""Idempotent handling of Razorpay's payment_link.paid / expired / cancelled
webhooks.

Razorpay delivers webhooks with AT-LEAST-ONCE semantics: the same event
can and will arrive more than once in production (retries, network
blips, dashboard replays). This file exists specifically to make sure
that never causes a double-fulfillment or a duplicated audit-trail entry.

Idempotency is enforced at the database level via a UNIQUE constraint on
(event_type, payment_link_id) in processed_webhook_events -- not just an
application-level "have I seen this before?" check, which would have a
race window between the check and the write under concurrent deliveries.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request

import audit_log
import razorpay_client

load_dotenv()

app = FastAPI(title="Amma's Kitchen -- Razorpay Webhook Handler")

_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")

_HANDLED_EVENTS = ("payment_link.paid", "payment_link.expired", "payment_link.cancelled")

_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_webhook_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    payment_link_id TEXT NOT NULL,
    received_at TEXT NOT NULL,
    UNIQUE(event_type, payment_link_id)
);
"""


def _init_events_table(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(_EVENTS_SCHEMA)


def _claim_event(event_type: str, payment_link_id: str, db_path: str) -> bool:
    """Returns True only for the delivery that actually gets to process
    this event. The UNIQUE constraint (not a prior SELECT) is what makes
    this safe under near-simultaneous duplicate deliveries."""
    _init_events_table(db_path)
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO processed_webhook_events (event_type, payment_link_id, received_at) "
                "VALUES (?, ?, ?)",
                (event_type, payment_link_id, datetime.now(timezone.utc).isoformat()),
            )
        return True
    except sqlite3.IntegrityError:
        return False


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
    if not _claim_event(event_type, payment_link_id, db_path):
        # A duplicate delivery of an event we've already fully processed.
        # This is the case the whole file exists to get right.
        return {"status": "duplicate_ignored", "event": event_type, "payment_link_id": payment_link_id}

    if event_type == "payment_link.paid":
        payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
        result = _handle_paid(payment_link_id, payment_entity, db_path)
    else:
        result = _handle_not_paid(payment_link_id, event_type, db_path)

    return {"status": result, "event": event_type, "payment_link_id": payment_link_id}
