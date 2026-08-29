"""The clock this project spent its whole life without.

Two capabilities were complete, tested, and unreachable. `mcp_orders
.expire()` turns a paid order the merchant never answered back into a
refund; `routines.check_and_fire()` places a standing order. Both were
written, both had tests, and neither had anything that would ever call
them -- so the lifecycle diagram in CLAUDE.md contained a transition
(`silence -> MERCHANT_TIMEOUT_REFUNDED -> REFUNDED`) that could not
happen, and a customer whose merchant went quiet after paying had no
automatic protection at all.

This module is the missing caller and deliberately nothing else. It
contains no business logic: it asks each owning module what is due and
calls the function that module already exposes. There is no second
charging path here, and there could not be -- every order it causes goes
through `orchestrator.negotiate_and_record()` exactly as if a human had
pressed the button.

Three things it has to get right
--------------------------------
**It must not die quietly.** Each call is wrapped separately, and an
exception is logged with its traceback and then stepped over. A
scheduler that stops on tick 3 and says nothing is worse than no
scheduler, because everything downstream now assumes something is
watching.

**It must not run twice.** `uvicorn --reload` runs two processes, and so
does any multi-worker deploy. Firing a standing order twice is money. So
each tick's work is claimed through the SAME `idempotency.py` ledger the
webhook and the reconciler use, keyed on the work rather than on the
tick, so two runners racing the same minute produce one charge.

**It must be quiet.** One line per tick, only when the tick did
something. A scheduler that logs every 60 seconds is 1,440 lines a day
for a real failure to hide in.
"""

import asyncio
import logging
import os
import traceback
from datetime import datetime, timezone

import audit_log
import idempotency

log = logging.getLogger("amma.scheduler")

SCHEDULER_INTERVAL_SECONDS = int(os.environ.get("SCHEDULER_INTERVAL_SECONDS", "60"))

# The claim namespace in the shared ledger. Distinct from the webhook's
# and the reconciler's so a tick can never collide with a real payment
# event, and stable so two runners on the same work collide with EACH
# OTHER, which is the entire point.
_EXPIRY_CLAIM = "scheduler.expire"
_ROUTINE_CLAIM = "scheduler.routine"


def is_enabled() -> bool:
    return (os.environ.get("SCHEDULER_ENABLED") or "true").strip().lower() not in (
        "false", "0", "no", "off",
    )


def _claim(kind: str, key: str) -> bool:
    """One runner gets the work; the other is told no.

    Through the existing ledger rather than a new one, for the reason
    that ledger exists: a second record of the same fact is a second
    record that can disagree with the first.
    """
    return idempotency.claim_event(kind, key, audit_log.DEFAULT_DB_PATH)


def _expire_due(now: datetime) -> list[str]:
    """Refund paid orders the merchant never answered."""
    import mcp_orders

    done = []
    for order_ref in mcp_orders.due_for_expiry(now=now):
        # Keyed on the ORDER, not the tick: two runners on the same minute
        # both see order #40, and exactly one of them expires it.
        if not _claim(_EXPIRY_CLAIM, str(order_ref)):
            continue
        try:
            mcp_orders.expire(order_ref)
            done.append(f"expired #{order_ref}")
        except Exception:
            # The claim is NOT released. `expire()` refunds and writes as
            # it goes, so a failure part-way through is not work that
            # provably did not happen -- and retrying it could refund
            # twice. It is logged loudly and left for a human.
            log.error("scheduler: expiring order #%s failed\n%s", order_ref, traceback.format_exc())
    return done


def _fire_due_routines(now: datetime) -> list[str]:
    """Place standing orders whose occurrence is now."""
    import routines

    done = []
    for routine_id in routines.due_now(now=now):
        # Keyed on routine + occurrence, so two runners cannot both charge
        # this morning's breakfast, and tomorrow's is a different key.
        occurrence = f"{routine_id}:{now:%Y-%m-%d}:{now:%H}"
        if not _claim(_ROUTINE_CLAIM, occurrence):
            continue
        try:
            result = routines.check_and_fire(routine_id, now=now)
            done.append(
                f"routine {routine_id} " + ("fired" if result.get("fired") else "asked first")
            )
        except Exception:
            log.error("scheduler: firing routine %s failed\n%s", routine_id, traceback.format_exc())
    return done


def tick(now: datetime | None = None) -> list[str]:
    """One pass. Returns what it did, for the log line and for tests.

    Each half is wrapped separately and on purpose: a failure inside
    `expire()` must not stop standing orders from firing on the same
    tick. They are unrelated pieces of work that happen to share a clock.
    """
    now = now or datetime.now(timezone.utc)
    did = []

    try:
        did += _expire_due(now)
    except Exception:
        log.error("scheduler: the expiry pass failed\n%s", traceback.format_exc())

    try:
        did += _fire_due_routines(now)
    except Exception:
        log.error("scheduler: the standing-order pass failed\n%s", traceback.format_exc())

    return did


async def run() -> None:
    """The loop. Cancelled by app.py on shutdown."""
    log.info(
        "scheduler: watching for merchant timeouts and standing orders, every %ss",
        SCHEDULER_INTERVAL_SECONDS,
    )
    try:
        while True:
            await asyncio.sleep(SCHEDULER_INTERVAL_SECONDS)
            try:
                # Off the event loop: both calls do blocking SQLite and
                # HTTP, and a tick that blocks the loop stalls every
                # request the server is serving.
                did = await asyncio.to_thread(tick)
            except Exception:
                # Belt and braces. `tick` already catches everything; this
                # is here so that if it ever does not, the LOOP survives.
                log.error("scheduler: tick raised\n%s", traceback.format_exc())
                continue
            if did:
                log.info("scheduler: %s", "; ".join(did))
    except asyncio.CancelledError:
        log.info("scheduler: stopped")
        raise
