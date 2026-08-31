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

import merchants
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

# ---------------------------------------------------------------- Meta
# WhatsApp Cloud API, straight from Meta rather than through a reseller.
# Worth having as an alternative because the free tier suits a demo far
# better than a Twilio trial: Meta hands you a test sender number, and it
# messages up to five recipient numbers you verify, with no daily cap and
# no sandbox to re-join.
#
# It does NOT escape WhatsApp's 24-hour rule -- free-form messages are
# only deliverable within 24h of the recipient's last message to you.
# That rule belongs to the WhatsApp platform, not to any one provider,
# and it follows you everywhere.
_META_PHONE_ID = os.environ.get("META_PHONE_NUMBER_ID", "")
_META_TOKEN = os.environ.get("META_ACCESS_TOKEN", "")
_META_API = os.environ.get("META_API_VERSION", "v21.0")

META_CONFIGURED = bool(SMS_ENABLED and _META_PHONE_ID and _META_TOKEN and MERCHANT_PHONE)

# ------------------------------------------------------------- TextBee
# Your own Android phone as the gateway: the app on the handset sends the
# message over its own SIM, and this posts to their API to ask it to.
#
# It is the best fit of the three for this project, for one reason that
# is specific to India. A2P SMS to Indian numbers needs TRAI/DLT sender
# registration -- days of business paperwork -- which is why this project
# used WhatsApp at all. DLT governs commercial routes through operators
# and aggregators; a text your own handset sends is an ordinary
# person-to-person message and needs none of it.
#
# It also has no 24-hour window. That rule belongs to the WhatsApp
# platform and has been the thing quietly dropping messages here; plain
# SMS simply arrives.
#
# The costs of that trade, stated plainly: it is SMS, so no formatting;
# it comes from your own number rather than a business identity; and the
# phone has to be on, in signal, with the app running.
_TEXTBEE_KEY = os.environ.get("TEXTBEE_API_KEY", "")
_TEXTBEE_DEVICE = os.environ.get("TEXTBEE_DEVICE_ID", "")
_TEXTBEE_API = os.environ.get(
    "TEXTBEE_API_BASE", "https://api.textbee.dev/api/v1")

TEXTBEE_CONFIGURED = bool(SMS_ENABLED and _TEXTBEE_KEY and _TEXTBEE_DEVICE
                          and MERCHANT_PHONE)


@dataclass
class SentMessage:
    to: str
    body: str
    transport: str          # "textbee" | "meta" | "twilio" | "mock"
    sent_at: str
    error: str | None = None
    # Who the message was written FOR. The two sides are asked different
    # questions in deliberately different vocabularies -- 1/2 for the
    # merchant, YES/NO for the customer -- and in a demo they are often
    # the same phone number, so the recipient cannot tell them apart. A
    # console that shows one side's message next to the other side's
    # buttons invites a reply that answers nothing.
    audience: str = "merchant"


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
            "audience": m.audience,
        }
        for m in list(reversed(_OUTBOX))[:limit]
    ]


def clear_outbox() -> None:
    _OUTBOX.clear()


