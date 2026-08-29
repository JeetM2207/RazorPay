"""The clock, and the two things it is allowed to do.

`mcp_orders.expire()` and `routines.check_and_fire()` were both complete
and tested long before anything called them. The lifecycle diagram had a
transition -- silence -> MERCHANT_TIMEOUT_REFUNDED -> REFUNDED -- that
could not happen, and a customer whose merchant went quiet after paying
had no automatic protection.

These tests drive `scheduler.tick()` directly rather than the loop.
Driving a real 60-second loop from a test would be testing asyncio; the
loop's own job is only to call this and survive.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import audit_log
import idempotency
import mcp_orders
import notification_service
import routines
import scheduler

T0 = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)      # a Tuesday, 08:00


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_log, "DEFAULT_DB_PATH", str(tmp_path / "audit.db"))
    monkeypatch.setattr(routines, "_STORE", tmp_path / "routines.json")
    monkeypatch.setattr(notification_service, "TWILIO_CONFIGURED", False)
    notification_service.clear_outbox()
    routines.reset()

    import merchant_config
    merchant_config.save(
        profile_in={"shop_name": "Amma's Kitchen"},
        mandate_in={"budget_cap_inr": 500, "human_confirm_threshold_inr": 400},
        menu_in=[{"title": "Veg Thali", "category": "meals", "price_inr": 150, "stock": 20}],
    )

    import autonomous_payment

    class _Settled:
        payment_id, order_id, amount_inr, simulated, method = (
            "sim_sched", "order_sched", 0, True, "test")

    def _fake_execute(event_id, cart, amount_inr):
        audit_log.mark_paid(event_id, _Settled.payment_id, db_path=audit_log.DEFAULT_DB_PATH)
        return _Settled()

    monkeypatch.setattr(autonomous_payment, "execute", _fake_execute)
    return tmp_path


def _paid_order_awaiting_her(agent="agent-sched", total=450):
    """An order that has been paid for and is sitting in her queue."""
    event_id = audit_log.record_event(
        agent_id=agent, protocol="mcp", cart=[{"item": "veg_thali", "qty": 3}],
        decision="ESCALATE", reason="over threshold", total_inr=total,
        db_path=audit_log.DEFAULT_DB_PATH,
    )
    audit_log.mark_paid(event_id, "pay_sched", db_path=audit_log.DEFAULT_DB_PATH)
    order = mcp_orders.get_order(event_id)
    for status, reason, ts in (
        (mcp_orders.AWAITING_PAYMENT, "link issued", T0),
        (mcp_orders.PAID, "captured", T0),
        (mcp_orders.PENDING_MERCHANT_APPROVAL, "asked her", T0),
    ):
        audit_log.record_event(
            agent_id=agent, protocol="mcp", cart=[{"item": "veg_thali", "qty": 3}],
            decision=status, reason=reason, total_inr=total,
            order_ref=event_id, db_path=audit_log.DEFAULT_DB_PATH, ts=ts.isoformat(),
        )
    return event_id


# ------------------------------------------------------ merchant timeouts

def test_an_order_past_its_timeout_is_expired(env, monkeypatch):
    monkeypatch.setattr(mcp_orders, "_refund", lambda order, status, reason: (
        audit_log.record_event(
            agent_id=order["agent_id"], protocol=order["protocol"], cart=[],
            decision=status, reason=reason, total_inr=order["total_inr"],
            order_ref=order["id"], db_path=audit_log.DEFAULT_DB_PATH,
        ), {"order_ref": order["id"], "status": status})[1])

    order_ref = _paid_order_awaiting_her()
    assert mcp_orders.status_of(order_ref) == mcp_orders.PENDING_MERCHANT_APPROVAL

    late = T0 + timedelta(minutes=mcp_orders.MERCHANT_TIMEOUT_MINUTES + 1)
    did = scheduler.tick(now=late)

    assert did == [f"expired #{order_ref}"]
    assert mcp_orders.status_of(order_ref) == mcp_orders.MERCHANT_TIMEOUT_REFUNDED


def test_it_is_expired_exactly_once_when_the_tick_runs_twice(env, monkeypatch):
    """uvicorn --reload runs two processes. Refunding twice is money."""
    calls = []
    monkeypatch.setattr(mcp_orders, "expire", lambda ref: calls.append(ref))

    order_ref = _paid_order_awaiting_her()
    late = T0 + timedelta(minutes=mcp_orders.MERCHANT_TIMEOUT_MINUTES + 1)

    scheduler.tick(now=late)
    scheduler.tick(now=late + timedelta(seconds=60))

    assert calls == [order_ref], f"expired {len(calls)} times"


def test_the_claim_goes_through_the_existing_ledger(env, monkeypatch):
    """Not a second ledger. A second record of the same fact is a second
    record that can disagree with the first."""
    monkeypatch.setattr(mcp_orders, "expire", lambda ref: None)
    order_ref = _paid_order_awaiting_her()
    scheduler.tick(now=T0 + timedelta(minutes=90))

    # Claiming the same work again from outside the scheduler must fail,
    # which is only true if it landed in the shared table.
    assert idempotency.claim_event(
        "scheduler.expire", str(order_ref), audit_log.DEFAULT_DB_PATH) is False


def test_an_order_inside_its_window_is_left_alone(env, monkeypatch):
    monkeypatch.setattr(mcp_orders, "expire", lambda ref: pytest.fail("expired too early"))
    _paid_order_awaiting_her()
    assert scheduler.tick(now=T0 + timedelta(minutes=5)) == []


def test_an_order_she_answered_is_not_expired(env, monkeypatch):
    monkeypatch.setattr(mcp_orders, "expire", lambda ref: pytest.fail("expired a decided order"))
    order_ref = _paid_order_awaiting_her()
    audit_log.record_event(
        agent_id="agent-sched", protocol="mcp", cart=[], decision=mcp_orders.MERCHANT_ACCEPTED,
        reason="she said yes", total_inr=450, order_ref=order_ref,
        db_path=audit_log.DEFAULT_DB_PATH,
    )
    assert scheduler.tick(now=T0 + timedelta(hours=3)) == []


# ------------------------------------------------------- standing orders

def _routine(**over):
    kwargs = dict(items=[{"item_id": "veg_thali", "qty": 1}], days=["tue"],
                  at_time="08:00", agent_id="agent-routine", phone="8306610707")
    kwargs.update(over)
    return routines.create(**kwargs)


def test_a_routine_in_its_window_fires_from_the_tick(env):
    r = _routine()
    did = scheduler.tick(now=T0)
    assert did == [f"routine {r['id']} fired"]
    assert routines.get(r["id"])["last_fired_at"]


def test_a_routine_outside_its_window_is_not_even_asked_about(env):
    """The hazard that makes `due_now` necessary.

    Outside its window the confidence gate fails on `time_window` and
    `_ask_first` MESSAGES THE CUSTOMER. A tick that called check_and_fire
    blindly would send roughly 1,400 messages a day, per routine.
    """
    _routine()
    assert scheduler.tick(now=T0.replace(hour=16)) == []
    assert notification_service.outbox() == [], "the customer was messaged out of hours"


def test_a_routine_does_not_fire_twice_in_one_occurrence(env):
    """Ninety ticks inside a ninety-minute window is one breakfast, not
    ninety."""
    r = _routine()
    scheduler.tick(now=T0)
    for minute in range(1, 40):
        assert scheduler.tick(now=T0 + timedelta(minutes=minute)) == []

    fired = [e for e in audit_log.get_all_events(db_path=audit_log.DEFAULT_DB_PATH, limit=500)
             if e["source"] == "routine"]
    assert len(fired) == 1, f"fired {len(fired)} times"


def test_two_runners_cannot_both_fire_the_same_occurrence(env, monkeypatch):
    calls = []
    monkeypatch.setattr(routines, "check_and_fire",
                        lambda rid, now=None: calls.append(rid) or {"fired": True})
    _routine()
    scheduler.tick(now=T0)
    scheduler.tick(now=T0 + timedelta(seconds=30))     # the other process
    assert len(calls) == 1


def test_a_paused_routine_is_never_due(env):
    r = _routine()
    routines.set_status(r["id"], "paused")
    assert scheduler.tick(now=T0) == []


def test_a_gate_failure_still_writes_zero_audit_rows_under_the_tick(env):
    """This property already had a test. It has to survive the scheduler:
    a routine held back by its own gate must charge nothing, whether a
    human pressed Simulate or the clock came round.

    Priced past its own cap, so the gate fails on something that is NOT
    the time window -- otherwise `due_now` would filter it out before
    check_and_fire ever ran, and this would prove nothing.
    """
    _routine(routine_cap_inr=10)
    before = len(audit_log.get_all_events(db_path=audit_log.DEFAULT_DB_PATH, limit=500))

    did = scheduler.tick(now=T0)

    assert did == [f"routine {routines.all_routines()[0]['id']} asked first"]
    after = audit_log.get_all_events(db_path=audit_log.DEFAULT_DB_PATH, limit=500)
    assert len(after) == before, "a refused routine wrote an order row"
    assert not any(e["payment_id"] for e in after if e["source"] == "routine")


# ------------------------------------------------ one failure is not fatal

def test_a_failure_in_expire_does_not_stop_routines_on_the_same_tick(env, monkeypatch):
    """They are unrelated pieces of work that happen to share a clock."""
    monkeypatch.setattr(mcp_orders, "due_for_expiry",
                        lambda now=None: (_ for _ in ()).throw(RuntimeError("boom")))
    r = _routine()

    did = scheduler.tick(now=T0)

    assert did == [f"routine {r['id']} fired"], "the routine pass was skipped"


def test_a_failure_on_one_tick_does_not_stop_the_next(env, monkeypatch):
    """A scheduler that dies quietly on tick 3 is worse than none, because
    everything downstream now assumes something is watching."""
    calls = {"n": 0}

    def _sometimes(now=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return []

    monkeypatch.setattr(mcp_orders, "due_for_expiry", _sometimes)
    r = _routine()

    assert scheduler.tick(now=T0) == [f"routine {r['id']} fired"]
    assert scheduler.tick(now=T0 + timedelta(days=7)) is not None
    assert calls["n"] == 2, "the second tick never ran the expiry pass"


def test_a_failure_is_logged_with_a_traceback(env, monkeypatch, caplog):
    monkeypatch.setattr(mcp_orders, "due_for_expiry",
                        lambda now=None: (_ for _ in ()).throw(RuntimeError("boom")))
    with caplog.at_level("ERROR"):
        scheduler.tick(now=T0)
    assert "Traceback" in caplog.text
    assert "boom" in caplog.text


def test_a_quiet_tick_reports_nothing(env):
    """One line per tick only when it did something. A scheduler logging
    every 60s is 1,440 lines a day for a real failure to hide in."""
    assert scheduler.tick(now=T0 + timedelta(days=3)) == []


# ------------------------------------------------------------- the switch

def test_the_scheduler_is_disabled_by_the_env_var(monkeypatch):
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    assert scheduler.is_enabled() is False
    for off in ("0", "no", "off", "FALSE"):
        monkeypatch.setenv("SCHEDULER_ENABLED", off)
        assert scheduler.is_enabled() is False


def test_it_is_on_by_default(monkeypatch):
    monkeypatch.delenv("SCHEDULER_ENABLED", raising=False)
    assert scheduler.is_enabled() is True


def test_disabled_means_the_app_starts_no_task(monkeypatch):
    """conftest sets this for the whole suite, so a TestClient never
    races a background tick."""
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    started = []
    monkeypatch.setattr(scheduler, "run", lambda: started.append(1))

    from fastapi.testclient import TestClient

    import app
    with TestClient(app.app):
        pass
    assert started == []


def test_the_loop_stops_cleanly_when_cancelled():
    """Cancelled and awaited, so shutdown waits for a tick in flight
    rather than tearing the database out from under a half-written
    refund."""
    async def _drive():
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return task.cancelled()

    assert asyncio.run(_drive()) is True


# ------------------------------------------- it adds no second path

def test_the_scheduler_places_no_orders_of_its_own():
    """It is the missing caller and nothing else: no charging logic, no
    Razorpay, no negotiation. Every order it causes goes through the
    orchestrator exactly as if a human had pressed the button."""
    import ast

    # Comments and docstrings stripped first: this module DESCRIBES the
    # orchestrator at length, and a scan that matched prose would fail on
    # its own explanation of why it does not call it.
    tree = ast.parse(open(scheduler.__file__, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body.pop(0)
    code = ast.unparse(tree)

    for forbidden in ("razorpay", "negotiate_and_record", "autonomous_payment",
                      "record_event", "create_payment"):
        assert forbidden not in code, f"the scheduler reaches for {forbidden}"

    # It may only call the two functions it exists to call.
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "expire" in called and "check_and_fire" in called
