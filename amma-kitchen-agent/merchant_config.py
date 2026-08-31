"""The merchant's live shop configuration: identity, limits, and menu.

`mandate.py` holds the DEFAULTS -- what Amma's Kitchen looks like out of
the box. This module holds what she has actually configured, and is what
the running system decides against. Everything here starts as a copy of
those defaults and can be edited from the merchant setup page.

Why this exists at all: a setup page that let Amma change her budget cap
or her menu without those edits reaching negotiation.py would be lying to
her -- the screen would show one set of limits while a different set was
enforced. So the config is server-side and the decision path reads it.

negotiation.py is untouched by this. It already takes `mandate` and
`menu` as arguments and only uses the module-level ones as defaults, so
passing live values needs no change to the decision core at all.

State is kept in memory and mirrored to a JSON file so a restart doesn't
wipe the shop. Tests call reset_to_defaults() to stay isolated from it.
"""

import json
import os
import re
from dataclasses import asdict, replace
from pathlib import Path

import velocity
import merchants
from mandate import MANDATE, MENU, Mandate, MenuItem

CONFIG_PATH = Path(
    os.environ.get("MERCHANT_CONFIG_PATH", Path(__file__).resolve().parent / "merchant_config.json")
)

_DEFAULT_PROFILE = {
    "shop_name": "Amma's Kitchen",
    "address": "",
    "phone": "",
    "configured": False,
}

# One cached shop per kitchen, not one for the platform. Keyed rather
# than swapped, because FastAPI serves sync endpoints from a threadpool:
# a single "current merchant" global would let two concurrent requests
# read each other's menu, and the failure would be a wrong price on a
# real order rather than an exception anyone would notice.
_states: dict[str, dict] = {}


def _key(merchant_id: str | None) -> str:
    """Which kitchen a call is about.

    None means the platform default, which is what every caller written
    before the marketplace existed passes. CONFIG_PATH is still honoured
    for that one, so MERCHANT_CONFIG_PATH and the test fixture that
    points it at a tmp_path keep working exactly as they did.
    """
    return merchant_id or merchants.default_id()


def _path(merchant_id: str | None) -> Path:
    if _key(merchant_id) == merchants.default_id():
        return CONFIG_PATH          # honours MERCHANT_CONFIG_PATH and the tests
    return merchants.config_path(merchant_id)


# ------------------------------------------------------------- internals

def _fresh_state() -> dict:
    return {
        "profile": dict(_DEFAULT_PROFILE),
        "mandate": asdict(MANDATE),
        # Kept in their own block rather than folded into "mandate",
        # because the mandate block is what becomes a `Mandate` -- the
        # object `negotiation.py` is handed. These are limits on an
        # agent's RECENT BEHAVIOUR, which is not a property of the cart,
        # and the core has no business being able to see them.
        "velocity": asdict(velocity.default_limits()),
        "menu": {name: asdict(item) for name, item in MENU.items()},
    }


def _load(merchant_id: str | None = None) -> dict:
    key = _key(merchant_id)
    cached = _states.get(key)
    if cached is not None:
        return cached

    state = _fresh_state()
    state["profile"]["shop_name"] = merchants.name_of(key)
    path = _path(merchant_id)
    if path.exists():
        try:
            stored = json.loads(path.read_text())
            state["profile"].update(stored.get("profile", {}))
            state["mandate"].update(stored.get("mandate", {}))
            state["velocity"].update(stored.get("velocity", {}))
            if stored.get("menu"):
                state["menu"] = stored["menu"]
        except (json.JSONDecodeError, OSError):
            # A corrupt config must not take the shop offline; fall back
            # to defaults rather than refusing to start.
            state = _fresh_state()
            state["profile"]["shop_name"] = merchants.name_of(key)
    _states[key] = state
    return state


def _persist(merchant_id: str | None = None) -> None:
    try:
        _path(merchant_id).write_text(json.dumps(_load(merchant_id), indent=2))
    except OSError:
        pass  # an unwritable disk must not break an in-flight order


def reset_to_defaults(merchant_id: str | None = None) -> None:
    """Used by tests, and by a merchant who wants a clean slate.

    With no id it clears EVERY kitchen, which is what the test fixture
    wants: one shop left behind in the cache would leak into the next
    test exactly as one saved in a browser used to leak into the suite.
    """
    if merchant_id is None:
        _states.clear()
        _states[merchants.default_id()] = _fresh_state()
        return
    _states[_key(merchant_id)] = _fresh_state()