def format_escalation_alert(
    order_id: int, agent_id: str, cart: list[tuple[str, int]], total_inr: int,
    reason: str, code: str = ""
) -> str:
    """The alert Amma reads on her phone.

    The last line is the whole point of the code: this message is the
    only place it appears, and a reply without it does not act. It is
    spaced apart from the digit rather than run together, because
    "1 4417" is what she has to type and "14417" is what she would type
    if the message ran them into each other.
    """
    items = ", ".join(f"{qty}x {name.replace('_', ' ').title()}" for name, qty in cart)
    instruction = (
        f"Reply  1 {code}  to APPROVE  or  2 {code}  to REJECT."
        if code else
        "Reply '1' to APPROVE, '2' to REJECT."
    )
    return (
        f"[{merchants.Platform.name} alert]\n"
        f"Order #{order_id} from {agent_id}:\n"
        f"Items: {items} (Rs.{total_inr})\n"
        f"Reason: {reason}.\n"
        + instruction
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


def _meta_number(recipient: str) -> str:
    """Meta wants a bare E.164 number with no `whatsapp:` scheme and no
    leading `+`. Twilio wants the opposite. The rest of this project
    stores one canonical form and each transport bends it here."""
    return recipient.replace("whatsapp:", "").replace("+", "").strip()


def _send_via_meta(body: str, recipient: str) -> str | None:
    """POST one text message. Returns an error string, or None on success.

    A non-2xx is turned into an error rather than raised, for the same
    reason the Twilio path swallows exceptions: a transport failure must
    never break an order. The failure is recorded ON the message so it is
    visible instead of silent -- a lesson this project learned when a
    customer's order completed correctly and three WhatsApps failed.
    """
    import requests

    try:
        response = requests.post(
            f"https://graph.facebook.com/{_META_API}/{_META_PHONE_ID}/messages",
            headers={"Authorization": f"Bearer {_META_TOKEN}"},
            json={
                "messaging_product": "whatsapp",
                "to": _meta_number(recipient),
                "type": "text",
                "text": {"preview_url": False, "body": body},
            },
            timeout=20,
        )
    except Exception as exc:
        return str(exc)

    if response.status_code >= 300:
        # Meta's errors are genuinely useful -- "Recipient phone number not
        # in allowed list" and "message outside the 24 hour window" are
        # both things you want to read, not a bare status code.
        try:
            detail = response.json()["error"]["message"]
        except Exception:
            detail = response.text[:200]
        return f"{response.status_code}: {detail}"
    return None


def _plain_number(recipient: str) -> str:
    """TextBee wants an ordinary E.164 number -- it is sending a text, not
    addressing a WhatsApp identity, so the `whatsapp:` scheme has to go."""
    number = recipient.replace("whatsapp:", "").strip()
    return number if number.startswith("+") else f"+{number}"


def _send_via_textbee(body: str, recipient: str) -> str | None:
    """Ask the phone to send one SMS. Returns an error string, or None.

    Like every other transport here it returns the failure rather than
    raising it: a transport failure must never break an order, and it
    must not vanish either -- it is recorded on the message so a screen
    can show it.
    """
    import requests

    try:
        response = requests.post(
            f"{_TEXTBEE_API}/gateway/devices/{_TEXTBEE_DEVICE}/send-sms",
            headers={"x-api-key": _TEXTBEE_KEY},
            json={"recipients": [_plain_number(recipient)], "message": body},
            timeout=20,
        )
    except Exception as exc:
        return str(exc)

    if response.status_code >= 300:
        # Worth surfacing verbatim: "device is offline" and "daily quota
        # reached" are both things you want to read on the console rather
        # than guess at from a status code.
        try:
            detail = response.json().get("error") or response.json().get("message")
        except Exception:
            detail = response.text[:200]
        return f"{response.status_code}: {detail}"
    return None


def send_sms(body: str, to: str | None = None, audience: str = "merchant") -> SentMessage:
    """`audience` is "merchant" or "customer": who this text is asking.

    Defaults to the merchant because the default recipient is hers -- a
    send with no `to` goes to MERCHANT_PHONE.

    Order of preference is TextBee, then Meta, then Twilio, then the mock
    outbox. TextBee first because it is the only one of the three with
    nothing between the message and the handset: no DLT registration, and
    no 24-hour window to fall outside of.
    """
    recipient = to or MERCHANT_PHONE or "+91-merchant-mock"
    now = datetime.now(timezone.utc).isoformat()

    if TEXTBEE_CONFIGURED:
        error = _send_via_textbee(body, recipient)
        message = SentMessage(recipient, body, "textbee", now,
                              error=error, audience=audience)
        _OUTBOX.append(message)
        return message

    if META_CONFIGURED:
        error = _send_via_meta(body, recipient)
        message = SentMessage(recipient, body, "meta", now,
                              error=error, audience=audience)
        _OUTBOX.append(message)
        return message

    if not TWILIO_CONFIGURED:
        message = SentMessage(recipient, body, "mock", now, audience=audience)
        _OUTBOX.append(message)
        return message

    try:
        from twilio.rest import Client

        Client(_ACCOUNT_SID, _AUTH_TOKEN).messages.create(
            body=body, from_=_FROM, to=_match_channel(recipient)
        )
        message = SentMessage(recipient, body, "twilio", now, audience=audience)
    except Exception as exc:  # a transport failure must not break an order
        message = SentMessage(recipient, body, "twilio", now, error=str(exc), audience=audience)

    _OUTBOX.append(message)
    return message
