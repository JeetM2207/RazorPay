"""Per-agent rate and spend limits.

Every other rule here is per-cart, and that leaves a hole you can drive
through using only valid requests: two hundred Rs.399 orders in ninety
seconds, each one clearing the budget cap, the category check and the
confirmation threshold, and nothing anywhere counting. "Bounded" was
bounded per order and unbounded in aggregate.

The clock is injected everywhere rather than slept on, so the flood test
runs in milliseconds.
"""

from datetime import datetime, timedelta, timezone

import pytest

import audit_log
import merchant_config
import negotiation
import notification_service
import orchestrator
import trust
import velocity

T0 = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def shop(monkeypatch, tmp_path):
    """Her real limits, not the wide ones conftest gives the rest of the
    suite. These tests are the ones that exercise the actual numbers."""
    monkeypatch.setattr(audit_log, "DEFAULT_DB_PATH", str(tmp_path / "audit.db"))
    monkeypatch.setattr(notification_service, "TWILIO_CONFIGURED", False)
    monkeypatch.setattr(velocity, "default_limits", velocity.VelocityLimits)
    merchant_config.reset_to_defaults()
    notification_service.clear_outbox()
    orchestrator.reset_alerts()
    return merchant_config.current_velocity_limits()


def _limits(orders=6, spend=2000):
    merchant_config._load()["velocity"] = {
        "max_orders_per_hour": orders,
        "max_spend_per_day_inr": spend,
    }


def _order(agent="agent-flood", cart=(("masala_dosa", 1),), at=T0):
    return orchestrator.negotiate_and_record(agent, "acp", list(cart), now=at)


# --------------------------------------------------------------- the flood

def test_two_hundred_compliant_carts_are_stopped_at_the_limit(shop, monkeypatch):
    """The attack, exactly as described: every cart is individually fine.

    Rs.68 masala dosa -- under the Rs.500 cap, under the Rs.400 threshold,
    in an allowed category, in stock. Nothing about any single one of them
    is refusable, which is the whole point.
    """
    _limits(orders=6, spend=1_000_000)          # isolate the rate limit
    calls = []
    monkeypatch.setattr(orchestrator.razorpay_client, "create_payment_link",
                        lambda *a, **k: calls.append(a) or {})

    accepted, refused = 0, 0
    for i in range(200):
        at = T0 + timedelta(seconds=i * 0.45)   # 200 orders in 90 seconds
        try:
            result = _order(at=at)
            assert result["decision"] == "APPROVE", "the cart itself was always compliant"
            accepted += 1
        except orchestrator.VelocityRefused:
            refused += 1

    # STANDARD tier is never reached (nothing is paid), so NEW's 0.5x
    # applies: 6 * 0.5 = 3.
    assert accepted == 3, f"let {accepted} through"
    assert refused == 197
    assert calls == [], "a refused order reached Razorpay"


def test_the_refused_order_never_reaches_razorpay(shop, monkeypatch):
    _limits(orders=1, spend=1_000_000)
    boom = lambda *a, **k: pytest.fail("Razorpay was called for a refused order")
    monkeypatch.setattr(orchestrator.razorpay_client, "create_payment_link", boom)
    monkeypatch.setattr(orchestrator.razorpay_client, "create_order", boom)

    _order(at=T0)
    with pytest.raises(orchestrator.VelocityRefused):
        _order(at=T0 + timedelta(seconds=1))


def test_a_refusal_writes_exactly_one_audit_row(shop):
    _limits(orders=1, spend=1_000_000)
    _order(at=T0)
    before = len(audit_log.get_all_events(db_path=audit_log.DEFAULT_DB_PATH, limit=500))

    with pytest.raises(orchestrator.VelocityRefused):
        _order(at=T0 + timedelta(seconds=1))

    rows = audit_log.get_all_events(db_path=audit_log.DEFAULT_DB_PATH, limit=500)
    assert len(rows) == before + 1
    assert rows[0]["decision"] == velocity.DECISION
    assert "rate limit" in rows[0]["reason"]
    assert rows[0]["payment_id"] is None and rows[0]["payment_link_id"] is None


