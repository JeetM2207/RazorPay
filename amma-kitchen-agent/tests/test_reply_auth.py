"""Authenticating /webhook/sms-reply.

This endpoint is a money action. A reply of "1" from a number whose last
ten digits match a pending escalation approves a merchant order and
releases food. The Razorpay webhook has verified signatures since it was
built; this one accepted any POST from anyone, which made it the one
place in the project where an unauthenticated stranger could move an
order.

Two doors, and the tests below exist to prove there is no third.
"""

import base64
import hashlib
import hmac

import pytest

import merchant_auth
from fastapi.testclient import TestClient

import audit_log
import escalations
import reply_auth

AUTH_TOKEN = "test_twilio_auth_token_0123456789"
INTERNAL = "test_internal_reply_token_abcdef"

URL = "http://testserver/webhook/sms-reply"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setenv("INTERNAL_REPLY_TOKEN", INTERNAL)
    escalations.reset()
    import app
    client = TestClient(app.app)
    # These tests drive merchant surfaces, which now need a login. The
    # session is minted directly rather than posted through the form,
    # because the login itself is what tests/test_merchant_auth.py is
    # for -- and leaving these anonymous would only prove the guard
    # fires, which is already covered there.
    client.cookies.set(merchant_auth.COOKIE_NAME, merchant_auth.issue_cookie())
    return client


@pytest.fixture
def waiting(client):
    """One escalation on the queue, so an accepted reply has something to
    move and a rejected one has something it demonstrably did not."""
    escalations.notify("acp", "sess-auth", {
        "event_id": 4242, "agent_id": "agent-auth", "total_inr": 440,
        "reason": "total Rs.440 at/above human confirmation threshold Rs.400",
    }, [("veg_thali", 2)], send=False)
    return escalations._oldest_unanswered()


def sign(params: dict, url: str = URL, token: str = AUTH_TOKEN) -> str:
    """Twilio's own scheme, written out independently of the code under
    test rather than by calling it -- a test that reuses the
    implementation to build its input proves only that the function is
    self-consistent."""
    payload = url + "".join(k + params[k] for k in sorted(params))
    digest = hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def post(client, params, headers=None):
    return client.post("/webhook/sms-reply", data=params, headers=headers or {})


# ------------------------------------------------------------- the doors

def test_a_valid_twilio_signature_is_accepted(client, waiting):
    params = {"Body": "1", "From": "+919876543210"}
    resp = post(client, params, {"X-Twilio-Signature": sign(params)})
    assert resp.status_code == 200


def test_a_valid_internal_token_is_accepted(client, waiting):
    """The console reply boxes are deliberately not Twilio-signed: the
    mock path has to exercise the real handler, or it proves nothing."""
    resp = post(client, {"Body": "1", "From": "+91-merchant-console"},
                {"X-Internal-Reply-Token": INTERNAL})
    assert resp.status_code == 200


def test_no_header_at_all_is_rejected(client, waiting):
    resp = post(client, {"Body": "1", "From": "+919876543210"})
    assert resp.status_code == 403
    assert waiting.answered is False, "an unauthenticated POST resolved an escalation"


def test_a_tampered_body_is_rejected_and_resolves_nothing(client, waiting):
    """Sign one message, send another. This is the actual attack: the
    signature is real, and it is not a signature for THIS body."""
    signature = sign({"Body": "2", "From": "+919876543210"})
    resp = post(client, {"Body": "1", "From": "+919876543210"},
                {"X-Twilio-Signature": signature})
    assert resp.status_code == 403
    assert waiting.answered is False
    assert waiting.outcome is None


def test_a_replayed_signature_against_a_different_body_is_rejected(client, waiting):
    """A signature captured off an earlier genuine reply cannot be reused
    to say something else."""
    genuine = {"Body": "2", "From": "+919876543210"}
    captured = sign(genuine)
    assert post(client, genuine, {"X-Twilio-Signature": captured}).status_code == 200

    escalations.reset()
    escalations.notify("acp", "s2", {
        "event_id": 99, "agent_id": "a2", "total_inr": 440, "reason": "over threshold",
    }, [("veg_thali", 1)], send=False)
    replayed = post(client, {"Body": "1", "From": "+919876543210"},
                    {"X-Twilio-Signature": captured})
    assert replayed.status_code == 403
    assert escalations._oldest_unanswered().answered is False


def test_a_wrong_internal_token_is_rejected(client, waiting):
    resp = post(client, {"Body": "1", "From": "+919876543210"},
                {"X-Internal-Reply-Token": "not-the-token"})
    assert resp.status_code == 403
    assert waiting.answered is False


def test_a_signature_from_the_wrong_auth_token_is_rejected(client, waiting):
    params = {"Body": "1", "From": "+919876543210"}
    resp = post(client, params, {"X-Twilio-Signature": sign(params, token="someone-elses")})
    assert resp.status_code == 403


# ------------------------------------------------- behind a tunnel

def test_forwarded_headers_reconstruct_the_url_twilio_signed(client, waiting):
    """The one that breaks everything if it is wrong.

    Twilio signs the address it SENT to. Behind ngrok that is
    https://<domain>.ngrok-free.dev/webhook/sms-reply, while request.url
    reads http://testserver/... -- the internal address the tunnel
    forwarded to. Sign the wrong one and every genuine reply 403s, which
    on a demo looks like Twilio being broken.
    """
    public = "https://amma.ngrok-free.dev/webhook/sms-reply"
    params = {"Body": "1", "From": "+919876543210"}
    resp = post(client, params, {
        "X-Twilio-Signature": sign(params, url=public),
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Host": "amma.ngrok-free.dev",
    })
    assert resp.status_code == 200, "a correctly signed tunnelled reply was rejected"


