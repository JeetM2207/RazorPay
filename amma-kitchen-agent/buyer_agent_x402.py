"""Simulated x402-style buyer agent (protocol #3).

Speaks the HTTP 402 challenge/response flow: ask for the resource, get
told what it costs, pay, then ask again carrying proof. Notice there is
no "checkout" step anywhere in this script -- the SAME request is simply
made twice, which is what makes x402 different in shape from ACP's
sessions and AP2's mandate chain.

Underneath it is the same negotiation core, unchanged. Claude (via
OpenRouter) only turns words into a cart.

Run (with the server up):
    uvicorn app:app --port 8000
    python buyer_agent_x402.py "Get me one masala dosa" x402-buyer-demo
"""

import json
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

BASE_URL = os.environ.get("X402_BASE_URL", "http://127.0.0.1:8000")
REQUEST_TEXT = sys.argv[1] if len(sys.argv) > 1 else "Get me one masala dosa"
AGENT_ID = sys.argv[2] if len(sys.argv) > 2 else "x402-buyer-demo"


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


def _wait_for_payment(payment_link_id: str) -> str | None:
    """Poll Razorpay until the human at the keyboard has paid the link."""
    print("\nWaiting for the payment link to settle (Ctrl+C to stop)...")
    while True:
        link = razorpay_sdk_client.payment_link.fetch(payment_link_id)
        print(f"  link status = {link['status']}")
        if link["status"] == "paid":
            captured = [p for p in (link.get("payments") or []) if p.get("status") == "captured"]
            return captured[0]["payment_id"] if captured else None
        if link["status"] in ("cancelled", "expired"):
            return None
        time.sleep(3)


def main() -> None:
    print(f"Buyer request: {REQUEST_TEXT!r}  (agent_id={AGENT_ID})")
    cart = parse_request_to_cart(REQUEST_TEXT)
    print(f"Parsed cart: {cart}")

    payload = {"agent_id": AGENT_ID, "items": cart}

    # ---- attempt 1: no proof, expect 402 --------------------------------
    print("\n[1] POST /x402/orders  (no payment proof)")
    first = requests.post(f"{BASE_URL}/x402/orders", json=payload)
    print(f"    <- HTTP {first.status_code}")

    if first.status_code == 200:
        detail = first.json()["decision_detail"]
        print(f"    decision={detail['decision']}  reason={detail['reason']}")
        print("\nNo 402 issued: there is nothing legitimate to pay for yet.")
        if first.json()["status"] == "requires_human":
            print(f"Merchant must decide first. Order id: {first.json()['order_id']}")
        return

    if first.status_code != 402:
        print(f"    unexpected: {first.text[:300]}")
        return

    challenge = first.json()
    offer = challenge["accepts"][0]
    print(f"    402 Payment Required — {offer['scheme']} on {offer['network']}")
    print(f"    amount: {int(offer['maxAmountRequired']) / 100:.2f} {offer['asset']}")
    print(f"    pay at: {offer['extra']['payment_link_url']}")
    print("    (test card 4100 2800 0000 1007, any future expiry/CVV, any 4-10 digit OTP)")

    payment_id = _wait_for_payment(offer["extra"]["payment_link_id"])
    if not payment_id:
        print("\nNot paid. Nothing was fulfilled.")
        return

    # ---- attempt 2: the SAME request, now carrying proof ----------------
    proof = {"challenge_id": challenge["challenge_id"], "payment_id": payment_id}
    print(f"\n[2] POST /x402/orders  (identical body, X-Payment: {payment_id})")
    second = requests.post(
        f"{BASE_URL}/x402/orders",
        json=payload,
        headers={"X-Payment": json.dumps(proof)},
    )
    print(f"    <- HTTP {second.status_code}")

    if second.status_code == 200:
        body = second.json()
        print(f"    settled: Rs.{body['amount_inr']}  payment {body['payment_id']}")
        print("\nOrder fulfilled through the x402 bridge, settled in fiat via Razorpay.")
    else:
        print(f"    refused: {second.json()}")
        return

    # ---- attempt 3: replay the same proof, expect refusal ---------------
    print("\n[3] Replaying the same proof, to show it cannot buy twice")
    third = requests.post(
        f"{BASE_URL}/x402/orders",
        json=payload,
        headers={"X-Payment": json.dumps(proof)},
    )
    print(f"    <- HTTP {third.status_code}  {third.json().get('detail', '')}")


if __name__ == "__main__":
    main()