def test_she_is_told_once_per_window_not_once_per_order(shop):
    """Two hundred refusals is two hundred messages, which is the same
    denial of service arriving by a different route."""
    _limits(orders=1, spend=1_000_000)
    _order(at=T0)

    for i in range(50):
        with pytest.raises(orchestrator.VelocityRefused):
            _order(at=T0 + timedelta(seconds=i + 1))

    alerts = [m for m in notification_service.outbox() if "ordering unusually fast" in m["body"]]
    assert len(alerts) == 1, f"she got {len(alerts)} messages"
    assert "Nothing was charged" in alerts[0]["body"]


def test_the_refusal_is_not_an_escalation(shop):
    """A flood is not something a human approves one order at a time, and
    putting 200 approvals in her queue IS the denial of service."""
    import escalations

    escalations.reset()
    _limits(orders=1, spend=1_000_000)
    _order(at=T0)
    with pytest.raises(orchestrator.VelocityRefused) as caught:
        _order(at=T0 + timedelta(seconds=1))

    assert caught.value.payload["decision"] == velocity.DECISION
    assert caught.value.payload["decision"] != "ESCALATE"
    assert escalations.pending() == [], "a rate refusal landed in her queue"


# ------------------------------------------------------------ spend limit

def test_the_daily_spend_limit_refuses_before_it_is_exceeded(shop):
    # 600 with NEW's 0.5x is an effective 300: one Rs.220 biryani fits,
    # two do not.
    _limits(orders=1000, spend=600)
    _order(cart=(("chicken_biryani", 1),), at=T0)          # Rs.220
    with pytest.raises(orchestrator.VelocityRefused) as caught:
        _order(cart=(("chicken_biryani", 1),), at=T0 + timedelta(minutes=5))
    assert "daily spend limit" in caught.value.detail


def test_yesterdays_spend_does_not_count_against_today(shop):
    _limits(orders=1000, spend=600)
    _order(cart=(("chicken_biryani", 1),), at=T0)
    later = _order(cart=(("chicken_biryani", 1),), at=T0 + timedelta(hours=25))
    assert later["decision"] == "APPROVE"


def test_an_hour_later_the_rate_window_has_moved_on(shop):
    _limits(orders=1, spend=1_000_000)
    _order(at=T0)
    with pytest.raises(orchestrator.VelocityRefused):
        _order(at=T0 + timedelta(minutes=30))
    assert _order(at=T0 + timedelta(hours=1, minutes=1))["decision"] == "APPROVE"


# ------------------------------------------------------- what counts

def test_an_unpaid_approval_still_occupies_the_window(shop):
    """It has to. If only settled orders counted, an attacker who never
    pays would never trip the limit -- which is the flood itself."""
    _limits(orders=4, spend=1_000_000)      # NEW's 0.5x -> 2
    _order(at=T0)
    _order(at=T0 + timedelta(seconds=1))
    orders, _ = velocity.usage("agent-flood", now=T0 + timedelta(seconds=2),
                               db_path=audit_log.DEFAULT_DB_PATH)
    assert orders == 2


def test_our_own_refusals_do_not_count_against_the_agent(shop):
    """A gate that counted its own refusals would ratchet: one breach
    would hold the window shut long after the traffic stopped."""
    _limits(orders=1, spend=1_000_000)
    _order(at=T0)
    for i in range(5):
        with pytest.raises(orchestrator.VelocityRefused):
            _order(at=T0 + timedelta(seconds=i + 1))

    orders, _ = velocity.usage("agent-flood", now=T0 + timedelta(seconds=10),
                               db_path=audit_log.DEFAULT_DB_PATH)
    assert orders == 1, "refusals were counted as orders"


def test_lifecycle_rows_do_not_multiply_one_order(shop):
    """The pay-first flow writes five rows for one order."""
    _limits(orders=1000, spend=1_000_000)
    placed = _order(at=T0)
    for state in ("AWAITING_PAYMENT", "PAID", "AUTO_CONFIRMED"):
        audit_log.record_event(
            agent_id="agent-flood", protocol="acp", cart=[], decision=state,
            reason=state, total_inr=68, order_ref=placed["event_id"],
            db_path=audit_log.DEFAULT_DB_PATH,
        )
    orders, _ = velocity.usage("agent-flood", now=T0 + timedelta(seconds=5),
                               db_path=audit_log.DEFAULT_DB_PATH)
    assert orders == 1


