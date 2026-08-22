"""Autonomous, no-browser settlement for a pre-authorised agent.

Why this module exists
----------------------
The human authorises a card once, in the buyer portal. After that the
agent is supposed to settle without anyone clicking anything -- that is
the whole premise of agentic commerce. A Razorpay Payment Link cannot do
that: it needs a browser, a card form and an OTP.

The correct production answer is Razorpay's Server-to-Server API, which
charges a card (or a saved token) directly from the server. S2S has to be
enabled by Razorpay on the account; on a standard test account it is not,
and both /v1/payments/create/json and /v1/payments/create/ajax refuse.

So this module does the honest thing available to us:

  1. Creates a REAL Razorpay Order via the real API. That part is
     genuine -- the order exists in the Razorpay dashboard.
  2. Records the capture as SIMULATED, and says so everywhere.

The simulated reference is prefixed `sim_`, never `pay_`. That prefix is
load-bearing: a real Razorpay payment id always starts `pay_`, so nothing
in the audit trail, the dashboard or the trust engine can mistake a
simulation for money that actually moved. An auditor scanning the trail
can separate the two with a string prefix.

If S2S is ever enabled on the account, `execute()` will use it and return
a real `pay_...` id with simulated=False. No caller needs to change.
"""

import os
from dataclasses import dataclass

import requests
from dotenv import load_dotenv

import audit_log
import razorpay_client

load_dotenv()

_KEY = os.environ.get("RAZORPAY_KEY_ID", "")
_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")

# The card the buyer portal shows as "on file". In a real deployment this
# would be a Razorpay token (token_...), never raw PAN -- storing card
# numbers puts you in PCI scope. It is here only because test mode gives
# us no token to work with.
_PREAUTH_CARD = {
    "number": "4100280000001007",
    "name": "Amma's Kitchen Buyer Agent",
    "expiry_month": 12,
    "expiry_year": 28,
    "cvv": "123",
}

_S2S_URL = "https://api.razorpay.com/v1/payments/create/json"


@dataclass(frozen=True)
class Settlement:
    payment_id: str
    order_id: str
    amount_inr: int
    simulated: bool
    method: str          # human-readable description of how it settled


def _try_s2s(order_id: str, amount_inr: int) -> str | None:
    """Attempt a genuine server-side charge. Returns a real payment id, or
    None if S2S is not available on this account."""
    try:
        response = requests.post(
            _S2S_URL,
            auth=(_KEY, _SECRET),
            timeout=15,
            json={
                "amount": amount_inr * 100,
                "currency": "INR",
                "order_id": order_id,
                "email": "buyer-agent@example.com",
                "contact": "9876543210",
                "method": "card",
                "card": _PREAUTH_CARD,
            },
        )
        if response.status_code == 200:
            payment_id = response.json().get("razorpay_payment_id") or response.json().get("id")
            if payment_id and payment_id.startswith("pay_"):
                return payment_id
        return None
    except requests.RequestException:
        return None


def execute(event_id: int, cart: list[tuple[str, int]], amount_inr: int) -> Settlement:
    """Settle an approved cart without a browser.

    The Razorpay Order is always real. The capture is real only if S2S is
    enabled; otherwise it is simulated and labelled as such.
    """
    description = " + ".join(f"{qty}x {name}" for name, qty in cart)
    order = razorpay_client.create_order(
        amount_inr=amount_inr, receipt=f"auto-{event_id}"
    )

    real_payment_id = _try_s2s(order["id"], amount_inr)

    if real_payment_id:
        settlement = Settlement(
            payment_id=real_payment_id,
            order_id=order["id"],
            amount_inr=amount_inr,
            simulated=False,
            method="Razorpay S2S card charge",
        )
    else:
        # `sim_` marks this as asserted by us, not settled by Razorpay.
        settlement = Settlement(
            payment_id=f"sim_{order['id'][6:]}",
            order_id=order["id"],
            amount_inr=amount_inr,
            simulated=True,
            method="simulated capture (S2S not enabled on this test account)",
        )

    audit_log.mark_paid(event_id, settlement.payment_id, db_path=audit_log.DEFAULT_DB_PATH)
    return settlement


def is_simulated(payment_id: str | None) -> bool:
    """One place that decides what counts as a real settlement, so the
    dashboard and consoles cannot drift apart on it."""
    return bool(payment_id) and payment_id.startswith("sim_")
