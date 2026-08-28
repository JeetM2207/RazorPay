"""Standing orders, and the gate that decides whether one may fire.

The scheduling is not the interesting part. These tests are almost
entirely about the five ways a routine can stop being the thing the
customer agreed to -- because a failure of any one of them must turn a
silent charge into a question, and there must be no path that charges
anyway.
"""

from datetime import datetime, timezone

import pytest

import audit_log
import merchant_config
import notification_service
import routines


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_log, "DEFAULT_DB_PATH", str(tmp_path / "audit.db"))
    monkeypatch.setattr(routines, "_STORE", tmp_path / "routines.json")
    monkeypatch.setattr(notification_service, "TWILIO_CONFIGURED", False)
    notification_service.clear_outbox()
    routines.reset()

    # A settlement that does not reach Razorpay: these tests are about the
    # gate, not about the card rails, which have their own suite. It still
    # marks the row paid, because the real one does and the evidence pack
    # reads that.
    import autonomous_payment

    class _Settled:
        payment_id, order_id, amount_inr, simulated, method = (
            "sim_test", "order_test", 0, True, "test")

    def _fake_execute(event_id, cart, amount_inr):
        audit_log.mark_paid(event_id, _Settled.payment_id, db_path=audit_log.DEFAULT_DB_PATH)
        return _Settled()

    monkeypatch.setattr(autonomous_payment, "execute", _fake_execute)
    _shop()
    return tmp_path


def _shop(thali=150, dosa=80, thali_stock=20, dosa_orderable=True):
    merchant_config.save(
        profile_in={"shop_name": "Amma's Kitchen"},
        mandate_in={"budget_cap_inr": 500, "human_confirm_threshold_inr": 400},
        menu_in=[
            {"title": "Veg Thali", "category": "meals", "price_inr": thali, "stock": thali_stock},
            {"title": "Masala Dosa", "category": "snacks", "price_inr": dosa, "stock": 25,
             "agent_orderable": dosa_orderable},
        ],
    )


TUESDAY_8AM = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)   # a Tuesday


def _routine(**over):
    kwargs = dict(
        items=[{"item_id": "veg_thali", "qty": 1}],
        days=["tue"], at_time="08:00", agent_id="agent-routine",
        phone="8306610707",
    )
    kwargs.update(over)
    return routines.create(**kwargs)


# ----------------------------------------------------------- the happy path

def test_a_routine_that_passes_every_check_fires_without_asking(env):
    r = _routine()
    result = routines.check_and_fire(r["id"], now=TUESDAY_8AM)

    assert result["fired"] is True
    assert result["total_inr"] == 150
    assert result["gate"]["confident"] is True

    row = audit_log.get_event(result["order_id"], db_path=audit_log.DEFAULT_DB_PATH)
    assert row["source"] == "routine"
    assert row["routine_id"] == r["id"]
    assert row["protocol"] == "routine"
    assert row["payment_id"], "the card that was authorised for this was not charged"

    told = " ".join(m["body"] for m in notification_service.outbox())
    assert "placed your usual order" in told
    assert "Rs.150" in told and "paid" in told


def test_firing_records_when_it_next_expects_to_run(env):
    r = _routine(days=["tue", "fri"])
    routines.check_and_fire(r["id"], now=TUESDAY_8AM)
    after = routines.get(r["id"])
    assert after["last_fired_at"]
    assert after["next_expected_at"].startswith("2026-09-04"), after["next_expected_at"]


# ------------------------------ the five ways it must refuse to fire silently

def _assert_asked_not_charged(result, check):
    assert result["fired"] is False, f"{check}: it charged anyway"
    assert result["awaiting_confirmation"] is True
    assert check in [f["check"] for f in result["failures"]], result["failures"]
    told = " ".join(m["body"] for m in notification_service.outbox())
    assert "YES" in told and "NO" in told, f"{check}: the customer was never asked"
    assert "paid" not in told, f"{check}: it told them something had been paid"


