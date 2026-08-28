"""Proof of Authorization: the complete factual record of one order.

When a customer says "I never authorised this", a small merchant has
nothing to answer with. The facts that would settle it -- what the
customer's agent was allowed to spend, what Amma's rules were at that
moment, what the system decided and why, whether a human was asked and
what they said, and what actually moved through Razorpay -- all exist in
this project already. They have simply never been assembled in one place.

That is all this module does. It reads; it decides nothing.

What this is NOT
----------------
It is not a ruling, a verdict, or a determination of fault. There is no
"liable" field and there is not going to be one. The two checks it
computes are arithmetic against the recorded limits -- was the total
inside the cap that was in force, and if the order crossed the
confirmation threshold, is a human answer on file -- and both are stated
as facts with their numbers shown. Who owes whom is a question for the
people involved and their card network, not for this file.

Where the facts come from
-------------------------
Every field traces to something already logged. Where a field genuinely
does not exist for a given order -- an ACP order carries no
`buyer_reasoning`, because only the MCP tools ask for it -- the pack says
so. It does not omit the row quietly and it does not invent a value: a
record with a hole in it that is clearly marked is worth something, and a
record with a plausible-looking guess in it is worth less than nothing.
"""

import json

import audit_log

# The reason string the core writes when the merchant's own confirmation
# threshold is what stopped an order. Matched rather than re-derived, so
# this file never forms a second opinion about why something escalated.
_THRESHOLD_MARKER = "human confirmation threshold"

# Rows that record a human answering. The trail is append-only, so these
# are what a confirmation actually looks like on disk once the in-memory
# escalation is long gone.
_HUMAN_ANSWERED = {
    "MERCHANT_ACCEPTED": "accepted",
    "MERCHANT_REJECTED": "declined",
    "MERCHANT_TIMEOUT_REFUNDED": "no answer before the order timed out",
    "REJECTED": "declined",
}


def _cart(row: dict) -> list[dict]:
    try:
        return json.loads(row["cart_json"])
    except (json.JSONDecodeError, TypeError):
        return []


def _snapshot(row: dict) -> dict | None:
    try:
        return json.loads(row["limits_snapshot"]) if row.get("limits_snapshot") else None
    except (json.JSONDecodeError, TypeError):
        return None


def _missing(what: str, why: str) -> dict:
    """A field that genuinely is not there, said out loud."""
    return {"available": False, "what": what, "why": why}


def _buyer_reasoning(row: dict) -> dict:
    text = (row.get("buyer_reasoning") or "").strip()
    if text:
        return {"available": True, "text": text}
    return _missing(
        "the customer's stated reason for the order",
        f"the {row['protocol'].upper()} path does not ask for it; only the MCP tools "
        "require a reason, so there is nothing recorded for this order",
    )


def _override_answers(row: dict, db_path: str) -> list[dict]:
    """A human decision that was recorded as its own row, not as a child.

    When Amma answers an ACP/AP2/x402 escalation, the orchestrator writes
    a SEPARATE audit entry -- an APPROVE carrying "human override of
    ESCALATE", or a REJECTED -- rather than editing the escalation. That
    is deliberate and right: the trail then shows both what the machine
    decided and that a human separately chose otherwise.

    It also means the answer is not an `order_ref` child, so it has to be
    found the way it is actually linked: the same agent and the same
    cart, decided after the escalation.
    """
    same_cart = sorted((line["item"], line["qty"]) for line in _cart(row))
    out = []
    for r in audit_log.get_events_for_agent(row["agent_id"], db_path=db_path):
        if r["id"] <= row["id"]:
            continue
        if sorted((l["item"], l["qty"]) for l in _cart(r)) != same_cart:
            continue
        if r["decision"] == "APPROVE" and "human override" in (r["reason"] or ""):
            out.append({"at": r["ts"], "outcome": "accepted",
                        "recorded_as": "APPROVE (human override)", "reason": r["reason"]})
        elif r["decision"] == "REJECTED":
            out.append({"at": r["ts"], "outcome": "declined",
                        "recorded_as": "REJECTED (human)", "reason": r["reason"]})
    return out


def _confirmation_trail(row: dict, rows: list[dict], db_path: str) -> dict:
    """Whether a human was asked, and what they said.

    Built from the audit trail rather than from the escalation queue,
    because the queue is in memory and does not survive a restart. The
    WhatsApp text itself is added only if it is still in the outbox, and
    labelled as the transient thing it is -- the durable record is the
    row, not the message.
    """
    answers = [
        {
            "at": r["ts"],
            "outcome": _HUMAN_ANSWERED[r["decision"]],
            "recorded_as": r["decision"],
            "reason": r["reason"],
        }
        for r in rows
        if r["decision"] in _HUMAN_ANSWERED
    ]
    answers += _override_answers(row, db_path)

    messages = []
    try:
        import notification_service

        for m in notification_service.outbox(limit=60):
            if f"Order #{row['id']}" in (m.get("body") or ""):
                messages.append({
                    "to": m["to"],
                    "audience": m.get("audience", "merchant"),
                    "body": m["body"],
                    "sent_at": m["sent_at"],
                    "delivery_error": m.get("error"),
                })
    except Exception:
        pass

    return {
        "answers": sorted(answers, key=lambda a: a["at"]),
        "messages": messages,
        "messages_note": (
            "The messages below are still in this process's outbox. They are a "
            "convenience, not the record -- the record is the timestamped rows above, "
            "which survive a restart."
            if messages else
            "No message text is still in memory for this order. That is expected on a "
            "restarted server and does not affect the rows above, which are the record."
        ),
    }


