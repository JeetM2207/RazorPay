import pytest
from fastapi.testclient import TestClient

import adapter_acp
import adapter_ap2
import app as unified
import audit_log


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_log, "DEFAULT_DB_PATH", str(tmp_path / "audit.db"))
    adapter_acp._SESSIONS.clear()
    adapter_ap2._INTENT_MANDATES.clear()
    adapter_ap2._CART_MANDATES.clear()
    return TestClient(unified.app)


def test_all_human_facing_pages_render(client):
    for path in ("/", "/buyer", "/buyer/order", "/merchant", "/audit"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert "Amma" in resp.text


def test_buyer_is_split_into_setup_then_ordering(client):
    """Account setup happens once at /buyer; /buyer/order is the page you
    return to every time after that."""
    setup = client.get("/buyer").text
    ordering = client.get("/buyer/order").text

    assert "Set up your account" in setup
    assert "Card number" in setup
    assert "Card number" not in ordering, "card entry must not reappear on the ordering page"
    assert "Deploy Agent" in ordering


def test_the_card_number_is_never_posted_to_the_server(client):
    """The setup page must tokenise in the browser. Nothing server-side
    should offer to receive a PAN."""
    schema = unified.app.openapi()
    blob = str(schema).lower()
    for forbidden in ("card_number", "cardnumber", "\"cvv\"", "pan"):
        assert forbidden not in blob, f"an API surface accepts {forbidden}"


def test_both_protocols_are_served_from_one_app(client):
    """The unified server exposes both adapters, which is what lets one
    merchant console act on either protocol's escalations."""
    acp = client.post(
        "/acp/checkout_sessions",
        json={"agent_id": "unified-1", "items": [{"item_id": "masala_dosa", "qty": 1}]},
    )
    ap2 = client.post(
        "/ap2/intent-mandates",
        json={"agent_id": "unified-2", "intent": {"items": [{"item_id": "veg_thali", "qty": 1}]}},
    )
    assert acp.status_code == 200
    assert ap2.status_code == 200


def test_menu_flags_items_agents_may_not_order(client):
    body = client.get("/api/menu").json()
    by_id = {item["id"]: item for item in body["items"]}
    assert by_id["masala_dosa"]["agent_orderable"] is True
    assert by_id["party_catering_tray"]["agent_orderable"] is False
    assert body["mandate"]["budget_cap_inr"] == 500


def test_pending_queue_merges_escalations_from_both_protocols(client):
    client.post(
        "/acp/checkout_sessions",
        json={"agent_id": "pending-acp", "items": [{"item_id": "chicken_biryani", "qty": 2}]},
    )
    client.post(
        "/ap2/intent-mandates",
        json={"agent_id": "pending-ap2", "intent": {"items": [{"item_id": "chicken_biryani", "qty": 2}]}},
    )

    pending = client.get("/api/pending").json()["pending"]
    protocols = {p["protocol"] for p in pending}
    assert protocols == {"acp", "ap2"}
    assert all(p["status"] == "requires_human" for p in pending)


def test_pending_excludes_orders_that_did_not_need_a_human(client):
    client.post(
        "/acp/checkout_sessions",
        json={"agent_id": "auto-ok", "items": [{"item_id": "masala_dosa", "qty": 1}]},
    )
    assert client.get("/api/pending").json()["pending"] == []


def test_pending_drops_an_order_once_it_is_decided(client):
    body = client.post(
        "/acp/checkout_sessions",
        json={"agent_id": "decide-me", "items": [{"item_id": "chicken_biryani", "qty": 2}]},
    ).json()
    assert len(client.get("/api/pending").json()["pending"]) == 1

    client.post(f"/acp/checkout_sessions/{body['session_id']}/human_reject")
    assert client.get("/api/pending").json()["pending"] == []


def test_agents_endpoint_reports_trust_tiers(client):
    detail = client.post(
        "/acp/checkout_sessions",
        json={"agent_id": "tiered", "items": [{"item_id": "masala_dosa", "qty": 1}]},
    ).json()["decision_detail"]

    assert client.get("/api/agents").json()["agents"][0]["tier"] == "NEW"

    audit_log.mark_paid(detail["event_id"], "pay_ui", db_path=audit_log.DEFAULT_DB_PATH)
    agent = client.get("/api/agents").json()["agents"][0]
    assert agent["tier"] == "STANDARD"
    assert agent["completed"] == 1


def test_events_endpoint_returns_recent_decisions(client):
    client.post(
        "/acp/checkout_sessions",
        json={"agent_id": "evented", "items": [{"item_id": "masala_dosa", "qty": 1}]},
    )
    events = client.get("/api/events?limit=5").json()["events"]
    assert events[0]["agent_id"] == "evented"
    assert events[0]["decision"] == "APPROVE"


def test_buyer_check_runs_the_buyers_own_gate(client):
    """The buyer's limits are enforced on the buyer's side, and an order
    refused there must leave no trace in the merchant's audit trail --
    the merchant genuinely never saw it."""
    resp = client.post(
        "/api/buyer-check",
        json={
            "items": [{"item_id": "chicken_biryani", "qty": 3}],
            "spend_cap_inr": 600,
            "confirm_above_inr": 300,
        },
    ).json()
    assert resp["decision"] == "REFUSE"
    assert resp["total_inr"] == 660

    assert client.get("/api/events").json()["events"] == []
    assert client.get("/api/pending").json()["pending"] == []


def test_buyer_check_asks_the_customer_in_the_middle_band(client):
    resp = client.post(
        "/api/buyer-check",
        json={
            "items": [{"item_id": "chicken_biryani", "qty": 2}],
            "spend_cap_inr": 600,
            "confirm_above_inr": 300,
        },
    ).json()
    assert resp["decision"] == "ASK_USER"


def test_buyer_check_is_independent_of_the_merchant_gate(client):
    """A cart the merchant would auto-approve can still be refused by a
    strict customer, and neither side defers to the other."""
    strict = {
        "items": [{"item_id": "masala_dosa", "qty": 1}],
        "spend_cap_inr": 50,
        "confirm_above_inr": 25,
    }
    assert client.post("/api/buyer-check", json=strict).json()["decision"] == "REFUSE"

    merchant = client.post(
        "/acp/checkout_sessions",
        json={"agent_id": "indep", "items": [{"item_id": "masala_dosa", "qty": 1}]},
    ).json()
    assert merchant["decision_detail"]["decision"] == "APPROVE"


def test_buyer_mandate_defaults_are_served_to_the_console(client):
    body = client.get("/api/buyer-mandate-defaults").json()
    assert body["spend_cap_inr"] > body["confirm_above_inr"]


def test_parse_is_constrained_to_the_catalog_the_agent_fetched(client, monkeypatch):
    """The buyer agent discovers the menu and sends it back, so the parse
    can only draw from dishes that actually exist."""
    seen = {}

    def fake_call(prompt, tool_name, description, parameters):
        seen["prompt"] = prompt
        seen["enum"] = parameters["properties"]["items"]["items"]["properties"]["item_id"]["enum"]
        return {"items": [{"item_id": "dosa", "qty": 1}], "unmatched": []}

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    import llm_client

    monkeypatch.setattr(llm_client, "call_with_forced_tool", fake_call)

    resp = client.post(
        "/api/parse-cart",
        json={
            "text": "one dosa please",
            "available_items": [
                {"id": "dosa", "title": "Dosa", "price_inr": 80, "agent_orderable": True},
                {"id": "tray", "title": "Party Tray", "price_inr": 300, "agent_orderable": False},
            ],
        },
    )

    assert resp.status_code == 200
    assert seen["enum"] == ["dosa", "tray"], "the fetched catalog should bound the choices"
    assert "Dosa" in seen["prompt"]
    assert "in-person orders only" in seen["prompt"], "the agent should be told what it may not buy"


def test_items_the_merchant_does_not_sell_come_back_unmatched(client, monkeypatch):
    """Asking for something off-menu must be reported, never silently
    swapped for a different dish."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    import llm_client

    monkeypatch.setattr(
        llm_client,
        "call_with_forced_tool",
        lambda *a, **k: {"items": [], "unmatched": ["pizza"]},
    )

    body = client.post(
        "/api/parse-cart",
        json={"text": "a pizza", "available_items": [{"id": "dosa", "title": "Dosa"}]},
    ).json()

    assert body["items"] == []
    assert body["unmatched"] == ["pizza"]


def test_parse_falls_back_to_the_live_menu_without_a_catalog(client, monkeypatch):
    """The scripted buyer agents don't send a catalog; they must still work."""
    seen = {}

    def fake_call(prompt, tool_name, description, parameters):
        seen["enum"] = parameters["properties"]["items"]["items"]["properties"]["item_id"]["enum"]
        return {"items": [{"item_id": "veg_thali", "qty": 1}]}

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    import llm_client

    monkeypatch.setattr(llm_client, "call_with_forced_tool", fake_call)

    client.post("/api/parse-cart", json={"text": "a thali"})
    assert "veg_thali" in seen["enum"]


def test_parse_cart_reports_clearly_when_no_model_key_is_set(client, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    resp = client.post("/api/parse-cart", json={"text": "two biryanis"})
    assert resp.status_code == 503
    assert "menu picker" in resp.json()["detail"]
