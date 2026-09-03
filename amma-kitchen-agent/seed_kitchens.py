"""Give every kitchen on the platform a business worth showing.

seed_demo.py builds Amma's history and is written around her menu. This
does the same job for the rest of the platform and is written around no
menu at all -- it reads each kitchen's own dishes, categories and limits
and builds a plausible month from them.

That constraint is deliberate and it is also a test of the marketplace:
a seeder that has to be told a shop's dishes is a seeder that knows
something it should be reading, and the moment it knows, so does
everything else.

WHAT IT WILL NOT DO
-------------------
It never writes a `pay_` reference. In this project `pay_` means a real
Razorpay capture that opens in the dashboard. Seeded settlements are
`demo_`, which counts as revenue everywhere a capture does and claims
nothing about Razorpay.

    python seed_kitchens.py            # rebuild every kitchen but Amma's
    python seed_kitchens.py --all      # include Amma's too (seed_demo owns it)
    python seed_kitchens.py --days 30
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import audit_log
import merchant_config
import merchants
import routines

DEMO = "demo_"

# Deterministic, so a rehearsal and the real pitch show the same board.
RNG = random.Random(20260831)

# Each kitchen gets its own regulars, so three boards do not show one cast.
CAST = {
    "bombay-tiffin": [
        ("Nikhil's Agent", "TRUSTED"), ("Sneha's Agent", "TRUSTED"),
        ("Omkar's Agent", "TRUSTED"), ("mcp:claude", "STANDARD"),
        ("Deepa's Agent", "STANDARD"), ("Farhan's Agent", "NEW"),
    ],
    "lahori-grill": [
        ("Imran's Agent", "TRUSTED"), ("Harpreet's Agent", "TRUSTED"),
        ("Zoya's Agent", "TRUSTED"), ("mcp:claude", "STANDARD"),
        ("Kabir's Agent", "STANDARD"), ("Ritu's Agent", "NEW"),
    ],
    "ammas-kitchen": [
        ("Jeet's Agent", "TRUSTED"), ("Priya's Agent", "TRUSTED"),
        ("Ananya's Agent", "TRUSTED"), ("mcp:claude", "STANDARD"),
        ("Rahul's Agent", "STANDARD"), ("Sana's Agent", "NEW"),
    ],
}

PROTOCOLS = ["acp", "ap2", "x402", "mcp"]

OFF_MENU = {
    "bombay-tiffin": [("idli sambar", 4), ("2 pizzas", 3), ("cold coffee", 2),
                      ("masala dosa", 2), ("chinese bhel", 1)],
    "lahori-grill": [("chicken 65", 5), ("veg biryani", 3), ("gulab jamun", 2),
                     ("pizza", 2), ("shawarma", 1)],
    "ammas-kitchen": [("2 pizzas", 5), ("chicken 65", 2), ("butter naan", 1)],
}

REASONS = [
    "dinner for two, nothing heavy", "working late, want something quick",
    "the usual weeknight order", "friends over, keeping it simple",
    "lunch for the family", "craving something after work",
    "office lunch", "first time ordering here",
]


# ─────────────────────────────────────────────── reading the shop

class Shop:
    """One kitchen's menu and limits, read rather than assumed."""

    def __init__(self, merchant_id: str):
        self.id = merchant_id
        self.name = merchants.name_of(merchant_id)
        self.mandate = merchant_config.current_mandate(merchant_id)
        self.menu = merchant_config.current_menu(merchant_id)
        self.sellable = sorted(
            (i for i in self.menu.values()
             if i.category in self.mandate.allowed_categories),
            key=lambda i: i.price_inr,
        )
        # Something no agent may order, for the category-refusal beat.
        self.forbidden = next(
            (i for i in self.menu.values()
             if i.category not in self.mandate.allowed_categories), None)

    @property
    def cheapest(self):
        return self.sellable[0]

    @property
    def mains(self):
        """Anything at least twice the cheapest thing -- a main course
        rather than a side, without naming a single dish."""
        big = [i for i in self.sellable if i.price_inr >= self.cheapest.price_inr * 2]
        return big or self.sellable[1:] or self.sellable

    def total(self, cart) -> int:
        return sum(self.menu[i].price_inr * q for i, q in cart)

    def everyday_carts(self) -> list:
        """Baskets under the confirmation threshold, so they clear.

        The cheapest item rides along in most of them on purpose: the
        "bought together before" upsell reads co-occurrence across PAID
        carts, so the history has to contain the pairing it is later
        supposed to have learned.
        """
        out = []
        for main in self.mains:
            for extra in ([], [(self.cheapest.name, 1)], [(self.cheapest.name, 2)]):
                cart = [(main.name, 1), *extra]
                if self.total(cart) < self.mandate.human_confirm_threshold_inr:
                    out.append(cart)
        return out or [[(self.cheapest.name, 1)]]

    def big_carts(self) -> list:
        """Baskets at or above the threshold, so a human is asked."""
        out = []
        for main in self.mains:
            for qty in (2, 3):
                cart = [(main.name, qty)]
                total = self.total(cart)
                if (self.mandate.human_confirm_threshold_inr
                        <= total <= self.mandate.budget_cap_inr):
                    out.append(cart)
        return out


