"""Standing orders: an order that originates on a schedule, and the gate
that decides whether it may fire without anyone watching.

The scheduling is the easy half and the uninteresting half. The half that
matters is knowing when NOT to fire. An agent that re-orders blindly on a
timer is precisely how "I never authorised this" starts; an agent that
checks its own confidence before spending money unattended is the thing
worth building.

So `check_and_fire()` runs five checks before it will charge anything, and
a failure of any one of them is not a warning to be overridden -- it turns
the routine back into an ordinary unconfirmed request, which means a
WhatsApp prompt and an explicit yes, exactly like a live order that
crosses a cap. There is deliberately no "fire anyway" path.

Why this may charge the card directly at all
--------------------------------------------
A brand-new chat order needs a fresh Razorpay link because it is new
consent: nobody agreed to those items at that price until they said so.
A standing order was consented to specifically and in advance -- these
items, this cap, these days -- which is the same shape as card-on-file
recurring billing and UPI's merchant reserve: authorise once, charges
follow inside the agreed limit without re-authenticating each time.

That justification only holds while the order still matches what was
agreed. The moment a check fails it is no longer the thing that was
consented to, so the pre-authorisation does not cover it and it goes back
through the link-and-confirm path. The gate is what earns the direct
charge; without it this would just be a recurring charge with extra steps.

Every fired order still goes through `orchestrator.negotiate_and_record()`
like everything else. There is no second charging path and no way around
`negotiation.py`.
"""

import json
import pathlib
import uuid
from datetime import datetime, timedelta, timezone

import audit_log
import merchant_config

_STORE = pathlib.Path(__file__).resolve().parent / "routines.json"

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# How far a price may move before a routine stops being the thing that was
# agreed to. Menu prices change -- she runs sales -- and a stale routine
# should not silently pay a new price without somebody glancing at it.
PRICE_DRIFT_TOLERANCE = 0.15

# Firing hours early or late is itself a signal that something is off,
# so "on time" is a window rather than an instant.
DEFAULT_WINDOW_MINUTES = 45

# The routine's own ceiling defaults to the setup cart plus a little room
# for the merchant's ordinary price movement.
DEFAULT_CAP_BUFFER = 0.10


# ------------------------------------------------------------- the store

