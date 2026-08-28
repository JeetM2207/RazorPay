"""Shared orchestration used by every protocol adapter.

This is plumbing, not business logic: look up trust, call the pure
negotiation core, record the audit event, and (if approved) create the
real Razorpay payment. Every adapter calls this; it has no idea which
protocol shape the original request arrived in, and negotiation.py has no
idea this file exists.
"""

import uuid
from datetime import datetime, timezone

import audit_log
import merchant_config
import negotiation
import razorpay_client
import trust
import upsell_ranking


def _limits_snapshot(mandate, tier, buyer_mandate: dict | None) -> dict:
    """What the rules WERE, written beside the decision they produced.

    Recorded rather than referenced, and that distinction is the whole
    point. Amma edits her cap whenever she likes; the moment she does, an
    order that referenced the live config starts describing limits that
    were never applied to it. Harmless on a dashboard, fatal in a record
    somebody is relying on to say what was authorised.

    The buyer's own caps are only present when the caller knew them. They
    live in the customer's browser and reach the server on the path that
    checked them, so an order placed through a protocol that never sees
    them says so plainly rather than carrying an invented number.
    """
    return {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "merchant": {
            "budget_cap_inr": mandate.budget_cap_inr,
            "human_confirm_threshold_inr": mandate.human_confirm_threshold_inr,
            "allowed_categories": list(mandate.allowed_categories),
            "flexible_margin_pct": mandate.flexible_margin_pct,
            "trust_tier_applied": tier.value,
        },
        "buyer": buyer_mandate or None,
    }


