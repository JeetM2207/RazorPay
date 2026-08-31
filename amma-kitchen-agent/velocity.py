"""Per-agent rate and spend limits: the gate that catches a flood.

Every other rule in this project is per-cart, and that turns out to be a
hole you can drive through with nothing but valid requests. One agent can
place two hundred Rs.399 orders in ninety seconds. Each one clears the
budget cap, clears the category check, sits under the confirmation
threshold, and never asks a human -- and nothing anywhere notices, because
nothing was ever counting. "Bounded" was bounded per order and unbounded
in aggregate.

Two limits close it, and they are hers, configured beside her others:

    max_orders_per_hour_per_agent     how often one agent may order
    max_spend_per_day_per_agent_inr   how much one agent may spend

Where this lives, and where it does not
---------------------------------------
In the orchestrator, before any Razorpay call. `negotiation.py` has never
heard of it and must not: the core decides whether a CART is acceptable,
and this is a question about an agent's recent behaviour, which is not a
property of the cart at all. Same separation as trust.py -- read the
trail, adjust what the orchestrator does, leave the pure core alone.

What counts, precisely
----------------------
Counted toward both limits:

  * **Decision rows only** -- rows with no `order_ref`. The pay-first
    lifecycle writes AWAITING_PAYMENT, PAID, AUTO_CONFIRMED and the rest
    as separate rows pointing back at the decision; counting those would
    multiply one order by five.
  * whose decision is **APPROVE or ESCALATE** -- the two verdicts money
    can follow from -- **or which carry a payment_id**, whatever the
    decision said.

Deliberately NOT counted:

  * **COUNTER_OFFER** -- nothing was bought; the agent was told to ask
    again, and holding that against it would punish the negotiation the
    core exists to do.
  * **Terminally closed orders**: REJECTED, MERCHANT_REJECTED, REFUNDED,
    REFUND_FAILED, PAYMENT_NOT_COMPLETED, MERCHANT_TIMEOUT_REFUNDED. The
    sale did not happen. A refunded order is excluded from the SPEND
    total too, by looking for a refund row pointing at it -- the same rule
    the merchant's revenue KPI already applies, because money returned is
    not money spent.
  * **UNMATCHED_DEMAND** -- a record of something asked for, not an order.
  * **Our own refusals** (`VELOCITY_REFUSED`). A gate that counted its own
    refusals would ratchet: one breach would keep the window shut long
    after the traffic stopped.

So the answer to "does an unpaid approval count?" is **yes**, and it has
to be. If only settled orders counted, an attacker who never pays would
never trip the limit -- which is precisely the flood being defended
against. An APPROVE is a standing invitation to pay, and it occupies the
window until it is closed out.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import audit_log

# Verdicts money can still follow from.
_LIVE_DECISIONS = ("APPROVE", "ESCALATE")

# Rows that record an order NOT happening. See the docstring.
_CLOSED_DECISIONS = frozenset({
    "COUNTER_OFFER",
    "REJECTED",
    "MERCHANT_REJECTED",
    "REFUNDED",
    "REFUND_FAILED",
    "PAYMENT_NOT_COMPLETED",
    "MERCHANT_TIMEOUT_REFUNDED",
    "UNMATCHED_DEMAND",
    "VELOCITY_REFUSED",
})

# The decision written when this gate refuses. Distinct on purpose: a
# reader of the trail must be able to tell "her rules said no to this
# cart" from "this agent was going too fast", because they call for
# completely different responses.
DECISION = "VELOCITY_REFUSED"

ORDER_WINDOW = timedelta(hours=1)
SPEND_WINDOW = timedelta(hours=24)


@dataclass(frozen=True)
class VelocityLimits:
    """Her limits on how fast one agent may go.

    Deliberately NOT fields on `Mandate`. Mandate is what
    `negotiation.py` is handed, and the core has no business knowing an
    agent's history exists -- a cart is acceptable or it is not,
    regardless of who asked or how recently.
    """

    max_orders_per_hour: int = 6
    max_spend_per_day_inr: int = 2000


def default_limits() -> VelocityLimits:
    """The shipped defaults, as a function rather than a bare constructor.

    One seam, so a caller that needs different numbers -- the test suite,
    which fires twenty carts through one agent to test something else --
    has exactly one place to change them. Everything that needs a default
    goes through here, including merchant_config.save(), so the value
    cannot be widened in one place and reset in another.
    """
    return VelocityLimits()


@dataclass(frozen=True)
class VelocityVerdict:
    ok: bool
    reason: str = ""
    orders_in_window: int = 0
    spend_in_window_inr: int = 0
    limits: VelocityLimits | None = None
    tier_multiplier: float = 1.0

    @property
    def effective_orders(self) -> int:
        return _scaled(self.limits.max_orders_per_hour, self.tier_multiplier)

    @property
    def effective_spend(self) -> int:
        return _scaled(self.limits.max_spend_per_day_inr, self.tier_multiplier)


def _scaled(limit: int, multiplier: float) -> int:
    """At least 1, so a narrowing multiplier can never reach zero and
    lock an agent out of ordering at all."""
    return max(1, int(limit * multiplier))


def _parse(ts: str) -> datetime:
    stamp = datetime.fromisoformat(ts)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _counts_toward_limits(row: dict) -> bool:
    if row.get("order_ref"):
        return False                       # a lifecycle row, not an order
    if row["decision"] in _CLOSED_DECISIONS:
        return False
    return bool(row["decision"] in _LIVE_DECISIONS or row.get("payment_id"))


def usage(
    agent_id: str,
    now: datetime | None = None,
    db_path: str | None = None,
    merchant_id: str | None = None,
) -> tuple[int, int]:
    """(orders in the last hour, rupees in the last day) for this agent.

    Counted PER KITCHEN. Each merchant sets her own rate and spend
    limits, so they have to be measured against her own traffic -- an
    agent that bought lunch at one kitchen has not used up anything at
    another, and counting across the platform meant one merchant's
    customers exhausting a different merchant's gate.

    None counts platform-wide, which is what a caller with no kitchen in
    hand gets and what the pre-marketplace behaviour was.

    Read from the audit trail, which is the same single source of truth
    trust.py reads. There is deliberately no counter table: a second
    record of the same fact is a second record that can disagree with the
    first, and the trail is the one that gets shown to a judge.
    """
    now = now or datetime.now(timezone.utc)
    db_path = db_path or audit_log.DEFAULT_DB_PATH
    rows = audit_log.get_events_for_agent(agent_id, db_path=db_path,
                                          merchant_id=merchant_id)

    # Orders whose money came back are not spend. Matched the way the
    # merchant console already matches them: by a refund row pointing at
    # the decision.
    refunded = {
        r["order_ref"] for r in rows
        if r["decision"] in ("REFUNDED", "MERCHANT_TIMEOUT_REFUNDED") and r.get("order_ref")
    }

    orders = 0
    spend = 0
    for row in rows:
        if not _counts_toward_limits(row):
            continue
        try:
            age = now - _parse(row["ts"])
        except (TypeError, ValueError):
            continue
        if age <= ORDER_WINDOW:
            orders += 1
        if age <= SPEND_WINDOW and row["id"] not in refunded:
            spend += row["total_inr"] or 0
    return orders, spend


def check(
    agent_id: str,
    cart_total_inr: int,
    limits: VelocityLimits,
    tier_multiplier: float = 1.0,
    now: datetime | None = None,
    db_path: str | None = None,
    merchant_id: str | None = None,
) -> VelocityVerdict:
    """Would letting this order through breach either limit?

    The spend check includes the order being asked about, because the
    question is whether it may proceed -- not whether the damage is
    already done.
    """
    orders, spend = usage(agent_id, now=now, db_path=db_path,
                          merchant_id=merchant_id)
    max_orders = _scaled(limits.max_orders_per_hour, tier_multiplier)
    max_spend = _scaled(limits.max_spend_per_day_inr, tier_multiplier)

    verdict = VelocityVerdict(
        ok=True, orders_in_window=orders, spend_in_window_inr=spend,
        limits=limits, tier_multiplier=tier_multiplier,
    )

    if orders >= max_orders:
        return VelocityVerdict(
            ok=False,
            reason=(f"agent rate limit reached: {orders} orders in the last hour, "
                    f"limit {max_orders}"),
            orders_in_window=orders, spend_in_window_inr=spend,
            limits=limits, tier_multiplier=tier_multiplier,
        )

    if spend + cart_total_inr > max_spend:
        return VelocityVerdict(
            ok=False,
            reason=(f"agent daily spend limit reached: Rs.{spend} spent in the last 24h, "
                    f"this order Rs.{cart_total_inr}, limit Rs.{max_spend}"),
            orders_in_window=orders, spend_in_window_inr=spend,
            limits=limits, tier_multiplier=tier_multiplier,
        )

    return verdict


def snapshot(limits: VelocityLimits, verdict: VelocityVerdict) -> dict:
    """What the velocity limits WERE, for the evidence pack.

    Same reason the cap snapshot exists: Amma edits these whenever she
    likes, and an order that referenced the live config would start
    describing limits that were never applied to it.
    """
    return {
        "max_orders_per_hour": limits.max_orders_per_hour,
        "max_spend_per_day_inr": limits.max_spend_per_day_inr,
        "tier_multiplier": verdict.tier_multiplier,
        "effective_orders_per_hour": verdict.effective_orders,
        "effective_spend_per_day_inr": verdict.effective_spend,
        "orders_in_window_at_decision": verdict.orders_in_window,
        "spend_in_window_at_decision_inr": verdict.spend_in_window_inr,
    }
