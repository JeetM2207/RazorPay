import pytest
from fastapi.testclient import TestClient

import adapter_acp
import app as unified
import audit_log
import merchant_config


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_log, "DEFAULT_DB_PATH", str(tmp_path / "audit.db"))
    adapter_acp._SESSIONS.clear()
    return TestClient(unified.app)


def _shop(budget=500, confirm=400, menu=None):
    return {
        "profile": {"shop_name": "Amma's Kitchen", "address": "Jayanagar", "phone": "9876543210"},
        "mandate": {"budget_cap_inr": budget, "human_confirm_threshold_inr": confirm},
        "menu": menu
        or [
            {"title": "Veg Thali", "category": "meals", "price_inr": 150, "stock": 20, "agent_orderable": True},
            {"title": "Filter Coffee", "category": "beverages", "price_inr": 30, "stock": 50, "agent_orderable": True},
        ],
    }


def test_defaults_are_served_before_any_setup(client):
    config = client.get("/api/merchant-config").json()
    assert config["profile"]["configured"] is False
    assert config["mandate"]["budget_cap_inr"] == 500
    assert any(i["id"] == "veg_thali" for i in config["menu"])


def test_saving_marks_the_shop_configured(client):
    saved = client.post("/api/merchant-config", json=_shop()).json()
    assert saved["profile"]["configured"] is True
    assert saved["profile"]["shop_name"] == "Amma's Kitchen"
    assert len(saved["menu"]) == 2


def test_a_new_menu_replaces_the_old_one(client):
    client.post("/api/merchant-config", json=_shop())
    ids = {i["id"] for i in client.get("/api/menu").json()["items"]}
    assert ids == {"veg_thali", "filter_coffee"}
    assert "chicken_biryani" not in ids, "the default menu should be gone"


def test_saved_prices_actually_govern_decisions(client):
    """The point of the whole module: what the merchant types is what the
    negotiation core decides against."""
    client.post(
        "/api/merchant-config",
        json=_shop(menu=[{"title": "Veg Thali", "category": "meals", "price_inr": 999, "stock": 5}]),
    )
    body = client.post(
        "/acp/checkout_sessions",
        json={"agent_id": "cfg-1", "items": [{"item_id": "veg_thali", "qty": 1}]},
    ).json()

    assert body["decision_detail"]["total_inr"] == 999, "the old default price was used"
    assert body["status"] == "requires_human"


def test_a_raised_threshold_stops_an_order_escalating(client):
    """Same cart, two different shops: the merchant's setting decides."""
    # Thali is Rs.150. Ask-me-from Rs.100 puts it above the line...
    client.post("/api/merchant-config", json=_shop(budget=1000, confirm=100))
    tight = client.post(
        "/acp/checkout_sessions",
        json={"agent_id": "cfg-2", "items": [{"item_id": "veg_thali", "qty": 1}]},
    ).json()
    assert tight["status"] == "requires_human"

    client.post("/api/merchant-config", json=_shop(budget=1000, confirm=900))
    loose = client.post(
        "/acp/checkout_sessions",
        json={"agent_id": "cfg-3", "items": [{"item_id": "veg_thali", "qty": 1}]},
    ).json()
    assert loose["status"] == "ready_for_payment"


def test_an_item_marked_in_person_only_is_refused(client):
    client.post(
        "/api/merchant-config",
        json=_shop(
            menu=[
                {"title": "Veg Thali", "category": "meals", "price_inr": 150, "stock": 20, "agent_orderable": True},
                {"title": "Party Tray", "category": "catering", "price_inr": 300, "stock": 5, "agent_orderable": False},
            ]
        ),
    )
    body = client.post(
        "/acp/checkout_sessions",
        json={"agent_id": "cfg-4", "items": [{"item_id": "party_tray", "qty": 1}]},
    ).json()

    assert body["status"] == "requires_human"
    assert "category not allowed" in body["decision_detail"]["reason"]


def test_the_catalog_reflects_the_saved_shop(client):
    client.post("/api/merchant-config", json=_shop(budget=750, confirm=250))
    catalog = client.get("/catalog").json()

    assert catalog["merchant"]["name"] == "Amma's Kitchen"
    assert catalog["order_limits"]["max_order_inr"] == 750
    assert catalog["order_limits"]["human_confirm_at_inr"] == 250
    assert {i["id"] for i in catalog["items"]} == {"veg_thali", "filter_coffee"}