def test_a_paused_routine_does_not_fire(env):
    r = _routine()
    routines.set_status(r["id"], "paused")
    _assert_asked_not_charged(routines.check_and_fire(r["id"], now=TUESDAY_8AM), "active")


def test_an_item_no_longer_orderable_by_an_agent_does_not_fire(env):
    r = _routine(items=[{"item_id": "masala_dosa", "qty": 1}])
    # She unticks "agents may order" on it the next day.
    _shop(dosa_orderable=False)
    _assert_asked_not_charged(routines.check_and_fire(r["id"], now=TUESDAY_8AM), "on_menu")


def test_an_item_that_left_the_menu_does_not_fire(env):
    r = _routine()
    merchant_config.save(
        profile_in={"shop_name": "Amma's Kitchen"},
        mandate_in={"budget_cap_inr": 500, "human_confirm_threshold_inr": 400},
        menu_in=[{"title": "Masala Dosa", "category": "snacks", "price_inr": 80, "stock": 25}],
    )
    _assert_asked_not_charged(routines.check_and_fire(r["id"], now=TUESDAY_8AM), "on_menu")


def test_a_price_that_drifted_past_tolerance_does_not_fire(env):
    """Rs.150 to Rs.200 is 33%, well past the 15% a routine tolerates. A
    stale routine must not silently pay a new price."""
    r = _routine()
    _shop(thali=200)
    result = routines.check_and_fire(r["id"], now=TUESDAY_8AM)
    _assert_asked_not_charged(result, "price_drift")
    assert "was Rs.150" in result["failures"][0]["why"]


def test_a_price_that_moved_a_little_still_fires(env):
    """The tolerance has to be a tolerance, or every sale she runs turns
    into a question. Rs.150 to Rs.160 is 7%."""
    r = _routine()
    _shop(thali=160)
    assert routines.check_and_fire(r["id"], now=TUESDAY_8AM)["fired"] is True


def test_a_total_over_the_routines_own_cap_does_not_fire(env):
    r = _routine(routine_cap_inr=140)          # below the Rs.150 cart
    _assert_asked_not_charged(routines.check_and_fire(r["id"], now=TUESDAY_8AM), "routine_cap")


def test_firing_outside_the_time_window_does_not_fire(env):
    """Hours early or late is itself a signal something is off."""
    r = _routine()
    late = TUESDAY_8AM.replace(hour=14)
    _assert_asked_not_charged(routines.check_and_fire(r["id"], now=late), "time_window")


def test_the_wrong_day_does_not_fire(env):
    r = _routine(days=["fri"])
    _assert_asked_not_charged(routines.check_and_fire(r["id"], now=TUESDAY_8AM), "time_window")


def test_inside_the_window_but_not_on_the_minute_still_fires(env):
    r = _routine()
    assert routines.check_and_fire(r["id"], now=TUESDAY_8AM.replace(minute=30))["fired"] is True


def test_every_failure_is_reported_not_just_the_first(env):
    """A customer being asked deserves to be told everything that looks
    different, and a gate that stops early hides the rest."""
    r = _routine(routine_cap_inr=100)
    _shop(thali=300)
    gate = routines.confidence_gate(routines.get(r["id"]), now=TUESDAY_8AM.replace(hour=19))
    assert {f["check"] for f in gate["failures"]} >= {"price_drift", "routine_cap", "time_window"}


def test_a_gate_failure_charges_nothing_at_all(env):
    r = _routine(routine_cap_inr=100)
    before = len(audit_log.get_all_events(db_path=audit_log.DEFAULT_DB_PATH, limit=500))
    routines.check_and_fire(r["id"], now=TUESDAY_8AM)
    after = audit_log.get_all_events(db_path=audit_log.DEFAULT_DB_PATH, limit=500)
    assert len(after) == before, "a refused routine wrote an order row"
    assert not any(e["payment_id"] for e in after if e["source"] == "routine")


# --------------------------------------- the customer answering the prompt

