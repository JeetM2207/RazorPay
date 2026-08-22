"""Shared orchestration used by every protocol adapter.

This is plumbing, not business logic: look up trust, call the pure
negotiation core, record the audit event, and (if approved) create the
real Razorpay payment. Every adapter calls this; it has no idea which
protocol shape the original request arrived in, and negotiation.py has no
idea this file exists.
"""

import uuid

import audit_log
import negotiation
import razorpay_client
import trust
from mandate import MANDATE, MENU


def negotiate_and_record(agent_id: str, protocol: str, cart: list[tuple[str, int]]) -> dict:
    db_path = audit_log.DEFAULT_DB_PATH
    adjusted_mandate, tier = trust.trust_adjusted_mandate(agent_id, MANDATE, db_path=db_path)
    result = negotiation.evaluate(cart, mandate=adjusted_mandate, menu=MENU)

    cart_payload = [{"item": name, "qty": qty} for name, qty in cart]
    event_id = audit_log.record_event(
        agent_id=agent_id,
        protocol=protocol,
        cart=cart_payload,
        decision=result.decision.value,
        reason=result.reason,
        total_inr=result.total_inr,
        db_path=db_path,
    )

    response = {
        "event_id": event_id,
        "agent_id": agent_id,
        "trust_tier": tier.value,
        "decision": result.decision.value,
        "reason": result.reason,
        "total_inr": result.total_inr,
        "alternatives": [
            [{"item": line.item, "qty": line.qty} for line in alt]
            for alt in result.alternatives
        ],
    }

    if result.decision == negotiation.Decision.APPROVE:
        upsell = negotiation.suggest_upsell(cart, mandate=adjusted_mandate, menu=MENU)
        if upsell:
            response["upsell_suggestion"] = {"item": upsell.name, "price_inr": upsell.price_inr}

    return response


def create_payment_for_cart(agent_id: str, event_id: int, cart: list[tuple[str, int]]) -> dict:
    """Defense in depth: re-validates the cart right now, at payment time,
    rather than trusting a decision made moments earlier. Only ever called
    after an adapter has already reached an APPROVE-shaped state."""
    db_path = audit_log.DEFAULT_DB_PATH
    adjusted_mandate, _ = trust.trust_adjusted_mandate(agent_id, MANDATE, db_path=db_path)
    result = negotiation.evaluate(cart, mandate=adjusted_mandate, menu=MENU)
    if result.decision != negotiation.Decision.APPROVE:
        raise ValueError(f"cart no longer approved at payment time: {result.decision}")

    description = " + ".join(f"{qty}x {name}" for name, qty in cart)
    link = razorpay_client.create_payment_link(
        amount_inr=result.total_inr,
        description=f"Amma's Kitchen order: {description}",
        reference_id=f"order-{event_id}-{uuid.uuid4().hex[:6]}",
    )
    audit_log.attach_payment_link(event_id, link["id"], db_path=db_path)
    return link