def _load() -> dict:
    if not _STORE.exists():
        return {"routines": []}
    try:
        return json.loads(_STORE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # A corrupt file must not take the ordering page down; an empty
        # list of routines is a working shop.
        return {"routines": []}


def _save(state: dict) -> None:
    _STORE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def reset() -> None:
    """Used by the tests, so a routine saved in a browser cannot leak in."""
    _save({"routines": []})


def all_routines() -> list[dict]:
    return _load()["routines"]


def get(routine_id: str) -> dict | None:
    return next((r for r in all_routines() if r["id"] == routine_id), None)


def _cart_total(items: list[dict], menu: dict) -> int:
    return sum(menu[i["item_id"]].price_inr * i["qty"] for i in items if i["item_id"] in menu)


def create(
    items: list[dict],
    days: list[str],
    at_time: str,
    agent_id: str,
    phone: str | None = None,
    routine_cap_inr: int | None = None,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    source: str = "manual",
    utc_offset_minutes: int | None = None,
) -> dict:
    """Record a standing order the customer has explicitly turned on.

    `source` is "manual" or "detected". A detected one is still only ever
    a suggestion the customer confirmed -- nothing in this module creates
    a routine on its own.
    """
    days = [d.lower()[:3] for d in days if d.lower()[:3] in DAYS]
    if not items:
        raise ValueError("A standing order needs at least one item.")
    if not days:
        raise ValueError("Pick at least one day of the week.")
    try:
        hour, minute = (int(part) for part in at_time.split(":"))
        assert 0 <= hour < 24 and 0 <= minute < 60
    except (ValueError, AssertionError):
        raise ValueError("Time should look like 08:00.")

    menu = merchant_config.current_menu()
    unknown = [i["item_id"] for i in items if i["item_id"] not in menu]
    if unknown:
        raise ValueError(f"Not on the menu: {', '.join(unknown)}")

    setup_total = _cart_total(items, menu)
    routine = {
        "id": uuid.uuid4().hex[:10],
        "agent_id": agent_id,
        "phone": phone,
        "items": items,
        "days": days,
        "time": f"{hour:02d}:{minute:02d}",
        "window_minutes": int(window_minutes),
        "routine_cap_inr": int(routine_cap_inr or round(setup_total * (1 + DEFAULT_CAP_BUFFER))),
        # What each item cost when the customer agreed to this. Drift is
        # measured against these, not against today's menu.
        "setup_prices": {i["item_id"]: menu[i["item_id"]].price_inr for i in items},
        "setup_total_inr": setup_total,
        "status": "active",
        "source": source,
        # The customer's own offset from UTC, sent by their browser. Their
        # "08:00" means eight where THEY are; without this the gate
        # measured it against UTC and a routine outside that zone could
        # never fire.
        "utc_offset_minutes": (
            int(utc_offset_minutes) if utc_offset_minutes is not None
            else _server_offset_minutes()
        ),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "last_fired_at": None,
        "next_expected_at": None,
    }
    state = _load()
    state["routines"].append(routine)
    _save(state)
    return routine


def update(routine_id: str, **changes) -> dict | None:
    state = _load()
    for routine in state["routines"]:
        if routine["id"] == routine_id:
            routine.update(changes)
            _save(state)
            return routine
    return None


def delete(routine_id: str) -> bool:
    state = _load()
    before = len(state["routines"])
    state["routines"] = [r for r in state["routines"] if r["id"] != routine_id]
    _save(state)
    return len(state["routines"]) != before


def set_status(routine_id: str, status: str) -> dict | None:
    if status not in ("active", "paused"):
        raise ValueError("A standing order is either active or paused.")
    return update(routine_id, status=status)


# ------------------------------------------------------ the confidence gate

def _local(now: datetime, routine: dict) -> datetime:
    """`now` as the customer's own wall clock.

    A routine's time is what somebody TYPED -- "08:00" means eight in the
    morning where they are. Everything internal runs in UTC, so comparing
    the two directly is wrong by the whole offset: a customer in India who
    asked for 22:13 was measured against 16:43 UTC and their routine could
    never fire at all. Silent, and it only shows up if you set one up and
    wait.

    So the offset is recorded when the routine is created, from the
    customer's own browser, and `now` is converted into that zone before
    any hour is read off it. Routines made before this defaulted to the
    server's offset, which is right whenever the kitchen and the customer
    share a timezone -- a neighbourhood food business, usually.
    """
    offset = routine.get("utc_offset_minutes")
    if offset is None:
        offset = _server_offset_minutes()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone(timedelta(minutes=int(offset))))