def _within_buyer_cap(row: dict, snapshot: dict | None) -> dict:
    """Was the total inside what the customer authorised at the time?"""
    buyer = (snapshot or {}).get("buyer")
    total = row["total_inr"]
    if not buyer or buyer.get("hard_cap_inr") in (None, ""):
        return {
            "question": "Was this order within the customer's authorised limit?",
            "result": "not_recorded",
            "tone": "brick",
            "label": "no customer limit on file",
            "detail": (
                f"This order totalled Rs.{total}. The customer's own spending limits are "
                "held by their agent and were not supplied on this path, so there is "
                "nothing recorded here to check the total against."
            ),
        }

    hard = int(buyer["hard_cap_inr"])
    soft = buyer.get("soft_cap_inr")
    within = total <= hard
    detail = f"Order total Rs.{total}; the customer's hard cap at the time was Rs.{hard}."
    if soft:
        detail += (
            f" Their soft cap was Rs.{soft}, above which their own agent was instructed "
            "to ask them first."
        )
    return {
        "question": "Was this order within the customer's authorised limit?",
        "result": "yes" if within else "no",
        "tone": "leaf" if within else "brick",
        "label": "within the authorised cap" if within else "over the authorised cap",
        "detail": detail,
        "numbers": {"total_inr": total, "hard_cap_inr": hard, "soft_cap_inr": soft},
    }


def _confirmation_check(row: dict, snapshot: dict | None, trail: dict) -> dict:
    """Did it cross her threshold, and if so is an answer on file?"""
    merchant = (snapshot or {}).get("merchant") or {}
    threshold = merchant.get("human_confirm_threshold_inr")
    total = row["total_inr"]
    crossed = bool(threshold is not None and total >= threshold) or (
        _THRESHOLD_MARKER in (row.get("reason") or "")
    )

    if threshold is None:
        return {
            "question": "Did this order need the merchant's confirmation, and is it on file?",
            "result": "not_recorded",
            "tone": "brick",
            "label": "limits not recorded",
            "detail": (
                "This order predates the recording of the merchant's limits alongside "
                "each decision, so there is nothing here to say what threshold applied."
            ),
        }

    if not crossed:
        return {
            "question": "Did this order need the merchant's confirmation, and is it on file?",
            "result": "not_required",
            "tone": "leaf",
            "label": "no confirmation required",
            "detail": (
                f"Order total Rs.{total} was below the merchant's confirmation threshold "
                f"of Rs.{threshold} in force at the time, so it was handled inside the "
                "bounds she had already set."
            ),
            "numbers": {"total_inr": total, "threshold_inr": threshold},
        }

    answered = bool(trail["answers"])
    return {
        "question": "Did this order need the merchant's confirmation, and is it on file?",
        "result": "confirmed" if answered else "missing",
        "tone": "rust" if answered else "brick",
        "label": "crossed threshold, answer on file" if answered
                 else "crossed threshold, NO answer on file",
        "detail": (
            f"Order total Rs.{total} met or exceeded the merchant's confirmation "
            f"threshold of Rs.{threshold} in force at the time. "
            + (
                f"A human answer is recorded: {trail['answers'][-1]['outcome']} at "
                f"{trail['answers'][-1]['at'][:19]} UTC."
                if answered else
                "No human answer is recorded against this order. That is a genuine gap "
                "in the record and is shown as one."
            )
        ),
        "numbers": {"total_inr": total, "threshold_inr": threshold},
    }


def build_evidence_pack(order_id: int, db_path: str | None = None) -> dict | None:
    """Everything recorded about one order, in one object.

    Read-only. Returns None if there is no such order.
    """
    db_path = db_path or audit_log.DEFAULT_DB_PATH
    row = audit_log.get_event(order_id, db_path=db_path)
    if row is None:
        return None

    # A lifecycle row points back at the decision it belongs to; a pack is
    # always assembled about the decision.
    if row.get("order_ref"):
        origin = audit_log.get_event(row["order_ref"], db_path=db_path)
        if origin:
            row = origin

    rows = audit_log.get_order_rows(row["id"], db_path=db_path)
    snapshot = _snapshot(row)
    trail = _confirmation_trail(row, rows, db_path)

    payments = [
        {
            "kind": "refund" if r["decision"] in ("REFUNDED", "REFUND_FAILED") else "capture",
            "at": r["ts"],
            "reference": r["payment_id"],
            "detail": r["reason"],
            "simulated": bool(r["payment_id"] and str(r["payment_id"]).startswith("sim_")),
        }
        for r in rows
        if r["payment_id"] or r["decision"] in ("REFUNDED", "REFUND_FAILED")
    ]

    return {
        "order_id": row["id"],
        "generated_at": audit_log.datetime.now(audit_log.timezone.utc).isoformat(),
        "disputed_at": row.get("disputed_at"),
        "order": {
            "placed_at": row["ts"],
            "agent_id": row["agent_id"],
            "protocol": row["protocol"],
            "cart": _cart(row),
            "total_inr": row["total_inr"],
            "delivery": {
                "name": row.get("delivery_name"),
                "phone": row.get("delivery_phone"),
                "address": row.get("delivery_address"),
            },
        },
        "limits_in_force": snapshot or _missing(
            "the limits that applied to this order",
            "this order was recorded before limits were snapshotted alongside each "
            "decision, so only the live configuration is available and that may since "
            "have changed",
        ),
        "buyer_reasoning": _buyer_reasoning(row),
        "system_decision": {
            "decision": row["decision"],
            "reason": row["reason"],
            "note": "Decided by plain Python in negotiation.py. No model was involved.",
        },
        "confirmation_trail": trail,
        "payments": payments,
        "lifecycle": [
            {"at": r["ts"], "state": r["decision"], "detail": r["reason"]}
            for r in rows
        ],
        "checks": [
            _within_buyer_cap(row, snapshot),
            _confirmation_check(row, snapshot, trail),
        ],
    }