def test_without_forwarded_headers_the_internal_url_is_used(client, waiting):
    """The reconstruction must not fire when there is no proxy, or direct
    requests would start failing instead."""
    params = {"Body": "1", "From": "+919876543210"}
    assert post(client, params, {"X-Twilio-Signature": sign(params)}).status_code == 200


def test_a_signature_for_the_internal_url_fails_when_forwarded(client, waiting):
    """The mirror of the case above: signing the address the tunnel
    forwarded TO is not a valid signature for the address it came in on."""
    params = {"Body": "1", "From": "+919876543210"}
    resp = post(client, params, {
        "X-Twilio-Signature": sign(params, url=URL),
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Host": "amma.ngrok-free.dev",
    })
    assert resp.status_code == 403


def test_a_forwarded_header_list_takes_the_first_hop():
    """Proxies append, so the value can be "https,http". The client's own
    scheme is the first entry."""
    class _Req:
        headers = {"x-forwarded-proto": "https,http", "x-forwarded-host": "a.example, b"}
        class url:
            scheme, netloc, path, query = "http", "127.0.0.1:8000", "/webhook/sms-reply", ""
            def __str__(self): return "http://127.0.0.1:8000/webhook/sms-reply"
        url = url()

    assert reply_auth.public_url(_Req()) == "https://a.example/webhook/sms-reply"


# ------------------------------------------ a missing token means reject

def test_an_unset_auth_token_rejects_rather_than_waves_through(client, waiting, monkeypatch):
    """A signed request we cannot check is a request we have not
    checked."""
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    params = {"Body": "1", "From": "+919876543210"}
    resp = post(client, params, {"X-Twilio-Signature": sign(params)})
    assert resp.status_code == 403
    assert waiting.answered is False


def test_a_missing_auth_token_is_warned_about_at_startup(monkeypatch):
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    warnings = " ".join(reply_auth.warn_if_misconfigured())
    assert "TWILIO_AUTH_TOKEN" in warnings
    assert "REJECTED" in warnings


# ------------------------------------------------- there is no third path

def test_disabling_sms_does_not_open_the_endpoint(client, waiting, monkeypatch):
    """The bypass this must never grow. A verification skip keyed on a
    config flag is a bypass an attacker gets by reading the repo."""
    monkeypatch.setenv("SMS_ENABLED", "false")
    import notification_service
    monkeypatch.setattr(notification_service, "TWILIO_CONFIGURED", False)

    resp = post(client, {"Body": "1", "From": "+919876543210"})
    assert resp.status_code == 403
    assert waiting.answered is False


def test_the_source_contains_no_sms_enabled_escape_hatch():
    """Asserted on the source because the behavioural test above only
    covers the flag as it exists today; this catches a new one."""
    source = open(reply_auth.__file__, encoding="utf-8").read()
    body = source[source.index("def authorise"):]
    for hatch in ("SMS_ENABLED", "TWILIO_CONFIGURED", "DEBUG", "TESTING"):
        assert hatch not in body, f"authorise() branches on {hatch}"


def test_secrets_are_compared_in_constant_time():
    """`==` on a secret leaks its prefix through timing."""
    source = open(reply_auth.__file__, encoding="utf-8").read()
    assert source.count("compare_digest") >= 2
    for bad in ("supplied == expected", "supplied == internal_token()"):
        assert bad not in source


# ---------------------------------------------- a refusal writes nothing

def test_a_rejected_request_writes_zero_audit_rows(client, waiting):
    before = len(audit_log.get_all_events(db_path=audit_log.DEFAULT_DB_PATH, limit=500))
    for headers in ({}, {"X-Twilio-Signature": "AAAA"}, {"X-Internal-Reply-Token": "nope"}):
        assert post(client, {"Body": "1", "From": "+919876543210"}, headers).status_code == 403
    after = len(audit_log.get_all_events(db_path=audit_log.DEFAULT_DB_PATH, limit=500))
    assert after == before, "a refused reply reached the audit log"


def test_a_rejected_request_sends_no_message(client, waiting):
    import notification_service
    notification_service.clear_outbox()
    assert post(client, {"Body": "1", "From": "+919876543210"}).status_code == 403
    assert notification_service.outbox() == []


# ------------------------------------------ the consoles get their token

def test_the_console_pages_are_served_with_the_credential(client):
    for path in ("/buyer/order", "/merchant/orders"):
        body = client.get(path).text
        assert "__INTERNAL_REPLY_TOKEN__" not in body, f"{path} shipped the placeholder"
        assert INTERNAL in body, f"{path} was served without the token"


def test_the_console_pages_are_not_cached(client):
    """A page carrying a credential must not sit in the disk cache."""
    for path in ("/buyer/order", "/merchant/orders"):
        assert client.get(path).headers["cache-control"] == "no-store"


def test_no_endpoint_hands_the_token_out(client):
    """The credential is stamped into the page, never fetchable. An
    endpoint that returns it to whoever asks is not a credential."""
    import app
    for route in app.app.routes:
        path = getattr(route, "path", "")
        if path.startswith("/api/") and "{" not in path and "GET" in getattr(route, "methods", ()):
            body = client.get(path).text
            assert INTERNAL not in body, f"{path} leaks the internal reply token"