# ----------------------------------------------------------------- reads

def profile(merchant_id: str | None = None) -> dict:
    return dict(_load(merchant_id)["profile"])


def is_configured(merchant_id: str | None = None) -> bool:
    return bool(_load(merchant_id)["profile"]["configured"])


def current_mandate(merchant_id: str | None = None) -> Mandate:
    """The limits the negotiation core should decide against right now."""
    data = _load(merchant_id)["mandate"]
    return Mandate(
        budget_cap_inr=int(data["budget_cap_inr"]),
        allowed_categories=tuple(data["allowed_categories"]),
        flexible_margin_pct=float(data["flexible_margin_pct"]),
        human_confirm_threshold_inr=int(data["human_confirm_threshold_inr"]),
    )


def current_velocity_limits(merchant_id: str | None = None) -> velocity.VelocityLimits:
    """How fast one agent may go, as configured right now.

    A separate accessor from `current_mandate()` on purpose: nothing that
    calls the negotiation core should be able to pick these up by
    accident.
    """
    data = _load(merchant_id).get("velocity") or {}
    default = velocity.default_limits()
    return velocity.VelocityLimits(
        max_orders_per_hour=int(data.get("max_orders_per_hour", default.max_orders_per_hour)),
        max_spend_per_day_inr=int(
            data.get("max_spend_per_day_inr", default.max_spend_per_day_inr)
        ),
    )


def current_menu(merchant_id: str | None = None) -> dict[str, MenuItem]:
    return {
        name: MenuItem(
            name=item["name"],
            category=item["category"],
            price_inr=int(item["price_inr"]),
            stock=int(item["stock"]),
        )
        for name, item in _load(merchant_id)["menu"].items()
    }


def as_dict(merchant_id: str | None = None) -> dict:
    """Everything the setup page needs to render itself."""
    mandate = current_mandate(merchant_id)
    raw = _load(merchant_id)["menu"]
    return {
        "profile": profile(merchant_id),
        "mandate": {
            "budget_cap_inr": mandate.budget_cap_inr,
            "human_confirm_threshold_inr": mandate.human_confirm_threshold_inr,
            "allowed_categories": list(mandate.allowed_categories),
            "flexible_margin_pct": mandate.flexible_margin_pct,
        },
        "velocity": asdict(current_velocity_limits(merchant_id)),
        "menu": [
            {
                "id": item.name,
                "title": item.name.replace("_", " ").title(),
                "category": item.category,
                "price_inr": item.price_inr,
                "list_price_inr": raw[item.name].get("list_price_inr", item.price_inr),
                "sale": bool(raw[item.name].get("sale", False)),
                "stock": item.stock,
                "agent_orderable": item.category in mandate.allowed_categories,
            }
            for item in current_menu(merchant_id).values()
        ],
    }


# ---------------------------------------------------------------- writes

_QTY_PREFIX = re.compile(r"^\s*\d+\s*(x|nos?\.?|pcs?\.?|plates?|portions?)?\s+", re.IGNORECASE)


def _normalise_request(text: str) -> str:
    """Reduce a phrase a person typed to something comparable with a menu
    entry: drop a leading quantity, lowercase, collapse punctuation, and
    singularise the last word ("2 masala dosas" -> "masala dosa")."""
    cleaned = _QTY_PREFIX.sub("", (text or "").strip().lower())
    cleaned = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in cleaned)
    words = cleaned.split()
    if words and len(words[-1]) > 3 and words[-1].endswith("s"):
        words[-1] = words[-1][:-1]
    return " ".join(words)


def resolve_item(text: str, merchant_id: str | None = None) -> str | None:
    """Best-effort free-text -> catalog item_id, deliberately conservative.

    Exists because a caller may report something it could not match, and
    it may simply have been wrong -- "2 masala dosas" is a real item
    described loosely. Getting a real match back is better than logging
    phantom demand for something the merchant already sells.

    Conservative on purpose: exact matches first, then a containment pass
    that requires exactly ONE candidate. Two possible items means the
    phrase is ambiguous, and guessing would put a dish nobody asked for
    into somebody's cart -- so it returns None and the caller logs it as
    unmatched instead. No fuzzy distance, no model call.
    """
    query = _normalise_request(text)
    if not query:
        return None

    menu = current_menu(merchant_id)
    by_id = {name: _normalise_request(name.replace("_", " ")) for name in menu}

    for item_id, title in by_id.items():
        if query in (item_id, title, _normalise_request(item_id)):
            return item_id

    candidates = [
        item_id
        for item_id, title in by_id.items()
        if title and (title in query or query in title)
    ]
    return candidates[0] if len(candidates) == 1 else None


