"""The BUYER's mandate -- what the customer authorized their agent to do.

There are two independent parties in this system and each has its own
limits, enforced by its own side:

  buyer_mandate.py   the customer's instructions to their shopping agent
                     ("don't spend over Rs.600; ask me above Rs.300").
                     Enforced BEFORE anything is sent to a merchant.

  mandate.py         the merchant's rules for accepting agent orders
                     ("no bulk catering to agents; let me see anything
                     over Rs.400"). Enforced by negotiation.py.

They are not the same thing and neither can override the other. An order
can be stopped by the buyer's own agent before a merchant ever sees it,
or accepted by the buyer's side and still refused by the merchant's, or
need a human on both sides.

Like negotiation.py, this is pure and deterministic -- no LLM, no I/O.
The model may propose a cart; it never decides whether the customer's
money can be spent on it.
"""

from dataclasses import dataclass
from enum import Enum

from mandate import MENU, MenuItem


class BuyerDecision(str, Enum):
    PROCEED = "PROCEED"      # inside what the customer authorized
    ASK_USER = "ASK_USER"    # over the customer's comfort line; ask them
    REFUSE = "REFUSE"        # over the hard cap; the agent must not ask


@dataclass(frozen=True)
class BuyerMandate:
    """What the customer told their agent. Set per-agent, by the customer."""

    # The agent must never spend more than this on one order, and must
    # not even ask a merchant to. No confirmation unlocks it -- if the
    # customer wants more, they raise the cap themselves.
    spend_cap_inr: int = 600

    # At or above this, the agent must check with the customer first.
    confirm_above_inr: int = 300


DEFAULT_BUYER_MANDATE = BuyerMandate()


@dataclass(frozen=True)
class BuyerCheck:
    decision: BuyerDecision
    reason: str
    total_inr: int


def check_cart(
    cart: list[tuple[str, int]],
    mandate: BuyerMandate = DEFAULT_BUYER_MANDATE,
    menu: dict[str, MenuItem] = MENU,
) -> BuyerCheck:
    """Run the buyer agent's own gate over a proposed cart."""
    for item, _qty in cart:
        if item not in menu:
            return BuyerCheck(
                BuyerDecision.REFUSE, f"cannot price an unknown item: {item}", 0
            )

    total = sum(menu[item].price_inr * qty for item, qty in cart)

    if total > mandate.spend_cap_inr:
        return BuyerCheck(
            BuyerDecision.REFUSE,
            f"Rs.{total} is over the Rs.{mandate.spend_cap_inr} your customer "
            f"authorized; the agent will not send this to a merchant",
            total,
        )

    if total >= mandate.confirm_above_inr:
        return BuyerCheck(
            BuyerDecision.ASK_USER,
            f"Rs.{total} is at or above the Rs.{mandate.confirm_above_inr} your "
            f"customer asked to be consulted about",
            total,
        )

    return BuyerCheck(
        BuyerDecision.PROCEED,
        f"Rs.{total} is inside what your customer authorized",
        total,
    )
