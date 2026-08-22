"""x402-style adapter: thin translation layer for the HTTP 402 flow.

Modeled on Coinbase's x402, the highest-volume agentic payment protocol
in use. Its shape is unusual and worth stating plainly: there is no
checkout session and no mandate chain. The buyer asks for the resource,
the server answers `402 Payment Required` with what it costs and how to
pay, and the buyer RETRIES THE SAME REQUEST carrying proof of payment.
Payment is a property of the retry, not a separate endpoint.

Real x402 settles in stablecoins and the proof is an EIP-712 signature.
This is a fiat bridge: the same challenge/response flow, settled through
Razorpay test-mode instead. That is the point of building it -- the same
negotiation core underneath a Web3-native agent payment UX, landing on
India's actual payment rails.

Two things this file does NOT do, deliberately:

  * It makes no decisions. The APPROVE/COUNTER_OFFER/ESCALATE call comes
    from orchestrator.negotiate_and_record() exactly as it does for ACP
    and AP2 -- the orchestrator has no idea a 402 is involved. The whole
    challenge loop lives here, at the adapter boundary.
  * It never takes the buyer's word that they paid. The proof presented
    in the X-Payment header is a claim; the adapter verifies it against
    Razorpay itself and matches it to the specific challenge it was
    issued for. A buyer cannot pay Rs.80 and present that as settlement
    for a Rs.440 order.
"""

import hashlib
import json
import time
import uuid

from fastapi import APIRouter, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import audit_log
import idempotency
import orchestrator
import razorpay_client

router = APIRouter()

_CHALLENGES: dict[str, dict] = {}
_ORDERS: dict[str, dict] = {}
_CHALLENGE_TTL_SECONDS = 900

X402_VERSION = 1

_STATUS_FOR_DECISION = {
    "APPROVE": "payment_required",
    "COUNTER_OFFER": "countered",
    "ESCALATE": "requires_human",
}

_HUMAN_OVERRIDABLE_MARKER = "human confirmation threshold"


class CartItemIn(BaseModel):
    item_id: str
    qty: int


class OrderRequest(BaseModel):
    agent_id: str
    items: list[CartItemIn]


class AcceptAlternativeRequest(BaseModel):
    index: int


class HumanConfirmRequest(BaseModel):
    items: list[CartItemIn] | None = None


