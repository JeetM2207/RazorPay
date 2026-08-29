"""Agent Trust Layer -- bounded-autonomy scoring for buyer agents.

This is a live-scale preview of the problem NPCI's still-unlaunched Unified
Agent Protocol (UAP) is trying to solve: how does a merchant safely
authorize an AI agent it has no prior relationship with, and extend more
autonomy to agents with a proven track record?

Design rule, mirrored from negotiation.py's own philosophy: trust is
computed from this system's OWN audit history only (never self-reported by
the agent), and trust may only ever widen the FLEXIBLE negotiation margin.
It must never touch the hard budget cap or the human-confirm threshold --
those are the merchant's absolute limits and stay fixed regardless of how
trusted an agent is. Trust buys a smoother negotiation, never a bigger
blast radius.

This module reads audit_log and produces an adjusted Mandate; it never
calls into negotiation.py, and negotiation.py never calls into this
module. The pure decision core is unmodified by this feature existing.
"""

from dataclasses import replace
from enum import Enum

import audit_log
from mandate import Mandate


class TrustTier(str, Enum):
    NEW = "NEW"
    STANDARD = "STANDARD"
    TRUSTED = "TRUSTED"


STANDARD_MIN_COMPLETED = 1
TRUSTED_MIN_COMPLETED = 5

# Flexible-margin widening by tier. Budget cap and human-confirm threshold
# are never present in this table on purpose -- they are not adjustable.
TIER_FLEXIBLE_MARGIN_PCT = {
    TrustTier.NEW: 0.05,
    TrustTier.STANDARD: 0.10,
    TrustTier.TRUSTED: 0.15,
}

# Trust's second lever: how WIDE this agent's rate and spend window is.
#
# Same rule as the margin above, and worth stating twice because it is the
# whole discipline of this module: a multiplier here scales a FLEXIBLE
# limit -- how many orders an hour, how much a day -- and can never touch
# the budget cap or the confirmation threshold. A TRUSTED agent may order
# more often; it may not order anything Amma would not have sold it, and
# no order it places skips her confirmation.
#
# A proven agent gets more room; an unproven one gets less, which is the
# point -- the flood in the threat model comes from an agent with no
# history at all, and it is the one this narrows hardest.
TIER_VELOCITY_MULTIPLIER = {
    TrustTier.NEW: 0.5,
    TrustTier.STANDARD: 1.0,
    TrustTier.TRUSTED: 1.5,
}


def velocity_multiplier(tier: TrustTier) -> float:
    """How much of her configured window this tier gets.

    Deliberately returns a plain number rather than an adjusted limits
    object: the caller applies it, so there is no route by which this
    module could hand back something with a bigger cap in it.
    """
    return TIER_VELOCITY_MULTIPLIER[tier]


def compute_trust_tier(agent_id: str, db_path: str = audit_log.DEFAULT_DB_PATH) -> TrustTier:
    events = audit_log.get_events_for_agent(agent_id, db_path=db_path)

    violations = sum(1 for e in events if "category not allowed" in (e["reason"] or ""))
    if violations > 0:
        return TrustTier.NEW

    completed = sum(1 for e in events if e["decision"] == "APPROVE" and e["payment_id"])
    if completed >= TRUSTED_MIN_COMPLETED:
        return TrustTier.TRUSTED
    if completed >= STANDARD_MIN_COMPLETED:
        return TrustTier.STANDARD
    return TrustTier.NEW


def trust_adjusted_mandate(
    agent_id: str, base_mandate: Mandate, db_path: str = audit_log.DEFAULT_DB_PATH
) -> tuple[Mandate, TrustTier]:
    tier = compute_trust_tier(agent_id, db_path=db_path)
    adjusted = replace(base_mandate, flexible_margin_pct=TIER_FLEXIBLE_MARGIN_PCT[tier])
    return adjusted, tier