def test_the_buyer_check_prices_against_the_live_menu(client):
    """A buyer budgeting against stale prices would be checking the wrong
    number entirely."""
    client.post(
        "/api/merchant-config",
        json=_shop(menu=[{"title": "Veg Thali", "category": "meals", "price_inr": 400, "stock": 9}]),
    )
    gate = client.post(
        "/api/buyer-check",
        json={"items": [{"item_id": "veg_thali", "qty": 1}], "spend_cap_inr": 600, "confirm_above_inr": 300},
    ).json()

    assert gate["total_inr"] == 400
    assert gate["decision"] == "ASK_USER"


@pytest.mark.parametrize(
    "payload,message",
    [
        (_shop(budget=0), "above zero"),
        (_shop(confirm=0), "above zero"),
        (_shop(budget=200, confirm=500), "at or below the maximum"),
        ({**_shop(), "profile": {"shop_name": "  "}}, "needs a name"),
        ({**_shop(), "menu": []}, "at least one dish"),
        ({**_shop(), "menu": [{"title": "", "price_inr": 10, "stock": 1}]}, "needs a name"),
        ({**_shop(), "menu": [{"title": "Free Lunch", "price_inr": 0, "stock": 1}]}, "price above zero"),
        (
            {**_shop(), "menu": [{"title": "Only In Person", "price_inr": 10, "stock": 1, "agent_orderable": False}]},
            "orderable by an agent",
        ),
    ],
)
def test_bad_configuration_is_refused_with_a_plain_reason(client, payload, message):
    resp = client.post("/api/merchant-config", json=payload)
    assert resp.status_code == 400
    assert message in resp.json()["detail"]


def test_a_refused_save_leaves_the_shop_untouched(client):
    client.post("/api/merchant-config", json=_shop(budget=500, confirm=400))
    client.post("/api/merchant-config", json=_shop(budget=0))

    assert client.get("/api/merchant-config").json()["mandate"]["budget_cap_inr"] == 500


def test_config_survives_a_restart(client, monkeypatch):
    client.post("/api/merchant-config", json=_shop(budget=888, confirm=333))

    # A restart means no in-memory state, forcing a read from disk --
    # which is different from reset_to_defaults(), that deliberately
    # throws the saved shop away.
    monkeypatch.setattr(merchant_config, "_state", None)

    assert merchant_config.current_mandate().budget_cap_inr == 888
    assert merchant_config.profile()["shop_name"] == "Amma's Kitchen"


def test_a_corrupt_config_falls_back_to_defaults_rather_than_crashing(monkeypatch, tmp_path):
    path = tmp_path / "merchant_config.json"
    path.write_text("{ this is not json")
    monkeypatch.setattr(merchant_config, "CONFIG_PATH", path)
    merchant_config.reset_to_defaults()
    monkeypatch.setattr(merchant_config, "_state", None)

    assert merchant_config.current_mandate().budget_cap_inr == 500


def test_setup_and_orders_are_separate_pages(client):
    setup = client.get("/merchant").text
    orders = client.get("/merchant/orders").text

    assert "Set up your shop" in setup
    assert "Today's menu" in setup
    assert "Orders needing your decision" in orders
    assert "Today's menu" not in orders


# --------------------------------------------- inventory-led pricing

def _set_shop(*dishes):
    merchant_config.save(
        profile_in={"shop_name": "Amma's Kitchen"},
        mandate_in={"budget_cap_inr": 500, "human_confirm_threshold_inr": 400},
        menu_in=[
            {"title": t, "category": c, "price_inr": p, "stock": s}
            for t, c, p, s in dishes
        ],
    )
    return {row["id"]: row for row in merchant_config.as_dict()["menu"]}


def test_a_dish_piling_up_goes_on_sale():
    _set_shop(("Veg Thali", "meals", 200, 20))
    merchant_config.optimize_prices()

    thali = {r["id"]: r for r in merchant_config.as_dict()["menu"]}["veg_thali"]
    assert thali["sale"] is True
    assert thali["price_inr"] == 170          # 15% off, rounded down
    assert thali["list_price_inr"] == 200     # what she actually charges