# ──────────────────────────────────────────────── writing the trail

class Seeder:
    def __init__(self, shop: Shop, days: int):
        self.shop = shop
        self.days = days
        self.n = 0
        self.rows = 0

    def ref(self) -> str:
        self.n += 1
        return f"{DEMO}{self.shop.id[:4]}{self.n:04d}{RNG.randrange(16**4):04x}"

    def at(self, day: int, hour: int) -> str:
        base = datetime.now(timezone.utc) - timedelta(days=day)
        return base.replace(hour=hour, minute=RNG.randint(0, 59),
                            second=RNG.randint(0, 59), microsecond=0).isoformat()

    def write(self, buyer_reasoning=None, **kw) -> int:
        """One row. The customer's stated reason is attached afterwards
        because record_event does not take it -- it is its own column and
        its own writer, so that a refusal can carry one too."""
        self.rows += 1
        event_id = audit_log.record_event(merchant_id=self.shop.id, **kw)
        if buyer_reasoning:
            audit_log.attach_buyer_reasoning(event_id, buyer_reasoning,
                                             db_path=audit_log.DEFAULT_DB_PATH)
        return event_id

    def approved(self, agent, cart, ts, source=None, routine_id=None, paid=True):
        return self.write(
            agent_id=agent, protocol=RNG.choice(PROTOCOLS),
            cart=[{"item": i, "qty": q} for i, q in cart],
            decision="APPROVE",
            reason="within budget and below human confirm threshold",
            total_inr=self.shop.total(cart),
            payment_id=self.ref() if paid else None,
            ts=ts, source=source, routine_id=routine_id,
            buyer_reasoning=RNG.choice(REASONS),
        )

    def escalated(self, agent, cart, ts):
        total = self.shop.total(cart)
        return self.write(
            agent_id=agent, protocol=RNG.choice(PROTOCOLS),
            cart=[{"item": i, "qty": q} for i, q in cart],
            decision="ESCALATE",
            reason=(f"total Rs.{total} at/above human confirmation threshold "
                    f"Rs.{self.shop.human_confirm}"),
            total_inr=total, ts=ts, buyer_reasoning=RNG.choice(REASONS),
        )

    def awaiting_her_answer(self, agent, cart, day, hour):
        """A PAID order sitting in her queue, waiting on a yes or no.

        The only kind of pending item worth seeding. A PRE-payment
        escalation lives in its adapter's memory and does not survive a
        restart, so seeding one writes a row no screen will ever show;
        this lifecycle is rebuilt from the trail, so it does.

        It is also the better beat: declining one of these refunds a real
        customer, which is what makes the pay-first defence something a
        judge can watch rather than take on trust.
        """
        ref = self.escalated(agent, cart, self.at(day, hour))
        total = self.shop.total(cart)
        line = [{"item": i, "qty": q} for i, q in cart]
        for minutes, status, why in (
            (1, "AWAITING_PAYMENT", "payment link issued; the customer pays it themselves"),
            (4, "PAID", "captured"),
            (4, "PENDING_MERCHANT_APPROVAL", "paid; awaiting the kitchen's yes or no"),
        ):
            self.write(agent_id=agent, protocol="ap2", cart=line,
                       decision=status, reason=why, total_inr=total,
                       order_ref=ref, ts=self.at(day, hour))
        return ref

    # -- the whole month ------------------------------------------------
    def run(self):
        shop, cast = self.shop, CAST[self.shop.id]
        everyday, big = shop.everyday_carts(), shop.big_carts()
        trusted = [a for a, t in cast if t == "TRUSTED"]
        standard = [a for a, t in cast if t == "STANDARD"]
        newcomers = [a for a, t in cast if t == "NEW"]
        regulars = trusted + standard

        # Ordinary trading, weekends busier.
        for day in range(self.days, -1, -1):
            when = datetime.now(timezone.utc) - timedelta(days=day)
            n = RNG.randint(3, 6) if when.weekday() >= 5 else RNG.randint(2, 5)
            if day == 0:
                n = RNG.randint(3, 5)          # today, so the board is live
            for _ in range(n):
                hour = RNG.choice([9, 12, 13, 13, 19, 20, 20, 21])
                if day == 0:
                    hour = RNG.choice([9, 11, 12, 13, 14])
                agent = RNG.choice(regulars)
                if RNG.random() < 0.06 and big:
                    # A counter-offer: nothing bought, and that is the point.
                    cart = RNG.choice(big)
                    self.write(
                        agent_id=agent, protocol=RNG.choice(PROTOCOLS),
                        cart=[{"item": i, "qty": q} for i, q in cart],
                        decision="COUNTER_OFFER",
                        reason=(f"total Rs.{shop.total(cart) + 200} over budget cap "
                                f"Rs.{shop.mandate.budget_cap_inr}; alternatives inside "
                                "the margin"),
                        total_inr=shop.total(cart) + 200,
                        ts=self.at(day, hour),
                        buyer_reasoning=RNG.choice(REASONS),
                    )
                    continue
                self.approved(agent, RNG.choice(everyday), self.at(day, hour))

        # A newcomer who arrived this week: one settled order puts them at
        # STANDARD, so NEW / STANDARD / TRUSTED are all legible with a
        # reason behind each.
        for day, paid in ((3, True), (1, True)):
            self.approved(newcomers[0], RNG.choice(everyday), self.at(day, 19),
                          paid=paid)

        # Escalations a human actually answered. The orchestrator writes
        # the answer as its OWN row rather than editing the escalation, so
        # the trail shows both what the machine decided and that a person
        # separately chose otherwise.
        if big:
            for i, day in enumerate((24, 18, 11, 5, 2)):
                agent = regulars[i % len(regulars)]
                cart = big[i % len(big)]
                self.escalated(agent, cart, self.at(day, 20))
                if i % 3 == 2:
                    self.write(
                        agent_id=agent, protocol="acp",
                        cart=[{"item": x, "qty": q} for x, q in cart],
                        decision="REJECTED", reason="human rejected ESCALATE",
                        total_inr=shop.total(cart), ts=self.at(day, 20),
                    )
                else:
                    self.write(
                        agent_id=agent, protocol="acp",
                        cart=[{"item": x, "qty": q} for x, q in cart],
                        decision="APPROVE", reason="human override of ESCALATE",
                        total_inr=shop.total(cart), payment_id=self.ref(),
                        ts=self.at(day, 20),
                    )

        # Two waiting on her right now, so the queue is not empty.
        if big:
            for i, hour in enumerate((15, 16)):
                self.escalated(regulars[i % len(regulars)],
                               big[i % len(big)], self.at(0, hour))

        # The category refusal, on this kitchen's own forbidden dish.
        if shop.forbidden:
            for day in (21, 8):
                self.write(
                    agent_id=newcomers[0], protocol="acp",
                    cart=[{"item": shop.forbidden.name, "qty": 1}],
                    decision="ESCALATE",
                    reason=f"category not allowed: {shop.forbidden.category}",
                    total_inr=shop.forbidden.price_inr, ts=self.at(day, 16),
                    buyer_reasoning="ordering for a party",
                )

        # The flood gate, refusing rather than queueing.
        for minute in (14, 15, 16):
            self.write(
                agent_id=newcomers[0], protocol="x402",
                cart=[{"item": shop.cheapest.name, "qty": 1}],
                decision="VELOCITY_REFUSED",
                reason=("agent rate limit reached: 3 orders in the last hour, "
                        "limit 3"),
                total_inr=shop.cheapest.price_inr, ts=self.at(12, 21),
            )

        # Standing orders: revenue nobody placed by hand. Spread over the
        # window, because the KPI is a share of the whole ledger and a
        # weekly routine would read 0% on six days out of seven.
        self.seed_routines(everyday, trusted)

        # Two orders sitting on her board right now, so the console she
        # opens on camera has something to decide. Written last so they
        # are the most recent rows and sort to the top of her queue.
        for offset, (agent, cart) in enumerate(
                zip(trusted[:2] or regulars[:2], big[:2])):
            self.awaiting_her_answer(agent, cart, 0, 11 + offset * 2)

        # What people asked for that this kitchen does not sell.
        for want, times in OFF_MENU[self.shop.id]:
            for _ in range(times):
                audit_log.record_unmatched_demand(
                    agent_id=RNG.choice(regulars), protocol="mcp", requested=want,
                    db_path=audit_log.DEFAULT_DB_PATH, merchant_id=self.shop.id)
                self.rows += 1

        # A disputed order, so the evidence pack has something to open.
        recent = audit_log.get_all_events(limit=40, merchant_id=self.shop.id)
        settled = [r for r in recent if r["payment_id"] and r["decision"] == "APPROVE"]
        if settled:
            audit_log.mark_disputed(settled[len(settled) // 2]["id"],
                                    db_path=audit_log.DEFAULT_DB_PATH)

        return self.rows

    def seed_routines(self, everyday, trusted):
        """Standing orders, in the trail and in routines.json.

        Both, deliberately. Seeding one without the other gives a buyer
        console showing no standing orders beside a merchant board
        reporting a fifth of revenue from them.
        """
        shop = self.shop
        plans = [
            (trusted[0], "mon,wed,fri", "08:30", 8),
            (trusted[1 % len(trusted)], "tue,thu", "20:00", 13),
        ]
        for agent, days, time_str, hour in plans:
            cart = RNG.choice(everyday)
            total = shop.total(cart)
            routine_id = f"rt-{shop.id[:4]}-{agent.split(chr(39))[0].lower()}"
            wanted = set(days.split(","))
            for day in range(self.days, -1, -1):
                when = datetime.now(timezone.utc) - timedelta(days=day)
                if ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][when.weekday()] \
                        not in wanted:
                    continue
                if RNG.random() < 0.12:
                    continue                    # the odd skipped morning
                self.approved(agent, cart, self.at(day, hour),
                              source="routine", routine_id=routine_id)
            try:
                routines.create(
                    agent_id=agent, phone="",
                    items=[{"item_id": i, "qty": q} for i, q in cart],
                    days=days.split(","), at_time=time_str,
                    routine_cap_inr=total + 60,
                    merchant_id=shop.id,
                )
            except Exception as exc:
                # Loud rather than swallowed. This was silent, and the
                # silence hid create() validating a Bombay cart against
                # Amma's menu and refusing every dish as "not on the
                # menu" -- so two kitchens had standing-order revenue in
                # the trail and no standing orders on their page.
                print(f"    ! routine for {agent} not created: {exc}")


