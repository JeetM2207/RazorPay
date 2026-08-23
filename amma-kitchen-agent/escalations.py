"""SMS escalation: alert Amma when a decision needs her, and resolve it
from her numeric reply.

Keyed on the audit event id, which is the canonical order record -- the
same "Order #" that appears in the SMS is the row in audit_log. Alongside
it we keep the protocol and the adapter-side session id, because that is
what actually unblocks the waiting buyer, whichever protocol it is on.

Design notes worth keeping:

  * negotiation.py has no idea this exists. The trigger lives at the
    adapter boundary, which is also the only layer that knows the session
    id needed to release a waiting buyer.
  * Parsing a reply is a regex, not a model. The input space is "1" or
    "2"; a language model here would add latency, cost and a failure mode
    to a decision that is a two-way branch.
  * SMS cannot approve what the web console cannot. A reply of '1' goes
    through the adapter's own human_confirm, so a hard merchant rule
    (disallowed category) is still refused -- over SMS you get told why.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import APIRouter, Form
from fastapi.responses import PlainTextResponse

import notification_service

router = APIRouter()


@dataclass
class Escalation:
    order_id: int                 # audit_log event id
    protocol: str                 # acp | ap2 | x402
    session_id: str               # adapter-side handle for the waiting buyer
    agent_id: str
    cart: list[tuple[str, int]]
    total_inr: int
    reason: str
    created_at: str
    answered: bool = False
    outcome: str | None = None    # APPROVED | REJECTED | REFUSED
    detail: str | None = None


_PENDING: dict[int, Escalation] = {}


def reset() -> None:
    _PENDING.clear()


def pending() -> list[dict]:
    return [
        {
            "order_id": e.order_id,
            "protocol": e.protocol,
            "agent_id": e.agent_id,
            "total_inr": e.total_inr,
            "reason": e.reason,
            "created_at": e.created_at,
            "answered": e.answered,
            "outcome": e.outcome,
        }
        for e in sorted(_PENDING.values(), key=lambda e: e.order_id)
    ]


# --------------------------------------------------------------- trigger

def notify(protocol: str, session_id: str, detail: dict, cart: list[tuple[str, int]]) -> Escalation | None:
    """Register an escalation and text Amma about it.

    Called by the adapters when a decision comes back ESCALATE. Returns
    None if this order was already registered, so a buyer polling for a
    verdict cannot fire a fresh SMS on every poll.
    """
    order_id = detail["event_id"]
    if order_id in _PENDING:
        return None

    escalation = Escalation(
        order_id=order_id,
        protocol=protocol,
        session_id=session_id,
        agent_id=detail["agent_id"],
        cart=list(cart),
        total_inr=detail["total_inr"],
        reason=detail["reason"],
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    _PENDING[order_id] = escalation

    notification_service.send_sms(
        notification_service.format_escalation_alert(
            order_id=order_id,
            agent_id=escalation.agent_id,
            cart=escalation.cart,
            total_inr=escalation.total_inr,
            reason=escalation.reason,
        )
    )
    return escalation


# ---------------------------------------------------------------- parser

# Deliberately strict. The whole message must be the decision, with an
# optional "#<order>" reference on either side. Anything else is treated
# as unparseable rather than guessed at -- a wrong guess here approves
# or refuses somebody's money.
_REPLY_RE = re.compile(
    r"""^\s*
    (?:\#(?P<order_before>\d{1,9})\s*[:,\-]?\s*)?
    (?P<action>1|2|approve[d]?|accept|yes|ok|reject[ed]?|decline[d]?|no)
    \s*
    (?:[:,\-]?\s*\#(?P<order_after>\d{1,9}))?
    \s*[.!]?\s*$""",
    re.IGNORECASE | re.VERBOSE,
)

_APPROVE_WORDS = {"1", "approve", "approved", "accept", "yes", "ok"}
_REJECT_WORDS = {"2", "reject", "rejected", "decline", "declined", "no"}


@dataclass
class ParsedReply:
    action: str | None            # APPROVE | REJECT | None
    order_id: int | None = None


def parse_reply(text: str) -> ParsedReply:
    """Extract the decision from an SMS body. Returns action=None when the
    message is not unambiguously one of the two options."""
    if not text:
        return ParsedReply(None)

    match = _REPLY_RE.match(text)
    if not match:
        return ParsedReply(None)

    token = match.group("action").lower()
    action = "APPROVE" if token in _APPROVE_WORDS else "REJECT" if token in _REJECT_WORDS else None

    raw_order = match.group("order_before") or match.group("order_after")
    return ParsedReply(action, int(raw_order) if raw_order else None)


# -------------------------------------------------------------- resolver

def _adapter_for(protocol: str):
    """Imported lazily so the adapters can import this module without a
    circular dependency."""
    if protocol == "acp":
        import adapter_acp

        return adapter_acp
    if protocol == "ap2":
        import adapter_ap2

        return adapter_ap2
    if protocol == "x402":
        import adapter_x402

        return adapter_x402
    raise ValueError(f"unknown protocol: {protocol}")


def _oldest_unanswered() -> Escalation | None:
    open_ones = [e for e in _PENDING.values() if not e.answered]
    return min(open_ones, key=lambda e: e.order_id) if open_ones else None


def _most_recently_answered() -> Escalation | None:
    """Used when a bare reply arrives with nothing outstanding. Telling
    Amma what happened to the order she just decided is far more useful
    than 'nothing is waiting', which reads like her message was lost."""
    done = [e for e in _PENDING.values() if e.answered]
    return max(done, key=lambda e: e.order_id) if done else None


def resolve(action: str, order_id: int | None = None) -> dict:
    """Apply a parsed reply to an in-flight escalation.

    Approval is NOT an override: it calls the adapter's own human_confirm,
    so anything the web console would refuse is refused here too.
    """
    if order_id is not None:
        escalation = _PENDING.get(order_id)
    else:
        escalation = _oldest_unanswered() or _most_recently_answered()

    if escalation is None:
        return {
            "ok": False,
            "outcome": "NOT_FOUND",
            "message": (
                f"No order #{order_id} is waiting for a decision."
                if order_id is not None
                else "Nothing is waiting for a decision right now."
            ),
        }
    if escalation.answered:
        return {
            "ok": False,
            "outcome": "ALREADY_ANSWERED",
            "order_id": escalation.order_id,
            "message": f"Order #{escalation.order_id} was already {escalation.outcome}.",
        }

    adapter = _adapter_for(escalation.protocol)
    from fastapi import HTTPException

    try:
        if action == "APPROVE":
            adapter.human_confirm(escalation.session_id)
            escalation.outcome = "APPROVED"
            message = f"Order #{escalation.order_id} approved. The buyer can pay now."
        else:
            adapter.human_reject(escalation.session_id)
            escalation.outcome = "REJECTED"
            message = f"Order #{escalation.order_id} rejected. No payment was taken."
    except HTTPException as exc:
        # e.g. a disallowed category, which no human may wave through.
        escalation.answered = True
        escalation.outcome = "REFUSED"
        escalation.detail = str(exc.detail)
        return {
            "ok": False,
            "outcome": "REFUSED",
            "order_id": escalation.order_id,
            "message": f"Order #{escalation.order_id} cannot be approved: {exc.detail}",
        }

    escalation.answered = True
    escalation.detail = message
    return {"ok": True, "outcome": escalation.outcome, "order_id": escalation.order_id, "message": message}


# --------------------------------------------------------------- webhook

@router.post("/webhook/sms-reply")
async def sms_reply(Body: str = Form(default=""), From: str = Form(default="")) -> PlainTextResponse:
    """Inbound SMS/WhatsApp, in Twilio's form-encoded shape.

    One number serves two different conversations -- Amma deciding an
    escalation, and a customer saying what to order instead -- because
    Twilio allows one webhook URL per number, and in a demo the same
    person is often both. So messages are routed by what they ARE, not
    only who sent them:

      1. A bare '1'/'2' while an escalation is waiting is Amma deciding.
         That reading wins, because those two characters mean nothing
         else and getting it wrong would move money.
      2. Anything else, from a number we have an open question with, is
         a customer answering it.
      3. Otherwise, fall through to the merchant path, which explains
         itself if the message made no sense.

    Replies in plain text so Twilio echoes it straight back to the sender.
    """
    import buyer_sms

    parsed = parse_reply(Body)
    waiting_escalation = _oldest_unanswered()
    buyer_asked_at = buyer_sms.open_question_asked_at(From)

    # Both a customer and the merchant can have an open question on the
    # same number, so pick using three things in order:
    #
    #   1. An explicit "#<order>" names a merchant order and wins outright.
    #   2. The message has to plausibly answer what the customer was asked
    #      -- nobody orders dinner by replying "1" to "what would you like
    #      instead?", though "1" answers "approve this?" fine.
    #   3. Otherwise the most recently asked question wins, because a
    #      person replying to their phone is answering what just arrived.
    # Plausibility and recency only matter when there is something to
    # choose BETWEEN. If the merchant has nothing outstanding, the
    # customer is the only one who could be replying.
    prefer_buyer = buyer_asked_at is not None and parsed.order_id is None and (
        waiting_escalation is None
        or (
            buyer_sms.reply_suits_open_question(From, Body)
            and buyer_asked_at > waiting_escalation.created_at
        )
    )

    if prefer_buyer:
        handled = buyer_sms.record_reply(From, Body)
        if handled:
            return PlainTextResponse(handled["message"], status_code=200)

    if parsed.action is None:
        return PlainTextResponse(
            "Sorry, I didn't understand that. Reply '1' to APPROVE or '2' to REJECT.",
            status_code=200,
        )

    result = resolve(parsed.action, parsed.order_id)
    return PlainTextResponse(result["message"], status_code=200)
