"""Reconcile the audit trail against Razorpay's own record of the truth.

Webhooks are the fast path, but they can be missed -- the server was
down, the tunnel was closed, the delivery failed every retry. Real
payment systems always keep a reconciliation job as the safety net, and
so does this one.

For every audit event that has a payment link but no recorded payment,
this asks Razorpay what actually happened to that link, records the
answer, and runs whatever follow-up the webhook would have run. It claims through the SAME idempotency ledger the webhook
handler uses (see idempotency.py), so a fact already recorded by a
webhook is never recorded twice here, and vice versa.

Run:
    python reconcile_payments.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import audit_log
import idempotency
from razorpay_client import client as razorpay_sdk_client


def _unresolved_events(db_path: str) -> list[dict]:
    return [
        event
        for event in audit_log.get_all_events(db_path=db_path, limit=1000)
        if event["payment_link_id"] and not event["payment_id"]
    ]


def _captured_payment_id(link: dict) -> str | None:
    for payment in link.get("payments") or []:
        if payment.get("status") == "captured":
            return payment.get("payment_id")
    return None


def reconcile(db_path: str = None) -> dict:
    db_path = db_path or audit_log.DEFAULT_DB_PATH
    stats = {"checked": 0, "marked_paid": 0, "marked_not_completed": 0, "still_open": 0}

    for event in _unresolved_events(db_path):
        link_id = event["payment_link_id"]
        stats["checked"] += 1
        link = razorpay_sdk_client.payment_link.fetch(link_id)
        status = link["status"]

        if status == "paid":
            payment_id = _captured_payment_id(link)
            if not payment_id:
                stats["still_open"] += 1
                continue
            if idempotency.claim_event("payment_link.paid", link_id, db_path):
                audit_log.mark_paid(event["id"], payment_id, db_path=db_path)
                stats["marked_paid"] += 1
                print(f"  event {event['id']}: link {link_id} -> PAID ({payment_id})")
                # Recording the payment is only half of it. An MCP order
                # still has to be confirmed to the customer and put in
                # front of Amma, and if the webhook never arrived this is
                # the only path that will do it. Same function the
                # webhook calls, claimed through the same ledger.
                import mcp_orders

                mcp_orders.follow_up_after_capture(event, payment_id)
            else:
                # A webhook already handled this one; nothing to redo.
                stats["still_open"] += 1

        elif status in ("expired", "cancelled"):
            if idempotency.claim_event(f"payment_link.{status}", link_id, db_path):
                audit_log.record_event(
                    agent_id=event["agent_id"],
                    protocol=event["protocol"],
                    cart=json.loads(event["cart_json"]),
                    decision="PAYMENT_NOT_COMPLETED",
                    reason=f"reconciliation found payment link {status} before payment",
                    total_inr=event["total_inr"],
                    db_path=db_path,
                )
                stats["marked_not_completed"] += 1
                print(f"  event {event['id']}: link {link_id} -> {status.upper()}")
            else:
                stats["still_open"] += 1
        else:
            stats["still_open"] += 1

    return stats


def main() -> None:
    print("Reconciling audit trail against Razorpay...")
    stats = reconcile()
    print(
        f"\nChecked {stats['checked']} unresolved event(s): "
        f"{stats['marked_paid']} marked paid, "
        f"{stats['marked_not_completed']} marked not completed, "
        f"{stats['still_open']} still open."
    )


if __name__ == "__main__":
    main()
