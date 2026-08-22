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


def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(_SCHEMA)


def record_event(
    agent_id: str,
    protocol: str,
    cart: list[dict],
    decision: str,
    reason: str,
    total_inr: int,
    payment_id: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> int:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO audit_events "
            "(ts, agent_id, protocol, cart_json, decision, reason, total_inr, payment_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                agent_id,
                protocol,
                json.dumps(cart),
                decision,
                reason,
                total_inr,
                payment_id,
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