# Words people write instead of digits. Deliberately short: this list is
# for the common cases, and anything outside it falls through to a
# quantity of 1 rather than to a guess.
_QTY_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "a couple of": 2,
    "couple of": 2, "dozen": 12, "half a dozen": 6,
}

# How people open an order. Stripped so "order 2 roti" and "2 roti" are
# the same request.
_LEAD_IN = re.compile(
    r"^\s*(please\s+)?(can\s+i\s+(get|have)|could\s+i\s+(get|have)|i(\s+would|'d)?\s+"
    r"(like|want)|get\s+me|give\s+me|send\s+me|order|buy)\s+",
    re.IGNORECASE,
)

# What separates one dish from the next.
_SPLIT = re.compile(r"\s*(?:,|;|\+|\band\b|\bplus\b|\bwith\b)\s*", re.IGNORECASE)

# A leading count, digits or words, kept so the quantity can be read off.
_QTY_READ = re.compile(
    r"^\s*(\d+|" + "|".join(sorted(_QTY_WORDS, key=len, reverse=True)) + r")\b\s*"
    r"(?:x|nos?\.?|pcs?\.?|plates?|portions?|servings?|orders?\s+of)?\s*",
    re.IGNORECASE,
)


def _read_qty(phrase: str) -> tuple[int, str]:
    match = _QTY_READ.match(phrase)
    if not match:
        return 1, phrase.strip()
    token = match.group(1).lower()
    qty = int(token) if token.isdigit() else _QTY_WORDS.get(token, 1)
    return max(1, min(qty, 99)), phrase[match.end():].strip()


def parse_request(text: str, merchant_id: str | None = None) -> dict:
    """Free text -> a cart proposal, with no model involved.

    The fallback for when the LLM that normally does this is unreachable.
    It is a real fallback, not a stub: it splits the sentence, reads each
    quantity, and resolves each phrase through `resolve_item` -- the same
    conservative matcher the MCP demand path uses, which returns None
    rather than guessing between two candidates.

    Being worse than the model is expected and is the correct trade. The
    model can tell "not sold here" from "close to something here" using
    the whole menu as context; this cannot, so anything it fails to
    resolve goes into `unmatched` in the customer's own words and is
    handled by exactly the same off-menu path a model-reported miss is.
    A miss is a question asked; a wrong match would be a dish nobody
    ordered, silently added to a cart.

    What it does NOT do is decide anything. It proposes a cart, the same
    as the model does, and every gate after it is untouched -- which is
    why swapping one for the other is safe at all.
    """
    cleaned = _LEAD_IN.sub("", (text or "").strip())
    items: dict[str, int] = {}
    unmatched: list[str] = []

    for phrase in _SPLIT.split(cleaned):
        phrase = phrase.strip(" .!\t")
        if not phrase:
            continue
        qty, remainder = _read_qty(phrase)
        item_id = resolve_item(remainder) or resolve_item(phrase, merchant_id)
        if item_id:
            # Summed, not appended: "a thali and another thali" is two
            # thalis, the same rule the console's basket follows.
            items[item_id] = items.get(item_id, 0) + qty
        else:
            unmatched.append(phrase)

    return {
        "items": [{"item_id": k, "qty": v} for k, v in items.items()],
        "unmatched": unmatched,
    }


def _slug(text: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in text.strip().lower())
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("_")


