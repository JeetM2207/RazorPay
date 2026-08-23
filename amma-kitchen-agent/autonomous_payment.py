"""Autonomous, no-browser settlement for a pre-authorised agent.

Why this module exists
----------------------
The human authorises a card once, in the buyer portal. After that the
agent is supposed to settle without anyone clicking anything -- that is
the whole premise of agentic commerce. A Razorpay Payment Link cannot do
that: it needs a browser, a card form and an OTP.

Two real settlement paths are attempted, in order:

  1. UPI collect to success@razorpay, Razorpay's official test VPA, which
     auto-approves in test mode. Preferred: no browser, no card data in
     scope, and a genuine payment id.
  2. A server-to-server card charge.

Both live behind Razorpay's S2S API, which has to be enabled on the
account. On a standard test account it is not, and every
/v1/payments/create/* endpoint answers "The requested URL was not found"
-- verified against this project's own keys for /upi, /json and /ajax.

So when neither is available this module does the honest thing left:

  1. Creates a REAL Razorpay Order via the real API. That part is
     genuine -- the order exists in the Razorpay dashboard.
  2. Records the capture as SIMULATED, and says so everywhere.

The simulated reference is prefixed `sim_`, never `pay_`. That prefix is
load-bearing: a real Razorpay payment id always starts `pay_`, so nothing
in the audit trail, the dashboard or the trust engine can mistake a
simulation for money that actually moved. An auditor scanning the trail
can separate the two with a string prefix.

If S2S is ever enabled on the account, `execute()` uses it and returns a
real `pay_...` id with simulated=False. No caller needs to change. To get
there, ask Razorpay Support to enable S2S / UPI collect on the merchant
account -- the code path is already here and tested.
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

# No card lives in this repository.
#
# The card S2S path needs one, so it is read from the environment and is
# absent by default -- with none configured, _try_s2s does not run at
# all. A payment credential checked into source is a payment credential
# regardless of whose test account it belongs to, and "it's only a test
# card" is exactly the habit that later commits a real one.
#
# In a real deployment this would be a Razorpay token (token_...) rather
# than a PAN in any case; raw card data puts you in PCI scope.
# RAZORPAY_S2S_TEST_CARD=number:MM:YYYY:cvv
_S2S_CARD_SPEC = os.environ.get("RAZORPAY_S2S_TEST_CARD", "").strip()


def _preauth_card() -> dict | None:
    if not _S2S_CARD_SPEC:
        return None
    try:
        number, month, year, cvv = _S2S_CARD_SPEC.split(":")
        return {
            "number": number.strip(),
            "name": "Amma's Kitchen Buyer Agent",
            "expiry_month": int(month),
            "expiry_year": int(year),
            "cvv": cvv.strip(),
        }
    except (ValueError, TypeError):
        return None

_S2S_CARD_URL = "https://api.razorpay.com/v1/payments/create/json"
_S2S_UPI_URL = "https://api.razorpay.com/v1/payments/create/upi"

# Razorpay's official test VPA: in test mode a collect request to this
# address is auto-approved, which is the closest thing to a genuine
# hands-off settlement. It needs the S2S UPI endpoint to be enabled on
# the account -- see the note in execute().
TEST_VPA = "success@razorpay"


@dataclass(frozen=True)
class Settlement:
    payment_id: str
    order_id: str
    amount_inr: int
    simulated: bool
    method: str          # human-readable description of how it settled


def _extract_payment_id(response) -> str | None:
    """Razorpay returns the id under different keys depending on the
    endpoint, so check both and insist on the real `pay_` prefix."""
    if response.status_code != 200:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    payment_id = body.get("razorpay_payment_id") or body.get("payment_id") or body.get("id")
    return payment_id if payment_id and payment_id.startswith("pay_") else None


def _try_upi_collect(order_id: str, amount_inr: int) -> str | None:
    """Send a UPI collect request to Razorpay's auto-approving test VPA.

    This is the preferred path: it settles with no browser and no card
    details, and produces a genuine `pay_` id. Returns None when the S2S
    UPI endpoint is not enabled on the account, which is the default.
    """
    try:
        response = requests.post(
            _S2S_UPI_URL,
            auth=(_KEY, _SECRET),
            timeout=20,
            json={
                "amount": amount_inr * 100,
                "currency": "INR",
                "order_id": order_id,
                "email": "buyer-agent@example.com",
                "contact": "9876543210",
                "method": "upi",
                "upi": {"flow": "collect", "vpa": TEST_VPA, "expiry_time": 5},
            },
        )
        return _extract_payment_id(response)
    except requests.RequestException:
        return None


def _try_s2s(order_id: str, amount_inr: int) -> str | None:
    """Fallback: a genuine server-side card charge. Requires S2S to be
    enabled AND a card supplied via RAZORPAY_S2S_TEST_CARD -- with none
    configured this path does not run, which is the default."""
    card = _preauth_card()
    if card is None:
        return None
    try:
        response = requests.post(
            _S2S_CARD_URL,
            auth=(_KEY, _SECRET),
            timeout=15,
            json={
                "amount": amount_inr * 100,
                "currency": "INR",
                "order_id": order_id,
                "email": "buyer-agent@example.com",
                "contact": "9876543210",
                "method": "card",
                "card": card,
            },
        )
        return _extract_payment_id(response)
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

    # UPI collect to the auto-approving test VPA first: no browser, no
    # card data, and a genuine payment id. Card S2S second. Both need
    # Razorpay to have enabled S2S on the account -- a default test
    # account has neither, and every /v1/payments/create/* endpoint
    # answers "URL was not found" until it does.
    real_payment_id = _try_upi_collect(order["id"], amount_inr)
    method = f"Razorpay UPI collect to {TEST_VPA}"

    if not real_payment_id:
        real_payment_id = _try_s2s(order["id"], amount_inr)
        method = "Razorpay S2S card charge"

    if real_payment_id:
        settlement = Settlement(
            payment_id=real_payment_id,
            order_id=order["id"],
            amount_inr=amount_inr,
            simulated=False,
            method=method,
        )
    else:
        # `sim_` marks this as asserted by us, not settled by Razorpay.
        settlement = Settlement(
            payment_id=f"sim_{order['id'][6:]}",
            order_id=order["id"],
            amount_inr=amount_inr,
            simulated=True,
            method=(
                "simulated capture - UPI collect and card S2S both need "
                "Razorpay to enable S2S on this account"
            ),
        )

    audit_log.mark_paid(event_id, settlement.payment_id, db_path=audit_log.DEFAULT_DB_PATH)
    return settlement


def is_simulated(payment_id: str | None) -> bool:
    """One place that decides what counts as a real settlement, so the
    dashboard and consoles cannot drift apart on it."""
    return bool(payment_id) and payment_id.startswith("sim_")