def test_a_refunded_order_is_not_counted_as_spend(shop):
    _limits(orders=1000, spend=1_000_000)
    placed = _order(cart=(("chicken_biryani", 1),), at=T0)
    _, spend = velocity.usage("agent-flood", now=T0 + timedelta(seconds=1),
                              db_path=audit_log.DEFAULT_DB_PATH)
    assert spend == 220

    audit_log.record_event(
        agent_id="agent-flood", protocol="acp", cart=[], decision="REFUNDED",
        reason="merchant declined", total_inr=220, order_ref=placed["event_id"],
        db_path=audit_log.DEFAULT_DB_PATH,
    )
    _, after = velocity.usage("agent-flood", now=T0 + timedelta(seconds=2),
                              db_path=audit_log.DEFAULT_DB_PATH)
    assert after == 0, "money that came back was counted as spend"


def test_the_limit_is_per_agent_not_global(shop):
    _limits(orders=1, spend=1_000_000)
    _order(agent="agent-a", at=T0)
    with pytest.raises(orchestrator.VelocityRefused):
        _order(agent="agent-a", at=T0 + timedelta(seconds=1))
    assert _order(agent="agent-b", at=T0 + timedelta(seconds=2))["decision"] == "APPROVE"


# ------------------------------------------------------------- trust

def _make_trusted(agent, db_path, n=trust.TRUSTED_MIN_COMPLETED):
    for i in range(n):
        event_id = audit_log.record_event(
            agent_id=agent, protocol="acp", cart=[{"item": "masala_dosa", "qty": 1}],
            decision="APPROVE", reason="fine", total_inr=68, db_path=db_path,
        )
        audit_log.mark_paid(event_id, f"pay_hist_{i}", db_path=db_path)


def test_a_trusted_agent_gets_a_wider_window_than_a_new_one(shop):
    db = audit_log.DEFAULT_DB_PATH
    _limits(orders=6, spend=1_000_000)

    # History placed a long time ago, so it is outside the rate window and
    # only affects the TIER, not the count.
    old = T0 - timedelta(days=30)
    _make_trusted("agent-trusted", db)
    for row in audit_log.get_events_for_agent("agent-trusted", db_path=db):
        import sqlite3
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE audit_events SET ts = ? WHERE id = ?", (old.isoformat(), row["id"]))

    assert trust.compute_trust_tier("agent-trusted", db_path=db) == trust.TrustTier.TRUSTED
    assert trust.compute_trust_tier("agent-new", db_path=db) == trust.TrustTier.NEW

    # NEW: 6 * 0.5 = 3. TRUSTED: 6 * 1.5 = 9.
    new_allowed = 0
    for i in range(12):
        try:
            _order(agent="agent-new", at=T0 + timedelta(seconds=i))
            new_allowed += 1
        except orchestrator.VelocityRefused:
            break

    trusted_allowed = 0
    for i in range(12):
        try:
            _order(agent="agent-trusted", at=T0 + timedelta(seconds=i))
            trusted_allowed += 1
        except orchestrator.VelocityRefused:
            break

    assert new_allowed == 3
    assert trusted_allowed == 9
    assert trusted_allowed > new_allowed


def test_trust_never_widens_the_cap_or_the_threshold(shop):
    """The rule this module has held since it was written, restated for
    the second lever: trust buys a smoother negotiation, never a bigger
    blast radius."""
    base = merchant_config.current_mandate()
    for tier in trust.TrustTier:
        adjusted, _ = trust.trust_adjusted_mandate("x", base)
        assert adjusted.budget_cap_inr == base.budget_cap_inr
        assert adjusted.human_confirm_threshold_inr == base.human_confirm_threshold_inr
        # And the velocity lever is a plain number, so there is no route
        # by which it could hand back something with a bigger cap in it.
        assert isinstance(trust.velocity_multiplier(tier), float)

    assert "budget_cap" not in str(trust.TIER_VELOCITY_MULTIPLIER)
    assert "threshold" not in str(trust.TIER_VELOCITY_MULTIPLIER)