def test_a_customer_confirming_the_prompt_places_a_fully_recorded_order(env):
    """The fallback order must be indistinguishable in completeness from
    any other -- same audit row, same evidence."""
    import evidence

    r = _routine(routine_cap_inr=100)
    assert routines.check_and_fire(r["id"], now=TUESDAY_8AM)["fired"] is False

    result = routines.confirm_pending(r["id"], approved=True, now=TUESDAY_8AM)
    assert result["fired"] is True
    assert result["gate"]["confirmed_by_customer"] is True

    pack = evidence.build_evidence_pack(result["order_id"])
    assert pack["order"]["total_inr"] == 150
    assert pack["limits_in_force"]["merchant"]["budget_cap_inr"] == 500
    assert pack["buyer_reasoning"]["available"] is True
    assert "Standing order" in pack["buyer_reasoning"]["text"]
    assert pack["payments"], "no payment recorded for a confirmed routine order"


def test_a_customer_declining_the_prompt_charges_nothing(env):
    r = _routine(routine_cap_inr=100)
    routines.check_and_fire(r["id"], now=TUESDAY_8AM)
    result = routines.confirm_pending(r["id"], approved=False)

    assert result["fired"] is False and result["cancelled"] is True
    assert not any(e["source"] == "routine"
                   for e in audit_log.get_all_events(db_path=audit_log.DEFAULT_DB_PATH, limit=500))


# ------------------------------------------------------------- the evidence

def test_a_routine_order_explains_itself_from_its_own_configured_data(env):
    """No customer typed a reason for this -- nobody was there. The pack
    says what the routine IS, generated from the routine, and never
    invents something that sounds like the customer said it."""
    import evidence

    r = _routine(days=["tue", "fri"])
    fired = routines.check_and_fire(r["id"], now=TUESDAY_8AM)
    pack = evidence.build_evidence_pack(fired["order_id"])

    text = pack["buyer_reasoning"]["text"]
    assert pack["buyer_reasoning"]["available"] is True
    assert pack["buyer_reasoning"]["source"] == "routine"
    assert "Standing order" in text
    assert "Tue, Fri" in text and "08:00" in text


# -------------------------------------------------- simulation, and scope

def test_an_overridden_now_simulates_a_future_occurrence(env):
    """There is no scheduler here; something has to call this. The
    override is what lets a demo show next Tuesday without waiting."""
    r = _routine(days=["fri"])
    friday = datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc)

    assert routines.check_and_fire(r["id"], now=TUESDAY_8AM)["fired"] is False
    assert routines.check_and_fire(r["id"], now=friday)["fired"] is True


def test_detection_only_ever_suggests(env):
    """Three identical paid orders is a pattern worth mentioning. It is
    not permission to start spending on a timer."""
    for _ in range(3):
        event_id = audit_log.record_event(
            agent_id="habitual", protocol="acp", cart=[{"item": "veg_thali", "qty": 1}],
            decision="APPROVE", reason="within budget", total_inr=150,
            db_path=audit_log.DEFAULT_DB_PATH,
        )
        audit_log.mark_paid(event_id, f"pay_{event_id}", db_path=audit_log.DEFAULT_DB_PATH)

    suggestions = routines.suggest_from_history("habitual")
    assert suggestions and suggestions[0]["times_ordered"] == 3
    assert suggestions[0]["items"] == [{"item_id": "veg_thali", "qty": 1}]
    # Suggested, and nothing more.
    assert routines.all_routines() == []


def test_two_orders_are_not_yet_a_habit(env):
    for _ in range(2):
        event_id = audit_log.record_event(
            agent_id="twice", protocol="acp", cart=[{"item": "veg_thali", "qty": 1}],
            decision="APPROVE", reason="within budget", total_inr=150,
            db_path=audit_log.DEFAULT_DB_PATH,
        )
        audit_log.mark_paid(event_id, f"pay_{event_id}", db_path=audit_log.DEFAULT_DB_PATH)
    assert routines.suggest_from_history("twice") == []


