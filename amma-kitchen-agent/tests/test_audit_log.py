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


def test_concurrent_init_does_not_collide_on_the_migration(tmp_path):
    """FastAPI serves sync endpoints from a threadpool, so several
    requests can enter init_db at once on a database that predates the
    added columns. Two of them racing must not 500 the loser.
    """
    import sqlite3
    import threading

    db_path = str(tmp_path / "audit.db")
    # A database at the ORIGINAL schema, before the columns were added.
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE audit_events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, "
            "agent_id TEXT NOT NULL, protocol TEXT NOT NULL, cart_json TEXT NOT NULL, "
            "decision TEXT NOT NULL, reason TEXT NOT NULL, total_inr INTEGER NOT NULL, "
            "payment_id TEXT, payment_link_id TEXT)"
        )

    errors = []
    barrier = threading.Barrier(8)

    def migrate():
        try:
            barrier.wait(timeout=5)
            audit_log.init_db(db_path)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=migrate) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == [], f"concurrent migration failed: {errors}"

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(audit_events)")}
    assert {"buyer_reasoning", "delivery_name", "delivery_phone", "delivery_address"} <= columns


def test_an_older_database_gains_the_new_columns(tmp_path):
    """An existing audit.db must not have to be thrown away."""
    import sqlite3

    db_path = str(tmp_path / "audit.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE audit_events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, "
            "agent_id TEXT NOT NULL, protocol TEXT NOT NULL, cart_json TEXT NOT NULL, "
            "decision TEXT NOT NULL, reason TEXT NOT NULL, total_inr INTEGER NOT NULL, "
            "payment_id TEXT, payment_link_id TEXT)"
        )
        conn.execute(
            "INSERT INTO audit_events (ts, agent_id, protocol, cart_json, decision, reason, total_inr) "
            "VALUES ('2026-01-01', 'old-agent', 'acp', '[]', 'APPROVE', 'legacy row', 100)"
        )

    event_id = audit_log.record_event(
        "new-agent", "mcp", [{"item": "masala_dosa", "qty": 1}], "APPROVE", "ok", 80,
        db_path=db_path,
    )
    audit_log.attach_buyer_reasoning(event_id, "because they asked", db_path=db_path)

    events = audit_log.get_all_events(db_path=db_path)
    assert len(events) == 2, "the pre-existing row was lost"
    assert events[0]["buyer_reasoning"] == "because they asked"
    old = [e for e in events if e["agent_id"] == "old-agent"][0]
    assert old["buyer_reasoning"] is None


def test_events_are_isolated_per_agent(tmp_path):
    db_path = str(tmp_path / "audit.db")
    audit_log.record_event("agent-a", "acp", [], "APPROVE", "ok", 100, db_path=db_path)
    audit_log.record_event("agent-b", "ap2", [], "ESCALATE", "too big", 900, db_path=db_path)

    assert len(audit_log.get_events_for_agent("agent-a", db_path=db_path)) == 1
    assert len(audit_log.get_events_for_agent("agent-b", db_path=db_path)) == 1
    assert len(audit_log.get_all_events(db_path=db_path)) == 2


# ------------------------------------- growth insights: read-only, additive

def _paid(db_path, agent, cart, total, payment_id, decision="APPROVE"):
    event_id = audit_log.record_event(
        agent_id=agent, protocol="mcp", cart=cart, decision=decision,
        reason="within budget", total_inr=total, db_path=db_path,
    )
    if payment_id:
        audit_log.mark_paid(event_id, payment_id, db_path=db_path)
    return event_id


def test_growth_stats_counts_only_money_that_actually_settled(tmp_path):
    db_path = str(tmp_path / "audit.db")
    _paid(db_path, "a1", [{"item": "veg_thali", "qty": 1}], 150, "pay_real")
    # A simulated settlement is an assertion of ours, not money that moved.
    _paid(db_path, "a2", [{"item": "veg_thali", "qty": 1}], 150, "sim_notreal")
    # And an approved order nobody ever paid for is not revenue either.
    _paid(db_path, "a3", [{"item": "veg_thali", "qty": 1}], 150, None)

    stats = audit_log.growth_stats(24, db_path=db_path)
    assert stats["revenue_inr"] == 150
    assert stats["orders_paid"] == 1


