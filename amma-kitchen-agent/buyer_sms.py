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

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import notification_service
import reply_codes

# Conversations expire so a reply that arrives an hour later doesn't
# silently resurrect an order the customer has long forgotten. This is
# reply_codes' clock rather than a second one: the code and the question
# it belongs to have to die together, or a stale code outlives the thing
# it was guarding.
CONVERSATION_TTL_SECONDS = reply_codes.TTL_SECONDS

_CONVERSATIONS: dict[str, "Conversation"] = {}


log = logging.getLogger(__name__)

SUBSTITUTE = "substitute"   # "we don't sell that -- what instead?"
APPROVAL = "approval"       # "this is over your soft cap -- proceed?"


@dataclass
class Conversation:
    agent_id: str
    phone: str
    original_request: str
    unmatched: list[str]
    asked_at: str
    kind: str = SUBSTITUTE
    reply: str | None = None
    replied_at: str | None = None
    consumed: bool = False
    transport: str = "mock"
    # Single-use code, sent in the message and required back. Without it
    # a reply is authenticated only by caller ID, which is spoofable.
    code: str = ""
    # Only meaningful for an APPROVAL question: True/False once answered.
    decision: bool | None = None
    # Which standing order asked, when one did. A soft-cap approval is
    # picked up by the deploy() that is already running and polling for
    # it; a routine's is not, because the entire point of a routine is
    # that nobody is running anything. Without this the reply was
    # recorded, answered "going ahead with your order now", and went
    # nowhere -- the message was simply untrue.
    routine_id: str | None = None
    # The message as sent. Kept because the buyer console has to be able
    # to show the question to somebody who was not watching when it was
    # asked -- and the REASON lives in this text and nowhere else. A
    # standing order held back by a price change says so here.
    question: str = ""

    def as_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "phone": self.phone,
            "kind": self.kind,
            "original_request": self.original_request,
            "unmatched": self.unmatched,
            "asked_at": self.asked_at,
            "reply": self.reply,
            "decision": self.decision,
            "answered": self.reply is not None,
            "consumed": self.consumed,
            "transport": self.transport,
            # The buyer console renders this beside the message so the
            # mock path stays usable: the customer reads the code off the
            # screen exactly as they would off their phone.
            "code": self.code,
            "question": self.question,
            "routine_id": self.routine_id,
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

def _compose(unmatched: list[str], available: list[dict], shop_name: str,
             code: str = "") -> str:
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
        + f"\n\nReply with what you'd like instead, starting with the code {code}."
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

    code = reply_codes.new_code()
    body = _compose(unmatched, available, shop_name, code)
    sent = notification_service.send_sms(body, to=normalised, audience="customer")

    conversation = Conversation(
        agent_id=agent_id,
        phone=normalised,
        original_request=original_request,
        unmatched=list(unmatched),
        asked_at=datetime.now(timezone.utc).isoformat(),
        transport=sent.transport,
        code=code,
        question=body,
    )
    _CONVERSATIONS[agent_id] = conversation
    return conversation


def ask_approval(
    agent_id: str,
    phone: str,
    cart_label: str,
    total_inr: int,
    soft_cap_inr: int,
    shop_name: str = "Amma's Kitchen",
    why: str | None = None,
    routine_id: str | None = None,
) -> Conversation:
    """Ask the customer to approve an order they have not authorised.

    Deliberately worded YES/NO rather than 1/2. The merchant's escalation
    messages use 1/2, and on a shared number distinct vocabularies make
    an ambiguous reply far less likely -- see the routing note at the top.

    `why` exists because the soft cap is not the only thing that can make
    an order need asking about. A standing order is stopped by its own
    confidence gate, and that gate's reason is usually not the amount at
    all -- it can be the clock, a price that moved, or a dish the merchant
    has stopped selling. Sending the soft-cap sentence in those cases
    states a reason that is not true and points the customer at the wrong
    thing. Callers genuinely asking about the soft cap pass nothing and
    get the original wording unchanged.
    """
    normalised = normalise_phone(phone)
    if not normalised:
        raise ValueError("No usable phone number on file for this customer.")

    code = reply_codes.new_code()
    body = (
        f"{shop_name}: your agent wants to order {cart_label} for Rs.{total_inr}.\n\n"
        + (
            f"{why}\n\n" if why
            else f"That's above the Rs.{soft_cap_inr} you asked to be checked on.\n\n"
        )
        + f"Reply  YES {code}  to go ahead, or  NO {code}  to cancel."
    )
    sent = notification_service.send_sms(body, to=normalised, audience="customer")

    conversation = Conversation(
        agent_id=agent_id,
        phone=normalised,
        original_request=cart_label,
        unmatched=[],
        asked_at=datetime.now(timezone.utc).isoformat(),
        kind=APPROVAL,
        transport=sent.transport,
        code=code,
        question=body,
        routine_id=routine_id,
    )
    _CONVERSATIONS[agent_id] = conversation
    return conversation