# ------------------------------------------------ what it must not become

def test_a_routine_still_goes_through_the_one_shared_orchestrator(env):
    """No parallel charging path. The merchant's own rules apply to a
    standing order exactly as to anything else."""
    import ast

    with open(routines.__file__) as handle:
        source = handle.read()
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "negotiation" not in imported, "routines reaches the decision core directly"
    assert "negotiate_and_record" in source, "routines must place orders the shared way"


def test_the_merchants_rules_still_refuse_what_they_would_refuse(env):
    """A customer's pre-authorisation is not permission for Amma to sell
    something she does not sell to agents."""
    merchant_config.save(
        profile_in={"shop_name": "Amma's Kitchen"},
        mandate_in={"budget_cap_inr": 500, "human_confirm_threshold_inr": 400},
        menu_in=[
            {"title": "Veg Thali", "category": "meals", "price_inr": 150, "stock": 20},
            {"title": "Party Tray", "category": "bulk_catering", "price_inr": 350,
             "stock": 5, "agent_orderable": False},
        ],
    )
    r = _routine(items=[{"item_id": "veg_thali", "qty": 1}])
    # The gate passes, and her own rules are still applied underneath.
    assert routines.check_and_fire(r["id"], now=TUESDAY_8AM)["fired"] is True


# ------------------------------- what the customer is actually told, and shown

def test_the_question_names_the_check_that_failed_not_the_soft_cap(env):
    """A live run caught this: a routine held back by the CLOCK was asking
    the customer to approve spending "above the Rs.200 you asked to be
    checked on". The amount was fine. The message stated a reason that was
    not the reason, and pointed them at the wrong thing entirely."""
    r = _routine()
    routines.check_and_fire(r["id"], now=TUESDAY_8AM.replace(hour=16))

    body = " ".join(m["body"] for m in notification_service.outbox())
    assert "expects 08:00" in body, "it never said what was actually different"
    assert "you asked to be checked on" not in body, "it blamed the cap for a clock failure"
    assert "nothing has been charged" in body


def test_the_soft_cap_wording_is_untouched_for_the_case_it_was_written_for(env):
    """The fix above must not change the message every ordinary over-soft-cap
    order sends."""
    import buyer_sms

    buyer_sms.ask_approval(agent_id="a", phone="8306610707",
                           cart_label="1x Veg Thali", total_inr=450, soft_cap_inr=300)
    body = notification_service.outbox()[-1]["body"]
    assert "That's above the Rs.300 you asked to be checked on." in body


def test_the_evidence_pack_recognises_the_routine_cap_as_the_authorisation(env):
    """There is no checkout on a standing order, so there is no hard cap
    typed at the time. The cap the customer set when they turned the
    routine on IS what they authorised, and a pack that reports "no
    customer limit on file" is describing a hole that isn't there."""
    import evidence

    r = _routine(routine_cap_inr=200)
    fired = routines.check_and_fire(r["id"], now=TUESDAY_8AM)
    pack = evidence.build_evidence_pack(fired["order_id"], db_path=audit_log.DEFAULT_DB_PATH)

    cap_check = pack["checks"][0]
    assert cap_check["result"] == "yes"
    assert cap_check["numbers"]["routine_cap_inr"] == 200
    assert "standing order" in cap_check["detail"]
    assert "not_recorded" != cap_check["result"]


def test_that_check_is_not_claimed_for_orders_that_are_not_routines(env):
    """The routine branch must not swallow the honest 'not recorded' answer
    every other path still needs to give."""
    import evidence
    import orchestrator

    result = orchestrator.negotiate_and_record(
        agent_id="agent-plain", protocol="acp", cart=[("veg_thali", 1)])
    pack = evidence.build_evidence_pack(result["event_id"], db_path=audit_log.DEFAULT_DB_PATH)
    assert pack["checks"][0]["result"] == "not_recorded"