def save(profile_in: dict, mandate_in: dict, menu_in: list[dict],
         velocity_in: dict | None = None,
         merchant_id: str | None = None) -> dict:
    """Replace the shop's configuration wholesale.

    Validation is strict and returns a plain message, because a merchant
    mis-typing a cap should get told, not silently get a shop that
    behaves differently from what the screen showed.
    """
    state = _load(merchant_id)

    shop_name = (profile_in.get("shop_name") or "").strip()
    if not shop_name:
        raise ValueError("Your shop needs a name.")

    budget_cap = int(mandate_in.get("budget_cap_inr") or 0)
    confirm_at = int(mandate_in.get("human_confirm_threshold_inr") or 0)
    if budget_cap <= 0:
        raise ValueError("The most you'll accept per order has to be above zero.")
    if confirm_at <= 0:
        raise ValueError("The amount you want to be asked about has to be above zero.")
    if confirm_at > budget_cap:
        raise ValueError(
            "You'd be asked about orders you would never accept anyway — "
            "keep the confirmation amount at or below the maximum."
        )

    # Her limits on how fast one agent may go. Validated in the same
    # breath as the others, and a refused save leaves the shop untouched
    # exactly as before.
    # Falls back to what she ALREADY has, not to the shipped defaults: a
    # caller that only means to edit the menu must not silently reset her
    # rate limits, and `velocity=None` is exactly that caller.
    defaults = current_velocity_limits(merchant_id)
    velocity_in = velocity_in or {}
    max_orders = int(velocity_in.get("max_orders_per_hour") or defaults.max_orders_per_hour)
    max_spend = int(
        velocity_in.get("max_spend_per_day_inr") or defaults.max_spend_per_day_inr
    )
    if max_orders <= 0:
        raise ValueError("An agent has to be allowed at least one order an hour.")
    if max_spend <= 0:
        raise ValueError("An agent's daily spending limit has to be above zero.")
    if max_spend < budget_cap:
        raise ValueError(
            "One agent's daily limit is below what you'd accept on a single order — "
            "no agent could ever place one."
        )

    if not menu_in:
        raise ValueError("Add at least one dish to your menu.")

    menu: dict[str, dict] = {}
    categories: list[str] = []
    for row in menu_in:
        title = (row.get("title") or "").strip()
        if not title:
            raise ValueError("Every dish needs a name.")
        item_id = (row.get("id") or "").strip() or _slug(title)
        if not item_id:
            raise ValueError(f"Couldn't make an id for '{title}'.")

        price = int(row.get("price_inr") or 0)
        stock = int(row.get("stock") or 0)
        if price <= 0:
            raise ValueError(f"'{title}' needs a price above zero.")
        if stock < 0:
            raise ValueError(f"'{title}' can't have negative stock.")

        category = (row.get("category") or "").strip().lower().replace(" ", "_") or "meals"
        # list_price_inr is what she actually charges; price_inr is what
        # is being charged right now, which is lower while a sale is on.
        # A row that carries neither is a row she typed by hand, so the
        # price she typed becomes the list price and any sale ends -- see
        # optimize_prices().
        menu[item_id] = {
            "name": item_id,
            "category": category,
            "price_inr": price,
            "stock": stock,
            "list_price_inr": int(row.get("list_price_inr") or price),
            "sale": bool(row.get("sale", False)),
        }
        # An item marked orderable contributes its category to the
        # allow-list; one marked otherwise is sold in person only.
        if row.get("agent_orderable", True) and category not in categories:
            categories.append(category)

    if not categories:
        raise ValueError("At least one dish has to be orderable by an agent.")

    state["profile"] = {
        "shop_name": shop_name,
        "address": (profile_in.get("address") or "").strip(),
        "phone": (profile_in.get("phone") or "").strip(),
        "configured": True,
    }
    state["mandate"] = {
        "budget_cap_inr": budget_cap,
        "human_confirm_threshold_inr": confirm_at,
        "allowed_categories": categories,
        "flexible_margin_pct": float(
            mandate_in.get("flexible_margin_pct", MANDATE.flexible_margin_pct)
        ),
    }
    state["velocity"] = {
        "max_orders_per_hour": max_orders,
        "max_spend_per_day_inr": max_spend,
    }
    state["menu"] = menu

    _persist(merchant_id)
    return as_dict(merchant_id)


# ------------------------------------------------- inventory-led pricing
#
# Writes ONLY to this module's live state, through save(), so every rule
# save() already enforces still applies. negotiation.py is untouched and
# unaware: it is handed a menu with a price on it, exactly as before, and
# has no idea whether that price is a sale price. A discount is a fact
# about the shop, not an input to a decision.

HIGH_STOCK = 10      # more than this and it needs moving
LOW_STOCK = 3        # fewer than this and it sells itself
DISCOUNT_PCT = 15