def test_running_the_optimiser_again_does_not_discount_the_discount():
    """The trap this feature is one line away from. Deriving the sale
    price from the CURRENT price compounds: two clicks is 28% off, ten is
    80%, and nothing in the system would have flagged it."""
    _set_shop(("Veg Thali", "meals", 200, 20))

    prices = []
    for _ in range(5):
        merchant_config.optimize_prices()
        prices.append({r["id"]: r for r in merchant_config.as_dict()["menu"]}["veg_thali"]["price_inr"])

    assert prices == [170] * 5, prices


def test_a_dish_running_out_goes_back_to_her_price():
    _set_shop(("Veg Thali", "meals", 200, 20))
    merchant_config.optimize_prices()

    _restock("veg_thali", 1)
    merchant_config.optimize_prices()

    thali = {r["id"]: r for r in merchant_config.as_dict()["menu"]}["veg_thali"]
    assert thali["sale"] is False
    assert thali["price_inr"] == 200


def _restock(item_id, stock):
    rows = merchant_config.as_dict()["menu"]
    for row in rows:
        if row["id"] == item_id:
            row["stock"] = stock
    state = merchant_config.as_dict()
    merchant_config.save(profile_in=state["profile"], mandate_in=state["mandate"], menu_in=rows)


def test_a_sale_keeps_running_while_the_dish_sells_down():
    """The middle band is inert on purpose. A dish discounted at 20
    portions keeps its price the whole way down to 3, instead of the sale
    flickering off the moment one is sold."""
    _set_shop(("Veg Thali", "meals", 200, 20))
    merchant_config.optimize_prices()

    for stock in (9, 6, 3):
        _restock("veg_thali", stock)
        merchant_config.optimize_prices()
        thali = {r["id"]: r for r in merchant_config.as_dict()["menu"]}["veg_thali"]
        assert thali["price_inr"] == 170, stock
        assert thali["sale"] is True, stock


def test_a_price_she_types_by_hand_ends_the_sale():
    """Editing the menu is her overruling the optimiser, and the price
    she typed becomes the new list price. Anything else would mean her
    shop page showing one number while the catalog published another."""
    _set_shop(("Veg Thali", "meals", 200, 20))
    merchant_config.optimize_prices()

    _set_shop(("Veg Thali", "meals", 120, 20))       # she retypes it

    thali = {r["id"]: r for r in merchant_config.as_dict()["menu"]}["veg_thali"]
    assert thali["price_inr"] == 120
    assert thali["list_price_inr"] == 120
    assert thali["sale"] is False


def test_a_discount_can_never_take_a_price_below_a_rupee():
    _set_shop(("Papad", "snacks", 1, 40))
    merchant_config.optimize_prices()

    assert {r["id"]: r for r in merchant_config.as_dict()["menu"]}["papad"]["price_inr"] >= 1


def test_the_optimiser_reports_exactly_what_it_changed():
    _set_shop(("Veg Thali", "meals", 200, 20), ("Party Tray", "meals", 300, 5))
    result = merchant_config.optimize_prices()

    assert result["discounted"] == 1
    assert result["restored"] == 0
    assert [c["id"] for c in result["changed"]] == ["veg_thali"]
    assert result["changed"][0]["was_inr"] == 200
    assert result["changed"][0]["now_inr"] == 170


def test_the_negotiation_core_is_handed_a_price_and_nothing_else():
    """A sale is a fact about the shop, not an input to a decision.
    MenuItem carries no sale flag, so negotiation.py cannot see one even
    if it wanted to -- it just prices the cheaper cart."""
    _set_shop(("Veg Thali", "meals", 200, 20))
    import negotiation

    before = negotiation.evaluate([("veg_thali", 2)], menu=merchant_config.current_menu(),
                                  mandate=merchant_config.current_mandate())
    merchant_config.optimize_prices()
    after = negotiation.evaluate([("veg_thali", 2)], menu=merchant_config.current_menu(),
                                 mandate=merchant_config.current_mandate())

    assert before.total_inr == 400
    assert after.total_inr == 340
    assert not hasattr(merchant_config.current_menu()["veg_thali"], "sale")


def test_a_buyer_agent_sees_the_new_price_on_its_next_fetch():
    """The whole point: nothing is pushed, but catalog.py reads the same
    live config, so the next fetch is already the sale price."""
    import catalog

    _set_shop(("Veg Thali", "meals", 200, 20))
    assert catalog.get_catalog()["items"][0]["price"] == 200

    merchant_config.optimize_prices()
    item = catalog.get_catalog()["items"][0]
    assert item["price"] == 170
    assert item["sale"] is True
    assert item["list_price"] == 200
