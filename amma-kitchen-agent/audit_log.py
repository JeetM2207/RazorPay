"""Append-only audit trail. SQLite-backed, human-readable, queryable.

Every negotiation decision gets recorded here -- this is what the
dashboard renders and what the trust engine reads to score agents.
Webhook idempotency (step 6) will also key off payment_id in this table.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB_PATH = str(Path(__file__).resolve().parent / "audit.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    protocol TEXT NOT NULL,
    cart_json TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    total_inr INTEGER NOT NULL,
    payment_id TEXT,
    payment_link_id TEXT
);
"""

# Added after the table shipped, so they arrive by migration rather than
# in _SCHEMA -- an existing audit.db must not have to be thrown away.
#
# `reason` is the SYSTEM's reason: why negotiation.py decided what it
# decided -- caps, categories, thresholds. `buyer_reasoning` is the
# HUMAN's context: the occasion, preference or need behind the order.
#
# The split matters. Having the agent justify the cart against the
# merchant's rules would just restate what `reason` already holds, in
# worse prose. The customer's actual reason is the one thing this system
# has no other way to see, so that is what the field is for.
# `order_ref` links a lifecycle transition back to the decision row that
# started the order. The trail stays append-only: a status change is a
# NEW row carrying the status in `decision`, not an edit of an old one,
# so reading top to bottom shows payment -> decision -> merchant action
# -> outcome in the order they actually happened.
_ADDED_COLUMNS = {
    "buyer_reasoning": "TEXT",
    "delivery_name": "TEXT",
    "delivery_phone": "TEXT",
    "delivery_address": "TEXT",
    "order_ref": "INTEGER",
}


def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(_SCHEMA)
        existing = {row[1] for row in conn.execute("PRAGMA table_info(audit_events)")}
        for column, coltype in _ADDED_COLUMNS.items():
            if column in existing:
                continue
            try:
                conn.execute(f"ALTER TABLE audit_events ADD COLUMN {column} {coltype}")
            except sqlite3.OperationalError as exc:
                # init_db runs on nearly every call, and FastAPI serves
                # sync endpoints from a threadpool -- so two requests can
                # both read PRAGMA before either ALTERs, and the loser
                # gets "duplicate column name". The check above is an
                # optimisation; THIS is what makes it correct. Anything
                # else is a real error and still raises.
                if "duplicate column name" not in str(exc).lower():
                    raise


def record_event(
    agent_id: str,
    protocol: str,
    cart: list[dict],
    decision: str,
    reason: str,
    total_inr: int,
    payment_id: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
    order_ref: int | None = None,
) -> int:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO audit_events "
            "(ts, agent_id, protocol, cart_json, decision, reason, total_inr, payment_id, order_ref) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                agent_id,
                protocol,
                json.dumps(cart),
                decision,
                reason,
                total_inr,
                payment_id,
                order_ref,
            ),
        )
        return cursor.lastrowid


def mark_paid(event_id: int, payment_id: str, db_path: str = DEFAULT_DB_PATH) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE audit_events SET payment_id = ? WHERE id = ?", (payment_id, event_id)
        )