_YES = re.compile(r"^\s*(y|yes|yeah|yep|ok|okay|approve[d]?|accept|go|go ahead|sure|1)\s*[.!]?\s*$", re.I)
_NO = re.compile(r"^\s*(n|no|nope|cancel|stop|reject|decline[d]?|don'?t|2)\s*[.!]?\s*$", re.I)


def parse_approval(text: str) -> bool | None:
    """Strictly yes or no. Anything else is unparseable rather than
    guessed at -- this answer authorises a charge."""
    if not text:
        return None
    if _YES.match(text):
        return True
    if _NO.match(text):
        return False
    return None


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


def open_question_asked_at(from_phone: str) -> str | None:
    """When the still-unanswered question to this number was sent, so the
    router can tell which of several open questions is the newest."""
    conversation = _find_open(from_phone)
    return conversation.asked_at if conversation else None


# "1", and now also "1 4417" -- a merchant decision carries its code, and
# without the optional code here a coded decision would stop looking like
# one and get routed to the customer instead.
_BARE_DIGIT = re.compile(r"^\s*[12]\s*[:,\-]?\s*(?:\d{4})?\s*[.!]?\s*$")


def reply_suits_open_question(from_phone: str, text: str) -> bool:
    """Whether this message plausibly answers the question we asked.

    A bare "1" answers "approve this order?" perfectly well, but nobody
    orders dinner by replying "1" to "what would you like instead?". When
    it doesn't suit, the router lets the merchant path have it -- which
    is also the safer way to be wrong, since misrouting a merchant's
    approval merely leaves it pending, while misrouting the other way
    would approve an order nobody confirmed.
    """
    conversation = _find_open(from_phone)
    if conversation is None:
        return False
    if conversation.kind == APPROVAL:
        return True
    return not _BARE_DIGIT.match(text or "")


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

    # The code first, before either branch acts. Caller ID is spoofable
    # and _same_number matches on the last ten digits by design, so this
    # is what actually proves the reply came from the person we asked.
    # Expiry counts as wrong: reply_codes owns the one clock, so a code
    # cannot outlive the question it was guarding.
    import hmac

    supplied = reply_codes.extract(text)
    valid = bool(
        supplied
        and conversation.code
        and not reply_codes.is_expired(conversation.asked_at)
        and hmac.compare_digest(supplied, conversation.code)
    )
    if not valid:
        # None, not a refusal. On a shared number this message may well
        # be the MERCHANT's -- "1 4417" carries a code that is simply not
        # this conversation's -- and claiming it here would swallow a
        # valid decision. Falling through lets the merchant path try its
        # own code; if that fails too, the router answers once and counts
        # it against the rate limit, so there is still exactly one place
        # keeping score.
        return None

    # Consumed here rather than at the end of each branch, so a replay
    # cannot act a second time whichever branch it takes. _find_open
    # already skips answered conversations; this closes the case where
    # the same code arrives twice before the first reply is stored.
    conversation.code = ""

    # Strip the code out before the rest is read as an order: "4417 2
    # dosas" is an order for two dosas, not for 4417 of something.
    cleaned = reply_codes.strip(text)

    if conversation.kind == APPROVAL:
        decision = parse_approval(cleaned)
        if decision is None:
            return {
                "handled": True,
                "agent_id": conversation.agent_id,
                "message": "Sorry, I didn't understand. Reply YES to go ahead, or NO to cancel.",
            }
        conversation.reply = cleaned
        conversation.decision = decision
        conversation.replied_at = datetime.now(timezone.utc).isoformat()

        # A standing order has nobody waiting to act on the answer. A
        # soft-cap approval does -- the deploy() that asked is polling
        # for it -- so only the routine case is placed from here, and
        # placing it is what makes "going ahead" true.
        if conversation.routine_id:
            import routines                      # local: routines imports this module

            try:
                routines.confirm_pending(conversation.routine_id, approved=decision)
            except Exception as exc:
                log.error("routine %s could not be actioned after a reply: %s",
                          conversation.routine_id, exc)
                return {
                    "handled": True,
                    "agent_id": conversation.agent_id,
                    "message": "Got your answer, but the order could not be placed. "
                               "Nothing has been charged.",
                }

        return {
            "handled": True,
            "agent_id": conversation.agent_id,
            "message": (
                "Thanks — going ahead with your order now."
                if decision
                else "Cancelled. Nothing has been charged."
            ),
        }

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
