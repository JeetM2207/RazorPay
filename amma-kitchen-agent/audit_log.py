"""Append-only audit trail. SQLite-backed, human-readable, queryable.

Every negotiation decision gets recorded here -- this is what the
dashboard renders and what the trust engine reads to score agents.
Webhook idempotency (step 6) will also key off payment_id in this table.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
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
# `limits_snapshot` is the one column here that exists for a reason other
# than convenience. Everything else in this row describes what happened;
# that column describes WHAT WAS TRUE AT THE TIME -- the merchant's cap,
# her confirmation threshold, the categories she allowed, and the
# customer's own caps if the caller knew them.
#
# Without it, an order's record referenced the live config, so the moment
# Amma edited her cap every past order silently started describing limits
# that were never applied to it. That is fine for a dashboard and fatal
# for evidence: the whole value of a record is that it says what the rule
# WAS, not what the rule is now.
#
# `disputed_at` is a single timestamp, deliberately. A disputed order is
# one whose evidence pack someone wants to look at -- not a ticket with a
# workflow.
_ADDED_COLUMNS = {
    "buyer_reasoning": "TEXT",
    "delivery_name": "TEXT",
    "delivery_phone": "TEXT",
    "delivery_address": "TEXT",
    "order_ref": "INTEGER",
    "limits_snapshot": "TEXT",
    "disputed_at": "TEXT",
    # How the order came to exist. Almost everything is a live request
    # from a customer or their assistant; `routine` marks the ones a
    # standing order placed on a schedule, with nobody watching at the
    # moment of charge. That distinction belongs in the record.
    "source": "TEXT",
    "routine_id": "TEXT",
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
    limits_snapshot: dict | None = None,
    source: str | None = None,
    routine_id: str | None = None,
    ts: str | None = None,
) -> int:
    """`ts` exists so the orchestrator's injectable clock reaches the
    ROW as well as the check. Without it a test can move the clock for
    the rate window and still write rows stamped with the real time, so
    the two disagree and the window never fills. Production never passes
    it."""
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO audit_events "
            "(ts, agent_id, protocol, cart_json, decision, reason, total_inr, payment_id, "
            " order_ref, limits_snapshot, source, routine_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ts or datetime.now(timezone.utc).isoformat(),
                agent_id,
                protocol,
                json.dumps(cart),
                decision,
                reason,
                total_inr,
                payment_id,
                order_ref,
                json.dumps(limits_snapshot) if limits_snapshot else None,
                source,
                routine_id,
            ),
        )
        return cursor.lastrowid


def mark_paid(event_id: int, payment_id: str, db_path: str = DEFAULT_DB_PATH) -> None:
    """Record the capture, and take the food out of the kitchen.

    The stock move lives here because this is the ONE function every
    capture path already reaches -- the webhook, the reconciler, x402,
    the autonomous settlement and the console's payment-status poll. The
    alternative was teaching all five, which is exactly how the
    reconciler ended up missing the pay-first lifecycle: a shared step
    that is not actually shared.

    Stock moves at CAPTURE, not at APPROVE. An approved cart is an
    invitation to pay that may never be taken up, and holding stock for
    every abandoned checkout would starve the menu of dishes nobody
    bought. Money arriving is the first moment the food is really spoken
    for. A rejected or timed-out order puts it back -- see mcp_orders.

    Only on the transition. Calling this twice for the same payment must
    not take the food twice, and these paths genuinely do overlap: a
    webhook and the reconciler can both learn about the same capture.
    The idempotency ledger stops most of that; this stops the rest.
    """
    import merchant_config          # local: audit_log stays stdlib-only at import

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        before = conn.execute(
            "SELECT payment_id, cart_json FROM audit_events WHERE id = ?", (event_id,)
        ).fetchone()
        conn.execute(
            "UPDATE audit_events SET payment_id = ? WHERE id = ?", (payment_id, event_id)
        )

    if before is None or before["payment_id"]:
        return                       # unknown row, or already paid for

    try:
        merchant_config.adjust_stock(json.loads(before["cart_json"] or "[]"), -1)
    except Exception:
        # The capture is the fact that matters and it is already written.
        # A shop file that will not save must not unwind somebody's
        # payment, so this is logged by its caller and stepped over.
        pass


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


def get_event_by_payment_id(
    payment_id: str, db_path: str = DEFAULT_DB_PATH
) -> dict | None:
    """The order a captured payment belongs to.

    Refund webhooks arrive carrying a payment id rather than a payment
    link id, so the existing lookup cannot answer them. Oldest first: the
    row that CARRIES the payment is the original decision, and later
    lifecycle rows copy it forward.
    """
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM audit_events WHERE payment_id = ? ORDER BY id ASC LIMIT 1",
            (payment_id,),
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


# --------------------------------------------------- growth insights (read-only)
#
# Everything below only READS. It is deliberately additive: no lifecycle
# code calls it, nothing it returns feeds a decision, and removing the
# whole block would change no order's outcome.

_SETTLED_ELSEWHERE = ("REFUNDED", "REFUND_FAILED", "MERCHANT_REJECTED",
                      "MERCHANT_TIMEOUT_REFUNDED")


def _cart_set(row: dict) -> frozenset:
    try:
        return frozenset(
            (line["item"], line["qty"]) for line in json.loads(row["cart_json"])
        )
    except (json.JSONDecodeError, TypeError, KeyError):
        return frozenset()


def _accepted_addons(rows: list[dict], paid: list[dict]) -> list[str]:
    """Add-ons a customer actually said yes to, inferred from the trail.

    Inferred rather than recorded, and worth being honest about why: the
    suggestion is computed at propose time and returned to the caller,
    but nothing writes it into the audit row -- and recording it would
    mean editing the orchestrator, which this feature is not allowed to
    touch. So it is reconstructed instead.

    The signal is a real one. A customer who accepts an add-on causes the
    cart to be proposed a SECOND time, identical but for one extra line,
    and that second cart is the one that gets paid for. An earlier cart
    from the same agent that is a strict subset differing by exactly one
    item is therefore the same order before the add-on went in, and the
    extra item is what they agreed to.

    It can undercount -- a customer who accepts before the first proposal
    leaves no pair to match -- so it is reported as "at least".
    """
    by_agent: dict[str, list[frozenset]] = {}
    for row in rows:
        by_agent.setdefault(row["agent_id"], []).append(_cart_set(row))

    accepted = []
    for order in paid:
        final = _cart_set(order)
        if len(final) < 2:
            continue
        for earlier in by_agent.get(order["agent_id"], []):
            if earlier and earlier < final and len(final - earlier) == 1:
                accepted.append(next(iter(final - earlier))[0])
                break
    return accepted


def growth_stats(hours: int = 24, db_path: str = DEFAULT_DB_PATH) -> dict:
    """A small factual summary of the last `hours`. Read-only.

    Simulated settlements are excluded from revenue for the same reason
    the dashboard excludes them: a `sim_` reference is an assertion of
    ours, not money that moved.
    """
    init_db(db_path)
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM audit_events WHERE ts >= ? ORDER BY id ASC", (since,)
        )]

    # An order that was refunded or declined is not revenue. Keyed on
    # order_ref, which lifecycle rows carry back to the decision row.
    undone = {
        row["order_ref"] for row in rows
        if row["decision"] in _SETTLED_ELSEWHERE and row.get("order_ref")
    }

    paid = [
        row for row in rows
        # Anything but a simulation. `pay_` is a real Razorpay capture and
        # `demo_` is seeded history; both are settled orders as far as a
        # summary of trading goes. `sim_` is not -- it is our own note
        # that we would have taken the money if the account let us.
        #
        # This used to require `pay_` specifically, which disagreed with
        # every other reader in the project (the dashboard, the merchant
        # KPIs and the statement all test for "not sim_"), so the same
        # order could count as revenue on one screen and not on another.
        if (row["payment_id"] or "") and not (row["payment_id"] or "").startswith("sim_")
        and row["id"] not in undone
    ]

    demand: dict[str, int] = {}
    for row in rows:
        if row["decision"] == UNMATCHED_DEMAND:
            key = (row["reason"] or "").strip().lower()
            if key:
                demand[key] = demand.get(key, 0) + 1

    addons = _accepted_addons(rows, paid)
    addon_counts: dict[str, int] = {}
    for name in addons:
        addon_counts[name] = addon_counts.get(name, 0) + 1

    refunded = [r for r in rows if r["decision"] == "REFUNDED"]

    return {
        "window_hours": hours,
        "orders_paid": len(paid),
        "revenue_inr": sum(row["total_inr"] for row in paid),
        "escalated_to_merchant": sum(1 for r in rows if r["decision"] == "ESCALATE"),
        "refunded_orders": len(refunded),
        "refunded_inr": sum(r["total_inr"] for r in refunded),
        "addons_accepted": len(addons),
        "top_addon": max(addon_counts, key=lambda k: (addon_counts[k], k)) if addon_counts else None,
        "unmatched_demand": [
            {"requested": name, "times": times}
            for name, times in sorted(demand.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        ],
        "total_decisions": len(rows),
    }


def transactions(db_path: str = DEFAULT_DB_PATH, limit: int = 60) -> list[dict]:
    """Money in and money out, newest first. Read-only.

    Built from the trail rather than a ledger table, because the trail is
    already the record and a second one could disagree with it. Three
    kinds of row:

      out       the customer paid -- a real `pay_` capture
      out_sim   an autonomous settlement, labelled `sim_`, where the order
                is real and the capture is asserted by us
      in        money coming back: a refund, or the reversal of a
                simulated capture

    A refund is matched to its order by `order_ref`, so the two sides of
    the same order line up without anything having to store a link.
    """
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM audit_events ORDER BY id DESC"
        )]

    by_id = {r["id"]: r for r in rows}
    out: list[dict] = []
    # A refund is written twice on purpose -- once when it is issued and
    # again when Razorpay confirms it processed -- so the trail reads as
    # what happened. That is one movement of money, not two, and rows are
    # newest first, so the first one seen is the latest word on it.
    seen_reversal: set[int] = set()

    for row in rows:
        ref = row.get("order_ref") or row["id"]
        origin = by_id.get(ref, row)
        cart = []
        try:
            cart = [f"{l['qty']}x {l['item'].replace('_', ' ')}" for l in json.loads(origin["cart_json"])]
        except (json.JSONDecodeError, TypeError, KeyError):
            pass

        if row["decision"] in ("REFUNDED", "REFUND_FAILED"):
            if ref in seen_reversal:
                continue
            seen_reversal.add(ref)
            simulated = "simulated capture" in (row["reason"] or "")
            out.append({
                "direction": "in",
                "kind": "reversal" if simulated else "refund",
                "order_ref": ref,
                "amount_inr": origin["total_inr"],
                "at": row["ts"],
                "protocol": row["protocol"],
                "cart": cart,
                "status": row["decision"],
                "detail": row["reason"],
            })
        elif row["payment_id"]:
            simulated = str(row["payment_id"]).startswith("sim_")
            out.append({
                "direction": "out",
                "kind": "simulated" if simulated else "payment",
                "order_ref": ref,
                "amount_inr": row["total_inr"],
                "at": row["ts"],
                "protocol": row["protocol"],
                "cart": cart,
                "status": "SIMULATED" if simulated else "PAID",
                "reference": row["payment_id"],
                "detail": row["reason"],
            })

    return out[:limit]


# ------------------------------------------------- disputes (read + one flag)

def mark_disputed(order_ref: int, db_path: str = DEFAULT_DB_PATH) -> bool:
    """Flag an order as disputed. One timestamp, no workflow.

    Deliberately NOT a lifecycle transition: a status row would become the
    order's latest status and shove it out of whatever state it is really
    in. Being disputed is a fact about the record, not a stage of it.
    """
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "UPDATE audit_events SET disputed_at = ? WHERE id = ? AND disputed_at IS NULL",
            (datetime.now(timezone.utc).isoformat(), order_ref),
        )
        if cursor.rowcount:
            return True
        # Already flagged is success, not failure -- the caller wanted it
        # disputed and it is disputed.
        row = conn.execute(
            "SELECT disputed_at FROM audit_events WHERE id = ?", (order_ref,)
        ).fetchone()
        return bool(row and row[0])


def get_disputed(db_path: str = DEFAULT_DB_PATH, limit: int = 50) -> list[dict]:
    """Orders someone has asked for the record on, newest first."""
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM audit_events WHERE disputed_at IS NOT NULL "
            "ORDER BY disputed_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_event(event_id: int, db_path: str = DEFAULT_DB_PATH) -> dict | None:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM audit_events WHERE id = ?", (event_id,)).fetchone()
        return dict(row) if row else None
