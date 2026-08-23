"""Asking the customer, over WhatsApp, what to order instead.

When someone tells their agent "2 pizzas" and Amma doesn't sell pizza,
the agent has three bad options and one good one. It can guess something
similar (wrong -- nobody asked for a dosa), give up silently (rude), sit
there blocking (useless if they've walked away), or go and ask them. This
module is the last one.

The customer gets a message on the number they gave at signup, listing
what IS available. Whatever they reply becomes the new request, and it
re-enters the ordinary flow from the top -- their own spending mandate,
Amma's rules, the audit trail. A WhatsApp reply is a *request*, never an
authorisation: it can propose a cart and nothing more.

Shared inbound number
---------------------
Twilio gives one webhook URL per number, and in a demo the same person is
often both buyer and merchant. So replies are routed by what they are,
not only who sent them: a bare "1" or "2" while an escalation is waiting
is a merchant decision; anything else, from a number we have an open
question with, is a customer answering. See escalations.sms_reply.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import notification_service

# Conversations expire so a reply that arrives an hour later doesn't
# silently resurrect an order the customer has long forgotten.
CONVERSATION_TTL_SECONDS = 900

_CONVERSATIONS: dict[str, "Conversation"] = {}


@dataclass
class Conversation:
    agent_id: str
    phone: str
    original_request: str
    unmatched: list[str]
    asked_at: str
    reply: str | None = None
    replied_at: str | None = None
    consumed: bool = False
    transport: str = "mock"

    def as_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "phone": self.phone,
            "original_request": self.original_request,
            "unmatched": self.unmatched,
            "asked_at": self.asked_at,
            "reply": self.reply,
            "answered": self.reply is not None,
            "consumed": self.consumed,
            "transport": self.transport,
        }


def reset() -> None:
    _CONVERSATIONS.clear()


def pending() -> list[dict]:
    return [c.as_dict() for c in _CONVERSATIONS.values()]


# ------------------------------------------------------------ phone bits

def normalise_phone(raw: str) -> str:
    """Best-effort E.164 for an Indian number typed by a human.

    Accepts '98765 43210', '+91 98765 43210', 'whatsapp:+919876543210'.
    Returns '+919876543210'. Anything already carrying a '+' and a country
    code is left alone apart from formatting.
    """
    if not raw:
        return ""
    cleaned = raw.strip()
    cleaned = re.sub(r"^whatsapp:", "", cleaned, flags=re.IGNORECASE)
    has_plus = cleaned.lstrip().startswith("+")
    digits = re.sub(r"\D", "", cleaned)
    if not digits:
        return ""

    if has_plus:
        return "+" + digits
    if len(digits) == 10:
        return "+91" + digits
    if len(digits) == 12 and digits.startswith("91"):
        return "+" + digits
    if len(digits) == 11 and digits.startswith("0"):
        return "+91" + digits[1:]
    return "+" + digits


def _same_number(a: str, b: str) -> bool:
    """Compare on the last 10 digits, so +91 prefixes and local formats
    don't cause a miss."""
    da, db = re.sub(r"\D", "", a or ""), re.sub(r"\D", "", b or "")
    return bool(da) and bool(db) and da[-10:] == db[-10:]


# ---------------------------------------------------------------- asking

def _compose(unmatched: list[str], available: list[dict], shop_name: str) -> str:
    missing = ", ".join(unmatched) if unmatched else "that"
    lines = [
        f"{item.get('title') or item['id']} Rs.{item.get('price_inr') or item.get('price')}"
        for item in available
        if item.get("agent_orderable", True)
    ][:8]
    return (
        f"{shop_name}: sorry, we don't have {missing}.\n\n"
        "Today we have:\n"
        + "\n".join(f"• {line}" for line in lines)
        + "\n\nReply with what you'd like instead, in your own words."
    )


def ask(
    agent_id: str,
    phone: str,
    original_request: str,
    unmatched: list[str],
    available: list[dict],
    shop_name: str = "Amma's Kitchen",
) -> Conversation:
    """Message the customer and open a conversation awaiting their answer."""
    normalised = normalise_phone(phone)
    if not normalised:
        raise ValueError("No usable phone number on file for this customer.")

    body = _compose(unmatched, available, shop_name)
    sent = notification_service.send_sms(body, to=normalised)

    conversation = Conversation(
        agent_id=agent_id,
        phone=normalised,
        original_request=original_request,
        unmatched=list(unmatched),
        asked_at=datetime.now(timezone.utc).isoformat(),
        transport=sent.transport,
    )
    _CONVERSATIONS[agent_id] = conversation
    return conversation


def status(agent_id: str) -> dict | None:
    conversation = _CONVERSATIONS.get(agent_id)
    return conversation.as_dict() if conversation else None


def consume(agent_id: str) -> str | None:
    """Take the reply once. Marking it consumed stops a stale answer being
    re-used if the customer deploys the agent again."""
    conversation = _CONVERSATIONS.get(agent_id)
    if not conversation or conversation.reply is None or conversation.consumed:
        return None
    conversation.consumed = True
    return conversation.reply


# --------------------------------------------------------------- replies

def has_open_question(from_phone: str) -> bool:
    return _find_open(from_phone) is not None


def _find_open(from_phone: str) -> Conversation | None:
    candidates = [
        c
        for c in _CONVERSATIONS.values()
        if c.reply is None and _same_number(c.phone, from_phone)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda c: c.asked_at)


def record_reply(from_phone: str, text: str) -> dict | None:
    """Attach an inbound message to the open question from that number.

    Returns a result dict when it belonged to a customer conversation, or
    None so the caller can fall through to the merchant path.
    """
    conversation = _find_open(from_phone)
    if conversation is None:
        return None

    cleaned = (text or "").strip()
    if not cleaned:
        return {
            "handled": True,
            "agent_id": conversation.agent_id,
            "message": "Sorry, I didn't catch that. What would you like to order?",
        }

    conversation.reply = cleaned
    conversation.replied_at = datetime.now(timezone.utc).isoformat()
    return {
        "handled": True,
        "agent_id": conversation.agent_id,
        "message": f"Got it — ordering \"{cleaned}\" now. Watch your screen.",
    }