def test_growth_stats_does_not_count_a_refunded_order_as_revenue(tmp_path):
    db_path = str(tmp_path / "audit.db")
    order = _paid(db_path, "a1", [{"item": "veg_thali", "qty": 3}], 450, "pay_x",
                  decision="ESCALATE")
    audit_log.record_event(
        agent_id="a1", protocol="mcp", cart=[{"item": "veg_thali", "qty": 3}],
        decision="REFUNDED", reason="merchant declined", total_inr=450,
        db_path=db_path, order_ref=order,
    )

    stats = audit_log.growth_stats(24, db_path=db_path)
    assert stats["revenue_inr"] == 0, "money she gave back is not money she made"
    assert stats["refunded_orders"] == 1
    assert stats["refunded_inr"] == 450


def test_growth_stats_ranks_the_top_three_things_she_does_not_sell(tmp_path):
    db_path = str(tmp_path / "audit.db")
    for _ in range(3):
        audit_log.record_unmatched_demand("a1", "mcp", "2 pizzas", db_path=db_path)
    for _ in range(2):
        audit_log.record_unmatched_demand("a1", "mcp", "Tiramisu", db_path=db_path)
    audit_log.record_unmatched_demand("a1", "mcp", "burger", db_path=db_path)
    audit_log.record_unmatched_demand("a1", "mcp", "sushi", db_path=db_path)

    demand = audit_log.growth_stats(24, db_path=db_path)["unmatched_demand"]
    assert [d["requested"] for d in demand] == ["2 pizzas", "tiramisu", "burger"]
    assert demand[0]["times"] == 3


def test_growth_stats_infers_an_accepted_addon_from_the_trail(tmp_path):
    """A customer who says yes to an add-on causes the same cart to be
    proposed again with exactly one more line, and it is the second one
    that gets paid for. Inferred rather than recorded, because recording
    it would mean editing the orchestrator."""
    db_path = str(tmp_path / "audit.db")
    base = [{"item": "paneer_bhurji", "qty": 2}, {"item": "tandoori_roti", "qty": 3}]
    _paid(db_path, "buyer", base, 450, None)                       # first proposal
    _paid(db_path, "buyer", base + [{"item": "filter_coffee", "qty": 1}], 480, "pay_up")

    stats = audit_log.growth_stats(24, db_path=db_path)
    assert stats["addons_accepted"] == 1
    assert stats["top_addon"] == "filter_coffee"


def test_growth_stats_does_not_invent_an_addon_for_a_one_shot_order(tmp_path):
    db_path = str(tmp_path / "audit.db")
    _paid(db_path, "buyer", [{"item": "veg_thali", "qty": 1},
                             {"item": "filter_coffee", "qty": 1}], 180, "pay_one")

    assert audit_log.growth_stats(24, db_path=db_path)["addons_accepted"] == 0


def test_growth_stats_respects_its_window(tmp_path):
    db_path = str(tmp_path / "audit.db")
    _paid(db_path, "a1", [{"item": "veg_thali", "qty": 1}], 150, "pay_now")

    assert audit_log.growth_stats(24, db_path=db_path)["orders_paid"] == 1
    # A window that ended before anything happened reports zero rather
    # than reaching for whatever it can find.
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE audit_events SET ts = '2020-01-01T00:00:00+00:00'")
    assert audit_log.growth_stats(24, db_path=db_path)["orders_paid"] == 0


def test_growth_stats_is_empty_not_broken_on_a_fresh_shop(tmp_path):
    stats = audit_log.growth_stats(24, db_path=str(tmp_path / "audit.db"))
    assert stats["revenue_inr"] == 0
    assert stats["unmatched_demand"] == []
    assert stats["top_addon"] is None
