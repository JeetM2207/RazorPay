"""Which add-on actually goes with what someone ordered. Pure, no I/O.

`audit_log.get_frequent_addons()` answers this from real co-purchase
history, and that is always the better answer -- it is evidence rather
than opinion. But a merchant who has just opened has no history, and
until she has some, `negotiation.suggest_upsell()` falls back to "the
most expensive thing that still fits".

That fallback produces bad suggestions. Two Paneer Bhurji and three
Tandoori Roti, and it offers a Chicken Biryani: the priciest item that
fits, and a second main course for people who already ordered dinner.
Nobody says yes to that, so the revenue hook earns nothing and the
customer learns the suggestions are noise.

This module supplies the missing ordering: candidates ranked by how well
their CATEGORY complements what is already in the cart. It decides
nothing and filters nothing -- it hands `suggest_upsell()` a preference
order through the `ranked_addons` parameter that already exists for
history, and the core still applies affordability, category and stock to
whatever comes back. A complement that breaks a limit is refused exactly
as a popular one is.
"""

from mandate import MenuItem

# What each category in the cart makes you want next, best first.
#
# Deliberately a table rather than a model: it is a handful of food
# pairings a cook could read and correct, and putting an LLM anywhere near
# the suggestion path would make the same cart give different answers on
# different days.
_COMPLEMENTS: dict[str, tuple[str, ...]] = {
    "meals": ("beverages", "desserts", "snacks"),
    "snacks": ("beverages", "desserts", "meals"),
    "beverages": ("snacks", "desserts", "meals"),
    "desserts": ("beverages", "snacks", "meals"),
}


def _cart_categories(cart: list[tuple[str, int]], menu: dict[str, MenuItem]) -> set[str]:
    return {menu[name].category for name, _qty in cart if name in menu}


def complements(
    cart: list[tuple[str, int]], menu: dict[str, MenuItem]
) -> list[str]:
    """Item names ordered by how well they complement this cart.

    Returns every item not already in the cart, most complementary first.
    Ties break on price descending, so among equally-fitting suggestions
    the merchant is offered the more valuable one -- and then on name, so
    the ranking is stable rather than dependent on dict order.
    """
    if not cart:
        return []

    present = _cart_categories(cart, menu)
    scores: dict[str, int] = {}
    for category in present:
        for position, complement in enumerate(_COMPLEMENTS.get(category, ())):
            # Earlier in the list is a better pairing. Summed across the
            # cart's categories, so a mixed cart wants what suits all of it.
            scores[complement] = scores.get(complement, 0) + (3 - position)

    in_cart = {name for name, _qty in cart}

    def rank(item: MenuItem) -> tuple[int, int, str]:
        score = scores.get(item.category, 0)
        if item.category in present:
            # They already have one. A second drink is not an upsell.
            score -= 5
        return (-score, -item.price_inr, item.name)

    return [item.name for item in sorted(menu.values(), key=rank) if item.name not in in_cart]