def test_a_narrowing_multiplier_can_never_reach_zero(shop):
    """Otherwise a NEW agent with a tight limit could not order at all."""
    assert velocity._scaled(1, 0.5) == 1
    assert velocity._scaled(0, 0.5) == 1


# ------------------------------------------------------------- routines

def test_a_routine_firing_into_a_breached_window_is_refused(shop, monkeypatch):
    """A standing order is not exempt. Passing its own confidence gate
    says it still looks like what the customer agreed to; it says nothing
    about how much this agent has already ordered today."""
    import routines

    monkeypatch.setattr(routines, "_STORE", __import__("pathlib").Path(
        str(audit_log.DEFAULT_DB_PATH) + ".routines.json"))
    routines.reset()
    _limits(orders=1, spend=1_000_000)

    r = routines.create(items=[{"item_id": "masala_dosa", "qty": 1}],
                        days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                        at_time="12:00", agent_id="agent-routine", phone="8306610707")

    assert routines.check_and_fire(r["id"], now=T0)["fired"] is True
    second = routines.check_and_fire(r["id"], now=T0 + timedelta(minutes=1))
    assert second["fired"] is False
    assert second["detail"]["decision"] == velocity.DECISION


# ------------------------------------------- the core never learns of it

def test_negotiation_has_never_heard_of_velocity():
    """Same purity assertion as the others, on real imports rather than
    string mentions -- plus the strings, because this rule is about the
    core not knowing the CONCEPT exists, not only the module."""
    import sys

    source = open(negotiation.__file__, encoding="utf-8").read()
    for forbidden in ("velocity", "per_hour", "per_day"):
        assert forbidden not in source, f"negotiation.py mentions {forbidden}"

    imported = {
        name for name, module in sys.modules.items()
        if module and getattr(module, "__name__", "") == "negotiation"
    }
    assert imported, "negotiation was not importable"

    import negotiation as core
    for attr in dir(core):
        value = getattr(core, attr)
        assert getattr(value, "__name__", "") != "velocity", "negotiation imports velocity"


def test_the_gate_runs_after_the_core_not_instead_of_it(shop):
    """The trail should record what she WOULD have decided, so a refusal
    still carries the cart's real total."""
    _limits(orders=1, spend=1_000_000)
    _order(cart=(("chicken_biryani", 1),), at=T0)
    with pytest.raises(orchestrator.VelocityRefused) as caught:
        _order(cart=(("chicken_biryani", 1),), at=T0 + timedelta(seconds=1))
    assert caught.value.payload["total_inr"] == 220


# ----------------------------------------------------- the snapshot

def test_the_limits_in_force_are_recorded_on_the_order(shop):
    """Same reason the cap snapshot exists: she edits these whenever she
    likes, and an evidence pack that referenced the live config would
    describe limits that were never applied."""
    _limits(orders=6, spend=2000)
    placed = _order(at=T0)

    row = audit_log.get_event(placed["event_id"], db_path=audit_log.DEFAULT_DB_PATH)
    import json
    snap = json.loads(row["limits_snapshot"])["velocity"]
    assert snap["max_orders_per_hour"] == 6
    assert snap["max_spend_per_day_inr"] == 2000
    assert snap["tier_multiplier"] == 0.5          # NEW
    assert snap["effective_orders_per_hour"] == 3

    _limits(orders=99, spend=99999)
    again = json.loads(
        audit_log.get_event(placed["event_id"], db_path=audit_log.DEFAULT_DB_PATH)["limits_snapshot"]
    )["velocity"]
    assert again["max_orders_per_hour"] == 6, "the snapshot moved when she edited her limits"


def test_the_snapshot_is_written_on_a_refusal_too(shop):
    _limits(orders=1, spend=1_000_000)
    _order(at=T0)
    with pytest.raises(orchestrator.VelocityRefused) as caught:
        _order(at=T0 + timedelta(seconds=1))

    import json
    row = audit_log.get_event(caught.value.event_id, db_path=audit_log.DEFAULT_DB_PATH)
    assert json.loads(row["limits_snapshot"])["velocity"]["max_orders_per_hour"] == 1
