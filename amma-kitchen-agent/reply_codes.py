"""The single-use code that has to be in a reply before it moves money.

`reply_auth.py` proved the request came from Twilio. This proves it came
from the person we actually asked.

Those are different questions, and only the first was being answered. A
reply of "1" was authenticated by caller ID -- which is spoofable -- and
matched to an escalation on the last ten digits, which is loose on
purpose so that "98765 43210" typed in a browser matches
`whatsapp:+919876543210` as Twilio delivers it. Put together, anyone who
learned an order number could approve it. The code closes that: it is
generated per question, sent only in the message, and required back.

Design notes worth keeping:

  * **Four digits, from `secrets.randbelow`.** Not `random` -- that is
    seeded predictably and this is a credential. Four digits is 1 in
    10,000 per guess, which is thin on its own and is why the re-ask is
    rate limited; it is chosen because a person has to read it off a
    phone and type it back, and a longer code gets copied wrong.
  * **One expiry clock, shared.** The code lives exactly as long as the
    question does. There is no separate code TTL to drift out of step
    with the question's, and `TTL_SECONDS` here is the only such number
    in the project.
  * **The re-ask is rate limited, and that is the point.** Without it,
    the endpoint answers "wrong code" quickly and "no such order"
    differently, and 10,000 requests walk the space. After
    `MAX_REASKS` failures from one sender the reply stops varying at
    all.
  * **A wrong code and an unknown order read identically.** Anything else
    tells an attacker which order numbers are live.
"""

import re
import secrets
from datetime import datetime, timedelta, timezone

# How long a question -- and therefore its code -- stays answerable. This
# is the project's ONLY expiry constant for pending questions; buyer_sms
# imports it rather than keeping its own.
TTL_SECONDS = 900

# Failed code attempts tolerated from one sender before the response
# stops varying. Generous enough that someone mistyping twice is not
# locked out, small enough that the space cannot be walked.
MAX_REASKS = 3
REASK_WINDOW_SECONDS = 600

_CODE_RE = re.compile(r"\b(\d{4})\b")

# sender -> [iso timestamps of failed attempts]
_FAILURES: dict[str, list[str]] = {}


def reset() -> None:
    _FAILURES.clear()


def new_code() -> str:
    """A fresh 4-digit code, zero-padded so "0417" is not shown as "417"
    and typed back as four different characters."""
    return f"{secrets.randbelow(10000):04d}"


def _parse_iso(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def is_expired(asked_at: str, now: datetime | None = None) -> bool:
    """Has the question this code belongs to gone stale?"""
    if not asked_at:
        return False
    now = now or datetime.now(timezone.utc)
    return now - _parse_iso(asked_at) > timedelta(seconds=TTL_SECONDS)


def extract(text: str) -> str | None:
    """The four-digit code out of a reply, if there is one.

    Deliberately not anchored: people reply "1 4417", "1, 4417", "approve
    4417" and occasionally "4417 1". What is NOT tolerated is a missing
    code -- the caller treats None as a refusal, never as a pass.
    """
    if not text:
        return None
    match = _CODE_RE.search(text)
    return match.group(1) if match else None


def strip(text: str) -> str:
    """The reply with its code taken out.

    A substitution answer becomes the customer's new request, so the code
    must not survive into it: "4417 2 dosas" is an order for two dosas,
    not for 4417 of anything. Only the first occurrence goes, so a code
    that happens to also be a quantity later in the sentence is left
    alone.
    """
    if not text:
        return ""
    return _CODE_RE.sub("", text, count=1).strip(" ,.-\t")


def _recent_failures(sender: str, now: datetime) -> int:
    window = now - timedelta(seconds=REASK_WINDOW_SECONDS)
    kept = [t for t in _FAILURES.get(sender, []) if _parse_iso(t) > window]
    _FAILURES[sender] = kept
    return len(kept)


def record_failure(sender: str, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    _recent_failures(sender, now)
    _FAILURES.setdefault(sender, []).append(now.isoformat())


def should_reask(sender: str, now: datetime | None = None) -> bool:
    """Whether this sender still gets a helpful answer.

    False means they have had their re-asks and the reply stops carrying
    any information about what was wrong.
    """
    now = now or datetime.now(timezone.utc)
    return _recent_failures(sender, now) < MAX_REASKS


# The two things a refusal may ever say. Neither distinguishes a wrong
# code from an unknown order, a used code or an expired one -- telling
# them apart is exactly the oracle this is here to deny.
REASK = ("Please reply with the code from the message, "
         "like '1 4417' to approve or '2 4417' to reject.")
STONEWALL = "Sorry, that didn't work. Please check the message and try again later."


def refusal(sender: str, now: datetime | None = None) -> str:
    """The message for a reply whose code did not check out."""
    if should_reask(sender, now):
        record_failure(sender, now)
        return REASK
    record_failure(sender, now)
    return STONEWALL
