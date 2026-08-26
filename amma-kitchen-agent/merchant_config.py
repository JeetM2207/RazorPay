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

_state: dict | None = None


# ------------------------------------------------------------- internals

def _fresh_state() -> dict:
    return {
        "profile": dict(_DEFAULT_PROFILE),
        "mandate": asdict(MANDATE),
        "menu": {name: asdict(item) for name, item in MENU.items()},
    }


def _load() -> dict:
    global _state
    if _state is not None:
        return _state

    _state = _fresh_state()
    if CONFIG_PATH.exists():
        try:
            stored = json.loads(CONFIG_PATH.read_text())
            _state["profile"].update(stored.get("profile", {}))
            _state["mandate"].update(stored.get("mandate", {}))
            if stored.get("menu"):
                _state["menu"] = stored["menu"]
        except (json.JSONDecodeError, OSError):
            # A corrupt config must not take the shop offline; fall back
            # to defaults rather than refusing to start.
            _state = _fresh_state()
    return _state


def _persist() -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(_load(), indent=2))
    except OSError:
        pass  # an unwritable disk must not break an in-flight order


def reset_to_defaults() -> None:
    """Used by tests, and by a merchant who wants a clean slate."""
    global _state
    _state = _fresh_state()


# ----------------------------------------------------------------- reads

def profile() -> dict:
    return dict(_load()["profile"])


def is_configured() -> bool:
    return bool(_load()["profile"]["configured"])


def current_mandate() -> Mandate:
    """The limits the negotiation core should decide against right now."""
    data = _load()["mandate"]
    return Mandate(
        budget_cap_inr=int(data["budget_cap_inr"]),
        allowed_categories=tuple(data["allowed_categories"]),
        flexible_margin_pct=float(data["flexible_margin_pct"]),
        human_confirm_threshold_inr=int(data["human_confirm_threshold_inr"]),
    )


def current_menu() -> dict[str, MenuItem]:
    return {
        name: MenuItem(
            name=item["name"],
            category=item["category"],
            price_inr=int(item["price_inr"]),
            stock=int(item["stock"]),
        )
        for name, item in _load()["menu"].items()
    }


def as_dict() -> dict:
    """Everything the setup page needs to render itself."""
    mandate = current_mandate()
    raw = _load()["menu"]
    return {
        "profile": profile(),
        "mandate": {
            "budget_cap_inr": mandate.budget_cap_inr,
            "human_confirm_threshold_inr": mandate.human_confirm_threshold_inr,
            "allowed_categories": list(mandate.allowed_categories),
            "flexible_margin_pct": mandate.flexible_margin_pct,
        },
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
            for item in current_menu().values()
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


def resolve_item(text: str) -> str | None:
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

    menu = current_menu()
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


def _slug(text: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in text.strip().lower())
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("_")


def save(profile_in: dict, mandate_in: dict, menu_in: list[dict]) -> dict:
    """Replace the shop's configuration wholesale.

    Validation is strict and returns a plain message, because a merchant
    mis-typing a cap should get told, not silently get a shop that
    behaves differently from what the screen showed.
    """
    state = _load()

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
    state["menu"] = menu

    _persist()
    return as_dict()


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


def optimize_prices() -> dict:
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
    state = _load()
    changed = []

    rows = []
    for item in as_dict()["menu"]:
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

    save(profile_in=state["profile"], mandate_in=state["mandate"], menu_in=rows)
    return {
        "changed": changed,
        "discounted": sum(1 for c in changed if c["sale"]),
        "restored": sum(1 for c in changed if not c["sale"]),
        "discount_pct": DISCOUNT_PCT,
        "high_stock": HIGH_STOCK,
        "low_stock": LOW_STOCK,
    }
