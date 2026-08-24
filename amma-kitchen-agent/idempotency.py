"""Shared idempotency ledger.

There are two independent paths by which this system can learn that a
payment link reached a terminal state: Razorpay's webhook (fast path)
and reconcile_payments.py polling Razorpay directly (safety net, for
when a webhook is missed because the server was down or the tunnel was
closed). Both must be able to run without ever double-recording the same
fact, so they claim through this one ledger.

Claiming is enforced by a UNIQUE constraint at the database level rather
than a check-then-write in application code -- the constraint is what
makes this safe when two deliveries land at nearly the same instant, not
merely one after the other.
"""

import sqlite3
from datetime import datetime, timezone

_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_webhook_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    payment_link_id TEXT NOT NULL,
    received_at TEXT NOT NULL,
    UNIQUE(event_type, payment_link_id)
);
"""


def init_ledger(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(_SCHEMA)


def claim_event(event_type: str, payment_link_id: str, db_path: str) -> bool:
    """Returns True only for the caller that actually gets to process this
    event. Every later caller for the same (event_type, payment_link_id)
    gets False, whether it arrived via webhook or via reconciliation."""
    init_ledger(db_path)
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO processed_webhook_events "
                "(event_type, payment_link_id, received_at) VALUES (?, ?, ?)",
                (event_type, payment_link_id, datetime.now(timezone.utc).isoformat()),
            )
        return True
    except sqlite3.IntegrityError:
        return False


def release_claim(event_type: str, payment_link_id: str, db_path: str) -> bool:
    """Give a claim back because the work it was guarding never happened.

    A claim means "this fact is recorded" for the webhook and reconciler,
    which is why they never release: the fact stays true. But a caller
    that claims BEFORE doing work -- adapter_mcp.checkout claims, then
    asks Razorpay for a payment link -- is using this as a lock, and a
    lock that is never released after a failure is a permanent one. That
    is not theoretical: a Razorpay error mid-checkout left one cart
    unbuyable for one agent forever, answering "already underway" to
    every retry with nothing actually underway.

    Only ever call this when the guarded work provably did not happen.
    """
    init_ledger(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM processed_webhook_events "
            "WHERE event_type = ? AND payment_link_id = ?",
            (event_type, payment_link_id),
        )
        return cursor.rowcount > 0
