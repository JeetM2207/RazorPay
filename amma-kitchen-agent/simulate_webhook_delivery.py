"""Locally simulate Razorpay delivering the SAME webhook event twice.

This is the build order's step 6 test method ("test by deliberately
sending the same webhook event twice") without needing a public tunnel
(ngrok) for Razorpay's real servers to reach your localhost.

Constructs a payment_link.paid payload, signs it with HMAC-SHA256 using
your real RAZORPAY_WEBHOOK_SECRET from .env -- the exact same signing
Razorpay itself uses -- and POSTs it to the running webhook handler
twice, to prove the second delivery is correctly ignored.

Run:
    uvicorn webhook_handler:app --port 8002
    python simulate_webhook_delivery.py <payment_link_id> <payment_id>
"""

import hashlib
import hmac
import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv()

WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "http://127.0.0.1:8002/webhooks/razorpay")


def _build_payload(payment_link_id: str, payment_id: str) -> bytes:
    body = {
        "entity": "event",
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {"entity": {"id": payment_link_id, "status": "paid"}},
            "payment": {"entity": {"id": payment_id, "status": "captured"}},
        },
    }
    return json.dumps(body).encode()


def _sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: python simulate_webhook_delivery.py <payment_link_id> <payment_id>")
        sys.exit(1)
    payment_link_id, payment_id = sys.argv[1], sys.argv[2]

    secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")
    if not secret or "xxxx" in secret:
        print(
            "RAZORPAY_WEBHOOK_SECRET is not set to a real value in .env.\n"
            "Get one from Razorpay Dashboard -> Settings -> Webhooks -> "
            "(create or open a webhook) -- the Secret you set there."
        )
        sys.exit(1)

    body = _build_payload(payment_link_id, payment_id)
    signature = _sign(body, secret)
    headers = {"Content-Type": "application/json", "X-Razorpay-Signature": signature}

    print("Sending delivery #1 (first time Razorpay would send this)...")
    r1 = requests.post(WEBHOOK_URL, data=body, headers=headers)
    print(f"  -> {r1.status_code} {r1.json()}")

    print("Sending delivery #2 (Razorpay retrying/re-delivering the SAME event)...")
    r2 = requests.post(WEBHOOK_URL, data=body, headers=headers)
    print(f"  -> {r2.status_code} {r2.json()}")

    if r1.json().get("status") == "processed" and r2.json().get("status") == "duplicate_ignored":
        print("\nIdempotency confirmed: processed once, second delivery correctly ignored.")
    else:
        print("\nUnexpected outcome -- check the responses above.")


if __name__ == "__main__":
    main()
