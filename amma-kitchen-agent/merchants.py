"""The platform, and the kitchens on it.

WHAT CHANGED AND WHY
--------------------
This project was built for one kitchen. Everything read "the shop"
because there was only ever one, and that was the right shape while the
question was "can an AI agent transact with a merchant at all".

The question now is "can it transact with a MARKETPLACE of them", which
is a different thing: a customer picks a kitchen, and from that moment
every rule, every menu, every limit and every audit row belongs to that
kitchen and to no other. A merchant signing in must see her own orders
and nobody else's.

WHERE THE SEAM IS
-----------------
negotiation.py already took `mandate` and `menu` as arguments and only
used the module-level ones as defaults -- it has never known which shop
it was deciding for. That is the whole reason this is affordable: the
decision core needed no change at all, and still has none. What changed
is only WHICH mandate and WHICH menu get handed to it.

THE ONE RULE
------------
A merchant id is never taken on trust from a buyer for anything except
choosing whose menu to read. It selects a tenant; it grants nothing. The
merchant console proves who it is with its own signed session, exactly
as before, and every write is scoped to the merchant that session names.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
STORE = Path(os.environ.get("MERCHANTS_DIR", HERE / "merchants"))


# ───────────────────────────────────────────────────── the platform

class Platform:
    """Who the customer and the merchant both think they are dealing with.

    A marketplace has a name of its own, and it is the name on the text
    message -- a customer who gets "Amma's Kitchen: your agent wants to
    order..." from a number they have never seen has no idea who is
    writing. It says the platform first and the kitchen second, because
    the platform is the relationship and the kitchen is the order.
    """

    # "Bhojanalaya" is the eatery; the AI is the half we added, which is
    # why it is the half that is capitalised and, in the consoles, lit.
    # This is the plain-text form -- an SMS has no stylesheet.
    name = "BhojnalAI"
    tagline = "the agent-ready kitchen network"
    # Said on every outbound message: "BhojnalAI - Amma's Kitchen: ..."
    blurb = ("One place for AI shopping agents to find real kitchens, "
             "agree an order inside limits both sides set, and pay for it.")


def message_prefix(shop_name: str | None = None) -> str:
    """How a message signs itself.

    Platform first so the recipient knows who is writing, kitchen second
    so they know what it is about. A message with no kitchen -- a
    platform-level notice -- just says the platform.
    """
    return f"{Platform.name} · {shop_name}" if shop_name else Platform.name


# ──────────────────────────────────────────────────── the register

# The first entry is the default, and it deliberately points at the
# ORIGINAL merchant_config.json. That file holds a real configured shop
# with real history behind it, so multi-tenancy is additive here rather
# than a migration: nothing that already worked reads a different file
# than it did before.
_SEED = [
    {
        "id": "ammas-kitchen",
        "name": "Amma's Kitchen",
        "cuisine": "South Indian home cooking",
        "blurb": "Thalis, dosas and filter coffee, cooked to order in a home kitchen.",
        "area": "Sardar Nagar West",
        "config": "merchant_config.json",     # the original, untouched
        "accent": "#8A5CFF",
    },
    {
        "id": "bombay-tiffin",
        "name": "Bombay Tiffin Room",
        "cuisine": "Maharashtrian tiffin",
        "blurb": "Vada pav, misal and poha, the way a Dadar lunch counter makes it.",
        "area": "Ghatkopar East",
        "config": "merchants/bombay-tiffin.json",
        "accent": "#FFB020",
    },
    {
        "id": "lahori-grill",
        "name": "Lahori Grill House",
        "cuisine": "North Indian & Mughlai",
        "blurb": "Charcoal kebabs, biryani and warm naan from a two-brother kitchen.",
        "area": "Navrangpura",
        "config": "merchants/lahori-grill.json",
        "accent": "#3DE8A0",
    },
]

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")


def all() -> list[dict]:
    """Every kitchen on the platform, in listing order."""
    return [dict(m) for m in _SEED]


def ids() -> list[str]:
    return [m["id"] for m in _SEED]


def default_id() -> str:
    """The kitchen used when nobody said which.

    Every existing caller resolves here, which is what lets the whole
    single-tenant codebase keep working untouched while the multi-tenant
    paths are threaded through one at a time.
    """
    return _SEED[0]["id"]


def get(merchant_id: str | None) -> dict:
    """Look up a kitchen, falling back to the default.

    Falls back rather than raising because this is reached from buyer
    traffic, and an unknown id is a bad request rather than an outage --
    the caller that cares (the merchant console) checks membership
    itself with `exists`.
    """
    if merchant_id:
        for m in _SEED:
            if m["id"] == merchant_id:
                return dict(m)
    return dict(_SEED[0])


def exists(merchant_id: str | None) -> bool:
    return bool(merchant_id) and merchant_id in ids()


def valid_id(merchant_id: str) -> bool:
    return bool(merchant_id) and bool(_ID.match(merchant_id))


def config_path(merchant_id: str | None) -> Path:
    """Where this kitchen's shop file lives.

    Resolved per call rather than cached in a module global, because
    FastAPI serves sync endpoints from a threadpool: two requests for
    two different kitchens can be in this function at the same moment,
    and a global "current merchant" would hand one of them the other's
    menu.
    """
    entry = get(merchant_id)
    path = HERE / entry["config"]
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def name_of(merchant_id: str | None) -> str:
    return get(merchant_id)["name"]