def _cart_fingerprint(agent_id: str, cart: list[tuple[str, int]]) -> str:
    """Same agent asking for the same cart should meet the same challenge
    rather than minting a fresh payment link on every retry."""
    payload = json.dumps([agent_id, sorted(cart)], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _live_order_for(fingerprint: str) -> str | None:
    """Find an in-flight order for this exact agent+cart.

    x402 has no session id, so the buyer's only move while waiting is to
    ask for the resource again. Without this lookup every such poll would
    mint a fresh order and a fresh audit event, filling the merchant's
    queue with duplicates of one decision. Asking again is a retry, not a
    new order.
    """
    for order_id, order in _ORDERS.items():
        if order["status"] in ("settled", "rejected"):
            continue
        if _cart_fingerprint(order["agent_id"], order["cart"]) == fingerprint:
            return order_id
    return None


def _live_challenge_for(fingerprint: str) -> str | None:
    for challenge_id, challenge in _CHALLENGES.items():
        if (
            challenge["fingerprint"] == fingerprint
            and not challenge["fulfilled"]
            and time.time() < challenge["expires_at"]
        ):
            return challenge_id
    return None


def _challenge_body(challenge_id: str, challenge: dict) -> dict:
    """The 402 body, shaped after real x402's `accepts` array."""
    return {
        "x402Version": X402_VERSION,
        "error": "payment required",
        "challenge_id": challenge_id,
        "accepts": [
            {
                "scheme": "razorpay-payment-link",
                "network": "razorpay-test",
                "asset": "INR",
                # Minor units, as x402 does -- paise, not rupees.
                "maxAmountRequired": str(challenge["total_inr"] * 100),
                "resource": "/x402/orders",
                "description": challenge["description"],
                "payTo": "amma-kitchen",
                "maxTimeoutSeconds": _CHALLENGE_TTL_SECONDS,
                "extra": {
                    "payment_link_id": challenge["payment_link_id"],
                    "payment_link_url": challenge["payment_link_url"],
                },
            }
        ],
        "retry": {
            "method": "POST",
            "resource": "/x402/orders",
            "header": "X-Payment",
            "payload_shape": {"challenge_id": "<challenge_id>", "payment_id": "pay_..."},
        },
    }


def _negotiate(agent_id: str, cart: list[tuple[str, int]]) -> dict:
    """One call into the shared orchestrator -- identical to what the ACP
    and AP2 adapters do. No 402-specific decision logic exists."""
    detail = orchestrator.negotiate_and_record(agent_id, "x402", cart)
    order_id = uuid.uuid4().hex
    _ORDERS[order_id] = {
        "agent_id": agent_id,
        "cart": cart,
        "detail": detail,
        "status": _STATUS_FOR_DECISION[detail["decision"]],
    }

    if _ORDERS[order_id]["status"] == "requires_human":
        # Isolated and swallowed: a notification problem must never stop
        # an order being recorded or resolvable through the console.
        try:
            import escalations

            escalations.notify("x402", order_id, detail, cart)
        except Exception:
            pass

    return order_id


def _order_view(order_id: str) -> dict:
    order = _ORDERS[order_id]
    return {
        "order_id": order_id,
        "status": order["status"],
        "decision_detail": order["detail"],
    }


def _issue_challenge(order_id: str) -> JSONResponse:
    """Create the real Razorpay payment link and answer 402."""
    order = _ORDERS[order_id]
    fingerprint = _cart_fingerprint(order["agent_id"], order["cart"])

    existing = _live_challenge_for(fingerprint)
    if existing:
        return JSONResponse(status_code=402, content=_challenge_body(existing, _CHALLENGES[existing]))

    link = orchestrator.create_payment_for_cart(
        order["agent_id"],
        order["detail"]["event_id"],
        order["cart"],
        skip_reevaluation=order.get("human_overridden", False),
    )

    challenge_id = uuid.uuid4().hex
    _CHALLENGES[challenge_id] = {
        "fingerprint": fingerprint,
        "order_id": order_id,
        "agent_id": order["agent_id"],
        "cart": order["cart"],
        "total_inr": order["detail"]["total_inr"],
        "event_id": order["detail"]["event_id"],
        "description": " + ".join(f"{qty}x {name}" for name, qty in order["cart"]),
        "payment_link_id": link["id"],
        "payment_link_url": link["short_url"],
        "expires_at": time.time() + _CHALLENGE_TTL_SECONDS,
        "fulfilled": False,
    }
    order["challenge_id"] = challenge_id
    return JSONResponse(status_code=402, content=_challenge_body(challenge_id, _CHALLENGES[challenge_id]))


def _verify_payment(challenge: dict, claimed_payment_id: str | None) -> str:
    """Confirm settlement with Razorpay. The buyer's claim is checked, not
    believed: the authoritative answer comes from the payment processor,
    and it must correspond to THIS challenge's link."""
    link = razorpay_client.fetch_payment_link(challenge["payment_link_id"])

    if link["status"] != "paid":
        raise HTTPException(402, f"payment not settled; link status is '{link['status']}'")

    captured = [p for p in (link.get("payments") or []) if p.get("status") == "captured"]
    if not captured:
        raise HTTPException(402, "link reports paid but carries no captured payment")

    actual_payment_id = captured[0]["payment_id"]
    if claimed_payment_id and claimed_payment_id != actual_payment_id:
        raise HTTPException(
            403,
            "the payment proof does not match what Razorpay recorded for this challenge",
        )
    return actual_payment_id


@router.get("/x402/orders")
def list_orders(status: str | None = None) -> dict:
    """Read-only, so the merchant console can surface x402 escalations in
    the same queue as the other protocols."""
    orders = [
        {
            "session_id": oid,
            "agent_id": order["agent_id"],
            "status": order.get("status"),
            "protocol": "x402",
            "cart": [{"item": name, "qty": qty} for name, qty in order["cart"]],
            "decision_detail": order.get("detail"),
        }
        for oid, order in _ORDERS.items()
        if status is None or order.get("status") == status
    ]
    return {"sessions": list(reversed(orders))}


@router.post("/x402/orders")
def create_or_settle_order(
    req: OrderRequest,
    x_payment: str | None = Header(default=None, alias="X-Payment"),
):
    """The whole protocol, in one endpoint -- which is x402's defining
    trait. Without proof you get a 402 and what it costs; with valid
    proof, the same request succeeds."""
    cart = [(item.item_id, item.qty) for item in req.items]

    # ---- second pass: the retry, carrying proof -------------------------
    if x_payment:
        try:
            proof = json.loads(x_payment)
        except json.JSONDecodeError:
            raise HTTPException(400, "X-Payment must be JSON: {\"challenge_id\":..., \"payment_id\":...}")

        challenge = _CHALLENGES.get(proof.get("challenge_id", ""))
        if not challenge:
            raise HTTPException(404, "unknown or expired challenge")
        if time.time() > challenge["expires_at"]:
            raise HTTPException(403, "challenge expired; request the resource again for a fresh one")
        if challenge["agent_id"] != req.agent_id:
            raise HTTPException(403, "this challenge was issued to a different agent")
        if _cart_fingerprint(req.agent_id, cart) != challenge["fingerprint"]:
            raise HTTPException(
                409, "the retried cart does not match the cart this challenge was issued for"
            )

        payment_id = _verify_payment(challenge, proof.get("payment_id"))

        db_path = audit_log.DEFAULT_DB_PATH
        # Replay guard: one settled payment fulfils exactly one order.
        # Shares the ledger the webhook handler and reconciler use.
        if not idempotency.claim_event("x402.fulfilled", challenge["payment_link_id"], db_path):
            raise HTTPException(409, "this payment has already been used to settle an order")

        audit_log.mark_paid(challenge["event_id"], payment_id, db_path=db_path)
        challenge["fulfilled"] = True
        _ORDERS[challenge["order_id"]]["status"] = "settled"

        return {
            "status": "settled",
            "order_id": challenge["order_id"],
            "payment_id": payment_id,
            "amount_inr": challenge["total_inr"],
            "items": [{"item": name, "qty": qty} for name, qty in challenge["cart"]],
        }

    # ---- first pass (or a poll): negotiate, then challenge --------------
    # An existing in-flight order for this exact cart is resumed rather
    # than duplicated -- see _live_order_for. This is also how an order
    # the merchant has just approved turns into a 402: the buyer simply
    # asks for the resource again.
    order_id = _live_order_for(_cart_fingerprint(req.agent_id, cart))
    if order_id is None:
        order_id = _negotiate(req.agent_id, cart)

    order = _ORDERS[order_id]

    # Only an approved cart has a price to demand. A counter-offer or an
    # escalation is answered 200 with the state, because there is nothing
    # legitimate to pay for yet.
    if order["status"] != "payment_required":
        return _order_view(order_id)

    return _issue_challenge(order_id)


@router.get("/x402/orders/{order_id}")
def get_order(order_id: str) -> dict:
    if order_id not in _ORDERS:
        raise HTTPException(404, "unknown order")
    view = _order_view(order_id)
    challenge_id = _ORDERS[order_id].get("challenge_id")
    if challenge_id and challenge_id in _CHALLENGES:
        view["challenge"] = _challenge_body(challenge_id, _CHALLENGES[challenge_id])
    return view


@router.post("/x402/orders/{order_id}/accept-alternative")
def accept_alternative(order_id: str, req: AcceptAlternativeRequest) -> dict:
    order = _ORDERS.get(order_id)
    if not order:
        raise HTTPException(404, "unknown order")
    alternatives = order["detail"]["alternatives"]
    if not (0 <= req.index < len(alternatives)):
        raise HTTPException(400, "alternative index out of range")

    new_cart = [(line["item"], line["qty"]) for line in alternatives[req.index]]
    order["detail"] = orchestrator.negotiate_and_record(order["agent_id"], "x402", new_cart)
    order["cart"] = new_cart
    order["status"] = _STATUS_FOR_DECISION[order["detail"]["decision"]]
    order.pop("human_overridden", None)
    return _order_view(order_id)


@router.post("/x402/orders/{order_id}/human_confirm")
def human_confirm(order_id: str, req: HumanConfirmRequest = HumanConfirmRequest()) -> dict:
    order = _ORDERS.get(order_id)
    if not order:
        raise HTTPException(404, "unknown order")
    if order["status"] != "requires_human":
        raise HTTPException(409, "order is not awaiting human confirmation")

    if req.items is not None:
        new_cart = [(item.item_id, item.qty) for item in req.items]
        order["detail"] = orchestrator.negotiate_and_record(order["agent_id"], "x402", new_cart)
        order["cart"] = new_cart
        order["status"] = _STATUS_FOR_DECISION[order["detail"]["decision"]]
        order.pop("human_overridden", None)
        if order["status"] != "payment_required":
            raise HTTPException(
                409,
                f"the proposed reduced cart did not resolve to an approvable order "
                f"(status={order['status']}, reason={order['detail']['reason']})",
            )
        return _order_view(order_id)

    if _HUMAN_OVERRIDABLE_MARKER not in order["detail"]["reason"]:
        raise HTTPException(
            403,
            "this escalation is a hard merchant rule (disallowed category, "
            "unknown item, or over the flexible margin) and cannot be "
            "human-overridden here",
        )

    new_event_id = orchestrator.record_human_override(
        order["agent_id"], "x402", order["cart"], order["detail"]
    )
    order["detail"] = {
        **order["detail"],
        "event_id": new_event_id,
        "decision": "APPROVE",
        "reason": f"human override: {order['detail']['reason']}",
    }
    order["status"] = "payment_required"
    order["human_overridden"] = True
    return _order_view(order_id)


@router.post("/x402/orders/{order_id}/human_reject")
def human_reject(order_id: str) -> dict:
    order = _ORDERS.get(order_id)
    if not order:
        raise HTTPException(404, "unknown order")
    if order["status"] != "requires_human":
        raise HTTPException(409, "order is not awaiting human confirmation")

    new_event_id = orchestrator.record_human_rejection(
        order["agent_id"], "x402", order["cart"], order["detail"]
    )
    order["detail"] = {
        **order["detail"],
        "event_id": new_event_id,
        "decision": "REJECTED",
        "reason": f"human rejected: {order['detail']['reason']}",
    }
    order["status"] = "rejected"
    return _order_view(order_id)


app = FastAPI(title="Amma's Kitchen -- x402 Adapter")
app.include_router(router)
