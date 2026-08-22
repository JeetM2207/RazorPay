"""AP2-style adapter: thin translation layer between an AP2-shaped buyer
request and the negotiation core.

Modeled on Google's real Agent Payments Protocol (AP2): a chain of three
mandate objects -- Intent Mandate (the buyer's initial ask), Cart Mandate
(the locked-in items + price once approved), and Payment Mandate (the
actual charge, referencing the cart it was matched against). This is a
structurally different envelope from adapter_acp.py's flat checkout
sessions + delegate tokens -- nested mandate objects, chained by id
across three separate endpoints rather than one session's sub-resources.

This file imports orchestrator.py UNCHANGED from adapter_acp.py -- not
one line of orchestrator.py or negotiation.py was touched to support this
second protocol. That's the point: the decision-making is protocol-
agnostic, only the envelope shape differs.
"""

import hashlib
import time
import uuid

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import orchestrator

app = FastAPI(title="Amma's Kitchen -- AP2 Adapter")

_INTENT_MANDATES: dict[str, dict] = {}
_CART_MANDATES: dict[str, dict] = {}
_MANDATE_TTL_SECONDS = 600

_STATUS_FOR_DECISION = {
    "APPROVE": "cart_ready",
    "COUNTER_OFFER": "countered",
    "ESCALATE": "requires_human",
}

_HUMAN_OVERRIDABLE_MARKER = "human confirmation threshold"


class CartItemIn(BaseModel):
    item_id: str
    qty: int


class Intent(BaseModel):
    items: list[CartItemIn]
    # Part of the mandate itself (unlike buyer_agent_a's locally-hardcoded
    # limit) -- AP2's real design lets a user's spending authorization
    # travel as data on the mandate object.
    auto_confirm_limit_inr: int | None = None


class IntentMandateRequest(BaseModel):
    agent_id: str
    intent: Intent


class AcceptAlternativeRequest(BaseModel):
    index: int


class HumanConfirmRequest(BaseModel):
    items: list[CartItemIn] | None = None


def _apply_intent_decision(intent_id: str, cart: list[tuple[str, int]]) -> dict:
    mandate = _INTENT_MANDATES[intent_id]
    mandate.pop("human_overridden", None)
    detail = orchestrator.negotiate_and_record(mandate["agent_id"], "ap2", cart)
    mandate["cart"] = cart
    mandate["detail"] = detail
    mandate["status"] = _STATUS_FOR_DECISION[detail["decision"]]
    return _intent_view(intent_id)


def _intent_view(intent_id: str) -> dict:
    mandate = _INTENT_MANDATES[intent_id]
    return {
        "intent_mandate": {
            "id": intent_id,
            "agent_id": mandate["agent_id"],
            "auto_confirm_limit_inr": mandate.get("auto_confirm_limit_inr"),
            "status": mandate["status"],
            "decision_detail": mandate["detail"],
        }
    }


@app.post("/ap2/intent-mandates")
def create_intent_mandate(req: IntentMandateRequest) -> dict:
    intent_id = uuid.uuid4().hex
    cart = [(item.item_id, item.qty) for item in req.intent.items]
    _INTENT_MANDATES[intent_id] = {
        "agent_id": req.agent_id,
        "auto_confirm_limit_inr": req.intent.auto_confirm_limit_inr,
    }
    return _apply_intent_decision(intent_id, cart)


@app.get("/ap2/intent-mandates/{intent_id}")
def get_intent_mandate(intent_id: str) -> dict:
    if intent_id not in _INTENT_MANDATES:
        raise HTTPException(404, "unknown intent mandate")
    return _intent_view(intent_id)


@app.post("/ap2/intent-mandates/{intent_id}/accept-alternative")
def accept_alternative(intent_id: str, req: AcceptAlternativeRequest) -> dict:
    mandate = _INTENT_MANDATES.get(intent_id)
    if not mandate:
        raise HTTPException(404, "unknown intent mandate")
    if mandate["status"] != "countered":
        raise HTTPException(409, "intent mandate is not in a countered state")
    alternatives = mandate["detail"]["alternatives"]
    if not (0 <= req.index < len(alternatives)):
        raise HTTPException(400, "alternative index out of range")
    new_cart = [(line["item"], line["qty"]) for line in alternatives[req.index]]
    return _apply_intent_decision(intent_id, new_cart)


@app.post("/ap2/intent-mandates/{intent_id}/accept-upsell")
def accept_upsell(intent_id: str) -> dict:
    mandate = _INTENT_MANDATES.get(intent_id)
    if not mandate:
        raise HTTPException(404, "unknown intent mandate")
    if mandate["status"] != "cart_ready":
        raise HTTPException(409, "intent mandate is not ready for a cart")
    upsell = mandate["detail"].get("upsell_suggestion")
    if not upsell:
        raise HTTPException(400, "no upsell suggestion on this intent mandate")
    new_cart = list(mandate["cart"]) + [(upsell["item"], 1)]
    return _apply_intent_decision(intent_id, new_cart)


