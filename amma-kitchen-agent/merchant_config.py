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
                "stock": item.stock,
                "agent_orderable": item.category in mandate.allowed_categories,
            }
            for item in current_menu().values()
        ],
    }


# ---------------------------------------------------------------- writes

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
        menu[item_id] = {
            "name": item_id,
            "category": category,
            "price_inr": price,
            "stock": stock,
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
