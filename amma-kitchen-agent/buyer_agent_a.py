"""Simulated ACP-style buyer agent (build order step 4).

Plays the role of an external AI shopping assistant speaking ACP's shape:
open a checkout session, negotiate, and pay via a delegate token.

Claude is used ONLY to turn natural language into a structured cart via
forced tool use -- it never sees or influences the APPROVE/COUNTER_OFFER/
ESCALATE decision, which happens entirely inside the ACP adapter.

Run (with adapter_acp:app already running in another terminal):
    uvicorn adapter_acp:app --port 8000
    python buyer_agent_a.py "Get me 2 chicken biryanis" buyer-agent-a-demo
"""

import os
import sys
import time
from pathlib import Path

import requests
from anthropic import Anthropic
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv()

from mandate import MENU
from razorpay_client import client as razorpay_sdk_client

ACP_BASE_URL = os.environ.get("ACP_BASE_URL", "http://127.0.0.1:8000")
REQUEST_TEXT = sys.argv[1] if len(sys.argv) > 1 else "Get me 2 chicken biryanis"
AGENT_ID = sys.argv[2] if len(sys.argv) > 2 else "buyer-agent-a-demo"

# A buyer-side spending rule, separate from the merchant's mandate: this
# agent may accept a merchant-suggested upsell on its own authority only
# if it adds at most this much -- otherwise it should ask its own human,
# which this simulator represents by simply declining.
AUTO_ACCEPT_UPSELL_LIMIT_INR = 100

anthropic_client = Anthropic()

PROPOSE_CART_TOOL = {
    "name": "propose_cart",
    "description": (
        "Convert the buyer's natural language food order into a structured "
        "cart of catalog item ids and quantities."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "item_id": {"type": "string", "enum": list(MENU.keys())},
                        "qty": {"type": "integer", "minimum": 1},
                    },
                    "required": ["item_id", "qty"],
                },
            }
        },
        "required": ["items"],
    },
}


def parse_request_to_cart(text: str) -> list[dict]:
    response = anthropic_client.messages.create(
        model="claude-sonnet-5",
        max_tokens=512,
        tools=[PROPOSE_CART_TOOL],
        tool_choice={"type": "tool", "name": "propose_cart"},
        messages=[{"role": "user", "content": text}],
    )
    for block in response.content:
        if block.type == "tool_use":
            return block.input["items"]
    raise RuntimeError("Claude did not return a structured cart")


def _drive_session_to_conclusion(resp: dict) -> dict:
    detail = resp["decision_detail"]

    if detail["decision"] == "COUNTER_OFFER" and detail["alternatives"]:
        print(f"  {len(detail['alternatives'])} alternative(s) offered; auto-accepting the first.")
        resp = requests.post(
            f"{ACP_BASE_URL}/acp/checkout_sessions/{resp['session_id']}/accept_alternative",
            json={"index": 0},
        ).json()
        print(f"  -> status={resp['status']}  decision={resp['decision_detail']['decision']}")

    if resp["status"] == "ready_for_payment":
        upsell = resp["decision_detail"].get("upsell_suggestion")
        if upsell and upsell["price_inr"] <= AUTO_ACCEPT_UPSELL_LIMIT_INR:
            print(
                f"  Merchant suggested adding {upsell['item']} (Rs.{upsell['price_inr']}); "
                f"within my own auto-accept limit (Rs.{AUTO_ACCEPT_UPSELL_LIMIT_INR}), accepting."
            )
            resp = requests.post(
                f"{ACP_BASE_URL}/acp/checkout_sessions/{resp['session_id']}/accept_upsell"
            ).json()
            print(f"  -> status={resp['status']}  total=Rs.{resp['decision_detail']['total_inr']}")
        elif upsell:
            print(
                f"  Merchant suggested adding {upsell['item']} (Rs.{upsell['price_inr']}); "
                f"over my auto-accept limit, declining without asking my human."
            )

    return resp


def _poll_payment(payment_link_id: str) -> None:
    print("\nPolling payment status (Ctrl+C to stop)...")
    while True:
        link = razorpay_sdk_client.payment_link.fetch(payment_link_id)
        status = link["status"]
        print(f"  status = {status}")
        if status == "paid":
            print("\nPayment captured via the ACP adapter, end to end.")
            return
        if status in ("cancelled", "expired"):
            print(f"\nLink ended in status={status}, not paid.")
            return
        time.sleep(3)


def main() -> None:
    print(f"Buyer request: {REQUEST_TEXT!r}  (agent_id={AGENT_ID})")

    cart = parse_request_to_cart(REQUEST_TEXT)
    print(f"Parsed cart: {cart}")

    resp = requests.post(
        f"{ACP_BASE_URL}/acp/checkout_sessions",
        json={"agent_id": AGENT_ID, "items": cart},
    ).json()
    print(
        f"\nSession {resp['session_id']}: status={resp['status']} "
        f"trust_tier={resp['decision_detail']['trust_tier']}"
    )
    print(f"  decision={resp['decision_detail']['decision']}  reason={resp['decision_detail']['reason']}")

    resp = _drive_session_to_conclusion(resp)

    if resp["status"] == "requires_human":
        print("\nESCALATED -- a human must confirm this order. No payment call was made.")
        return
    if resp["status"] != "ready_for_payment":
        print(f"\nCould not reach a payable state (status={resp['status']}). Stopping.")
        return

    complete = requests.post(
        f"{ACP_BASE_URL}/acp/checkout_sessions/{resp['session_id']}/complete",
        json={"delegate_token": resp["delegate_token"]},
    ).json()
    print(f"\nPay at: {complete['payment_link_url']}  (Rs.{complete['amount_inr']})")
    print("Use domestic test card 4100 2800 0000 1007, any future expiry/CVV, OTP any 4-10 digits.")

    _poll_payment(complete["payment_link_id"])


if __name__ == "__main__":
    main()
