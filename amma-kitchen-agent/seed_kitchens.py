"""Give the other kitchens a fortnight of trading, so their boards work.

seed_demo.py builds Amma's history and is tuned to her menu. This does
the same job for the rest of the platform, reading each kitchen's own
menu and limits rather than assuming anybody's -- which is also a check
on the marketplace itself: a seeder that has to be told a shop's dishes
is a seeder that knows something it should be reading.

Amma's Kitchen is skipped. Her history is real and seed_demo owns it.

    python seed_kitchens.py           # add history for the other kitchens
    python seed_kitchens.py --wipe    # remove theirs first, then rebuild
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import audit_log
import merchant_config
import merchants

DEMO = "demo_"
RNG = random.Random(20260831)

# Each kitchen's regulars, so the boards do not all show the same cast.
CUSTOMERS = {
    "bombay-tiffin": ["Nikhil's Agent", "Sneha's Agent", "Omkar's Agent",
                      "mcp:claude", "Deepa's Agent"],
    "lahori-grill": ["Imran's Agent", "Harpreet's Agent", "Zoya's Agent",
                     "mcp:claude", "Kabir's Agent"],
}
PROTOCOLS = ["acp", "ap2", "x402", "mcp"]

OFF_MENU = {
    "bombay-tiffin": ["idli sambar", "2 pizzas", "cold coffee", "dosa"],
    "lahori-grill": ["chicken 65", "veg biryani", "gulab jamun", "pizza"],
}


def carts_for(merchant_id: str) -> list[list[tuple[str, int]]]:
    """Plausible baskets built from whatever this kitchen actually sells.

    Cheapest item rides along often on purpose: the "bought together
    before" upsell reads co-occurrence across PAID carts, so the history
    has to contain the pairing it is later supposed to have learned.
    """
    menu = merchant_config.current_menu(merchant_id)
    mandate = merchant_config.current_mandate(merchant_id)
    sellable = [i for i in menu.values() if i.category in mandate.allowed_categories]
    sellable.sort(key=lambda i: i.price_inr)
    cheapest = sellable[0]
    mains = [i for i in sellable if i.price_inr >= cheapest.price_inr * 2] or sellable

    out = []
    for main in mains:
        out.append([(main.name, 1)])
        out.append([(main.name, 1), (cheapest.name, 1)])
        out.append([(main.name, 1), (cheapest.name, 2)])
    for main in mains[:3]:
        out.append([(main.name, 2)])
    return out


def total_of(merchant_id: str, cart) -> int:
    menu = merchant_config.current_menu(merchant_id)
    return sum(menu[i].price_inr * q for i, q in cart)


def at(day: int, hour: int) -> str:
    base = datetime.now(timezone.utc) - timedelta(days=day)
    return base.replace(hour=hour, minute=RNG.randint(0, 59),
                        second=RNG.randint(0, 59), microsecond=0).isoformat()


def seed(merchant_id: str, days: int = 14) -> int:
    name = merchants.name_of(merchant_id)
    mandate = merchant_config.current_mandate(merchant_id)
    carts = carts_for(merchant_id)
    people = CUSTOMERS[merchant_id]
    written = 0
    counter = 0

    for day in range(days, 0, -1):
        for _ in range(RNG.randint(2, 5)):
            counter += 1
            agent = RNG.choice(people)
            cart = RNG.choice(carts)
            total = total_of(merchant_id, cart)
            over = total >= mandate.human_confirm_threshold_inr
            payload = [{"item": i, "qty": q} for i, q in cart]
            reference = None if over else f"{DEMO}{merchant_id[:3]}{counter:05d}"

            audit_log.record_event(
                agent_id=agent,
                protocol=RNG.choice(PROTOCOLS),
                cart=payload,
                decision="ESCALATE" if over else "APPROVE",
                reason=(f"total Rs.{total} at/above human confirmation threshold "
                        f"Rs.{mandate.human_confirm_threshold_inr}") if over
                       else "within budget and below human confirm threshold",
                total_inr=total,
                payment_id=reference,
                ts=at(day, RNG.choice([9, 12, 13, 19, 20, 21])),
                merchant_id=merchant_id,
            )
            written += 1

    # What people asked this kitchen for that it does not sell.
    for i, want in enumerate(OFF_MENU[merchant_id]):
        audit_log.record_unmatched_demand(
            agent_id=RNG.choice(people), protocol="mcp", requested=want,
            db_path=audit_log.DEFAULT_DB_PATH, merchant_id=merchant_id,
        )
        written += 1

    print(f"  {name:<22} {written} rows over {days} days")
    return written


def wipe(merchant_id: str) -> int:
    with sqlite3.connect(audit_log.DEFAULT_DB_PATH) as conn:
        cur = conn.execute("DELETE FROM audit_events WHERE merchant_id = ?",
                           (merchant_id,))
        return cur.rowcount


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--wipe", action="store_true")
    args = ap.parse_args()

    audit_log.init_db(audit_log.DEFAULT_DB_PATH)
    targets = [m["id"] for m in merchants.all() if m["id"] != merchants.default_id()]

    if args.wipe:
        for merchant_id in targets:
            print(f"  removed {wipe(merchant_id)} rows from {merchants.name_of(merchant_id)}")

    for merchant_id in targets:
        seed(merchant_id)

    print()
    for m in merchants.all():
        rows = audit_log.get_all_events(limit=99999, merchant_id=m["id"])
        settled = [r for r in rows if r["payment_id"]
                   and not str(r["payment_id"]).startswith("sim_")]
        print(f"  {m['name']:<22} {len(rows):>4} orders, "
              f"Rs.{sum(r['total_inr'] for r in settled):>6} settled")
    return 0


if __name__ == "__main__":
    sys.exit(main())
