"""Which add-on actually goes with what someone ordered. Pure, no I/O.

`audit_log.get_frequent_addons()` answers this from real co-purchase
history, and that is always the better answer -- it is evidence rather
than opinion. But a merchant who has just opened has no history, and
until she has some, `negotiation.suggest_upsell()` falls back to "the
most expensive thing that still fits".

That fallback produces bad suggestions. One Rs.80 Masala Dosa, and it
offers a Rs.220 Chicken Biryani: the priciest item that fits, and a
second main course for somebody who already ordered dinner. Nobody says
yes, so the revenue hook earns nothing and the customer learns the
suggestions are noise.

This module supplies the missing ordering. It decides and filters
nothing: it hands `suggest_upsell()` a preference order through the
`ranked_addons` parameter that already exists for history, and the core
still applies affordability, category and stock to whatever comes back.
A pairing that breaks a limit is refused exactly as a popular item is.

Everything here is derived from the menu it is given
-------------------------------------------------------------
The first version of this was a table of food pairings -- meals want
beverages, snacks want desserts, and so on. It read well and it was
wrong, because the menu belongs to the merchant. She can rename a
category, add "breads" or "combos" or "tiffin", or run a shop with no
beverages at all, and a hardcoded table silently stops applying to the
shop it is supposed to be selling. Nothing below names a category, a dish
or a price. Two rules, both computed from the live menu and the cart:

1. **Something they have not got yet.** A category already in the cart is
   the one they need least -- a second coffee is not an upsell.
2. **An accompaniment, not another main.** An add-on that costs a large
   fraction of the order is a second dinner. Preferring the dearest item
   that still sits comfortably under the order's own size is what keeps a
   Rs.30 coffee ahead of a Rs.220 biryani for a Rs.80 dosa, without
   anyone having to say so.

Within those, dearest first -- among suggestions someone would plausibly
accept, the merchant is offered the more valuable one -- then by name, so
the ranking is stable rather than dependent on dict order.
"""

from mandate import MenuItem

# An add-on worth more than this share of the order stops reading as an
# extra and starts reading as a second meal. A fraction rather than a
# rupee figure, because it has to hold for a Rs.30 order and a Rs.500 one
# on a menu whose prices the merchant changes whenever she likes.
_ACCOMPANIMENT_SHARE = 0.5


def complements(cart: list[tuple[str, int]], menu: dict[str, MenuItem]) -> list[str]:
    """Item names ordered by how well they complement this cart.

    Returns every item on the menu that is not already in the cart, most
    complementary first. Judging is left entirely to the caller.
    """
    if not cart:
        return []

    in_cart = {name for name, _qty in cart}
    cart_categories = {menu[name].category for name in in_cart if name in menu}
    cart_total = sum(menu[name].price_inr * qty for name, qty in cart if name in menu)
    accompaniment_ceiling = cart_total * _ACCOMPANIMENT_SHARE

    def rank(item: MenuItem) -> tuple[int, int, int, str]:
        return (
            # They already have this category; it is what they need least.
            1 if item.category in cart_categories else 0,
            # Small enough beside the order to read as an addition to it.
            0 if item.price_inr <= accompaniment_ceiling else 1,
            -item.price_inr,
            item.name,
        )

    return [item.name for item in sorted(menu.values(), key=rank) if item.name not in in_cart]
