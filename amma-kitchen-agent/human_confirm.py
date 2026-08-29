"""Stands in for a human ops person clicking 'confirm' on an escalated
order, until the real dashboard (build order step 7) exists.

With no extra arguments, approves the order exactly as asked (only works
for orders escalated specifically for being at/above the human-confirm
threshold -- disallowed categories and unknown items are hard merchant
rules and can never be approved this way; the endpoint enforces that).

With item:qty arguments, proposes a SMALLER cart instead of a blanket
approval -- e.g. rejecting the part that's too much while still selling
what's left. This goes back through the real negotiation core, so it
only succeeds if the smaller cart genuinely clears the gate on its own.

Run:
    python human_confirm.py <session_id>
    python human_confirm.py <session_id> chicken_biryani:1
"""

import os
import sys
import time
from pathlib import Path

import requests

import merchant_session
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv()

from razorpay_client import client as razorpay_sdk_client

ACP_BASE_URL = os.environ.get("ACP_BASE_URL", "http://127.0.0.1:8000")


def _poll_payment(payment_link_id: str) -> None:
    print("\nPolling payment status (Ctrl+C to stop)...")
    while True:
        link = razorpay_sdk_client.payment_link.fetch(payment_link_id)
        status = link["status"]
        print(f"  status = {status}")
        if status == "paid":
            print("\nPayment captured after human override.")
            return
        if status in ("cancelled", "expired"):
            print(f"\nLink ended in status={status}, not paid.")
            return
        time.sleep(3)


def _parse_reduced_cart(args: list[str]) -> list[dict] | None:
    if not args:
        return None
    items = []
    for arg in args:
        item_id, qty = arg.split(":")
        items.append({"item_id": item_id, "qty": int(qty)})
    return items


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python human_confirm.py <session_id> [item_id:qty ...]")
        sys.exit(1)
    session_id = sys.argv[1]
    reduced_cart = _parse_reduced_cart(sys.argv[2:])
    request_body = {"items": reduced_cart} if reduced_cart is not None else {}

    merchant = merchant_session.login(ACP_BASE_URL)
    resp = merchant.post(
        f"{ACP_BASE_URL}/acp/checkout_sessions/{session_id}/human_confirm", json=request_body
    )
    if resp.status_code != 200:
        print(f"Could not confirm (HTTP {resp.status_code}): {resp.json()}")
        sys.exit(1)

    confirmed = resp.json()
    print(f"Human-confirmed session {session_id}. status={confirmed['status']}")
    print(f"  reason: {confirmed['decision_detail']['reason']}")

    complete = requests.post(
        f"{ACP_BASE_URL}/acp/checkout_sessions/{session_id}/complete",
        json={"delegate_token": confirmed["delegate_token"]},
    ).json()
    print(f"\nPay at: {complete['payment_link_url']}  (Rs.{complete['amount_inr']})")
    print("Use domestic test card 4100 2800 0000 1007, any future expiry/CVV, OTP any 4-10 digits.")

    _poll_payment(complete["payment_link_id"])


if __name__ == "__main__":
    main()
