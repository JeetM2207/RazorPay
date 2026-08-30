"""WhatsApp through Meta's Cloud API, as an alternative to Twilio.

The same endpoint now accepts two wire shapes, and one of them can
approve an order and release food. So the parts that decide whether a
request is genuine, and whether it is even a reply, are pinned here.
"""

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

import app as app_module
import escalations
import reply_auth

SECRET = "test-app-secret"
VERIFY = "test-verify-token"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("META_APP_SECRET", SECRET)
    monkeypatch.setenv("META_VERIFY_TOKEN", VERIFY)
    escalations.reset()
    return TestClient(app_module.app)


def signed(body: bytes) -> dict:
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={digest}",
            "Content-Type": "application/json"}


def envelope(text: str, sender: str = "918306610707") -> dict:
    """The shape Meta actually posts for an inbound text."""
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "messaging_product": "whatsapp",
        "messages": [{"from": sender, "type": "text", "text": {"body": text}}],
    }}]}]}


# ------------------------------------------------------- the handshake

def test_the_verification_handshake_echoes_the_challenge(client):
    """Meta GETs the URL once before it will deliver anything. Get this
    wrong and the dashboard only says the callback could not be
    verified."""
    r = client.get("/webhook/sms-reply", params={
        "hub.mode": "subscribe", "hub.verify_token": VERIFY,
        "hub.challenge": "1158201444",
    })
    assert r.status_code == 200
    assert r.text == "1158201444"


def test_a_wrong_verify_token_is_refused(client):
    r = client.get("/webhook/sms-reply", params={
        "hub.mode": "subscribe", "hub.verify_token": "not-it",
        "hub.challenge": "1158201444",
    })
    assert r.status_code == 403


def test_an_unset_verify_token_refuses_rather_than_waving_through(client, monkeypatch):
    """Otherwise a deployment that forgot to configure one would verify
    anybody's subscription."""
    monkeypatch.setenv("META_VERIFY_TOKEN", "")
    r = client.get("/webhook/sms-reply", params={
        "hub.mode": "subscribe", "hub.verify_token": "", "hub.challenge": "x",
    })
    assert r.status_code == 403


# -------------------------------------------------------- the signature

def test_an_unsigned_meta_delivery_is_refused(client):
    body = json.dumps(envelope("1 4417")).encode()
    r = client.post("/webhook/sms-reply", content=body,
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 403


def test_a_tampered_body_is_refused(client):
    """The signature covers the bytes, so changing one after signing must
    fail -- this is the check that stops somebody replaying a genuine
    delivery with a different answer in it."""
    original = json.dumps(envelope("2 4417")).encode()
    headers = signed(original)
    tampered = json.dumps(envelope("1 4417")).encode()
    r = client.post("/webhook/sms-reply", content=tampered, headers=headers)
    assert r.status_code == 403


def test_an_unset_app_secret_cannot_authorise(monkeypatch):
    """A signed request that cannot be checked has not been checked."""
    monkeypatch.setenv("META_APP_SECRET", "")

    class FakeRequest:
        headers = {"X-Hub-Signature-256": "sha256=" + "0" * 64}

    assert reply_auth.meta_signature_ok(FakeRequest(), b"{}") is False


def test_a_correctly_signed_delivery_is_accepted(client):
    body = json.dumps(envelope("hello")).encode()
    r = client.post("/webhook/sms-reply", content=body, headers=signed(body))
    assert r.status_code == 200


# ----------------------------------------------- what is NOT a reply

@pytest.mark.parametrize("payload, why", [
    ({"entry": [{"changes": [{"value": {"statuses": [{"status": "delivered"}]}}]}]},
     "a delivery receipt"),
    ({"entry": [{"changes": [{"value": {"statuses": [{"status": "read"}]}}]}]},
     "a read receipt"),
    ({"entry": [{"changes": [{"value": {"messages": [
        {"from": "91", "type": "image", "image": {"id": "1"}}]}}]}]},
     "an image"),
    ({}, "an empty envelope"),
])
def test_a_status_callback_is_not_read_as_an_answer(client, payload, why):
    """Meta sends the same envelope for things that are not messages.
    Reading a "delivered" callback as an inbound "1" would approve an
    order nobody answered -- and Meta retries anything it does not get a
    200 for, so these must be acknowledged, not rejected."""
    assert escalations._from_meta_payload(payload) is None, why

    body = json.dumps(payload).encode()
    r = client.post("/webhook/sms-reply", content=body, headers=signed(body))
    assert r.status_code == 200, why


def test_a_text_message_is_read_as_one():
    got = escalations._from_meta_payload(envelope("1 4417", sender="918306610707"))
    assert got == ("1 4417", "918306610707")


# ------------------------------------------------------------ outbound

def test_meta_is_preferred_when_both_are_configured(monkeypatch):
    """Its free tier is the one that survives a demo."""
    import notification_service as ns

    # TextBee outranks Meta, so it has to be off for this to test what it
    # says. Not defaulted off in the fixture: a test that silently
    # depended on the developer's own .env is how this broke.
    monkeypatch.setattr(ns, "TEXTBEE_CONFIGURED", False)
    monkeypatch.setattr(ns, "META_CONFIGURED", True)
    monkeypatch.setattr(ns, "TWILIO_CONFIGURED", True)
    monkeypatch.setattr(ns, "_send_via_meta", lambda body, to: None)

    sent = ns.send_sms("hello", to="+918306610707")
    assert sent.transport == "meta"


def test_a_send_failure_is_recorded_on_the_message_not_raised(monkeypatch):
    """A transport failure must never break an order -- and it must not be
    swallowed silently either, which is how three failed messages once
    sat behind an order that had completed correctly."""
    import notification_service as ns

    monkeypatch.setattr(ns, "TEXTBEE_CONFIGURED", False)
    monkeypatch.setattr(ns, "META_CONFIGURED", True)
    monkeypatch.setattr(ns, "_send_via_meta",
                        lambda body, to: "131047: outside the 24 hour window")

    sent = ns.send_sms("hello", to="+918306610707")
    assert sent.transport == "meta"
    assert "24 hour window" in sent.error


def test_meta_gets_a_bare_number_and_twilio_gets_a_scheme():
    """Meta rejects `whatsapp:+91...`; Twilio requires it. One canonical
    form is stored and each transport bends it at the edge."""
    import notification_service as ns

    assert ns._meta_number("whatsapp:+918306610707") == "918306610707"
    assert ns._meta_number("+918306610707") == "918306610707"
