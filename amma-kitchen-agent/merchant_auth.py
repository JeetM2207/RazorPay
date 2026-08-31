"""A login in front of every merchant surface that writes.

The consoles had no authentication at all. `/api/merchant/optimize-prices`
reprices the shop, the setup page sets the budget cap the decision core
runs on, and the accept/reject endpoints move money -- and all of it was
reachable by anyone holding the ngrok URL, which gets pasted into a
public connector setting.

What is protected, and what deliberately is not
----------------------------------------------
Protected: the merchant's two pages, everything under `/api/merchant/`,
her configuration, the escalation accept/reject endpoints, the disputes
endpoints, `/api/insights`, and the evidence pack.

NOT protected, on purpose:

  * `/catalog` and `/mcp` -- an agent has to be able to read the menu,
    and MCP is how somebody else's model reaches the shop at all.
    Putting a cookie in front of `/mcp` would break the connector.
  * The adapters' buyer-facing endpoints -- that IS the shopfront.
  * `/webhook/*` -- these have their own authentication, which is
    stronger than a cookie: a Twilio signature or a Razorpay signature.
    See reply_auth.py.
  * The buyer console -- a different party, with a different problem.
  * `/audit` -- see the note on it below.

`/audit` stays readable
-----------------------
Its whole purpose is being checkable by someone who does not have an
account: "every rejected order never touched Razorpay" is a claim, and a
trail behind a login is a claim you have to take on trust. So it stays
open, and the customer's NAME, PHONE and ADDRESS are redacted out of it
instead. The unredacted record lives at `/evidence/<id>`, which is behind
the login -- that is the page a dispute needs and the one that should
cost a password.

The cookie
----------
HMAC-SHA256 over "issued-at:expiry", keyed on SECRET_KEY, HttpOnly and
SameSite=Lax. It carries no identity because there is only one merchant;
it is a proof of login and an expiry, nothing else. Nothing here logs the
password or the cookie value -- a credential in a log file is a
credential.
"""

import hashlib
import hmac
import logging
import os

import merchants
import secrets
import time

from fastapi import HTTPException, Request, status
from fastapi.responses import RedirectResponse

log = logging.getLogger(__name__)

COOKIE_NAME = "amma_merchant"
SESSION_SECONDS = 12 * 60 * 60      # a working day; she is not re-typing it hourly

# Generated per process when SECRET_KEY is unset, so a fresh clone is
# never accidentally signing with a literal that is in the repository.
# A restart invalidates outstanding cookies, which is the right default
# for a secret nobody chose.
_EPHEMERAL_SECRET = secrets.token_urlsafe(48)


def _secret() -> bytes:
    return (os.environ.get("SECRET_KEY") or _EPHEMERAL_SECRET).encode("utf-8")


def _password() -> str:
    return os.environ.get("MERCHANT_CONSOLE_PASSWORD") or ""


def warn_if_misconfigured() -> list[str]:
    warnings = []
    if not _password():
        warnings.append(
            "MERCHANT_CONSOLE_PASSWORD is not set: the merchant console cannot be "
            "logged into, so every write surface is closed. Set it in .env."
        )
    if not os.environ.get("SECRET_KEY"):
        warnings.append(
            "SECRET_KEY is not set: signing merchant sessions with a per-process "
            "random key, so logins do not survive a restart."
        )
    for line in warnings:
        log.warning(line)
    return warnings


# ------------------------------------------------------------- the cookie

def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def issue_cookie(now: float | None = None, merchant_id: str | None = None) -> str:
    """A signed proof of login, for ONE kitchen, with an expiry baked in.

    Both the expiry and the kitchen are inside the signature rather than
    beside it. That is the whole security of multi-tenancy here: the
    merchant id in this cookie is what every merchant-facing read is
    scoped by, so a session that could be edited to name a different
    kitchen would be a session that could read somebody else's orders.
    """
    now = now or time.time()
    merchant_id = merchant_id or merchants.default_id()
    payload = f"{int(now)}:{int(now + SESSION_SECONDS)}:{merchant_id}"
    return f"{payload}:{_sign(payload)}"


