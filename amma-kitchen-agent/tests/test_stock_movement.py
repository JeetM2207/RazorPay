"""Selling a dish takes it out of the kitchen.

Until this existed, stock was read everywhere and written nowhere: the
core refused dishes that had run out, the repricer decided what needed
moving, and a shop that had sold twenty thalis still showed twenty. Every
one of those readers was reasoning about a number that never changed.

Stock moves at CAPTURE, not at approval, and comes back if the order is
declined.
"""

import pytest

import audit_log
import mcp_orders
import merchant_config


def stock_of(item_id: str) -> int:
    return merchant_config.current_menu()[item_id].stock


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "trail.db")
    audit_log.init_db(path)
    return path


def approved(db, cart, total=300, agent="Jeet's Agent"):
    return audit_log.record_event(
        agent, "ap2", [{"item": i, "qty": q} for i, q in cart],
        "APPROVE", "within budget", total, db_path=db)


# ------------------------------------------------------- the movement

def test_a_capture_takes_the_food_out_of_the_kitchen(db):
    before = stock_of("veg_thali")
    audit_log.mark_paid(approved(db, [("veg_thali", 1)]), "pay_X", db_path=db)
    assert stock_of("veg_thali") == before - 1


def test_the_quantity_ordered_is_the_quantity_taken(db):
    before = stock_of("tandoori_roti")
    audit_log.mark_paid(approved(db, [("tandoori_roti", 4)]), "pay_X", db_path=db)
    assert stock_of("tandoori_roti") == before - 4


def test_every_line_of_the_cart_moves(db):
    thali, coffee = stock_of("veg_thali"), stock_of("filter_coffee")
    audit_log.mark_paid(
        approved(db, [("veg_thali", 2), ("filter_coffee", 3)]), "pay_X", db_path=db)
    assert stock_of("veg_thali") == thali - 2
    assert stock_of("filter_coffee") == coffee - 3


# ------------------------------------------------ when it must NOT move

def test_an_approval_alone_takes_nothing(db):
    """An approved cart is an invitation to pay that may never be taken
    up. Holding stock for every abandoned checkout would starve the menu
    of dishes nobody bought."""
    before = stock_of("veg_thali")
    approved(db, [("veg_thali", 3)])
    assert stock_of("veg_thali") == before


def test_the_same_capture_twice_takes_the_food_once(db):
    """A webhook and the reconciler can both learn about the same
    capture. The idempotency ledger stops most of that; this stops the
    rest."""
    before = stock_of("veg_thali")
    event = approved(db, [("veg_thali", 2)])
    audit_log.mark_paid(event, "pay_X", db_path=db)
    audit_log.mark_paid(event, "pay_X", db_path=db)
    assert stock_of("veg_thali") == before - 2


def test_marking_an_unknown_row_paid_changes_nothing(db):
    before = stock_of("veg_thali")
    audit_log.mark_paid(999999, "pay_X", db_path=db)
    assert stock_of("veg_thali") == before


# ------------------------------------------------------- giving it back

def test_a_declined_order_puts_the_food_back(db, monkeypatch):
    """The kitchen never cooked it. Holding its stock would slowly starve
    the menu of dishes nobody ate."""
    before = stock_of("chicken_biryani")
    event = approved(db, [("chicken_biryani", 2)], total=440)
    audit_log.mark_paid(event, "pay_X", db_path=db)
    assert stock_of("chicken_biryani") == before - 2

    monkeypatch.setattr(mcp_orders, "_transition", lambda *a, **k: None)
    monkeypatch.setattr(mcp_orders, "_tell", lambda *a, **k: None)
    monkeypatch.setattr(mcp_orders, "_refund_outstanding",
                        lambda *a, **k: {"id": "rfnd_X"}, raising=False)
    order = dict(audit_log.get_event(event, db_path=db))
    try:
        mcp_orders._refund(order, "MERCHANT_REJECTED", "declined")
    except Exception:
        # Razorpay is not reachable in a test; the stock move happens
        # before the refund is attempted, which is the point.
        pass
    assert stock_of("chicken_biryani") == before


# ------------------------------------------------------------ the writer

def test_stock_never_goes_negative():
    """Two agents can pass the stock check a millisecond apart -- that
    needs a lock this project does not have. What must not happen is a
    number nobody can act on, or a dish dragged below LOW_STOCK so its
    sale silently ends."""
    merchant_config.adjust_stock([("veg_thali", 9999)], -1)
    assert stock_of("veg_thali") == 0


def test_both_cart_shapes_are_accepted():
    """Carts arrive as (id, qty) pairs from the core and as dicts from
    the trail."""
    before = stock_of("gulab_jamun")
    merchant_config.adjust_stock([("gulab_jamun", 1)], -1)
    merchant_config.adjust_stock([{"item": "gulab_jamun", "qty": 1}], -1)
    assert stock_of("gulab_jamun") == before - 2


def test_an_item_not_on_the_menu_is_ignored():
    before = {k: v.stock for k, v in merchant_config.current_menu().items()}
    merchant_config.adjust_stock([("pizza", 5)], -1)
    assert {k: v.stock for k, v in merchant_config.current_menu().items()} == before


def test_an_empty_cart_saves_nothing():
    assert merchant_config.adjust_stock([], -1) == []
    assert merchant_config.adjust_stock(None, -1) == []