@app.post("/ap2/intent-mandates/{intent_id}/human-confirm")
def human_confirm(intent_id: str, req: HumanConfirmRequest = HumanConfirmRequest()) -> dict:
    """Stands in for a human ops person confirming an escalated Intent
    Mandate (until the real dashboard, build order step 7, exists).

    If req.items is given, the human is proposing a smaller/adjusted cart
    instead of a blanket override -- that goes back through the real
    negotiation core via _apply_intent_decision, so it only proceeds if
    it genuinely clears the gate on its own."""
    mandate = _INTENT_MANDATES.get(intent_id)
    if not mandate:
        raise HTTPException(404, "unknown intent mandate")
    if mandate["status"] != "requires_human":
        raise HTTPException(409, "intent mandate is not awaiting human confirmation")

    if req.items is not None:
        new_cart = [(item.item_id, item.qty) for item in req.items]
        result = _apply_intent_decision(intent_id, new_cart)
        if result["intent_mandate"]["status"] != "cart_ready":
            raise HTTPException(
                409,
                f"the proposed reduced cart did not resolve to an approvable "
                f"order (status={result['intent_mandate']['status']}, "
                f"reason={result['intent_mandate']['decision_detail']['reason']}); "
                f"try a smaller cart",
            )
        return result

    if _HUMAN_OVERRIDABLE_MARKER not in mandate["detail"]["reason"]:
        raise HTTPException(
            403,
            "this escalation is a hard merchant rule (disallowed category, "
            "unknown item, or over the flexible margin) and cannot be "
            "human-overridden here",
        )

    new_event_id = orchestrator.record_human_override(
        mandate["agent_id"], "ap2", mandate["cart"], mandate["detail"]
    )
    mandate["detail"] = {
        **mandate["detail"],
        "event_id": new_event_id,
        "decision": "APPROVE",
        "reason": f"human override: {mandate['detail']['reason']}",
    }
    mandate["status"] = "cart_ready"
    mandate["human_overridden"] = True
    return _intent_view(intent_id)


@app.post("/ap2/intent-mandates/{intent_id}/human-reject")
def human_reject(intent_id: str) -> dict:
    mandate = _INTENT_MANDATES.get(intent_id)
    if not mandate:
        raise HTTPException(404, "unknown intent mandate")
    if mandate["status"] != "requires_human":
        raise HTTPException(409, "intent mandate is not awaiting human confirmation")

    new_event_id = orchestrator.record_human_rejection(
        mandate["agent_id"], "ap2", mandate["cart"], mandate["detail"]
    )
    mandate["detail"] = {
        **mandate["detail"],
        "event_id": new_event_id,
        "decision": "REJECTED",
        "reason": f"human rejected: {mandate['detail']['reason']}",
    }
    mandate["status"] = "rejected"
    return _intent_view(intent_id)


@app.post("/ap2/intent-mandates/{intent_id}/cart-mandate")
def create_cart_mandate(intent_id: str) -> dict:
    mandate = _INTENT_MANDATES.get(intent_id)
    if not mandate:
        raise HTTPException(404, "unknown intent mandate")
    if mandate["status"] != "cart_ready":
        raise HTTPException(409, "intent mandate is not ready to lock a cart")

    cart_mandate_id = uuid.uuid4().hex
    _CART_MANDATES[cart_mandate_id] = {
        "intent_mandate_id": intent_id,
        "agent_id": mandate["agent_id"],
        "cart": mandate["cart"],
        "total_inr": mandate["detail"]["total_inr"],
        "event_id": mandate["detail"]["event_id"],
        "human_overridden": mandate.get("human_overridden", False),
        "expires_at": time.time() + _MANDATE_TTL_SECONDS,
        "used": False,
    }
    mandate["status"] = "cart_locked"

    return {
        "cart_mandate": {
            "id": cart_mandate_id,
            "intent_mandate_id": intent_id,
            "items": [{"item": name, "qty": qty} for name, qty in mandate["cart"]],
            "total_inr": _CART_MANDATES[cart_mandate_id]["total_inr"],
            "expires_at": _CART_MANDATES[cart_mandate_id]["expires_at"],
        }
    }


@app.post("/ap2/cart-mandates/{cart_mandate_id}/payment-mandate")
def create_payment_mandate(cart_mandate_id: str) -> dict:
    cart_mandate = _CART_MANDATES.get(cart_mandate_id)
    if not cart_mandate:
        raise HTTPException(404, "unknown cart mandate")
    if cart_mandate["used"]:
        raise HTTPException(409, "cart mandate already used")
    if time.time() > cart_mandate["expires_at"]:
        raise HTTPException(403, "cart mandate expired")

    cart_mandate["used"] = True
    link = orchestrator.create_payment_for_cart(
        cart_mandate["agent_id"],
        cart_mandate["event_id"],
        cart_mandate["cart"],
        skip_reevaluation=cart_mandate["human_overridden"],
    )

    # A simplified stand-in for AP2's real cryptographic mandate binding:
    # a hash tying this payment to the exact intent+cart it was matched
    # against. Not a real signature -- there's no key material here --
    # just a structural echo of what a payment processor would actually
    # verify in the real protocol.
    matched_mandate_hash = hashlib.sha256(
        f"{cart_mandate['intent_mandate_id']}:{cart_mandate_id}:{cart_mandate['total_inr']}".encode()
    ).hexdigest()

    return {
        "payment_mandate": {
            "id": uuid.uuid4().hex,
            "cart_mandate_id": cart_mandate_id,
            "matched_mandate_hash": matched_mandate_hash,
            "amount_inr": cart_mandate["total_inr"],
            "payment_link_id": link["id"],
            "payment_link_url": link["short_url"],
        }
    }
