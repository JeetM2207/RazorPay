"""Many kitchens on one platform, and the walls between them.

The claim a marketplace makes is not that it has several merchants. It
is that each one's rules apply to her own orders and to nobody else's,
and that she can see her own books and nobody else's. These test the
walls, because a marketplace with a leak is a shared inbox.
"""

import pytest

import audit_log
import merchant_config
import merchants
import negotiation
import orchestrator
import velocity


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "trail.db")
    audit_log.init_db(path)
    monkeypatch.setattr(audit_log, "DEFAULT_DB_PATH", path)
    return path


def order(db, merchant_id, cart, agent="Jeet's Agent"):
    return orchestrator.negotiate_and_record(
        agent_id=agent, protocol="ap2", cart=cart, merchant_id=merchant_id)


# ------------------------------------------------- the core is unchanged

def test_the_core_still_knows_nothing_about_the_platform():
    """negotiation.py has never known which shop it decides for -- it
    takes mandate and menu as arguments. That is the whole reason this
    was affordable, and it must stay true."""
    source = (audit_log.Path(negotiation.__file__)
              if hasattr(audit_log, "Path") else None)
    import pathlib
    text = pathlib.Path(negotiation.__file__).read_text(encoding="utf-8")
    for forbidden in ("merchants", "merchant_id", "platform", "tenant"):
        assert forbidden not in text, (
            f"negotiation.py mentions {forbidden!r} -- the decision core must "
            "stay unaware there is more than one kitchen"
        )


def test_one_core_gives_three_kitchens_three_answers():
    """The same cart shape, decided differently, because the rules differ
    and only the rules differ."""
    amma = negotiation.evaluate(
        [("veg_thali", 1)],
        mandate=merchant_config.current_mandate("ammas-kitchen"),
        menu=merchant_config.current_menu("ammas-kitchen"))
    bombay = negotiation.evaluate(
        [("pav_bhaji", 3)],
        mandate=merchant_config.current_mandate("bombay-tiffin"),
        menu=merchant_config.current_menu("bombay-tiffin"))
    lahori = negotiation.evaluate(
        [("catering_platter", 1)],
        mandate=merchant_config.current_mandate("lahori-grill"),
        menu=merchant_config.current_menu("lahori-grill"))

    assert amma.decision.value == "APPROVE"
    assert bombay.decision.value == "ESCALATE"      # its own Rs.300 threshold
    assert "category not allowed" in lahori.reason  # its own category list


# ----------------------------------------------------------- the walls

def test_a_kitchen_sees_its_own_orders_and_no_others(db):
    order(db, "ammas-kitchen", [("veg_thali", 1)])
    order(db, "bombay-tiffin", [("misal_pav", 1)])
    order(db, "lahori-grill", [("seekh_kebab", 1)])

    for merchant_id, dish in (("ammas-kitchen", "veg_thali"),
                              ("bombay-tiffin", "misal_pav"),
                              ("lahori-grill", "seekh_kebab")):
        rows = audit_log.get_all_events(db_path=db, limit=99, merchant_id=merchant_id)
        assert len(rows) == 1, merchant_id
        assert dish in rows[0]["cart_json"]


def test_the_platform_can_still_see_everything(db):
    """The public audit trail is not a merchant view. It is the thing
    that makes the whole claim checkable, and it is deliberately whole."""
    order(db, "ammas-kitchen", [("veg_thali", 1)])
    order(db, "lahori-grill", [("seekh_kebab", 1)])
    assert len(audit_log.get_all_events(db_path=db, limit=99)) == 2


def test_each_kitchen_enforces_its_own_limits(db):
    """Rs.330 clears Amma's Rs.400 threshold and trips Bombay's Rs.300.
    Same money, different kitchen, different answer."""
    at_bombay = order(db, "bombay-tiffin", [("pav_bhaji", 3)])
    assert at_bombay["decision"] == "ESCALATE"
    assert order(db, "ammas-kitchen", [("veg_thali", 2)])["decision"] == "APPROVE"


# ------------------------------------------------- what must not leak

def test_the_flood_gate_counts_one_kitchen_at_a_time(db):
    """Found by running six orders across three kitchens: the fourth was
    refused because the first three had used up a limit that belonged to
    a different merchant entirely."""
    limits = velocity.VelocityLimits(max_orders_per_hour=2,
                                     max_spend_per_day_inr=100_000)
    a, _ = velocity.usage("Jeet's Agent", db_path=db, merchant_id="ammas-kitchen")
    order(db, "ammas-kitchen", [("veg_thali", 1)])
    order(db, "ammas-kitchen", [("veg_thali", 1)])

    at_amma, _ = velocity.usage("Jeet's Agent", db_path=db, merchant_id="ammas-kitchen")
    at_lahori, _ = velocity.usage("Jeet's Agent", db_path=db, merchant_id="lahori-grill")
    assert at_amma == a + 2
    assert at_lahori == 0, "one kitchen's traffic used up another's gate"


def test_trust_is_earned_at_one_kitchen_at_a_time(db):
    """An agent that has proved itself at Amma's has proved nothing at
    the grill house."""
    import trust

    for _ in range(6):
        event = order(db, "ammas-kitchen", [("veg_thali", 1)])
        audit_log.mark_paid(event["event_id"], "demo_x", db_path=db)

    assert trust.compute_trust_tier("Jeet's Agent", db_path=db,
                                    merchant_id="ammas-kitchen").value == "TRUSTED"
    assert trust.compute_trust_tier("Jeet's Agent", db_path=db,
                                    merchant_id="lahori-grill").value == "NEW"


# ----------------------------------------------------------- the trail

def test_every_order_is_stamped_with_its_kitchen(db):
    event = order(db, "lahori-grill", [("seekh_kebab", 1)])
    row = audit_log.get_event(event["event_id"], db_path=db)
    assert row["merchant_id"] == "lahori-grill"


def test_rows_from_before_the_platform_belong_to_the_default_kitchen(db):
    """They genuinely were that kitchen's orders. Matched rather than
    backfilled -- inventing a value in an append-only trail to make a
    query tidier is how a record stops being one."""
    audit_log.record_event("Jeet's Agent", "acp", [{"item": "veg_thali", "qty": 1}],
                           "APPROVE", "legacy row", 150, db_path=db)  # no merchant_id
    default = audit_log.get_all_events(db_path=db, limit=99,
                                       merchant_id=merchants.default_id())
    other = audit_log.get_all_events(db_path=db, limit=99, merchant_id="lahori-grill")
    assert len(default) == 1
    assert other == []


def test_an_unknown_kitchen_falls_back_rather_than_exploding():
    """Reached from buyer traffic, where an unknown id is a bad request
    and not an outage."""
    assert merchants.get("no-such-kitchen")["id"] == merchants.default_id()
    assert merchants.exists("no-such-kitchen") is False


def test_the_platform_signs_its_messages_with_both_names():
    """A customer who gets a text from an unknown number needs to know
    who is writing before they know what it is about."""
    assert merchants.message_prefix("Amma's Kitchen").startswith(merchants.Platform.name)
    assert "Amma's Kitchen" in merchants.message_prefix("Amma's Kitchen")
    assert merchants.message_prefix() == merchants.Platform.name
