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
    for path in ("/", "/buyer", "/merchant", "/audit"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert "Amma" in resp.text


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


def test_parse_cart_reports_clearly_when_no_model_key_is_set(client, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    resp = client.post("/api/parse-cart", json={"text": "two biryanis"})
    assert resp.status_code == 503
    assert "menu picker" in resp.json()["detail"]
