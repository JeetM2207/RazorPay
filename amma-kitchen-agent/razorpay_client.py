"""Thin wrapper around Razorpay's test-mode APIs.

No business logic here — just typed calls the rest of the system uses.
Reads credentials from environment (see .env.example).
"""

import os

import razorpay
from dotenv import load_dotenv

load_dotenv()

_KEY_ID = os.environ.get("RAZORPAY_KEY_ID")
_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET")

if not _KEY_ID or not _KEY_SECRET:
    raise RuntimeError(
        "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET not set. Copy .env.example to "
        ".env and fill in your Razorpay TEST mode keys from the dashboard "
        "(Settings > API Keys)."
    )

client = razorpay.Client(auth=(_KEY_ID, _KEY_SECRET))


def create_order(amount_inr: int, receipt: str) -> dict:
    """Create a Razorpay order for amount_inr rupees. Returns the order object."""
    return client.order.create(
        {
            "amount": amount_inr * 100,  # paise
            "currency": "INR",
            "receipt": receipt,
            "payment_capture": 1,  # auto-capture on authorization
        }
    )


def create_payment_link(
    amount_inr: int,
    description: str,
    reference_id: str,
    customer_name: str = "Test Buyer Agent",
    customer_email: str = "buyer-agent@example.com",
    customer_contact: str = "9876543210",
) -> dict:
    """Create a hosted Payment Link. Buyer pays at link['short_url'].

    This needs no frontend code — Razorpay hosts the checkout page.
    """
    return client.payment_link.create(
        {
            "amount": amount_inr * 100,
            "currency": "INR",
            "description": description,
            "reference_id": reference_id,
            "customer": {
                "name": customer_name,
                "email": customer_email,
                "contact": customer_contact,
            },
            "notify": {"sms": False, "email": False},
            "reminder_enable": False,
        }
    )


def fetch_payment_link(payment_link_id: str) -> dict:
    return client.payment_link.fetch(payment_link_id)


def fetch_payment(payment_id: str) -> dict:
    return client.payment.fetch(payment_id)


def verify_webhook_signature(body: bytes, signature: str, webhook_secret: str) -> bool:
    try:
        client.utility.verify_webhook_signature(
            body.decode() if isinstance(body, bytes) else body,
            signature,
            webhook_secret,
        )
        return True
    except razorpay.errors.SignatureVerificationError:
        return False
