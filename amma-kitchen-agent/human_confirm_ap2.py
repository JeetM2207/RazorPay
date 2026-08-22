"""AP2 counterpart to human_confirm.py -- stands in for a human ops person
confirming an escalated Intent Mandate, until the real dashboard (build
order step 7) exists.

With no extra arguments, approves the order exactly as asked (only works
for Intent Mandates escalated specifically for being at/above the
human-confirm threshold). With item:qty arguments, proposes a smaller
cart instead -- that goes back through the real negotiation core, so it
only succeeds if the smaller cart genuinely clears the gate on its own.

Run:
    python human_confirm_ap2.py <intent_mandate_id>
    python human_confirm_ap2.py <intent_mandate_id> chicken_biryani:1
"""

import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv()

from razorpay_client import client as razorpay_sdk_client

AP2_BASE_URL = os.environ.get("AP2_BASE_URL", "http://127.0.0.1:8001")


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
        print("Usage: python human_confirm_ap2.py <intent_mandate_id> [item_id:qty ...]")
        sys.exit(1)
    intent_id = sys.argv[1]
    reduced_cart = _parse_reduced_cart(sys.argv[2:])
    request_body = {"items": reduced_cart} if reduced_cart is not None else {}

    resp = requests.post(
        f"{AP2_BASE_URL}/ap2/intent-mandates/{intent_id}/human-confirm", json=request_body
    )
    if resp.status_code != 200:
        print(f"Could not confirm (HTTP {resp.status_code}): {resp.json()}")
        sys.exit(1)

    mandate = resp.json()["intent_mandate"]
    print(f"Human-confirmed Intent Mandate {intent_id}. status={mandate['status']}")
    print(f"  reason: {mandate['decision_detail']['reason']}")

    cart_mandate = requests.post(
        f"{AP2_BASE_URL}/ap2/intent-mandates/{intent_id}/cart-mandate"
    ).json()["cart_mandate"]
    payment_mandate = requests.post(
        f"{AP2_BASE_URL}/ap2/cart-mandates/{cart_mandate['id']}/payment-mandate"
    ).json()["payment_mandate"]

    print(f"\nPay at: {payment_mandate['payment_link_url']}  (Rs.{payment_mandate['amount_inr']})")
    print("Use domestic test card 4100 2800 0000 1007, any future expiry/CVV, OTP any 4-10 digits.")

    _poll_payment(payment_mandate["payment_link_id"])


if __name__ == "__main__":
    main()
