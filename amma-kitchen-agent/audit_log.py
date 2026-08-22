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
    payment_id TEXT
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


def get_events_for_agent(agent_id: str, db_path: str = DEFAULT_DB_PATH) -> list[dict]:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM audit_events WHERE agent_id = ? ORDER BY id", (agent_id,)
        ).fetchall()
        return [dict(row) for row in rows]


def get_all_events(db_path: str = DEFAULT_DB_PATH, limit: int = 200) -> list[dict]:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]
