"""ACP-style adapter: thin translation layer between an ACP-shaped buyer
request and the negotiation core.

Modeled on the real Agentic Commerce Protocol (OpenAI + Stripe): stateful
checkout sessions and delegate tokens for payment. This file has NO
business logic of its own -- every decision is made by negotiation.py via
orchestrator.negotiate_and_record(); this file only shapes ACP's request/
response envelope and manages session/delegate-token bookkeeping.
"""

import secrets
import time
import uuid

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import orchestrator

app = FastAPI(title="Amma's Kitchen -- ACP Adapter")

_SESSIONS: dict[str, dict] = {}
_TOKEN_TTL_SECONDS = 600

_STATUS_FOR_DECISION = {
    "APPROVE": "ready_for_payment",
    "COUNTER_OFFER": "countered",
    "ESCALATE": "requires_human",
}

# Only an ESCALATE caused by the human-confirm threshold is something a
# human can wave through here. A disallowed category or an unknown item
# is a hard merchant rule, not a "needs a second opinion" situation, and
# is never overridable through this endpoint.
_HUMAN_OVERRIDABLE_MARKER = "human confirmation threshold"


class CartItemIn(BaseModel):
    item_id: str
    qty: int


class CreateSessionRequest(BaseModel):
    agent_id: str
    items: list[CartItemIn]


class AcceptAlternativeRequest(BaseModel):
    index: int


class CompleteRequest(BaseModel):
    delegate_token: str


def _apply_decision(session_id: str, cart: list[tuple[str, int]]) -> dict:
    session = _SESSIONS[session_id]
    session.pop("human_overridden", None)
    detail = orchestrator.negotiate_and_record(session["agent_id"], "acp", cart)
    session["cart"] = cart
    session["detail"] = detail
    status = _STATUS_FOR_DECISION[detail["decision"]]
    session["status"] = status

    delegate_token = None
    if status == "ready_for_payment":
        delegate_token = secrets.token_urlsafe(24)
        session["delegate_token"] = delegate_token
        session["delegate_token_expires_at"] = time.time() + _TOKEN_TTL_SECONDS
        session["delegate_token_used"] = False
    else:
        session.pop("delegate_token", None)

    return {
        "session_id": session_id,
        "status": status,
        "decision_detail": detail,
        "delegate_token": delegate_token,
    }


@app.post("/acp/checkout_sessions")
def create_session(req: CreateSessionRequest) -> dict:
    session_id = uuid.uuid4().hex
    _SESSIONS[session_id] = {"agent_id": req.agent_id}
    cart = [(item.item_id, item.qty) for item in req.items]
    return _apply_decision(session_id, cart)


@app.get("/acp/checkout_sessions/{session_id}")
def get_session(session_id: str) -> dict:
    session = _SESSIONS.get(session_id)
    if not session:
        raise HTTPException(404, "unknown session")
    return {
        "session_id": session_id,
        "status": session["status"],
        "decision_detail": session["detail"],
        "delegate_token": session.get("delegate_token"),
    }


@app.post("/acp/checkout_sessions/{session_id}/accept_alternative")
def accept_alternative(session_id: str, req: AcceptAlternativeRequest) -> dict:
    session = _SESSIONS.get(session_id)
    if not session:
        raise HTTPException(404, "unknown session")
    alternatives = session["detail"]["alternatives"]
    if not (0 <= req.index < len(alternatives)):
        raise HTTPException(400, "alternative index out of range")
    new_cart = [(line["item"], line["qty"]) for line in alternatives[req.index]]
    return _apply_decision(session_id, new_cart)


@app.post("/acp/checkout_sessions/{session_id}/accept_upsell")
def accept_upsell(session_id: str) -> dict:
    session = _SESSIONS.get(session_id)
    if not session:
        raise HTTPException(404, "unknown session")
    upsell = session["detail"].get("upsell_suggestion")
    if not upsell:
        raise HTTPException(400, "no upsell suggestion on this session")
    new_cart = list(session["cart"]) + [(upsell["item"], 1)]
    return _apply_decision(session_id, new_cart)


@app.post("/acp/checkout_sessions/{session_id}/human_confirm")
def human_confirm(session_id: str) -> dict:
    """Stands in for a human ops person clicking 'confirm' on an escalated
    order (until the real dashboard, build order step 7, exists)."""
    session = _SESSIONS.get(session_id)
    if not session:
        raise HTTPException(404, "unknown session")
    if session["status"] != "requires_human":
        raise HTTPException(409, "session is not awaiting human confirmation")
    if _HUMAN_OVERRIDABLE_MARKER not in session["detail"]["reason"]:
        raise HTTPException(
            403,
            "this escalation is a hard merchant rule (disallowed category, "
            "unknown item, or over the flexible margin) and cannot be "
            "human-overridden here",
        )

    new_event_id = orchestrator.record_human_override(
        session["agent_id"], "acp", session["cart"], session["detail"]
    )
    session["detail"] = {
        **session["detail"],
        "event_id": new_event_id,
        "decision": "APPROVE",
        "reason": f"human override: {session['detail']['reason']}",
    }
    session["status"] = "ready_for_payment"
    session["human_overridden"] = True

    delegate_token = secrets.token_urlsafe(24)
    session["delegate_token"] = delegate_token
    session["delegate_token_expires_at"] = time.time() + _TOKEN_TTL_SECONDS
    session["delegate_token_used"] = False

    return {
        "session_id": session_id,
        "status": session["status"],
        "decision_detail": session["detail"],
        "delegate_token": delegate_token,
    }


@app.post("/acp/checkout_sessions/{session_id}/complete")
def complete_session(session_id: str, req: CompleteRequest) -> dict:
    session = _SESSIONS.get(session_id)
    if not session:
        raise HTTPException(404, "unknown session")
    if session.get("status") != "ready_for_payment":
        raise HTTPException(409, "session is not ready for payment")
    if session.get("delegate_token_used"):
        raise HTTPException(409, "delegate token already used")
    if req.delegate_token != session.get("delegate_token"):
        raise HTTPException(403, "invalid delegate token")
    if time.time() > session.get("delegate_token_expires_at", 0):
        raise HTTPException(403, "delegate token expired")

    session["delegate_token_used"] = True
    link = orchestrator.create_payment_for_cart(
        session["agent_id"],
        session["detail"]["event_id"],
        session["cart"],
        skip_reevaluation=session.get("human_overridden", False),
    )
    return {
        "payment_link_id": link["id"],
        "payment_link_url": link["short_url"],
        "amount_inr": session["detail"]["total_inr"],
    }
