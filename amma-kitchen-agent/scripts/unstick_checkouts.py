"""Release checkout locks whose payment link was never created.

adapter_mcp.checkout claims the shared idempotency ledger BEFORE asking
Razorpay for a payment link, which makes that claim a lock rather than a
record of a fact. If the link creation then fails -- Razorpay refuses, the
network drops -- the lock used to be held forever, and that exact cart
became unbuyable by that agent: every retry was told "a checkout for this
cart is already underway" with nothing underway.

checkout now releases the lock itself when that happens, so this script is
for locks stranded BEFORE that fix, or by a process killed mid-checkout.

A lock is only released when the audit trail proves the work did not
happen: the decision row exists, and it has no payment_link_id. A cart
that got as far as a real link keeps its lock, because a retry there must
return the original order rather than make a second one.

    python scripts/unstick_checkouts.py            # report only
    python scripts/unstick_checkouts.py --release  # actually release
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import adapter_mcp
import audit_log
import idempotency

CLAIM_KIND = "mcp.checkout"


def _held(db_path: str) -> set[str]:
    try:
        with sqlite3.connect(db_path) as conn:
            return {
                row[0]
                for row in conn.execute(
                    "SELECT payment_link_id FROM processed_webhook_events WHERE event_type = ?",
                    (CLAIM_KIND,),
                )
            }
    except sqlite3.Error:
        return set()


def find_stuck(db_path: str) -> list[tuple[dict, str]]:
    held = _held(db_path)
    events = audit_log.get_all_events(db_path=db_path, limit=1000)

    # A fingerprint that reached a real payment link is NOT stuck, and its
    # lock must stay: a retry has to return the original order rather than
    # make a second one. The same agent+cart often has several decision
    # rows -- one per propose_cart -- and only the one checkout used gets
    # the link, so judging a row on its own would flag every sibling.
    settled = set()
    for event in events:
        if event["protocol"] != adapter_mcp.PROTOCOL or not event["payment_link_id"]:
            continue
        cart = adapter_mcp._cart_from(event)
        if cart:
            settled.add(adapter_mcp._fingerprint(event["agent_id"], cart))

    stuck: list[tuple[dict, str]] = []
    seen: set[str] = set()
    for event in events:
        if event["protocol"] != adapter_mcp.PROTOCOL or event["payment_link_id"]:
            continue
        if event["decision"] not in ("APPROVE", "ESCALATE"):
            continue
        cart = adapter_mcp._cart_from(event)
        if not cart:
            continue
        fingerprint = adapter_mcp._fingerprint(event["agent_id"], cart)
        if fingerprint in held and fingerprint not in settled and fingerprint not in seen:
            seen.add(fingerprint)
            stuck.append((event, fingerprint))
    return stuck


def main() -> int:
    release = "--release" in sys.argv
    db_path = audit_log.DEFAULT_DB_PATH
    stuck = find_stuck(db_path)

    if not stuck:
        print("No stuck checkout locks.")
        return 0

    print(f"{len(stuck)} cart(s) locked with no payment link:\n")
    for event, fingerprint in stuck:
        cart = ", ".join(f"{qty}x {name}" for name, qty in adapter_mcp._cart_from(event))
        print(f"  event {event['id']}  {event['agent_id']}  {cart}  (Rs.{event['total_inr']})")
        if release:
            freed = idempotency.release_claim(CLAIM_KIND, fingerprint, db_path)
            print(f"      -> {'released' if freed else 'already released'}")

    if not release:
        print("\nNothing changed. Re-run with --release to free them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
