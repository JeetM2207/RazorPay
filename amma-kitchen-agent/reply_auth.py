"""Who is allowed to POST an inbound reply.

`/webhook/sms-reply` is a money action. A reply of "1" from a number
whose last ten digits match a pending escalation approves a merchant
order and releases food. Razorpay's webhook has been signature-verified
since it was built; this one was not, which made it the one endpoint in
the project where an unauthenticated stranger could move an order.

There are exactly TWO ways in, and deliberately no third:

  1. **A real Twilio delivery**, proved by `X-Twilio-Signature`.
  2. **The consoles' own reply boxes**, proved by `X-Internal-Reply-Token`.
     These are not Twilio-signed and must keep working, because the mock
     transport is what lets the whole escalation loop be demoed offline --
     and the point of that path is that it exercises the REAL handler
     rather than a stub beside it.

Anything else is 403 before a single thing is read or written.

**There is no "skip when SMS_ENABLED=false" branch, and there must never
be one.** A bypass keyed on a config flag is a bypass an attacker gets by
reading the repository, and this repository is public.

What this does NOT do
---------------------
The internal token is delivered to the console pages by the server that
serves them, and this project has no user authentication anywhere -- so
anyone who can load `/merchant/orders` can read it. That is a real
limitation and it is stated rather than papered over. What this closes is
the blind unauthenticated POST: a scanner, a CSRF from another origin, or
anyone who never loaded the console at all. Closing the rest means
authenticating the consoles themselves, which is a larger piece of work
than this and is recorded as an open gap.
"""

import base64
import hashlib
import hmac
import logging
import os
import secrets

log = logging.getLogger(__name__)

INTERNAL_TOKEN_HEADER = "X-Internal-Reply-Token"
TWILIO_SIGNATURE_HEADER = "X-Twilio-Signature"

# Generated when the environment does not set one, so the consoles work
# out of the box on a fresh clone without the endpoint ever being open.
# A restart rotates it, which is the correct behaviour for a credential
# nobody wrote down.
_EPHEMERAL_INTERNAL_TOKEN = secrets.token_urlsafe(32)


def internal_token() -> str:
    """The credential the console reply boxes present."""
    return os.environ.get("INTERNAL_REPLY_TOKEN") or _EPHEMERAL_INTERNAL_TOKEN


def warn_if_misconfigured() -> list[str]:
    """Said once at startup, because a missing token fails at the worst
    possible moment otherwise -- a live reply, mid-demo, rejected with a
    403 that looks like a bug in Twilio."""
    warnings = []
    if not os.environ.get("TWILIO_AUTH_TOKEN"):
        warnings.append(
            "TWILIO_AUTH_TOKEN is not set: inbound Twilio replies to "
            "/webhook/sms-reply will be REJECTED, not waved through. The consoles' "
            "own reply boxes still work. Set it before demoing over real WhatsApp."
        )
    if not os.environ.get("INTERNAL_REPLY_TOKEN"):
        warnings.append(
            "INTERNAL_REPLY_TOKEN is not set: using a per-process random token. "
            "The consoles pick it up automatically; it changes on restart."
        )
    for line in warnings:
        log.warning(line)
    return warnings


def public_url(request) -> str:
    """The URL Twilio actually signed.

    Twilio signs the address IT sent the request to. Behind a tunnel that
    is `https://<something>.ngrok-free.dev/webhook/sms-reply`, while
    `request.url` reads `http://127.0.0.1:8000/...` -- the internal
    address the proxy forwarded to. Signing the wrong one makes EVERY
    genuine reply fail, which is why the forwarded-header case has a test
    of its own rather than being assumed to work.
    """
    url = str(request.url)
    proto = request.headers.get("x-forwarded-proto")
    host = request.headers.get("x-forwarded-host")
    if not (proto or host):
        return url

    scheme = (proto.split(",")[0].strip() if proto else request.url.scheme)
    netloc = (host.split(",")[0].strip() if host else request.url.netloc)
    path = request.url.path
    if request.url.query:
        path = f"{path}?{request.url.query}"
    return f"{scheme}://{netloc}{path}"


def expected_signature(url: str, params: dict, auth_token: str) -> str:
    """Twilio's scheme: the full URL, then every POST parameter sorted by
    key and appended as key immediately followed by value, HMAC-SHA1'd
    under the auth token and base64'd."""
    payload = url
    for key in sorted(params):
        payload += key + (params[key] if params[key] is not None else "")
    digest = hmac.new(auth_token.encode("utf-8"),
                      payload.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("utf-8")


def _signature_ok(request, params: dict) -> bool:
    supplied = request.headers.get(TWILIO_SIGNATURE_HEADER)
    if not supplied:
        return False

    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    if not auth_token:
        # Unset means reject, never wave through. A signed request we
        # cannot check is a request we have not checked.
        log.warning("a Twilio-signed reply arrived but TWILIO_AUTH_TOKEN is unset; rejecting")
        return False

    expected = expected_signature(public_url(request), params, auth_token)
    return hmac.compare_digest(supplied, expected)


def _internal_ok(request) -> bool:
    supplied = request.headers.get(INTERNAL_TOKEN_HEADER)
    if not supplied:
        return False
    return hmac.compare_digest(supplied, internal_token())


def authorise(request, params: dict) -> str | None:
    """Which door this request came through, or None if neither.

    Returns "twilio" or "console" so the caller can say so; the caller
    turns None into a 403. Both comparisons use `compare_digest` -- `==`
    on a secret leaks its prefix through timing.
    """
    if _signature_ok(request, params):
        return "twilio"
    if _internal_ok(request):
        return "console"
    return None
