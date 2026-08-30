"""SMS through TextBee: your own Android phone as the gateway.

Chosen over Twilio and Meta for one reason specific to where this is
being demoed. A2P SMS to Indian numbers needs TRAI/DLT sender
registration; a text your own handset sends is person-to-person and
needs none of it. It also has no 24-hour window, which is a WhatsApp
platform rule rather than a provider one.

A reply here approves an order and releases food, so the parts deciding
whether a request is genuine -- and whether it is even a reply -- are
pinned.
"""

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

import app as app_module
import escalations
import reply_auth

SECRET = "textbee-webhook-secret"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("TEXTBEE_WEBHOOK_SECRET", SECRET)
    escalations.reset()
    return TestClient(app_module.app)


def signed(body: bytes) -> dict:
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Signature": digest, "Content-Type": "application/json"}


def received(text: str, sender: str = "+918306610707") -> dict:
    """The shape TextBee posts for an inbound SMS."""
    return {
        "event": "MESSAGE_RECEIVED",
        "timestamp": "2026-08-31T10:30:00Z",
        "data": {
            "_id": "abc123", "sender": sender, "message": text,
            "receivedAt": "2026-08-31T10:30:00Z",
            "device": {"_id": "device123", "enabled": True, "model": "Pixel 7"},
        },
    }


# ------------------------------------------------------------ reading it

def test_an_inbound_sms_is_read_as_a_reply():
    assert escalations._from_textbee_payload(received("1 4417")) == (
        "1 4417", "+918306610707")


@pytest.mark.parametrize("payload, why", [
    ({"event": "MESSAGE_SENT", "data": {"sender": "+91", "message": "1"}},
     "a send notification is not an answer"),
    ({"event": "MESSAGE_RECEIVED", "data": {"sender": "+91"}},
     "no message text"),
    ({"event": "MESSAGE_RECEIVED", "data": {"message": "1 4417"}},
     "no sender"),
    ({}, "an empty envelope"),
])
def test_anything_that_is_not_a_received_message_is_ignored(payload, why):
    """Reading a delivery notification as an inbound "1" would approve an
    order nobody replied to."""
    assert escalations._from_textbee_payload(payload) is None, why


def test_a_non_reply_event_is_acknowledged_not_rejected(client):
    """TextBee retries anything it does not get a 200 for, so an event we
    ignore still has to be accepted."""
    body = json.dumps({"event": "MESSAGE_SENT", "data": {}}).encode()
    r = client.post("/webhook/sms-reply", content=body, headers=signed(body))
    assert r.status_code == 200


# ------------------------------------------------------------ proving it

def test_an_unsigned_delivery_is_refused(client):
    body = json.dumps(received("1 4417")).encode()
    r = client.post("/webhook/sms-reply", content=body,
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 403


def test_a_tampered_body_is_refused(client):
    """Signed "2 4417", delivered "1 4417" -- the difference between
    rejecting an order and approving it."""
    original = json.dumps(received("2 4417")).encode()
    headers = signed(original)
    tampered = json.dumps(received("1 4417")).encode()
    r = client.post("/webhook/sms-reply", content=tampered, headers=headers)
    assert r.status_code == 403


def test_a_correctly_signed_delivery_is_accepted(client):
    body = json.dumps(received("hello")).encode()
    r = client.post("/webhook/sms-reply", content=body, headers=signed(body))
    assert r.status_code == 200


def test_an_unset_secret_cannot_authorise(monkeypatch):
    monkeypatch.setenv("TEXTBEE_WEBHOOK_SECRET", "")

    class FakeRequest:
        headers = {"X-Signature": "0" * 64}

    assert reply_auth.textbee_signature_ok(FakeRequest(), b"{}") is False


def test_a_sha256_prefix_is_tolerated(monkeypatch):
    """Signed the same either way, in case the prefix is ever added."""
    monkeypatch.setenv("TEXTBEE_WEBHOOK_SECRET", SECRET)
    body = b'{"event":"MESSAGE_RECEIVED"}'
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()

    class FakeRequest:
        headers = {"X-Signature": f"sha256={digest}"}

    assert reply_auth.textbee_signature_ok(FakeRequest(), body) is True


# ------------------------------------------------------------- outbound

def test_textbee_is_preferred_over_the_others(monkeypatch):
    """Nothing sits between the message and the handset: no DLT
    registration, and no 24-hour window to fall outside of."""
    import notification_service as ns

    monkeypatch.setattr(ns, "TEXTBEE_CONFIGURED", True)
    monkeypatch.setattr(ns, "META_CONFIGURED", True)
    monkeypatch.setattr(ns, "TWILIO_CONFIGURED", True)
    monkeypatch.setattr(ns, "_send_via_textbee", lambda body, to: None)

    assert ns.send_sms("hi", to="+918306610707").transport == "textbee"


def test_a_send_failure_is_recorded_rather_than_raised(monkeypatch):
    """The phone being off is a normal Tuesday, and it must not break an
    order -- but it must not be silent either."""
    import notification_service as ns

    monkeypatch.setattr(ns, "TEXTBEE_CONFIGURED", True)
    monkeypatch.setattr(ns, "_send_via_textbee",
                        lambda body, to: "400: device is offline")

    sent = ns.send_sms("hi", to="+918306610707")
    assert sent.transport == "textbee"
    assert "offline" in sent.error


def test_the_whatsapp_scheme_is_stripped_for_a_plain_text():
    """The number is stored one way; each transport bends it at the edge.
    TextBee is sending an SMS, not addressing a WhatsApp identity."""
    import notification_service as ns

    assert ns._plain_number("whatsapp:+918306610707") == "+918306610707"
    assert ns._plain_number("918306610707") == "+918306610707"
    assert ns._plain_number("+918306610707") == "+918306610707"
