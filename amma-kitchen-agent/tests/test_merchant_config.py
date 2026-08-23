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