def negotiate_and_record(
    agent_id: str,
    protocol: str,
    cart: list[tuple[str, int]],
    buyer_mandate: dict | None = None,
    source: str | None = None,
    routine_id: str | None = None,
) -> dict:
    db_path = audit_log.DEFAULT_DB_PATH
    # Read the merchant's LIVE configuration, so edits she makes on the
    # setup page are the limits actually enforced here.
    menu = merchant_config.current_menu()
    adjusted_mandate, tier = trust.trust_adjusted_mandate(
        agent_id, merchant_config.current_mandate(), db_path=db_path
    )
    result = negotiation.evaluate(cart, mandate=adjusted_mandate, menu=menu)

    cart_payload = [{"item": name, "qty": qty} for name, qty in cart]
    event_id = audit_log.record_event(
        agent_id=agent_id,
        protocol=protocol,
        cart=cart_payload,
        decision=result.decision.value,
        reason=result.reason,
        total_inr=result.total_inr,
        db_path=db_path,
        # Written here, once, so every adapter gets it without knowing it
        # exists. negotiation.py is untouched: this records what it was
        # given, it does not change what it decides.
        limits_snapshot=_limits_snapshot(adjusted_mandate, tier, buyer_mandate),
        # How the order originated. A standing order still comes through
        # here like everything else -- there is no second charging path --
        # it just says so on the row.
        source=source,
        routine_id=routine_id,
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
        suggestion = suggest_addon(cart, adjusted_mandate, menu, db_path=db_path)
        if suggestion:
            response["upsell_suggestion"] = suggestion

    return response


def suggest_addon(
    cart: list[tuple[str, int]],
    mandate,
    menu: dict,
    db_path: str | None = None,
) -> dict | None:
    """One optional add-on to offer, or None. Never affects a decision.

    Split out of negotiate_and_record so the MCP adapter can ask the same
    question with its own ceiling -- see adapter_mcp. Everything that
    decides anything still happens in negotiation.suggest_upsell(); this
    only assembles the ranking and labels the answer.

    The history lookup happens HERE, not inside negotiation.py, so the
    decision core stays free of I/O. It receives the ranking as plain
    data and still applies every mandate limit to it -- a ranking
    reorders candidates, it never admits one.
    """
    db_path = db_path or audit_log.DEFAULT_DB_PATH
    names = [name for name, _qty in cart]

    # Evidence first: what people actually paid for alongside these items.
    ranked_addons = audit_log.get_frequent_addons(names, db_path=db_path)
    # Then what simply goes with it, which is what a new merchant has
    # instead of history. Without this the core falls back to "priciest
    # thing that fits", and offers a second main course to someone who
    # just ordered dinner.
    pairings = upsell_ranking.complements(cart, menu)
    ranking = ranked_addons + [name for name in pairings if name not in ranked_addons]

    upsell = negotiation.suggest_upsell(
        cart, mandate=mandate, menu=menu, ranked_addons=ranking
    )
    if not upsell:
        return None

    if upsell.name in ranked_addons:
        basis = "bought together before"
    elif upsell.name in pairings:
        basis = "goes well with this order"
    else:
        basis = "best value that fits"

    return {
        "item": upsell.name,
        "price_inr": upsell.price_inr,
        # Lets the merchant console and the assistant say WHY this was
        # suggested, rather than presenting it as an unexplained hunch.
        "basis": basis,
    }


def create_payment_for_cart(
    agent_id: str,
    event_id: int,
    cart: list[tuple[str, int]],
    skip_reevaluation: bool = False,
) -> dict:
    """Defense in depth: re-validates the cart right now, at payment time,
    rather than trusting a decision made moments earlier. Only ever called
    after an adapter has already reached an APPROVE-shaped state.

    skip_reevaluation exists ONLY for the human-override path: once a
    human has explicitly confirmed a specific escalated cart (recorded via
    record_human_override), re-running evaluate() would just say ESCALATE
    again forever -- the human's explicit, audited decision is what
    authorizes payment in that case, not the algorithmic re-check.
    """
    db_path = audit_log.DEFAULT_DB_PATH
    if skip_reevaluation:
        total_inr = sum(
            merchant_config.current_menu()[name].price_inr * qty for name, qty in cart
        )
    else:
        adjusted_mandate, _ = trust.trust_adjusted_mandate(
            agent_id, merchant_config.current_mandate(), db_path=db_path
        )
        result = negotiation.evaluate(
            cart, mandate=adjusted_mandate, menu=merchant_config.current_menu()
        )
        if result.decision != negotiation.Decision.APPROVE:
            raise ValueError(f"cart no longer approved at payment time: {result.decision}")
        total_inr = result.total_inr

    description = " + ".join(f"{qty}x {name}" for name, qty in cart)
    link = razorpay_client.create_payment_link(
        amount_inr=total_inr,
        description=f"Amma's Kitchen order: {description}",
        reference_id=f"order-{event_id}-{uuid.uuid4().hex[:6]}",
    )
    audit_log.attach_payment_link(event_id, link["id"], db_path=db_path)
    return link


def record_human_override(agent_id: str, protocol: str, cart: list[tuple[str, int]], original_detail: dict) -> int:
    """A human explicitly approved an order the negotiation core escalated.

    This is recorded as its own, clearly-labeled audit event -- never
    silently merged into or replacing the original algorithmic ESCALATE
    entry. Anyone reading the audit trail sees both: what the machine
    decided, and that a human separately chose to proceed anyway.
    """
    db_path = audit_log.DEFAULT_DB_PATH
    cart_payload = [{"item": name, "qty": qty} for name, qty in cart]
    return audit_log.record_event(
        agent_id=agent_id,
        protocol=protocol,
        cart=cart_payload,
        decision="APPROVE",
        reason=f"human override of ESCALATE ({original_detail['reason']})",
        total_inr=original_detail["total_inr"],
        db_path=db_path,
    )


def record_human_rejection(
    agent_id: str, protocol: str, cart: list[tuple[str, int]], original_detail: dict
) -> int:
    """A human explicitly declined an escalated order.

    Recorded as its own terminal audit entry (decision="REJECTED", a value
    outside negotiation.Decision on purpose -- it's a human action, never
    something the negotiation core itself produces) so the trail can tell
    "a human said no" apart from "nobody has looked at this yet".
    """
    db_path = audit_log.DEFAULT_DB_PATH
    cart_payload = [{"item": name, "qty": qty} for name, qty in cart]
    return audit_log.record_event(
        agent_id=agent_id,
        protocol=protocol,
        cart=cart_payload,
        decision="REJECTED",
        reason=f"human rejected ESCALATE ({original_detail['reason']})",
        total_inr=original_detail["total_inr"],
        db_path=db_path,
    )
