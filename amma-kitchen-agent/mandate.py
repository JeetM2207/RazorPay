"""Merchant mandate + today's menu for Amma's Kitchen. Plain data, no logic.

Everything the negotiation core is allowed to know about what can be sold,
for how much, and how far it can bend, lives here. Nothing else should
hardcode these numbers.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class MenuItem:
    name: str
    category: str
    price_inr: int  # rupees, not paise
    stock: int


# Today's menu/inventory. Prices are illustrative test-mode numbers.
MENU: dict[str, MenuItem] = {
    "veg_thali": MenuItem("veg_thali", "meals", 150, stock=20),
    "chicken_biryani": MenuItem("chicken_biryani", "meals", 220, stock=15),
    "masala_dosa": MenuItem("masala_dosa", "snacks", 80, stock=25),
    "filter_coffee": MenuItem("filter_coffee", "beverages", 30, stock=50),
    "gulab_jamun": MenuItem("gulab_jamun", "desserts", 60, stock=30),
}


@dataclass(frozen=True)
class Mandate:
    """Bounds the negotiation core is allowed to operate within."""

    # Hard ceiling on a single order. Above this, always ESCALATE.
    budget_cap_inr: int = 500

    # Categories a buyer agent is allowed to order from at all.
    allowed_categories: tuple[str, ...] = ("meals", "snacks", "beverages", "desserts")

    # If the requested total exceeds budget_cap_inr by more than this
    # fraction, still ESCALATE even with a counter-offer on the table.
    # (e.g. 0.10 = up to 10% over cap can still get a counter-offer)
    flexible_margin_pct: float = 0.10

    # Orders at or above this value always require human confirmation,
    # regardless of whether they're within budget.
    human_confirm_threshold_inr: int = 400


MANDATE = Mandate()