def cookie_is_valid(value: str | None, now: float | None = None) -> bool:
    """Constant-time, and an expired cookie is simply not valid.

    Deliberately tolerant of a malformed value -- a cookie somebody made
    up is the ordinary case here, not an exceptional one, so it returns
    False rather than raising.
    """
    return merchant_from_cookie(value, now) is not None


def merchant_from_cookie(value: str | None, now: float | None = None) -> str | None:
    """Which kitchen this session is for, or None if it proves nothing.

    Deliberately tolerant of a malformed value -- a cookie somebody made
    up is the ordinary case here, not an exceptional one, so it returns
    None rather than raising.

    A cookie issued before kitchens existed has three parts and no id.
    It is honoured as the default kitchen rather than rejected: the
    signature still proves it, and logging every open session out to add
    a field would be a worse answer than reading the one it was issued
    under.
    """
    if not value:
        return None
    parts = value.split(":")
    if len(parts) == 3:
        issued, expires, signature = parts
        merchant_id = merchants.default_id()
        payload = f"{issued}:{expires}"
    elif len(parts) == 4:
        issued, expires, merchant_id, signature = parts
        payload = f"{issued}:{expires}:{merchant_id}"
    else:
        return None

    if not hmac.compare_digest(signature, _sign(payload)):
        return None

    try:
        if (now or time.time()) >= int(expires):
            return None
    except ValueError:
        return None
    return merchant_id if merchants.exists(merchant_id) else None


def password_is_correct(supplied: str) -> bool:
    """`compare_digest`, never `==`: equality on a secret leaks its prefix
    through timing. An unset password rejects everything rather than
    letting anything through."""
    expected = _password()
    if not expected:
        return False
    return hmac.compare_digest(supplied or "", expected)


# ------------------------------------------------------- the dependency

def is_authenticated(request: Request) -> bool:
    return cookie_is_valid(request.cookies.get(COOKIE_NAME))


def signed_in_merchant(request: Request) -> str:
    """The kitchen this request is signed in as.

    THE one source of truth for whose data a merchant surface may read.
    Taken from the signed cookie and never from a query string or a
    header, because those are things the caller chooses -- and a merchant
    id a caller can choose is a merchant id a caller can change.
    """
    return (merchant_from_cookie(request.cookies.get(COOKIE_NAME))
            or merchants.default_id())


def require_merchant(request: Request) -> None:
    """FastAPI dependency for every merchant surface that writes.

    A browser asking for a page gets sent to the login; anything else --
    a script, a fetch from the console's own JS -- gets a 401 it can
    actually act on. Redirecting an XHR just produces a login page parsed
    as JSON, which surfaces as a baffling error rather than "log in".
    """
    if is_authenticated(request):
        return

    wants_html = "text/html" in (request.headers.get("accept") or "")
    if wants_html and request.method == "GET":
        raise _redirect_to_login(request)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="This is a merchant surface. Log in at /merchant/login.",
    )


class _LoginRedirect(HTTPException):
    """Carried as an exception so a dependency can produce a redirect.

    A dependency cannot return a response, and raising a plain
    RedirectResponse is not something FastAPI handles -- so this wraps
    one and app.py installs the handler that sends it.
    """

    def __init__(self, response: RedirectResponse):
        super().__init__(status_code=response.status_code, detail="login required")
        self.response = response


def _redirect_to_login(request: Request) -> _LoginRedirect:
    nxt = request.url.path
    if request.url.query:
        nxt += "?" + request.url.query
    return _LoginRedirect(RedirectResponse(f"/merchant/login?next={nxt}", status_code=303))


# ------------------------------- surfaces that live in modules we do not edit

# The escalation accept/reject endpoints belong to the adapters, and the
# adapters are not to be changed. They are still money actions, so they
# are matched here by path and checked in middleware that calls exactly
# the same `is_authenticated` the dependency does -- one rule, two places
# it is applied, rather than two rules.
#
# Matched on the SUFFIX so a new protocol's accept/reject is covered the
# day it is added, rather than the day somebody remembers this list.
PROTECTED_SUFFIXES = (
    "/human_confirm",
    "/human-confirm",
    "/human_reject",
    "/human-reject",
)


def path_needs_merchant(path: str) -> bool:
    return path.endswith(PROTECTED_SUFFIXES)
