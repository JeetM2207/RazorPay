"""Simulated AP2-style buyer agent (build order step 5).

Speaks AP2's mandate-chain shape (Intent Mandate -> Cart Mandate ->
Payment Mandate) instead of ACP's checkout-session shape used by
buyer_agent_a.py -- yet negotiates through the EXACT SAME negotiation
core and orchestrator, with zero changes to either file. That's the
whole point of this script existing: proof the intelligence is
protocol-agnostic, only the envelope shape differs.

Claude (via OpenRouter, see llm_client.py) is used ONLY to turn natural
language into a structured cart -- it never sees or influences the
APPROVE/COUNTER_OFFER/ESCALATE decision.

Run (with adapter_ap2:app already running in another terminal):
    uvicorn adapter_ap2:app --port 8001
    python buyer_agent_b.py "Get me one veg thali" buyer-agent-b-demo
"""

import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv()

from llm_client import call_with_forced_tool
from mandate import MENU
from razorpay_client import client as razorpay_sdk_client

AP2_BASE_URL = os.environ.get("AP2_BASE_URL", "http://127.0.0.1:8001")
REQUEST_TEXT = sys.argv[1] if len(sys.argv) > 1 else "Get me one veg thali"
AGENT_ID = sys.argv[2] if len(sys.argv) > 2 else "buyer-agent-b-demo"

# Travels as part of THIS buyer's Intent Mandate itself (unlike
# buyer_agent_a's locally-hardcoded constant) -- AP2's real design lets a
# user's spending authorization ride along as data on the mandate object.
AUTO_CONFIRM_LIMIT_INR = 250


def parse_request_to_cart(text: str) -> list[dict]:
    args = call_with_forced_tool(
        text,
        tool_name="propose_cart",
        description=(
            "Convert the buyer's natural language food order into a structured "
            "cart of catalog item ids and quantities."
        ),
        parameters={
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
    )
    return args["items"]


def _drive_intent_to_conclusion(mandate: dict) -> dict:
    detail = mandate["decision_detail"]

    if detail["decision"] == "COUNTER_OFFER" and detail["alternatives"]:
        print(f"  {len(detail['alternatives'])} alternative(s) offered; auto-accepting the first.")
        mandate = requests.post(
            f"{AP2_BASE_URL}/ap2/intent-mandates/{mandate['id']}/accept-alternative",
            json={"index": 0},
        ).json()["intent_mandate"]
        print(f"  -> status={mandate['status']}  decision={mandate['decision_detail']['decision']}")

    if mandate["status"] == "cart_ready":
        upsell = mandate["decision_detail"].get("upsell_suggestion")
        if upsell and upsell["price_inr"] <= AUTO_CONFIRM_LIMIT_INR:
            print(
                f"  Merchant suggested adding {upsell['item']} (Rs.{upsell['price_inr']}); "
                f"within my Intent Mandate's auto-confirm limit "
                f"(Rs.{AUTO_CONFIRM_LIMIT_INR}), accepting."
            )
            mandate = requests.post(
                f"{AP2_BASE_URL}/ap2/intent-mandates/{mandate['id']}/accept-upsell"
            ).json()["intent_mandate"]
            print(f"  -> status={mandate['status']}  total=Rs.{mandate['decision_detail']['total_inr']}")
        elif upsell:
            print(
                f"  Merchant suggested adding {upsell['item']} (Rs.{upsell['price_inr']}); "
                f"over my Intent Mandate's auto-confirm limit, declining without asking my human."
            )

    return mandate


def _poll_payment(payment_link_id: str) -> None:
    print("\nPolling payment status (Ctrl+C to stop)...")
    while True:
        link = razorpay_sdk_client.payment_link.fetch(payment_link_id)
        status = link["status"]
        print(f"  status = {status}")
        if status == "paid":
            print("\nPayment captured via the AP2 adapter, end to end.")
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
        f"{AP2_BASE_URL}/ap2/intent-mandates",
        json={
            "agent_id": AGENT_ID,
            "intent": {"items": cart, "auto_confirm_limit_inr": AUTO_CONFIRM_LIMIT_INR},
        },
    ).json()
    mandate = resp["intent_mandate"]
    print(
        f"\nIntent Mandate {mandate['id']}: status={mandate['status']} "
        f"trust_tier={mandate['decision_detail']['trust_tier']}"
    )
    print(f"  decision={mandate['decision_detail']['decision']}  reason={mandate['decision_detail']['reason']}")

    mandate = _drive_intent_to_conclusion(mandate)

    if mandate["status"] == "requires_human":
        print("\nESCALATED -- a human must confirm this order. No payment call was made.")
        print(f"To confirm as the merchant: python human_confirm_ap2.py {mandate['id']}")
        return
    if mandate["status"] != "cart_ready":
        print(f"\nCould not reach a payable state (status={mandate['status']}). Stopping.")
        return

    cart_mandate = requests.post(
        f"{AP2_BASE_URL}/ap2/intent-mandates/{mandate['id']}/cart-mandate"
    ).json()["cart_mandate"]
    print(f"\nCart Mandate {cart_mandate['id']} locked: Rs.{cart_mandate['total_inr']}")

    payment_mandate = requests.post(
        f"{AP2_BASE_URL}/ap2/cart-mandates/{cart_mandate['id']}/payment-mandate"
    ).json()["payment_mandate"]
    print(
        f"Payment Mandate {payment_mandate['id']}  "
        f"hash={payment_mandate['matched_mandate_hash'][:12]}..."
    )
    print(f"\nPay at: {payment_mandate['payment_link_url']}  (Rs.{payment_mandate['amount_inr']})")
    print("Use domestic test card 4100 2800 0000 1007, any future expiry/CVV, OTP any 4-10 digits.")

    _poll_payment(payment_mandate["payment_link_id"])


if __name__ == "__main__":
    main()
