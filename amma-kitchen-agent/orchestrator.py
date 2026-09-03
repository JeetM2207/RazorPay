"""Shared orchestration used by every protocol adapter.

This is plumbing, not business logic: look up trust, call the pure
negotiation core, record the audit event, and (if approved) create the
real Razorpay payment. Every adapter calls this; it has no idea which
protocol shape the original request arrived in, and negotiation.py has no
idea this file exists.
"""

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException

import audit_log
import merchant_config
import merchants
import negotiation
import razorpay_client
import trust
import upsell_ranking
import velocity


class VelocityRefused(HTTPException):
    """Refused for going too fast.

    An HTTPException so the four adapters need no change at all: FastAPI
    turns it into a 429 without any of them knowing this rule exists.
    Callers running in-process -- routines -- catch it and read `payload`
    for the same dict `negotiate_and_record` would have returned.
    """

    def __init__(self, event_id: int, verdict, notified: bool, payload: dict):
        super().__init__(status_code=429, detail=verdict.reason)
        self.event_id = event_id
        self.verdict = verdict
        self.merchant_notified = notified
        self.payload = payload


# One notification per window per agent, not one per refused order.
# Two hundred refusals is two hundred messages, which is the same denial
# of service arriving by a different route -- and she can act on the first
# one just as well as the two hundredth.
_ALERTED: dict[str, str] = {}
_ALERT_COOLDOWN_SECONDS = 3600


def _tell_her_once(agent_id: str, verdict, now: datetime) -> bool:
    """Returns whether a message was actually sent."""
    last = _ALERTED.get(agent_id)
    if last:
        try:
            if (now - datetime.fromisoformat(last)).total_seconds() < _ALERT_COOLDOWN_SECONDS:
                return False
        except ValueError:
            pass
    _ALERTED[agent_id] = now.isoformat()

    try:
        import notification_service

        notification_service.send_sms(
            f"[{merchants.Platform.name} alert]\n"
            f"Agent {agent_id} is ordering unusually fast and has been stopped.\n"
            f"{verdict.reason}.\n"
            "Nothing was charged. No action needed unless you expected this."
        )
    except Exception:
        # Telling her can never be the thing that breaks a refusal. The
        # order is already refused and already on the record.
        return False
    return True


def reset_alerts() -> None:
    """Tests, and a merchant who has dealt with it."""
    _ALERTED.clear()


def _limits_snapshot(mandate, tier, buyer_mandate: dict | None,
                    limits=None, verdict=None) -> dict:
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
        # Her rate and spend limits as they stood for THIS order. Recorded
        # for the same reason the cap is: she edits them whenever she
        # likes, and an evidence pack that referenced the live config
        # would describe limits that were never applied.
        "velocity": velocity.snapshot(limits, verdict) if (limits and verdict) else None,
    }


def _refuse_for_velocity(
    agent_id: str,
    protocol: str,
    cart_payload: list[dict],
    result,
    verdict,
    adjusted_mandate,
    tier,
    buyer_mandate: dict | None,
    limits,
    source: str | None,
    routine_id: str | None,
    now: datetime,
    db_path: str,
    merchant_id: str | None = None,
) -> dict:
    """A HARD refusal, and deliberately not an escalation.

    An escalation is the right answer when one order needs a person's
    judgement. A flood is not that. Two hundred orders in ninety seconds
    is not two hundred decisions somebody should make one at a time --
    putting them in her queue IS the denial of service, just arriving by
    a different route, and no answer she could give to any single one of
    them would be the right answer to the pattern.

    So: refused outright, one audit row with its own decision value, no
    Razorpay call, and one message to her per window rather than per
    order.
    """
    event_id = audit_log.record_event(
        agent_id=agent_id,
        protocol=protocol,
        cart=cart_payload,
        decision=velocity.DECISION,
        reason=verdict.reason,
        total_inr=result.total_inr,
        db_path=db_path,
        limits_snapshot=_limits_snapshot(
            adjusted_mandate, tier, buyer_mandate, limits, verdict
        ),
        source=source,
        routine_id=routine_id,
        ts=now.isoformat(),
        merchant_id=merchant_id,
    )
    notified = _tell_her_once(agent_id, verdict, now)

    raise VelocityRefused(event_id, verdict, notified, {
        "event_id": event_id,
        "agent_id": agent_id,
        "trust_tier": tier.value,
        "decision": velocity.DECISION,
        "reason": verdict.reason,
        "total_inr": result.total_inr,
        "alternatives": [],
        "velocity": {
            "orders_in_window": verdict.orders_in_window,
            "spend_in_window_inr": verdict.spend_in_window_inr,
            "max_orders_per_hour": verdict.effective_orders,
            "max_spend_per_day_inr": verdict.effective_spend,
            "merchant_notified": notified,
        },
    })