def _sale_price(list_price: int) -> int:
    """Rounded DOWN to the rupee, and never below one.

    Down rather than nearest, because the only direction that can
    surprise a customer is upward -- Rs.127 advertised and Rs.128 charged
    is a complaint, the reverse is not.
    """
    return max(1, int(list_price * (100 - DISCOUNT_PCT) // 100))


def adjust_stock(cart: list, direction: int = -1,
                 merchant_id: str | None = None) -> list[dict]:
    """Move stock by a paid order. -1 consumes it, +1 puts it back.

    THE ONE WRITER. Everything else in this project reads stock -- the
    core to refuse a dish that has run out, the repricer to decide what
    needs moving -- and until now nothing ever changed it, so a kitchen
    that sold twenty thalis still showed twenty in stock and the
    "discounted, plenty in stock" logic was reasoning about a number that
    never moved.

    Saved through the same save() the setup page uses, so every
    validation she is already protected by still runs, and a refused save
    leaves the shop untouched.

    Clamped at zero. Overselling is possible in a way this cannot fix --
    two agents can both pass the stock check a millisecond apart, and the
    real answer to that is a lock this project does not have -- but stock
    going negative would be a number nobody can act on, and it would drag
    a dish below LOW_STOCK and silently end its sale.
    """
    state = _load(merchant_id)
    rows, moved = [], []

    wanted = {}
    for line in cart or []:
        # Carts arrive as (id, qty) pairs from the core and as
        # {"item": id, "qty": n} dicts from the trail. Accept both rather
        # than making every caller normalise.
        if isinstance(line, dict):
            item_id, qty = line.get("item") or line.get("item_id"), line.get("qty", 0)
        else:
            item_id, qty = line[0], line[1]
        if item_id:
            wanted[item_id] = wanted.get(item_id, 0) + int(qty or 0)

    for item in as_dict(merchant_id)["menu"]:
        row = dict(item)
        qty = wanted.get(row["id"], 0)
        if qty:
            was = int(row["stock"])
            row["stock"] = max(0, was + direction * qty)
            if row["stock"] != was:
                moved.append({"id": row["id"], "title": row["title"],
                              "was": was, "now": row["stock"]})
        rows.append(row)

    if moved:
        save(merchant_id=merchant_id, profile_in=state["profile"], mandate_in=state["mandate"], menu_in=rows)
    return moved


def optimize_prices(merchant_id: str | None = None) -> dict:
    """Discount what is piling up, restore what is running out.

    Three bands, and the middle one is deliberately inert: above
    HIGH_STOCK a dish goes on sale, below LOW_STOCK it goes back to her
    list price, and in between whatever is already true stays true. That
    is what lets a sale actually run -- a dish discounted at 20 portions
    keeps its price the whole way down to 3, instead of flickering off
    the moment it sells one.

    The sale price is always derived from `list_price_inr`, NEVER from
    the current price. Deriving it from the current price would compound:
    two clicks of a 15% discount is 28% off, ten clicks is 80% off, and
    nothing in the system would have flagged it. A test runs this five
    times and asserts the price does not move after the first.
    """
    state = _load(merchant_id)
    changed = []

    rows = []
    for item in as_dict(merchant_id)["menu"]:
        row = dict(item)
        list_price = int(row.get("list_price_inr") or row["price_inr"])
        row["list_price_inr"] = list_price
        was, on_sale = int(row["price_inr"]), bool(row.get("sale"))

        if int(row["stock"]) > HIGH_STOCK:
            row["price_inr"], row["sale"] = _sale_price(list_price), True
        elif int(row["stock"]) < LOW_STOCK:
            row["price_inr"], row["sale"] = list_price, False
        # else: leave the dish exactly as it is.

        if (row["price_inr"], row["sale"]) != (was, on_sale):
            changed.append({
                "id": row["id"],
                "title": row["title"],
                "was_inr": was,
                "now_inr": row["price_inr"],
                "stock": int(row["stock"]),
                "sale": row["sale"],
                "why": "discounted, plenty in stock" if row["sale"] else "back to list price, running low",
            })
        rows.append(row)

    save(merchant_id=merchant_id, profile_in=state["profile"], mandate_in=state["mandate"], menu_in=rows)
    return {
        "changed": changed,
        "discounted": sum(1 for c in changed if c["sale"]),
        "restored": sum(1 for c in changed if not c["sale"]),
        "discount_pct": DISCOUNT_PCT,
        "high_stock": HIGH_STOCK,
        "low_stock": LOW_STOCK,
    }
