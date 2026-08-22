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


def test_events_are_isolated_per_agent(tmp_path):
    db_path = str(tmp_path / "audit.db")
    audit_log.record_event("agent-a", "acp", [], "APPROVE", "ok", 100, db_path=db_path)
    audit_log.record_event("agent-b", "ap2", [], "ESCALATE", "too big", 900, db_path=db_path)

    assert len(audit_log.get_events_for_agent("agent-a", db_path=db_path)) == 1
    assert len(audit_log.get_events_for_agent("agent-b", db_path=db_path)) == 1
    assert len(audit_log.get_all_events(db_path=db_path)) == 2