def negotiate_and_record(
    agent_id: str,
    protocol: str,
    cart: list[tuple[str, int]],
    buyer_mandate: dict | None = None,
    source: str | None = None,
    routine_id: str | None = None,
    now: datetime | None = None,
    merchant_id: str | None = None,
) -> dict:
    """Decide a cart against ONE kitchen's rules and record it against it.

    `merchant_id` selects whose menu, whose caps and whose velocity gate
    apply. It is the only thing a buyer gets to choose about a merchant,
    and it grants nothing -- it picks a tenant, and that tenant's rules
    then apply in full.

    None means the platform's default kitchen, which is what every
    caller written before the marketplace passes.
    """
    db_path = audit_log.DEFAULT_DB_PATH
    merchant_id = merchant_id or merchants.default_id()
    # Read the merchant's LIVE configuration, so edits she makes on the
    # setup page are the limits actually enforced here.
    menu = merchant_config.current_menu(merchant_id)
    adjusted_mandate, tier = trust.trust_adjusted_mandate(
        agent_id, merchant_config.current_mandate(merchant_id),
        db_path=db_path, merchant_id=merchant_id,
    )
    result = negotiation.evaluate(cart, mandate=adjusted_mandate, menu=menu)

    # How fast this agent has been going. Checked HERE rather than in the
    # core, because it is a question about the agent's recent history and
    # not about the cart -- negotiation.py has never heard of it, and a
    # test asserts it never will.
    #
    # It runs after the core so the trail records what she WOULD have
    # decided, and before any Razorpay call, which is the only ordering
    # that matters for money.
    now = now or datetime.now(timezone.utc)
    limits = merchant_config.current_velocity_limits(merchant_id)
    verdict = velocity.check(
        agent_id, result.total_inr, limits,
        tier_multiplier=trust.velocity_multiplier(tier),
        now=now, db_path=db_path, merchant_id=merchant_id,
    )

    cart_payload = [{"item": name, "qty": qty} for name, qty in cart]

    if not verdict.ok:
        return _refuse_for_velocity(
            agent_id, protocol, cart_payload, result, verdict,
            adjusted_mandate, tier, buyer_mandate, limits, source, routine_id,
            now=now, db_path=db_path, merchant_id=merchant_id,
        )
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
        limits_snapshot=_limits_snapshot(
            adjusted_mandate, tier, buyer_mandate, limits, verdict
        ),
        # How the order originated. A standing order still comes through
        # here like everything else -- there is no second charging path --
        # it just says so on the row.
        source=source,
        routine_id=routine_id,
        ts=now.isoformat(),
        merchant_id=merchant_id,
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

    response["merchant_id"] = merchant_id
    response["merchant_name"] = merchants.name_of(merchant_id)

    if result.decision == negotiation.Decision.APPROVE:
        suggestion = suggest_addon(cart, adjusted_mandate, menu, db_path=db_path,
                                   merchant_id=merchant_id)
        if suggestion:
            response["upsell_suggestion"] = suggestion

    return response


def suggest_addon(
    cart: list[tuple[str, int]],
    mandate,
    menu: dict,
    db_path: str | None = None,
    merchant_id: str | None = None,
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
    merchant_id: str | None = None,
) -> dict:
    """Defense in depth: re-validates the cart right now, at payment time,
    rather than trusting a decision made moments earlier. Only ever called
    after an adapter has already reached an APPROVE-shaped state.

    skip_reevaluation exists ONLY for the human-override path: once a
    human has explicitly confirmed a specific escalated cart (recorded via
    record_human_override), re-running evaluate() would just say ESCALATE
    again forever -- the human's explicit, audited decision is what
    authorizes payment in that case, not the algorithmic re-check.

    `merchant_id` is WHICH KITCHEN is being paid, and every read below
    needs it. This function had none, so the re-validation priced the
    cart against the platform's DEFAULT menu whoever was selling: a
    grill-house order for a dish Amma does not stock died on a bare
    `KeyError: 'seekh_kebab'`, surfacing to the customer as HTTP 500
    immediately after their screen said the payment mandate was locked.

    It is the ninth wrong-tenant read in this project and the first on
    the money path -- the eight before it were wrong prices and wrong
    queues, and this one is a sale that cannot complete at all. The
    lesson is the same one every time: a config read with no kitchen is
    a config read that silently means "the first one".
    """
    db_path = audit_log.DEFAULT_DB_PATH
    if skip_reevaluation:
        menu = merchant_config.current_menu(merchant_id)
        total_inr = sum(menu[name].price_inr * qty for name, qty in cart)
    else:
        adjusted_mandate, _ = trust.trust_adjusted_mandate(
            agent_id, merchant_config.current_mandate(merchant_id),
            db_path=db_path, merchant_id=merchant_id,
        )
        result = negotiation.evaluate(
            cart, mandate=adjusted_mandate,
            menu=merchant_config.current_menu(merchant_id),
        )
        if result.decision != negotiation.Decision.APPROVE:
            raise ValueError(f"cart no longer approved at payment time: {result.decision}")
        total_inr = result.total_inr

    description = " + ".join(f"{qty}x {name}" for name, qty in cart)
    try:
        link = razorpay_client.create_payment_link(
            amount_inr=total_inr,
            # The kitchen actually being paid. This said "Amma's Kitchen"
            # for every order on the platform -- and unlike the leaks
            # above, this string is on the RAZORPAY PAGE the customer
            # pays on, so a grill-house customer was asked to pay a
            # different shop.
            description=f"{merchants.name_of(merchant_id)} order: {description}",
            reference_id=f"order-{event_id}-{uuid.uuid4().hex[:6]}",
        )
    except Exception as exc:
        # Razorpay test mode caps an account at 30 payment links EVER
        # created -- cancelling them does not give the quota back, which
        # is easy to assume and wrong. Past that every checkout dies here,
        # and the customer was being shown a bare "HTTP 500": no idea
        # whether their order was wrong, the kitchen was shut, or the
        # money had moved.
        #
        # Nothing about the order was wrong. It cleared both mandates and
        # every rule; the account simply cannot mint another link. Say
        # exactly that, and say it as a 503 -- the service is unavailable,
        # the request was fine.
        if "limit of 30" in str(exc) or "payment_link" in str(exc).lower():
            raise HTTPException(
                status_code=503,
                detail="Razorpay test-mode payment links are used up (30 of 30 "
                       "created; cancelling them does not free the quota). Nothing "
                       "is wrong with this order -- it passed every check. Create a "
                       "fresh Razorpay test account and swap the keys in .env.",
            ) from exc
        raise
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
