import pytest

import audit_log
import orchestrator


def test_negotiate_and_record_approves_and_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_log, "DEFAULT_DB_PATH", str(tmp_path / "audit.db"))

    detail = orchestrator.negotiate_and_record("agent-x", "acp", [("masala_dosa", 1)])

    assert detail["decision"] == "APPROVE"
    assert detail["trust_tier"] == "NEW"
    events = audit_log.get_events_for_agent("agent-x", db_path=str(tmp_path / "audit.db"))
    assert len(events) == 1
    assert events[0]["decision"] == "APPROVE"


def test_negotiate_and_record_includes_upsell_only_on_approve(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_log, "DEFAULT_DB_PATH", str(tmp_path / "audit.db"))

    approved = orchestrator.negotiate_and_record("agent-x", "acp", [("masala_dosa", 1)])
    assert "upsell_suggestion" in approved

    escalated = orchestrator.negotiate_and_record("agent-x", "acp", [("chicken_biryani", 2)])
    assert "upsell_suggestion" not in escalated


def test_upsell_becomes_predictive_once_history_exists(tmp_path, monkeypatch):
    """End to end: the orchestrator reads co-purchase history and the
    suggestion changes from best-value to what people actually buy."""
    db_path = str(tmp_path / "audit.db")
    monkeypatch.setattr(audit_log, "DEFAULT_DB_PATH", db_path)

    # Cold start: no history at all, so the suggestion comes from what
    # goes WITH a dosa. It used to be "the priciest thing that fits",
    # which offered a Rs.220 biryani to someone who had just ordered one
    # snack -- an upsell nobody accepts.
    cold = orchestrator.negotiate_and_record("pred-1", "acp", [("masala_dosa", 1)])
    assert cold["upsell_suggestion"]["item"] == "filter_coffee"
    assert cold["upsell_suggestion"]["basis"] == "goes well with this order"

    # Three paid orders where masala_dosa went out with filter_coffee.
    for _ in range(3):
        event_id = audit_log.record_event(
            agent_id="past-buyer",
            protocol="acp",
            cart=[{"item": "masala_dosa", "qty": 1}, {"item": "filter_coffee", "qty": 1}],
            decision="APPROVE",
            reason="within budget",
            total_inr=110,
            db_path=db_path,
        )
        audit_log.mark_paid(event_id, f"pay_{event_id}", db_path=db_path)

    warm = orchestrator.negotiate_and_record("pred-2", "acp", [("masala_dosa", 1)])
    assert warm["upsell_suggestion"]["item"] == "filter_coffee"
    assert warm["upsell_suggestion"]["basis"] == "bought together before"


def test_trust_tier_improves_after_a_completed_order(tmp_path, monkeypatch):
    db_path = str(tmp_path / "audit.db")
    monkeypatch.setattr(audit_log, "DEFAULT_DB_PATH", db_path)

    first = orchestrator.negotiate_and_record("agent-y", "acp", [("masala_dosa", 1)])
    assert first["trust_tier"] == "NEW"
    audit_log.mark_paid(first["event_id"], "pay_test123", db_path=db_path)

    second = orchestrator.negotiate_and_record("agent-y", "acp", [("masala_dosa", 1)])
    assert second["trust_tier"] == "STANDARD"


def test_create_payment_for_cart_rejects_carts_that_no_longer_approve(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_log, "DEFAULT_DB_PATH", str(tmp_path / "audit.db"))
    detail = orchestrator.negotiate_and_record("agent-z", "acp", [("chicken_biryani", 2)])
    assert detail["decision"] == "ESCALATE"

    with pytest.raises(ValueError):
        orchestrator.create_payment_for_cart("agent-z", detail["event_id"], [("chicken_biryani", 2)])


def test_create_payment_for_cart_calls_razorpay_and_attaches_link(tmp_path, monkeypatch):
    db_path = str(tmp_path / "audit.db")
    monkeypatch.setattr(audit_log, "DEFAULT_DB_PATH", db_path)

    fake_link = {"id": "plink_fake123", "short_url": "https://rzp.io/rzp/fake123"}
    monkeypatch.setattr(orchestrator.razorpay_client, "create_payment_link", lambda **kwargs: fake_link)

    detail = orchestrator.negotiate_and_record("agent-w", "acp", [("masala_dosa", 1)])
    link = orchestrator.create_payment_for_cart("agent-w", detail["event_id"], [("masala_dosa", 1)])

    assert link == fake_link
    events = audit_log.get_events_for_agent("agent-w", db_path=db_path)
    assert events[0]["payment_link_id"] == "plink_fake123"


