"""Build a demo-ready audit trail: thirty days of a kitchen that works.

WHY THIS EXISTS
---------------
The trail this project accumulated while being built is a record of it
being built: 596 rows from one debugging agent, ten load-test agents
called agent-0 through agent-9, fourteen hundred escalations, sixty
velocity refusals. Every panel read correctly off that and every panel
looked broken -- Rs.0 revenue, 171 interventions, 0% from standing
orders. None of those numbers were wrong. They were just answers about
development, not about a business.

This replaces that with thirty days of plausible trading, so the boards
show what they are for.

WHAT IT WILL NOT DO
-------------------
It never writes a `pay_` reference. In this project `pay_` means a real
Razorpay capture that can be opened in the dashboard, and the whole
audit trail is worth nothing the moment that stops being true. Seeded
settlements are `demo_`, which counts as revenue everywhere a capture
does but claims nothing about Razorpay.

The real `pay_` captures already in the trail are PRESERVED, along with
every lifecycle row hanging off them. Those are genuine test-mode
payments; they are the answer when somebody asks whether any of this is
real, and they are the one thing here that must not be regenerated.

USAGE
-----
    python seed_demo.py                 # back up, then rebuild
    python seed_demo.py --dry-run       # report only, touch nothing
    python seed_demo.py --restore LAST  # put the previous database back
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import audit_log
import merchant_config
import merchants

DB = Path(audit_log.DEFAULT_DB_PATH)
BACKUP_DIR = DB.parent / "backups"

# One reference prefix for seeded settlements. Not `pay_`, deliberately.
DEMO = "demo_"

# Deterministic: the same command twice produces the same board, so a
# rehearsal and the real pitch look identical.
RNG = random.Random(20260830)


# ────────────────────────────────────────────────────────── the cast

class Agent:
    def __init__(self, name, protocol, tier, weight, note=""):
        self.name = name
        self.protocol = protocol
        self.tier = tier            # what its history should ADD UP to
        self.weight = weight        # how often it orders
        self.note = note


AGENTS = [
    # The account the demo is driven from. Its history is why it reads
    # TRUSTED on camera rather than NEW.
    Agent("Jeet's Agent",    "ap2",  "TRUSTED",  30, "the demo account"),
    Agent("Priya's Agent",   "acp",  "TRUSTED",  22),
    Agent("Ananya's Agent",  "ap2",  "TRUSTED",  18, "runs a standing order"),
    Agent("Rahul's Agent",   "x402", "STANDARD", 14),
    # The external Claude connector. Namespaced `mcp:` by the adapter, so
    # it cannot present as an agent from another protocol.
    Agent("mcp:claude",      "mcp",  "STANDARD", 12, "somebody else's model"),
    # Pinned at NEW by a single disallowed-category attempt. This is the
    # trust-reset story, and it needs an agent it actually happened to.
    Agent("Vikram's Agent",  "acp",  "NEW",       3, "tried the catering tray"),
    # Ordered too fast once and was refused outright rather than queued.
    Agent("Meera's Agent",   "x402", "NEW",       3, "hit the flood gate"),
]


MENU = merchant_config.current_menu()
MANDATE = merchant_config.current_mandate()


def price(item_id: str) -> int:
    return MENU[item_id].price_inr


# Carts people actually order, weighted. filter_coffee and gulab_jamun
# ride along often on purpose: the "bought together before" upsell reads
# co-occurrence across PAID carts, so the history has to contain the
# pairings it is later supposed to have learned.
CARTS = [
    ([("veg_thali", 1)],                                            10),
    ([("veg_thali", 1), ("filter_coffee", 1)],                      12),
    ([("masala_dosa", 1), ("filter_coffee", 1)],                    11),
    ([("chicken_biryani", 1)],                                       8),
    ([("chicken_biryani", 1), ("filter_coffee", 1)],                 9),
    ([("paneer_bhurji", 1), ("tandoori_roti", 3)],                   8),
    ([("paneer_bhurji", 1), ("tandoori_roti", 2), ("filter_coffee", 1)], 7),
    ([("veg_thali", 1), ("gulab_jamun", 1)],                         7),
    ([("masala_dosa", 2)],                                           5),
    ([("veg_thali", 2), ("filter_coffee", 2)],                       4),
    ([("chicken_biryani", 1), ("gulab_jamun", 1)],                   5),
    ([("tandoori_roti", 4), ("paneer_bhurji", 1), ("gulab_jamun", 1)], 4),
]

# Over the confirmation threshold, so they escalate and a human answers.
BIG_CARTS = [
    [("chicken_biryani", 2)],
    [("veg_thali", 2), ("chicken_biryani", 1)],
    [("paneer_bhurji", 2), ("tandoori_roti", 4), ("filter_coffee", 2)],
    [("chicken_biryani", 1), ("veg_thali", 1), ("gulab_jamun", 2)],
]

# What people ask for that she does not sell. Verbatim, because the
# demand panel shows the customer's own words.
OFF_MENU = [
    "2 pizzas", "chicken 65", "butter naan", "birthday cake",
    "veg fried rice", "cold coffee", "pizza margherita", "samosa",
]

REASONS = {
    "APPROVE": "within budget and below human confirm threshold",
    "ESCALATE": "total Rs.{total} at/above human confirmation threshold Rs.{th}",
    "CATEGORY": "category not allowed: bulk_catering",
    "COUNTER": "total Rs.{total} over budget cap Rs.{cap}; alternatives inside the margin",
}

BUYER_REASONS = [
    "dinner for two, nothing heavy",
    "working late, want something quick",
    "the usual weeknight order",
    "friend visiting, wants to try South Indian",
    "lunch for the family",
    "craving something sweet after dinner",
    "office lunch, keeping it simple",
]


def cart_json(items):
    return [{"item": i, "qty": q} for i, q in items]


def total_of(items):
    return sum(price(i) * q for i, q in items)


def snapshot(tier: str, orders_in_window: int, spend_in_window: int) -> dict:
    mult = {"NEW": 0.5, "STANDARD": 1.0, "TRUSTED": 1.5}[tier]
    return {
        "recorded_at": None,          # filled per row
        "merchant": {
            "budget_cap_inr": MANDATE.budget_cap_inr,
            "human_confirm_threshold_inr": MANDATE.human_confirm_threshold_inr,
            "allowed_categories": list(MANDATE.allowed_categories),
            "flexible_margin_pct": {"NEW": 0.05, "STANDARD": 0.10,
                                    "TRUSTED": 0.15}[tier],
            "trust_tier_applied": tier,
        },
        "buyer": None,
        "velocity": {
            "max_orders_per_hour": 6,
            "max_spend_per_day_inr": 2000,
            "tier_multiplier": mult,
            "effective_orders_per_hour": int(6 * mult),
            "effective_spend_per_day_inr": int(2000 * mult),
            "orders_in_window_at_decision": orders_in_window,
            "spend_in_window_at_decision_inr": spend_in_window,
        },
    }


# ─────────────────────────────────────────────────── building the trail

class Trail:
    """Rows accumulated in time order, then written in one pass.

    Lifecycle rows carry order_ref back to the decision that started
    them, and that is a real row id -- so rows are inserted oldest first
    and each child looks its parent's id up after the fact.
    """

    def __init__(self):
        self.rows = []

    def add(self, ts, agent, protocol, cart, decision, reason, total,
            payment_id=None, parent=None, limits=None, source=None,
            routine_id=None, buyer_reasoning=None, delivery=None):
        row = {
            "ts": ts, "agent_id": agent, "protocol": protocol,
            "cart_json": json.dumps(cart_json(cart)), "decision": decision,
            "reason": reason, "total_inr": total, "payment_id": payment_id,
            "parent": parent, "limits_snapshot": limits, "source": source,
            "routine_id": routine_id, "buyer_reasoning": buyer_reasoning,
            "delivery": delivery, "_key": len(self.rows),
        }
        self.rows.append(row)
        return row

    def write(self, conn):
        self.rows.sort(key=lambda r: r["ts"])
        ids = {}
        for row in self.rows:
            limits = row["limits_snapshot"]
            if limits:
                limits = dict(limits)
                limits["recorded_at"] = row["ts"]
            parent = row["parent"]
            order_ref = ids.get(parent["_key"]) if parent else None
            d = row["delivery"] or (None, None, None)
            cur = conn.execute(
                "INSERT INTO audit_events (ts, agent_id, protocol, cart_json, "
                " decision, reason, total_inr, payment_id, order_ref, "
                " limits_snapshot, source, routine_id, buyer_reasoning, "
                " delivery_name, delivery_phone, delivery_address, merchant_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["ts"], row["agent_id"], row["protocol"], row["cart_json"],
                 row["decision"], row["reason"], row["total_inr"],
                 row["payment_id"], order_ref,
                 json.dumps(limits) if limits else None,
                 row["source"], row["routine_id"], row["buyer_reasoning"],
                 d[0], d[1], d[2], merchants.default_id()),
            )
            ids[row["_key"]] = cur.lastrowid
        return len(self.rows)


def pick_agent():
    return RNG.choices(AGENTS, weights=[a.weight for a in AGENTS])[0]


def pick_cart():
    return RNG.choices([c for c, _ in CARTS], weights=[w for _, w in CARTS])[0]


def at(day_offset: int, hour: int, minute: int = 0) -> str:
    now = datetime.now(timezone.utc)
    base = now - timedelta(days=day_offset)
    return base.replace(hour=hour, minute=minute,
                        second=RNG.randint(0, 59), microsecond=0).isoformat()


DELIVERY = ("Jeet Manseta", "+918306610707", "Sharad Appartment")


def build() -> Trail:
    t = Trail()
    counter = [0]

    def ref():
        counter[0] += 1
        return f"{DEMO}{counter[0]:04d}{RNG.randrange(16**6):06x}"

    # ── thirty days of ordinary trading ─────────────────────────────
    for day in range(30, 0, -1):
        weekend = (datetime.now(timezone.utc) - timedelta(days=day)).weekday() >= 5
        n = RNG.randint(3, 6) if weekend else RNG.randint(2, 5)
        for _ in range(n):
            agent = pick_agent()
            if agent.name in ("Vikram's Agent", "Meera's Agent"):
                continue                     # these two have their own scripts
            cart = pick_cart()
            total = total_of(cart)
            hour = RNG.choice([8, 9, 12, 13, 13, 19, 20, 20, 21])
            ts = at(day, hour, RNG.randint(0, 59))
            lim = snapshot(agent.tier, RNG.randint(0, 2), RNG.randint(0, 600))

            roll = RNG.random()
            if roll < 0.07:
                # A counter-offer: nothing was bought, and that is the point.
                t.add(ts, agent.name, agent.protocol, cart, "COUNTER_OFFER",
                      REASONS["COUNTER"].format(total=total + 180,
                                                cap=MANDATE.budget_cap_inr),
                      total + 180, limits=lim,
                      buyer_reasoning=RNG.choice(BUYER_REASONS))
                continue

            t.add(ts, agent.name, agent.protocol, cart, "APPROVE",
                  REASONS["APPROVE"], total, payment_id=ref(), limits=lim,
                  buyer_reasoning=RNG.choice(BUYER_REASONS),
                  delivery=DELIVERY if agent.name == "Jeet's Agent" else None)

    # ── standing orders: revenue nobody placed by hand ──────────────
    # Ananya's weekday breakfast. Gives "From standing orders" something
    # true to report, and it has to span days for the share to mean
    # anything.
    for day in range(28, 0, -1):
        d = datetime.now(timezone.utc) - timedelta(days=day)
        if d.weekday() >= 5 or RNG.random() < 0.15:
            continue
        cart = [("masala_dosa", 1), ("filter_coffee", 1)]
        t.add(at(day, 8, 5), "Ananya's Agent", "ap2", cart, "APPROVE",
              REASONS["APPROVE"], total_of(cart), payment_id=ref(),
              limits=snapshot("TRUSTED", 0, 0),
              source="routine", routine_id="rt-ananya-breakfast")

    # Jeet's Tuesday dinner, so the demo account owns one too.
    for day in range(29, 0, -1):
        d = datetime.now(timezone.utc) - timedelta(days=day)
        if d.weekday() != 1:
            continue
        cart = [("paneer_bhurji", 1), ("tandoori_roti", 3)]
        t.add(at(day, 20, 0), "Jeet's Agent", "ap2", cart, "APPROVE",
              REASONS["APPROVE"], total_of(cart), payment_id=ref(),
              limits=snapshot("TRUSTED", 0, 0),
              source="routine", routine_id="rt-jeet-dinner",
              delivery=DELIVERY)

    # Priya's Friday family dinner -- a bigger, weekly one, so the share
    # is not made up entirely of Rs.110 breakfasts.
    for day in range(29, 0, -1):
        d = datetime.now(timezone.utc) - timedelta(days=day)
        if d.weekday() != 4:
            continue
        cart = [("veg_thali", 2), ("gulab_jamun", 2)]
        t.add(at(day, 20, 15), "Priya's Agent", "acp", cart, "APPROVE",
              REASONS["APPROVE"], total_of(cart), payment_id=ref(),
              limits=snapshot("TRUSTED", 0, 0),
              source="routine", routine_id="rt-priya-friday")

    # Rahul's weekday lunch.
    for day in range(27, 0, -1):
        d = datetime.now(timezone.utc) - timedelta(days=day)
        if d.weekday() >= 5 or RNG.random() < 0.25:
            continue
        cart = [("veg_thali", 1), ("filter_coffee", 1)]
        t.add(at(day, 13, 0), "Rahul's Agent", "x402", cart, "APPROVE",
              REASONS["APPROVE"], total_of(cart), payment_id=ref(),
              limits=snapshot("STANDARD", 0, 0),
              source="routine", routine_id="rt-rahul-lunch")

    # ── escalations a human actually answered ───────────────────────
    # The orchestrator writes the human's answer as its OWN row rather
    # than editing the escalation, so the trail shows both what the
    # machine decided and that a person separately chose otherwise.
    answered = [(26, "Priya's Agent", "acp", 0, True),
                (21, "Jeet's Agent", "ap2", 1, True),
                (17, "Ananya's Agent", "ap2", 2, False),
                (11, "Priya's Agent", "acp", 3, True),
                (6, "Rahul's Agent", "x402", 1, True),
                (3, "Jeet's Agent", "ap2", 2, False)]
    for day, name, proto, idx, approved in answered:
        agent = next(a for a in AGENTS if a.name == name)
        cart = BIG_CARTS[idx]
        total = total_of(cart)
        lim = snapshot(agent.tier, 1, 300)
        esc = t.add(at(day, 19, 30), name, proto, cart, "ESCALATE",
                    REASONS["ESCALATE"].format(
                        total=total, th=MANDATE.human_confirm_threshold_inr),
                    total, limits=lim,
                    buyer_reasoning=RNG.choice(BUYER_REASONS),
                    delivery=DELIVERY if name == "Jeet's Agent" else None)
        if approved:
            t.add(at(day, 19, 44), name, proto, cart, "APPROVE",
                  "human override of ESCALATE", total,
                  payment_id=ref(), limits=lim)
        else:
            t.add(at(day, 19, 52), name, proto, cart, "REJECTED",
                  "human rejected ESCALATE", total, limits=lim, parent=esc)

    # ── the pay-first lifecycle, end to end ─────────────────────────
    # Paid up front, confirmed afterwards over WhatsApp. Three outcomes,
    # because the interesting one is the refund.
    def lifecycle(day, name, proto, cart, outcome):
        agent = next(a for a in AGENTS if a.name == name)
        total = total_of(cart)
        lim = snapshot(agent.tier, 1, 250)
        pid = ref()
        dec = t.add(at(day, 20, 5), name, proto, cart, "ESCALATE",
                    REASONS["ESCALATE"].format(
                        total=total, th=MANDATE.human_confirm_threshold_inr),
                    total, limits=lim,
                    buyer_reasoning=RNG.choice(BUYER_REASONS),
                    delivery=DELIVERY)
        add = lambda m, d, r, p=None: t.add(
            at(day, 20, m), name, proto, cart, d, r, total,
            payment_id=p, parent=dec)
        add(6, "AWAITING_PAYMENT", "payment link issued; awaiting the customer")
        add(9, "PAID", "payment captured", pid)
        add(10, "PENDING_MERCHANT_APPROVAL", "paid; awaiting the kitchen's yes or no")
        if outcome == "accepted":
            add(21, "MERCHANT_ACCEPTED", "kitchen accepted the order")
        elif outcome == "rejected":
            add(24, "MERCHANT_REJECTED", "kitchen declined the order")
            add(25, "REFUNDED", f"refunded Rs.{total} to the original payment method")
        else:
            add(55, "MERCHANT_TIMEOUT_REFUNDED",
                "no answer inside 45 minutes; refunding automatically")
            add(56, "REFUNDED", f"refunded Rs.{total} to the original payment method")

    lifecycle(19, "mcp:claude", "mcp", [("chicken_biryani", 2)], "accepted")
    lifecycle(13, "Jeet's Agent", "ap2",
              [("veg_thali", 2), ("chicken_biryani", 1)], "rejected")
    lifecycle(8, "mcp:claude", "mcp",
              [("paneer_bhurji", 2), ("tandoori_roti", 4)], "accepted")
    lifecycle(4, "Priya's Agent", "acp",
              [("chicken_biryani", 1), ("veg_thali", 1), ("gulab_jamun", 2)],
              "timeout")

    # ── the refusals, each demonstrating a different gate ───────────
    # A category she does not sell to agents. This pins Vikram at NEW no
    # matter how much he later orders, which is the trust-reset story.
    for day in (24, 9):
        cart = [("party_catering_tray", 1)]
        t.add(at(day, 16, 20), "Vikram's Agent", "acp", cart, "ESCALATE",
              REASONS["CATEGORY"], total_of(cart),
              limits=snapshot("NEW", 0, 0),
              buyer_reasoning="office party for twelve people")
    # And some ordinary orders from him, so the refusal is visibly not
    # the only thing he ever did.
    for day in (23, 20, 15, 7):
        cart = pick_cart()
        t.add(at(day, 13, 10), "Vikram's Agent", "acp", cart, "APPROVE",
              REASONS["APPROVE"], total_of(cart), payment_id=ref(),
              limits=snapshot("NEW", 0, 0))

    # The flood gate: refused outright, not queued. Two attempts inside
    # a minute of each other is what it is there to stop.
    for day, minute in ((12, 14), (12, 15), (12, 16)):
        cart = [("veg_thali", 1)]
        t.add(at(day, 21, minute), "Meera's Agent", "x402", cart,
              "VELOCITY_REFUSED",
              "agent rate limit reached: 3 orders in the last hour, limit 3",
              total_of(cart), limits=snapshot("NEW", 3, 450))
    for day in (18, 5):
        cart = pick_cart()
        t.add(at(day, 12, 40), "Meera's Agent", "x402", cart, "APPROVE",
              REASONS["APPROVE"], total_of(cart), payment_id=ref(),
              limits=snapshot("NEW", 0, 0))

    # A customer who arrived this week. One settled order puts her at
    # STANDARD, which is what makes the tier ladder legible on the board:
    # NEW, STANDARD and TRUSTED all visible with a reason behind each.
    for day, cart in ((2, [("masala_dosa", 1), ("filter_coffee", 1)]),
                      (1, [("veg_thali", 1)])):
        t.add(at(day, 19, 20), "Sana's Agent", "acp", cart, "APPROVE",
              REASONS["APPROVE"], total_of(cart), payment_id=ref(),
              limits=snapshot("NEW", 0, 0),
              buyer_reasoning="first time ordering here")

    # ── what people wanted and she does not sell ────────────────────
    for i, want in enumerate(OFF_MENU):
        day = 27 - (i * 3)
        if day < 1:
            day = RNG.randint(1, 20)
        t.add(at(day, RNG.choice([12, 19, 20]), RNG.randint(0, 59)),
              RNG.choice(["mcp:claude", "Priya's Agent", "Jeet's Agent"]),
              "mcp", [], "UNMATCHED_DEMAND", want, 0)
    # pizza asked for more than once, so the panel has a clear top row
    for day in (22, 14, 6, 2):
        t.add(at(day, 20, RNG.randint(0, 59)), "mcp:claude", "mcp", [],
              "UNMATCHED_DEMAND", "2 pizzas", 0)

    # ── today, so the board is not a museum ─────────────────────────
    for hour, name, proto, cart in [
        (9,  "Ananya's Agent", "ap2",  [("masala_dosa", 1), ("filter_coffee", 1)]),
        (12, "Priya's Agent",  "acp",  [("veg_thali", 1), ("filter_coffee", 1)]),
        (13, "Rahul's Agent",  "x402", [("chicken_biryani", 1)]),
        (13, "mcp:claude",     "mcp",  [("masala_dosa", 2), ("filter_coffee", 1)]),
        (14, "Jeet's Agent",   "ap2",  [("paneer_bhurji", 1), ("tandoori_roti", 3)]),
        (13, "Rahul's Agent",  "x402", [("veg_thali", 1), ("filter_coffee", 1)]),
        (14, "Priya's Agent",  "acp",  [("chicken_biryani", 1), ("gulab_jamun", 1)]),
        (15, "Ananya's Agent", "ap2",  [("veg_thali", 1), ("gulab_jamun", 1)]),
        (16, "Jeet's Agent",   "ap2",  [("masala_dosa", 1), ("filter_coffee", 1)]),
        (17, "mcp:claude",     "mcp",  [("veg_thali", 1), ("filter_coffee", 1)]),
    ]:
        agent = next(a for a in AGENTS if a.name == name)
        src = "routine" if name in ("Ananya's Agent", "Rahul's Agent") else None
        t.add(at(0, hour, RNG.randint(0, 55)), name, proto, cart, "APPROVE",
              REASONS["APPROVE"], total_of(cart), payment_id=ref(),
              limits=snapshot(agent.tier, 1, 200),
              source=src,
              routine_id=({"Ananya's Agent": "rt-ananya-breakfast",
                           "Rahul's Agent": "rt-rahul-lunch"}.get(name)
                          if src else None),
              buyer_reasoning=RNG.choice(BUYER_REASONS),
              delivery=DELIVERY if name == "Jeet's Agent" else None)

    # Two waiting on her right now, so the queue is not empty on camera.
    #
    # These have to be PAID orders sitting at PENDING_MERCHANT_APPROVAL,
    # not bare escalations. A pre-payment escalation lives in its
    # adapter's memory and does not survive a restart, so seeding one
    # writes a row that no screen will ever show. The paid lifecycle is
    # rebuilt from this trail, which is why it is the only kind that can
    # be seeded at all -- and it is the better beat anyway: already paid
    # for, waiting on her yes or no, and a no refunds by itself.
    for hour, name, proto, cart in [
        (15, "Priya's Agent", "acp", [("chicken_biryani", 2)]),
        (16, "mcp:claude",    "mcp", [("veg_thali", 2), ("chicken_biryani", 1)]),
    ]:
        agent = next(a for a in AGENTS if a.name == name)
        total = total_of(cart)
        lim = snapshot(agent.tier, 1, 300)
        dec = t.add(at(0, hour, 2), name, proto, cart, "ESCALATE",
                    REASONS["ESCALATE"].format(
                        total=total, th=MANDATE.human_confirm_threshold_inr),
                    total, limits=lim,
                    buyer_reasoning=RNG.choice(BUYER_REASONS),
                    delivery=DELIVERY)
        t.add(at(0, hour, 4), name, proto, cart, "AWAITING_PAYMENT",
              "payment link issued; awaiting the customer", total, parent=dec)
        t.add(at(0, hour, 7), name, proto, cart, "PAID",
              "payment captured", total, payment_id=ref(), parent=dec)
        t.add(at(0, hour, 8), name, proto, cart, "PENDING_MERCHANT_APPROVAL",
              "paid; awaiting the kitchen's yes or no", total, parent=dec)

    return t


# ──────────────────────────────────────────────────────────── driving it

# The handles the system generated before agents were named after their
# owner. Every one of these is the same small set of actors -- the
# project's own testing and the two scripted buyer agents -- under a
# scheme that has since been replaced, so they are folded into the named
# cast rather than left on the board as noise.
#
# This renames the ACTOR, never the payment: the `pay_` references on
# these rows are untouched and still resolve in Razorpay.
LEGACY_NAMES = {
    "agent-aniyp":        "Jeet's Agent",
    "agent-mylun":        "Jeet's Agent",
    "agent-hv1w0":        "Jeet's Agent",
    "shopper-m45qe":      "Jeet's Agent",
    "shopper-obcxe":      "Jeet's Agent",
    "buyer-agent-a-demo": "Priya's Agent",     # the scripted ACP buyer
    "demo-acp-171742":    "Priya's Agent",
    "x402-demo":          "Rahul's Agent",     # the scripted x402 buyer
}


def rename_legacy_agents(conn) -> int:
    changed = 0
    for old_name, new_name in LEGACY_NAMES.items():
        cur = conn.execute(
            "UPDATE audit_events SET agent_id = ? WHERE agent_id = ?",
            (new_name, old_name))
        changed += cur.rowcount
    return changed


def seed_routines() -> int:
    """The standing orders the seeded history was placed BY.

    routines.json is a separate store from the audit trail, so seeding
    one without the other gives a buyer console showing no standing
    orders beside a merchant board reporting 21% of revenue from them.
    Only Jeet's are created: the console shows the signed-in customer's
    own routines, and the other three belong to other people.

    Written through routines.create() rather than by hand, so every
    validation a real one goes through applies to these too.
    """
    import routines

    for existing in routines.all_routines():
        if existing.get("agent_id") == "Jeet's Agent":
            routines.delete(existing["id"])

    routines.create(
        items=[{"item_id": "paneer_bhurji", "qty": 1},
               {"item_id": "tandoori_roti", "qty": 3}],
        days=["tue"], at_time="20:00", agent_id="Jeet's Agent",
        phone="+918306610707", routine_cap_inr=400,
        utc_offset_minutes=330,
    )
    routines.create(
        items=[{"item_id": "masala_dosa", "qty": 1},
               {"item_id": "filter_coffee", "qty": 1}],
        days=["mon", "wed", "fri"], at_time="08:30", agent_id="Jeet's Agent",
        phone="+918306610707", routine_cap_inr=250,
        utc_offset_minutes=330,
    )
    return sum(1 for r in routines.all_routines()
               if r.get("agent_id") == "Jeet's Agent")


def flag_disputes(conn) -> int:
    """Two orders marked disputed, so the Disputes tab is not empty.

    disputed_at is a single timestamp rather than a status row, because
    being disputed is a fact ABOUT a record, not a stage the order is in
    -- a status row would become the order's latest status and shove it
    out of whatever state it is really in.

    Deliberately one refunded order and one settled one: those are the
    two arguments a merchant actually has to answer, and the evidence
    pack reads differently for each.
    """
    picks = [r[0] for r in conn.execute(
        "SELECT id FROM audit_events WHERE decision = 'ESCALATE' "
        "AND id IN (SELECT order_ref FROM audit_events WHERE decision = 'REFUNDED') "
        "ORDER BY id DESC LIMIT 1")]
    picks += [r[0] for r in conn.execute(
        "SELECT id FROM audit_events WHERE decision = 'APPROVE' "
        "AND payment_id IS NOT NULL AND total_inr >= 300 "
        "ORDER BY id DESC LIMIT 1")]
    now = datetime.now(timezone.utc).isoformat()
    for oid in picks:
        conn.execute("UPDATE audit_events SET disputed_at = ? WHERE id = ?",
                     (now, oid))
    return len(picks)


def real_payment_rows(conn) -> set[int]:
    """Every row belonging to an order that really was paid.

    Those `pay_` ids are genuine Razorpay test-mode captures. Keeping
    them is the whole reason "is any of this real?" has an answer, so
    they and their lifecycle children survive the wipe.
    """
    keep = {r[0] for r in conn.execute(
        "SELECT id FROM audit_events WHERE payment_id LIKE 'pay_%'")}
    keep |= {r[0] for r in conn.execute(
        "SELECT id FROM audit_events WHERE order_ref IN "
        "(SELECT id FROM audit_events WHERE payment_id LIKE 'pay_%')")}
    parents = {r[0] for r in conn.execute(
        "SELECT DISTINCT order_ref FROM audit_events "
        "WHERE payment_id LIKE 'pay_%' AND order_ref IS NOT NULL")}
    keep |= parents
    return keep


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--restore", metavar="FILE",
                    help="path to a backup, or LAST for the most recent")
    args = ap.parse_args()

    if args.restore:
        backups = sorted(BACKUP_DIR.glob("audit_log-*.db"))
        src = backups[-1] if args.restore == "LAST" else Path(args.restore)
        if not src.exists():
            print(f"no such backup: {src}")
            return 1
        shutil.copy2(src, DB)
        print(f"restored {src.name} -> {DB.name}")
        return 0

    audit_log.init_db(str(DB))
    with sqlite3.connect(DB) as conn:
        keep = real_payment_rows(conn)
        before = conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]

    print(f"  current trail       {before} rows")
    print(f"  real pay_ captures  {len(keep)} rows preserved")

    trail = build()
    print(f"  seeded history      {len(trail.rows)} rows to write")

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return 0

    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = BACKUP_DIR / f"audit_log-{stamp}.db"
    shutil.copy2(DB, backup)
    print(f"  backed up to        backups/{backup.name}")

    with sqlite3.connect(DB) as conn:
        placeholders = ",".join("?" * len(keep)) or "NULL"  # noqa: E501
        # Scoped to the DEFAULT kitchen. This script owns Amma's history
        # and nobody else's -- unscoped it deleted every other kitchen on
        # the platform, which is a thing you notice only after running
        # the seeder that rebuilt them.
        conn.execute(
            f"DELETE FROM audit_events WHERE id NOT IN ({placeholders}) "
            "AND (merchant_id = ? OR merchant_id IS NULL)",
            (*keep, merchants.default_id()))
        written = trail.write(conn)
        renamed = rename_legacy_agents(conn)
        disputed = flag_disputes(conn)
        conn.commit()
    made = seed_routines()
    print(f"  standing orders     {made} for the demo account")
    with sqlite3.connect(DB) as conn:
        print(f"  flagged             {disputed} orders as disputed")
        print(f"  renamed             {renamed} legacy-handle rows")
        after = conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]

    print(f"  wrote               {written} rows")
    print(f"  trail is now        {after} rows")
    print(f"\n  undo with:  python seed_demo.py --restore LAST")
    return 0


if __name__ == "__main__":
    sys.exit(main())
