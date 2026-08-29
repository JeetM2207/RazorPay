"""Sign in with Google, for both sides of the shop.

What this actually verifies
---------------------------
The browser gets an ID token from Google and posts it here. An ID token is
a signed JWT, and the whole security of this rests on checking that
signature properly rather than decoding the payload and believing it --
an unverified JWT is a string the client wrote, and a client can write
any email it likes into one.

So every token is checked for: Google's own RSA signature (fetched from
their published JWKS and cached), the audience matching OUR client id
(a token minted for somebody else's app is not a login for this one),
the issuer being Google, and expiry. PyJWT does all four; there is no new
dependency.

The merchant allowlist
----------------------
"Sign in with Google" on the merchant console, with no further check,
is WEAKER than the password it sits beside: anybody with a Google account
could open her shop settings and approve orders, where the password at
least only lets in whoever knows it.

So merchant sign-in requires MERCHANT_GOOGLE_EMAILS to say something.
Either a list of addresses, or `*` for "anyone with a Google account" --
which is a legitimate choice for a demo, and is spelled out precisely so
it has to be chosen. Blank means SHUT: an unconfigured door should be a
closed one, and an accidentally-open door looks identical to a
deliberately-open one from the outside.

The buyer side has no allowlist on purpose: any Google account is a
legitimate customer, and their identity is the point rather than a gate.
"""

import os

import jwt
from jwt import PyJWKClient

GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
GOOGLE_JWKS = "https://www.googleapis.com/oauth2/v3/certs"

# Cached across requests: fetching Google's keys on every sign-in would
# add a round trip to a page load, and PyJWKClient caches internally.
_jwks_client: PyJWKClient | None = None


class NotConfigured(RuntimeError):
    """Google sign-in has not been set up on this deployment."""


class NotAllowed(RuntimeError):
    """A valid Google account that is not permitted here."""


def client_id() -> str:
    return (os.environ.get("GOOGLE_CLIENT_ID") or "").strip()


def is_enabled() -> bool:
    """Whether the button should be shown at all.

    A sign-in button that cannot work is worse than no button: it looks
    like the intended path and fails with something cryptic.
    """
    return bool(client_id())


def merchant_emails() -> set[str]:
    raw = os.environ.get("MERCHANT_GOOGLE_EMAILS") or ""
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


# Set MERCHANT_GOOGLE_EMAILS to this to let ANY Google account run the
# kitchen. It exists so "open to everyone" is something somebody typed on
# purpose, rather than what you get by leaving a setting blank -- an
# accidental open door and a deliberate one look identical from outside,
# and only one of them is a decision.
OPEN_TO_ANYONE = "*"


def merchant_is_open_to_anyone() -> bool:
    return OPEN_TO_ANYONE in merchant_emails()


def merchant_google_enabled() -> bool:
    """Google sign-in for the MERCHANT needs a client id, and either an
    allowlist or an explicit `*`. Blank still means shut."""
    return is_enabled() and bool(merchant_emails())


def _keys() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = PyJWKClient(GOOGLE_JWKS, cache_keys=True)
    return _jwks_client


def verify(id_token: str) -> dict:
    """Google's claims about this person, or an exception.

    Returns only what we actually use -- subject, email, name, picture --
    rather than the whole payload, so nothing downstream starts depending
    on a claim we have not thought about.
    """
    if not is_enabled():
        raise NotConfigured(
            "GOOGLE_CLIENT_ID is not set, so Google sign-in cannot be verified."
        )
    if not id_token:
        raise NotAllowed("No Google credential was supplied.")

    try:
        signing_key = _keys().get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=client_id(),
            issuer=GOOGLE_ISSUERS,
            options={"require": ["exp", "iat", "aud", "iss", "sub"]},
        )
    except jwt.PyJWTError as exc:
        # Deliberately one message for every failure mode -- expired,
        # forged, wrong audience, unknown key. Telling them apart tells an
        # attacker which part of their token to fix.
        raise NotAllowed("That Google sign-in could not be verified.") from exc

    if not claims.get("email_verified", False):
        raise NotAllowed("That Google account has no verified email address.")

    return {
        "sub": claims["sub"],
        "email": (claims.get("email") or "").lower(),
        "name": claims.get("name") or claims.get("given_name") or "",
        "picture": claims.get("picture") or "",
    }


def verify_merchant(id_token: str) -> dict:
    """As above, and then: is this person allowed to run this kitchen?"""
    if not merchant_emails():
        raise NotConfigured(
            "MERCHANT_GOOGLE_EMAILS is not set. Google sign-in for the merchant "
            "console is refused until you say who may use it -- either a list of "
            f"addresses, or '{OPEN_TO_ANYONE}' to mean anyone with a Google account. "
            "Blank means shut, because an unconfigured door should be a closed one."
        )

    person = verify(id_token)

    if merchant_is_open_to_anyone():
        # Chosen deliberately. The signature was still verified, so this
        # is "any REAL Google account", not "anyone who can type an
        # email" -- which is a meaningfully different thing from no
        # authentication at all.
        return person

    if person["email"] not in merchant_emails():
        raise NotAllowed("That Google account is not on this shop's list.")
    return person


def agent_name_for(display_name: str, email: str = "") -> str:
    """The same rule the profile page uses, server-side.

    Jeet -> "Jeet's Agent". Kept here as well as in the browser because a
    Google sign-in gives the server the name directly, and two places
    deriving it differently would put two ids in the audit trail for one
    person.
    """
    source = (display_name or "").strip() or (email.split("@")[0] if email else "")
    first = "".join(c for c in source.split(" ")[0] if c.isalnum() or c in "'-")
    if not first:
        return ""
    named = first[0].upper() + first[1:]
    return named + ("' Agent" if named.lower().endswith("s") else "'s Agent")