def wipe(merchant_id: str) -> int:
    with sqlite3.connect(audit_log.DEFAULT_DB_PATH) as conn:
        return conn.execute("DELETE FROM audit_events WHERE merchant_id = ?",
                            (merchant_id,)).rowcount


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--all", action="store_true",
                    help="include the default kitchen (seed_demo.py owns it)")
    args = ap.parse_args()

    audit_log.init_db(audit_log.DEFAULT_DB_PATH)
    targets = [m["id"] for m in merchants.all()
               if args.all or m["id"] != merchants.default_id()]

    for merchant_id in targets:
        removed = wipe(merchant_id)
        shop = Shop(merchant_id)
        shop.human_confirm = shop.mandate.human_confirm_threshold_inr
        written = Seeder(shop, args.days).run()
        print(f"  {shop.name:<22} -{removed:<4} +{written} rows")

    print()
    today = datetime.now(timezone.utc).date().isoformat()
    for m in merchants.all():
        rows = audit_log.get_all_events(limit=99999, merchant_id=m["id"])
        refunded = {r["order_ref"] for r in rows
                    if r["decision"] == "REFUNDED" and r["order_ref"]}
        banked = [r for r in rows if r["payment_id"]
                  and not str(r["payment_id"]).startswith("sim_")
                  and r["id"] not in refunded]
        total = sum(r["total_inr"] for r in banked)
        standing = sum(r["total_inr"] for r in banked if r["source"] == "routine")
        print(f"  {m['name']:<22} {len(rows):>4} rows  Rs.{total:>6} settled  "
              f"AOV Rs.{total // max(1, len(banked)):<4} "
              f"today Rs.{sum(r['total_inr'] for r in banked if r['ts'].startswith(today)):<5} "
              f"standing {round(standing / max(1, total) * 100):>2}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