# ------------------------------------------- what actually goes with what

def test_a_cold_start_suggestion_is_a_pairing_not_the_priciest_item(tmp_path, monkeypatch):
    """Two Paneer Bhurji and three Tandoori Roti is dinner for a few
    people. The old fallback offered whatever cost the most and still
    fit -- another main course. A drink is the suggestion someone might
    actually say yes to."""
    monkeypatch.setattr(audit_log, "DEFAULT_DB_PATH", str(tmp_path / "audit.db"))

    detail = orchestrator.negotiate_and_record(
        "pair-1", "acp", [("paneer_bhurji", 1), ("tandoori_roti", 3)]
    )
    suggestion = detail["upsell_suggestion"]

    assert suggestion["item"] == "filter_coffee"
    assert suggestion["basis"] == "goes well with this order"


def test_it_never_suggests_a_second_of_what_they_already_have(tmp_path, monkeypatch):
    """Somebody who ordered a coffee does not want a second coffee, and
    the category they already covered is the one they need least."""
    import merchant_config
    import upsell_ranking

    ranked = upsell_ranking.complements(
        [("filter_coffee", 1)], merchant_config.current_menu()
    )
    beverages = [
        name for name in ranked
        if merchant_config.current_menu()[name].category == "beverages"
    ]
    assert not beverages or ranked.index(beverages[0]) == len(ranked) - 1


def test_history_still_beats_a_pairing(tmp_path, monkeypatch):
    """Evidence outranks opinion. If people really do buy gulab jamun
    with a thali, that wins over the category table."""
    db_path = str(tmp_path / "audit.db")
    monkeypatch.setattr(audit_log, "DEFAULT_DB_PATH", db_path)

    for _ in range(3):
        event_id = audit_log.record_event(
            agent_id="past", protocol="acp",
            cart=[{"item": "veg_thali", "qty": 1}, {"item": "gulab_jamun", "qty": 1}],
            decision="APPROVE", reason="within budget", total_inr=210, db_path=db_path,
        )
        audit_log.mark_paid(event_id, f"pay_{event_id}", db_path=db_path)

    detail = orchestrator.negotiate_and_record("pair-2", "acp", [("veg_thali", 1)])
    assert detail["upsell_suggestion"]["item"] == "gulab_jamun"
    assert detail["upsell_suggestion"]["basis"] == "bought together before"


def test_a_pairing_that_breaks_a_limit_is_still_refused(tmp_path, monkeypatch):
    """The ranking only reorders what already passed the mandate. It can
    never introduce a candidate -- the same property history has."""
    import merchant_config
    import upsell_ranking

    menu = merchant_config.current_menu()
    ranked = upsell_ranking.complements([("veg_thali", 1)], menu)

    # The catering tray is in no allowed category, so however it ranks it
    # must never come back as a suggestion.
    assert "party_catering_tray" in ranked

    monkeypatch.setattr(audit_log, "DEFAULT_DB_PATH", str(tmp_path / "audit.db"))
    detail = orchestrator.negotiate_and_record("pair-3", "acp", [("veg_thali", 1)])
    assert detail["upsell_suggestion"]["item"] != "party_catering_tray"


def test_upsell_ranking_does_no_io():
    """It sits beside the pure core and must stay as pure: same inputs,
    same answer, no database, no model. Checked on REAL imports, not
    string mentions -- the docstring names audit_log deliberately, to say
    what this module is not."""
    import ast

    import merchant_config
    import upsell_ranking

    with open(upsell_ranking.__file__) as handle:
        tree = ast.parse(handle.read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    for forbidden in ("audit_log", "sqlite3", "llm_client", "razorpay_client", "requests"):
        assert forbidden not in imported, forbidden

    cart = [("paneer_bhurji", 1), ("tandoori_roti", 3)]
    menu = merchant_config.current_menu()
    assert upsell_ranking.complements(cart, menu) == upsell_ranking.complements(cart, menu)
