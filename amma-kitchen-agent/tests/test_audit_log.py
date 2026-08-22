import audit_log


def test_record_and_retrieve_event(tmp_path):
    db_path = str(tmp_path / "audit.db")

    event_id = audit_log.record_event(
        agent_id="agent-a",
        protocol="acp",
        cart=[{"item": "veg_thali", "qty": 1}],
        decision="APPROVE",
        reason="within budget",
        total_inr=150,
        db_path=db_path,
    )

    events = audit_log.get_events_for_agent("agent-a", db_path=db_path)
    assert len(events) == 1
    assert events[0]["id"] == event_id
    assert events[0]["decision"] == "APPROVE"
    assert events[0]["payment_id"] is None


def test_mark_paid_updates_existing_event(tmp_path):
    db_path = str(tmp_path / "audit.db")
    event_id = audit_log.record_event(
        agent_id="agent-a",
        protocol="acp",
        cart=[{"item": "veg_thali", "qty": 1}],
        decision="APPROVE",
        reason="within budget",
        total_inr=150,
        db_path=db_path,
    )

    audit_log.mark_paid(event_id, "pay_xyz", db_path=db_path)

    events = audit_log.get_events_for_agent("agent-a", db_path=db_path)
    assert events[0]["payment_id"] == "pay_xyz"


def _paid_order(db_path, items, agent_id="buyer"):
    event_id = audit_log.record_event(
        agent_id=agent_id,
        protocol="acp",
        cart=[{"item": name, "qty": 1} for name in items],
        decision="APPROVE",
        reason="within budget",
        total_inr=100,
        db_path=db_path,
    )
    audit_log.mark_paid(event_id, f"pay_{event_id}", db_path=db_path)
    return event_id


def test_frequent_addons_ranks_by_how_often_bought_together(tmp_path):
    db_path = str(tmp_path / "audit.db")
    # filter_coffee appears with veg_thali 3 times, gulab_jamun once.
    for _ in range(3):
        _paid_order(db_path, ["veg_thali", "filter_coffee"])
    _paid_order(db_path, ["veg_thali", "gulab_jamun"])

    ranked = audit_log.get_frequent_addons(["veg_thali"], db_path=db_path)
    assert ranked[0] == "filter_coffee"
    assert "gulab_jamun" in ranked


def test_frequent_addons_excludes_items_already_in_the_cart(tmp_path):
    db_path = str(tmp_path / "audit.db")
    _paid_order(db_path, ["veg_thali", "filter_coffee"])

    ranked = audit_log.get_frequent_addons(["veg_thali", "filter_coffee"], db_path=db_path)
    assert "veg_thali" not in ranked
    assert "filter_coffee" not in ranked


def test_frequent_addons_ignores_orders_that_were_never_paid(tmp_path):
    db_path = str(tmp_path / "audit.db")
    # Approved but abandoned at checkout: not evidence anyone wanted this.
    audit_log.record_event(
        agent_id="buyer",
        protocol="acp",
        cart=[{"item": "veg_thali", "qty": 1}, {"item": "chicken_biryani", "qty": 1}],
        decision="APPROVE",
        reason="within budget",
        total_inr=370,
        db_path=db_path,
    )
    assert audit_log.get_frequent_addons(["veg_thali"], db_path=db_path) == []

    _paid_order(db_path, ["veg_thali", "filter_coffee"])
    assert audit_log.get_frequent_addons(["veg_thali"], db_path=db_path) == ["filter_coffee"]


def test_frequent_addons_is_empty_with_no_history(tmp_path):
    db_path = str(tmp_path / "audit.db")
    assert audit_log.get_frequent_addons(["veg_thali"], db_path=db_path) == []
    assert audit_log.get_frequent_addons([], db_path=db_path) == []


def test_frequent_addons_ordering_is_stable_on_ties(tmp_path):
    db_path = str(tmp_path / "audit.db")
    _paid_order(db_path, ["veg_thali", "gulab_jamun"])
    _paid_order(db_path, ["veg_thali", "filter_coffee"])

    first = audit_log.get_frequent_addons(["veg_thali"], db_path=db_path)
    second = audit_log.get_frequent_addons(["veg_thali"], db_path=db_path)
    assert first == second, "tied results must not reorder between calls"


def test_events_are_isolated_per_agent(tmp_path):
    db_path = str(tmp_path / "audit.db")
    audit_log.record_event("agent-a", "acp", [], "APPROVE", "ok", 100, db_path=db_path)
    audit_log.record_event("agent-b", "ap2", [], "ESCALATE", "too big", 900, db_path=db_path)

    assert len(audit_log.get_events_for_agent("agent-a", db_path=db_path)) == 1
    assert len(audit_log.get_events_for_agent("agent-b", db_path=db_path)) == 1
    assert len(audit_log.get_all_events(db_path=db_path)) == 2
