"""Outbound alerts to Amma, with a mock transport by default.

If TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM / MERCHANT_PHONE
are all set, messages go out over Twilio for real. Otherwise they land in
an in-memory outbox that the merchant console renders, so the whole
escalation loop is demoable offline, and tests never touch the network.

Sending must never be able to break an order. A transport failure is
recorded and swallowed: the escalation still sits in the merchant queue
and the web console remains a complete way to resolve it. SMS is a faster
way to reach Amma, never the only way.

This module knows nothing about negotiation. It formats and delivers.
"""

import os
from dataclasses import dataclass
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
_FROM = os.environ.get("TWILIO_FROM", "")
MERCHANT_PHONE = os.environ.get("MERCHANT_PHONE", "")

# Flipping this off forces the mock transport even with credentials
# present -- useful for a dry run before a live demo.
SMS_ENABLED = os.environ.get("SMS_ENABLED", "true").lower() not in ("false", "0", "no")

TWILIO_CONFIGURED = bool(SMS_ENABLED and _ACCOUNT_SID and _AUTH_TOKEN and _FROM and MERCHANT_PHONE)


@dataclass
class SentMessage:
    to: str
    body: str
    transport: str          # "twilio" | "mock"
    sent_at: str
    error: str | None = None


_OUTBOX: list[SentMessage] = []


def outbox(limit: int = 20) -> list[dict]:
    """Most recent first. This is what the merchant console shows when
    there's no real phone in the loop."""
    return [
        {
            "to": m.to,
            "body": m.body,
            "transport": m.transport,
            "sent_at": m.sent_at,
            "error": m.error,
        }
        for m in list(reversed(_OUTBOX))[:limit]
    ]


def clear_outbox() -> None:
    _OUTBOX.clear()


def format_escalation_alert(
    order_id: int, agent_id: str, cart: list[tuple[str, int]], total_inr: int, reason: str
) -> str:
    items = ", ".join(f"{qty}x {name.replace('_', ' ').title()}" for name, qty in cart)
    return (
        "[Amma's Kitchen AI Alert]\n"
        f"Order #{order_id} from {agent_id}:\n"
        f"Items: {items} (Rs.{total_inr})\n"
        f"Reason: {reason}.\n"
        "Reply '1' to APPROVE, '2' to REJECT."
    )


def _match_channel(recipient: str) -> str:
    """Twilio addresses WhatsApp as `whatsapp:+91...`. If the sender is a
    WhatsApp address, the recipient has to be one too.

    WhatsApp matters for India specifically: sending SMS to Indian numbers
    requires TRAI/DLT sender registration, which takes days and business
    paperwork. Twilio's WhatsApp Sandbox needs neither -- you join it by
    texting a code -- so it is the realistic channel for a demo here.
    """
    if _FROM.startswith("whatsapp:") and not recipient.startswith("whatsapp:"):
        return f"whatsapp:{recipient}"
    return recipient


def send_sms(body: str, to: str | None = None) -> SentMessage:
    recipient = to or MERCHANT_PHONE or "+91-merchant-mock"
    now = datetime.now(timezone.utc).isoformat()

    if not TWILIO_CONFIGURED:
        message = SentMessage(recipient, body, "mock", now)
        _OUTBOX.append(message)
        return message

    try:
        from twilio.rest import Client

        Client(_ACCOUNT_SID, _AUTH_TOKEN).messages.create(
            body=body, from_=_FROM, to=_match_channel(recipient)
        )
        message = SentMessage(recipient, body, "twilio", now)
    except Exception as exc:  # a transport failure must not break an order
        message = SentMessage(recipient, body, "twilio", now, error=str(exc))

    _OUTBOX.append(message)
    return message
