"""Stands in for a human ops person clicking 'confirm' on an escalated
order, until the real dashboard (build order step 7) exists.

Only works for orders escalated specifically for being at/above the
human-confirm threshold. Disallowed categories and unknown items are hard
merchant rules and can never be approved this way -- the endpoint itself
enforces that, this script just surfaces the result.

Run:
    python human_confirm.py <session_id>
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


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python human_confirm.py <session_id>")
        sys.exit(1)
    session_id = sys.argv[1]

    resp = requests.post(f"{ACP_BASE_URL}/acp/checkout_sessions/{session_id}/human_confirm")
    if resp.status_code != 200:
        print(f"Could not confirm (HTTP {resp.status_code}): {resp.json()}")
        sys.exit(1)

    body = resp.json()
    print(f"Human-confirmed session {session_id}. status={body['status']}")
    print(f"  reason: {body['decision_detail']['reason']}")

    complete = requests.post(
        f"{ACP_BASE_URL}/acp/checkout_sessions/{session_id}/complete",
        json={"delegate_token": body["delegate_token"]},
    ).json()
    print(f"\nPay at: {complete['payment_link_url']}  (Rs.{complete['amount_inr']})")
    print("Use domestic test card 4100 2800 0000 1007, any future expiry/CVV, OTP any 4-10 digits.")

    _poll_payment(complete["payment_link_id"])


if __name__ == "__main__":
    main()