def _server_offset_minutes() -> int:
    local = datetime.now().astimezone()
    return int(local.utcoffset().total_seconds() // 60)


def _within_window(now: datetime, routine: dict) -> tuple[bool, str]:
    here = _local(now, routine)
    day = DAYS[here.weekday()]
    if day not in routine["days"]:
        return False, (
            f"today is {day}, and this routine repeats on "
            f"{', '.join(routine['days'])}"
        )
    hour, minute = (int(p) for p in routine["time"].split(":"))
    expected = here.replace(hour=hour, minute=minute, second=0, microsecond=0)
    drift = abs((here - expected).total_seconds()) / 60
    window = routine.get("window_minutes", DEFAULT_WINDOW_MINUTES)
    if drift > window:
        return False, (
            f"it is {here.strftime('%H:%M')} and this routine expects "
            f"{routine['time']} give or take {int(window)} minutes"
        )
    return True, ""


def confidence_gate(routine: dict, now: datetime | None = None) -> dict:
    """May this fire without asking anybody? Pure, and testable alone.

    Returns every failure rather than the first, because a customer being
    asked deserves to be told everything that looks different -- and
    because a gate that stops at the first problem hides the others.
    """
    now = now or datetime.now(timezone.utc)
    menu = merchant_config.current_menu()
    mandate = merchant_config.current_mandate()
    failures = []

    if routine.get("status") != "active":
        failures.append({
            "check": "active",
            "why": f"this standing order is {routine.get('status')}, not active",
        })

    # Every item still sold, and still sold to an agent.
    for item in routine["items"]:
        dish = menu.get(item["item_id"])
        name = item["item_id"].replace("_", " ")
        if dish is None:
            failures.append({"check": "on_menu", "why": f"{name} is no longer on the menu"})
        elif dish.category not in mandate.allowed_categories:
            failures.append({
                "check": "on_menu",
                "why": f"{name} is no longer something an agent may order",
            })
        elif dish.stock <= 0:
            failures.append({"check": "on_menu", "why": f"{name} is out of stock"})

    # Price drift, measured against what it cost when they agreed to it.
    for item in routine["items"]:
        dish = menu.get(item["item_id"])
        was = routine.get("setup_prices", {}).get(item["item_id"])
        if dish is None or not was:
            continue
        drift = abs(dish.price_inr - was) / was
        if drift > PRICE_DRIFT_TOLERANCE:
            failures.append({
                "check": "price_drift",
                "why": (
                    f"{item['item_id'].replace('_', ' ')} was Rs.{was} when you set this "
                    f"up and is Rs.{dish.price_inr} now, a {round(drift * 100)}% change"
                ),
            })

    total = _cart_total(routine["items"], menu)
    cap = routine.get("routine_cap_inr")
    if cap is not None and total > cap:
        failures.append({
            "check": "routine_cap",
            "why": f"today it comes to Rs.{total}, above the Rs.{cap} you set for this routine",
        })

    on_time, why = _within_window(now, routine)
    if not on_time:
        failures.append({"check": "time_window", "why": why})

    return {
        "confident": not failures,
        "failures": failures,
        "total_inr": total,
        "checked_at": now.isoformat(),
    }


# ------------------------------------------------------------ firing it

def _cart_tuples(routine: dict) -> list[tuple[str, int]]:
    return [(i["item_id"], i["qty"]) for i in routine["items"]]


def _label(routine: dict) -> str:
    menu = merchant_config.current_menu()
    return ", ".join(
        f"{i['qty']}x {(menu[i['item_id']].name if i['item_id'] in menu else i['item_id']).replace('_', ' ').title()}"
        for i in routine["items"]
    )


def describe(routine: dict) -> str:
    """The routine in one factual line, from its own configured data.

    Used in place of a customer's stated reason on the evidence pack, and
    generated from what they actually set up -- never written to sound
    like something they said.
    """
    days = ", ".join(d.capitalize() for d in routine["days"])
    since = (routine.get("created_at") or "")[:10]
    return (
        f"Standing order: repeats {days} at {routine['time']}, "
        f"set up on {since} and unchanged since"
    )


def _occurrence_open(now: datetime, routine: dict) -> bool:
    """Has this occurrence's moment arrived -- and not yet passed?

    The confidence gate tolerates the scheduled time give or take its
    window, which is right for a CHECK: a routine examined a few minutes
    either side is still the same occurrence. It is wrong for FIRING. A
    20:00 dinner became due at 19:15, so the food arrived three quarters
    of an hour before anybody wanted it, every week.

    So firing opens AT the scheduled minute and stays open for the rest
    of the window. Late is recoverable -- a tick was missed, the machine
    was asleep, the order still wants placing. Early is just wrong.
    """
    here = _local(now, routine)
    if DAYS[here.weekday()] not in routine["days"]:
        return False
    hour, minute = (int(p) for p in routine["time"].split(":"))
    expected = here.replace(hour=hour, minute=minute, second=0, microsecond=0)
    late_by = (here - expected).total_seconds() / 60
    window = routine.get("window_minutes", DEFAULT_WINDOW_MINUTES)
    return 0 <= late_by <= window


def due_now(now: datetime | None = None) -> list[str]:
    """Routines whose occurrence is happening and has not run yet.

    This predicate is the reason a scheduler is safe to point at
    `check_and_fire`, and it lives here because this module owns the
    schedule. Ticking every routine every minute would be catastrophic
    in both directions:

      * OUTSIDE its window -- which is most of the day -- the confidence
        gate fails on `time_window` and `_ask_first` messages the
        customer. A 60-second tick would send them roughly 1,400 messages
        a day, per routine.
      * INSIDE its window it would fire, and then fire again on the next
        tick, and the next -- ninety charges for one breakfast.

    So a routine is due only when it is active, inside its window, and
    has not already fired for THIS occurrence. `last_fired_at` is the
    guard for the second half: a routine that ran at 08:03 is not due
    again at 08:04, because 08:04 is the same 08:00 occurrence.

    Being conservative is the right failure mode here: a routine this
    skips simply does not fire, and the customer can still press
    Simulate. A routine this fires twice is money.
    """
    now = now or datetime.now(timezone.utc)
    due = []
    for routine in all_routines():
        if routine.get("status") != "active":
            continue
        if not _occurrence_open(now, routine):
            continue
        if _already_fired_this_occurrence(routine, now):
            continue
        due.append(routine["id"])
    return due


def _already_fired_this_occurrence(routine: dict, now: datetime) -> bool:
    """Has it run since this occurrence's window opened?

    Compared against the window's start rather than against a fixed
    interval, so a routine that fired at the very end of yesterday's
    window is not mistaken for having covered today's.
    """
    last = routine.get("last_fired_at")
    if not last:
        return False
    try:
        fired_at = datetime.fromisoformat(last)
    except (TypeError, ValueError):
        return True          # unreadable: assume it ran, and do not charge again
    if fired_at.tzinfo is None:
        fired_at = fired_at.replace(tzinfo=timezone.utc)

    here = _local(now, routine)
    hour, minute = (int(p) for p in routine["time"].split(":"))
    expected = here.replace(hour=hour, minute=minute, second=0, microsecond=0)
    window = routine.get("window_minutes", DEFAULT_WINDOW_MINUTES)
    return fired_at >= expected - timedelta(minutes=window)


def check_and_fire(routine_id: str, now: datetime | None = None) -> dict:
    """Run the gate, then either charge or ask.

    `now` is overridable so a future occurrence can be simulated without
    waiting for real time -- which is also how the demo button works.

    Called by `scheduler.py` for whatever `due_now()` returns, and
    directly by the console's Simulate button.
    """
    routine = get(routine_id)
    if routine is None:
        raise ValueError(f"no standing order {routine_id}")

    gate = confidence_gate(routine, now=now)
    if not gate["confident"]:
        return _ask_first(routine, gate)
    return _fire(routine, gate)


def _fire(routine: dict, gate: dict) -> dict:
    """Every check passed, so charge the card that was authorised for it.

    Still through the shared orchestrator: the merchant's own rules run on
    this exactly as on any other order, and can still escalate it to her.
    """
    import autonomous_payment

    try:
        detail = orchestrator().negotiate_and_record(
            routine["agent_id"], "routine", _cart_tuples(routine),
            buyer_mandate={"routine_cap_inr": routine.get("routine_cap_inr")},
            source="routine", routine_id=routine["id"],
        )
    except orchestrator().VelocityRefused as refused:
        # A standing order is NOT exempt from her rate limits. Passing its
        # own confidence gate says the routine still looks like the thing
        # the customer agreed to; it says nothing about how much this
        # agent has already ordered today, which is her side of the
        # question and is answered on her side.
        detail = refused.payload

    if detail["decision"] not in ("APPROVE", "ESCALATE"):
        # Her rules refused it. The customer's pre-authorisation does not
        # override the merchant's, and never has.
        _tell(routine, f"Your usual order could not be placed today: {detail['reason']}.")
        return {"fired": False, "reason": "merchant refused", "detail": detail, "gate": gate}

    settlement = autonomous_payment.execute(
        event_id=detail["event_id"],
        cart=_cart_tuples(routine),
        amount_inr=detail["total_inr"],
    )
    update(routine["id"],
           last_fired_at=(gate["checked_at"]),
           next_expected_at=_next_occurrence(routine, gate["checked_at"]))

    # Nobody was watching when this was charged, so they hear about it
    # immediately afterwards. A charge with no message is how a surprise
    # becomes a dispute.
    _tell(routine, (
        f"Your agent placed your usual order — {_label(routine)}, "
        f"Rs.{detail['total_inr']}, paid."
    ))
    return {
        "fired": True,
        "order_id": detail["event_id"],
        "total_inr": detail["total_inr"],
        "payment_id": settlement.payment_id,
        "simulated": settlement.simulated,
        "detail": detail,
        "gate": gate,
    }


def _ask_first(routine: dict, gate: dict) -> dict:
    """A check failed, so this is no longer the thing that was agreed to.

    It becomes an ordinary unconfirmed request: the customer is asked, and
    nothing is charged unless they say yes. There is no path from here
    that charges anyway.
    """
    import buyer_sms

    # Say what actually stopped it. The reason a routine is held back is
    # usually not the amount at all, so the soft-cap sentence would state
    # something untrue -- see buyer_sms.ask_approval's `why`.
    reasons = "; ".join(f["why"] for f in gate["failures"])
    why = (
        "This is your standing order, but it isn't the usual one:\n"
        + "\n".join(f"- {f['why']}" for f in gate["failures"])
        + "\n\nSo nothing has been ordered and nothing has been charged."
    )
    conversation = None
    if routine.get("phone"):
        try:
            conversation = buyer_sms.ask_approval(
                agent_id=routine["agent_id"],
                phone=routine["phone"],
                cart_label=f"{_label(routine)} (your usual order)",
                total_inr=gate["total_inr"],
                soft_cap_inr=routine.get("routine_cap_inr") or gate["total_inr"],
                why=why,
                # So a YES can actually place it. Without this the reply
                # was recorded, answered "going ahead with your order
                # now", and nothing went ahead.
                routine_id=routine["id"],
            )
        except Exception:
            conversation = None

    return {
        "fired": False,
        "awaiting_confirmation": True,
        "reason": "confidence gate",
        "failures": gate["failures"],
        "asked": bool(conversation),
        "total_inr": gate["total_inr"],
        "gate": gate,
    }


def confirm_pending(routine_id: str, approved: bool, now: datetime | None = None) -> dict:
    """The customer answered the prompt a gate failure raised.

    A yes places the order through the ordinary path -- recorded, checked
    against the merchant's rules, settled. It does NOT re-run the gate:
    the customer has now looked at exactly the thing the gate was unsure
    about and said go ahead, which is the confirmation the gate wanted.
    """
    routine = get(routine_id)
    if routine is None:
        raise ValueError(f"no standing order {routine_id}")
    if not approved:
        _tell(routine, "Cancelled — nothing was charged.")
        return {"fired": False, "cancelled": True}

    gate = confidence_gate(routine, now=now)
    gate["confident"] = True          # confirmed by the customer, not by the gate
    gate["confirmed_by_customer"] = True
    return _fire(routine, gate)


def _next_occurrence(routine: dict, after_iso: str) -> str | None:
    try:
        after = datetime.fromisoformat(after_iso)
    except ValueError:
        return None
    hour, minute = (int(p) for p in routine["time"].split(":"))
    for ahead in range(1, 8):
        day = after + timedelta(days=ahead)
        if DAYS[day.weekday()] in routine["days"]:
            return day.replace(hour=hour, minute=minute, second=0, microsecond=0).isoformat()
    return None


def _tell(routine: dict, message: str) -> None:
    """Message the customer, and never let a transport failure break an
    order that has already happened."""
    if not routine.get("phone"):
        return
    try:
        import buyer_sms
        import notification_service

        notification_service.send_sms(
            message,
            to=buyer_sms.normalise_phone(routine["phone"]) or routine["phone"],
            audience="customer",
        )
    except Exception:
        pass


def orchestrator():
    """Imported lazily so this module can be unit-tested without dragging
    in the Razorpay client for a pure gate check."""
    import orchestrator as _orchestrator

    return _orchestrator


# --------------------------------------------------- detect, never create

def suggest_from_history(agent_id: str, min_occurrences: int = 3) -> list[dict]:
    """Carts this customer has paid for repeatedly, at a similar time.

    Only ever a suggestion. Nothing here creates a routine -- the customer
    confirms one explicitly or it does not exist, which is the whole
    difference between a helpful nudge and an agent that decided on its
    own to start spending on a timer.
    """
    seen: dict[tuple, list[str]] = {}
    for event in audit_log.get_events_for_agent(agent_id, db_path=audit_log.DEFAULT_DB_PATH):
        if not event["payment_id"]:
            continue
        try:
            cart = json.loads(event["cart_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not cart:
            continue
        key = tuple(sorted((line["item"], line["qty"]) for line in cart))
        seen.setdefault(key, []).append(event["ts"])

    out = []
    for key, timestamps in seen.items():
        if len(timestamps) < min_occurrences:
            continue
        stamps = [datetime.fromisoformat(t) for t in sorted(timestamps)]
        days = sorted({DAYS[t.weekday()] for t in stamps})
        hour = round(sum(t.hour + t.minute / 60 for t in stamps) / len(stamps))
        out.append({
            "items": [{"item_id": item, "qty": qty} for item, qty in key],
            "times_ordered": len(timestamps),
            "days": days,
            "time": f"{hour % 24:02d}:00",
            "note": (
                f"You have ordered this {len(timestamps)} times. Turn it into a standing "
                "order if you want it placed automatically."
            ),
        })
    return sorted(out, key=lambda s: -s["times_ordered"])