def attach_payment_link(
    event_id: int, payment_link_id: str, db_path: str = DEFAULT_DB_PATH
) -> None:
    """Record a just-created (not-yet-paid) Razorpay Payment Link against
    an event. Distinct from mark_paid, which records actual capture."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE audit_events SET payment_link_id = ? WHERE id = ?",
            (payment_link_id, event_id),
        )


def attach_buyer_reasoning(
    event_id: int, reasoning: str, db_path: str = DEFAULT_DB_PATH
) -> None:
    """Record the human context behind an order -- occasion, preference,
    need -- as reported by the buyer's agent.

    Kept separate from `reason`, which is why the system decided what it
    decided. A merchant reading the trail sees both: why the person
    wanted it, and what the rules allowed.
    """
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE audit_events SET buyer_reasoning = ? WHERE id = ?", (reasoning, event_id)
        )


def attach_delivery(
    event_id: int,
    name: str,
    phone: str,
    address: str,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    """Put a real recipient on the order. Without this an agent-placed
    order is a price with nobody to hand the food to."""
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE audit_events SET delivery_name = ?, delivery_phone = ?, "
            "delivery_address = ? WHERE id = ?",
            (name, phone, address, event_id),
        )


def get_event_by_payment_link(
    payment_link_id: str, db_path: str = DEFAULT_DB_PATH
) -> dict | None:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM audit_events WHERE payment_link_id = ? ORDER BY id DESC LIMIT 1",
            (payment_link_id,),
        ).fetchone()
        return dict(row) if row else None


UNMATCHED_DEMAND = "UNMATCHED_DEMAND"


def record_unmatched_demand(
    agent_id: str,
    protocol: str,
    requested: str,
    db_path: str = DEFAULT_DB_PATH,
) -> int:
    """Someone asked for something this merchant does not sell.

    Worth a row of its own. Every other surface in this project *tells*
    the customer an item is unavailable and then forgets it, which throws
    away the most useful thing a merchant could learn from an agent
    channel: what people keep trying to buy from her that she has not put
    on the menu.

    Written through the same writer as every other event, into the same
    table, distinguishable only by `decision` and the source tag on
    `agent_id` -- not a parallel log. `reason` holds the customer's words
    verbatim, so a demand report is a plain query rather than prose
    parsing. Priced at zero because nothing was sold.
    """
    return record_event(
        agent_id=agent_id,
        protocol=protocol,
        cart=[],
        decision=UNMATCHED_DEMAND,
        reason=requested.strip(),
        total_inr=0,
        db_path=db_path,
    )


def get_unmatched_demand(db_path: str = DEFAULT_DB_PATH, limit: int = 50) -> list[dict]:
    """What people asked for and could not be sold, most requested first.

    The merchant-facing point of the whole thing: "eleven people asked
    for pizza this week" is a menu decision she can act on.
    """
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT LOWER(TRIM(reason)) AS requested, COUNT(*) AS times, "
            "       MAX(ts) AS last_asked "
            "FROM audit_events WHERE decision = ? "
            "GROUP BY requested ORDER BY times DESC, requested ASC LIMIT ?",
            (UNMATCHED_DEMAND, limit),
        ).fetchall()
    return [{"requested": r[0], "times": r[1], "last_asked": r[2]} for r in rows]


def get_order_rows(order_ref: int, db_path: str = DEFAULT_DB_PATH) -> list[dict]:
    """Everything that has happened to one order, oldest first.

    The decision row itself, plus every lifecycle transition pointing at
    it. This is what makes the trail readable end to end: payment, then
    the decision being actioned, then the merchant's answer, then the
    outcome, each with its own timestamp.
    """
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM audit_events WHERE id = ? OR order_ref = ? ORDER BY id",
            (order_ref, order_ref),
        ).fetchall()
        return [dict(row) for row in rows]


def get_order_status(order_ref: int, db_path: str = DEFAULT_DB_PATH) -> str | None:
    """The order's current status: the most recent transition recorded
    against it, or None if it has not entered the lifecycle yet."""
    rows = get_order_rows(order_ref, db_path=db_path)
    transitions = [r for r in rows if r["order_ref"] == order_ref]
    return transitions[-1]["decision"] if transitions else None


def get_orders_with_status(
    status: str, db_path: str = DEFAULT_DB_PATH, protocol: str | None = None
) -> list[dict]:
    """Orders whose LATEST transition is `status`.

    Deliberately not "orders that ever hit this status" -- an order that
    was pending and has since been accepted is no longer pending, and a
    queue built the other way would never empty.
    """
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        refs = conn.execute(
            "SELECT DISTINCT order_ref FROM audit_events WHERE order_ref IS NOT NULL"
            + (" AND protocol = ?" if protocol else ""),
            (protocol,) if protocol else (),
        ).fetchall()

    matching = []
    for row in refs:
        ref = row["order_ref"]
        if get_order_status(ref, db_path=db_path) != status:
            continue
        origin = get_order_rows(ref, db_path=db_path)
        matching.append(origin[0])
    return matching


def get_events_for_agent(agent_id: str, db_path: str = DEFAULT_DB_PATH) -> list[dict]:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM audit_events WHERE agent_id = ? ORDER BY id", (agent_id,)
        ).fetchall()
        return [dict(row) for row in rows]


def get_frequent_addons(
    cart_items: list[str], db_path: str = DEFAULT_DB_PATH, limit: int = 5
) -> list[str]:
    """Item names most often bought ALONGSIDE the given items, in orders
    that were actually paid for -- ranked most frequent first.

    This is the evidence behind a predictive upsell. It only reads history
    and returns names; it decides nothing. The caller (negotiation.py)
    still applies the mandate's limits to whatever comes back, so a
    popular item that would breach a threshold is never suggested.

    "Successful" means payment_id IS NOT NULL -- money genuinely arrived.
    An order that was approved but abandoned at checkout is not evidence
    that anyone wanted the combination.
    """
    if not cart_items:
        return []
    init_db(db_path)

    slots = ",".join("?" * len(cart_items))
    sql = f"""
        WITH paid AS (
            SELECT id, cart_json FROM audit_events WHERE payment_id IS NOT NULL
        ),
        expanded AS (
            SELECT paid.id AS order_id,
                   json_extract(line.value, '$.item') AS item_name
            FROM paid, json_each(paid.cart_json) AS line
        ),
        anchored AS (
            SELECT DISTINCT order_id FROM expanded WHERE item_name IN ({slots})
        )
        SELECT expanded.item_name, COUNT(DISTINCT expanded.order_id) AS orders
        FROM expanded
        JOIN anchored ON anchored.order_id = expanded.order_id
        WHERE expanded.item_name NOT IN ({slots})
        GROUP BY expanded.item_name
        -- item_name breaks ties so the ranking is stable, not arbitrary
        ORDER BY orders DESC, expanded.item_name ASC
        LIMIT ?
    """
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(sql, (*cart_items, *cart_items, limit)).fetchall()
    return [row[0] for row in rows]


def get_all_events(db_path: str = DEFAULT_DB_PATH, limit: int = 200) -> list[dict]:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]
