import pytest
from fastapi.testclient import TestClient

import adapter_acp
import audit_log


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_log, "DEFAULT_DB_PATH", str(tmp_path / "audit.db"))
    adapter_acp._SESSIONS.clear()
    return TestClient(adapter_acp.app)


def _mock_payment_link(monkeypatch, link_id="plink_test", short_url="https://rzp.io/rzp/test"):
    fake_link = {"id": link_id, "short_url": short_url}
    monkeypatch.setattr(
        adapter_acp.orchestrator.razorpay_client, "create_payment_link", lambda **kwargs: fake_link
    )
    return fake_link


def test_full_approve_and_pay_flow(client, monkeypatch):
    fake_link = _mock_payment_link(monkeypatch)

    resp = client.post(
        "/acp/checkout_sessions",
        json={"agent_id": "buyer-1", "items": [{"item_id": "masala_dosa", "qty": 1}]},
    )
    body = resp.json()
    assert resp.status_code == 200
    assert body["status"] == "ready_for_payment"
    assert body["delegate_token"]

    complete = client.post(
        f"/acp/checkout_sessions/{body['session_id']}/complete",
        json={"delegate_token": body["delegate_token"]},
    )
    assert complete.status_code == 200
    assert complete.json()["payment_link_url"] == fake_link["short_url"]


def test_escalated_session_has_no_delegate_token(client):
    resp = client.post(
        "/acp/checkout_sessions",
        json={"agent_id": "buyer-2", "items": [{"item_id": "chicken_biryani", "qty": 2}]},
    )
    body = resp.json()
    assert body["status"] == "requires_human"
    assert body["delegate_token"] is None


def test_counter_offer_then_accept_alternative(client, monkeypatch):
    _mock_payment_link(monkeypatch)

    resp = client.post(
        "/acp/checkout_sessions",
        json={
            "agent_id": "buyer-3",
            "items": [{"item_id": "chicken_biryani", "qty": 2}, {"item_id": "masala_dosa", "qty": 1}],
        },
    )
    body = resp.json()
    assert body["status"] == "countered"
    assert len(body["decision_detail"]["alternatives"]) >= 1

    accepted = client.post(
        f"/acp/checkout_sessions/{body['session_id']}/accept_alternative", json={"index": 0}
    ).json()
    assert accepted["status"] == "ready_for_payment"


def test_delegate_token_cannot_be_reused(client, monkeypatch):
    _mock_payment_link(monkeypatch)

    body = client.post(
        "/acp/checkout_sessions",
        json={"agent_id": "buyer-4", "items": [{"item_id": "masala_dosa", "qty": 1}]},
    ).json()
    token = body["delegate_token"]
    session_id = body["session_id"]

    first = client.post(f"/acp/checkout_sessions/{session_id}/complete", json={"delegate_token": token})
    assert first.status_code == 200

    second = client.post(f"/acp/checkout_sessions/{session_id}/complete", json={"delegate_token": token})
    assert second.status_code == 409


def test_wrong_delegate_token_is_rejected(client, monkeypatch):
    _mock_payment_link(monkeypatch)

    body = client.post(
        "/acp/checkout_sessions",
        json={"agent_id": "buyer-5", "items": [{"item_id": "masala_dosa", "qty": 1}]},
    ).json()
    session_id = body["session_id"]

    resp = client.post(
        f"/acp/checkout_sessions/{session_id}/complete", json={"delegate_token": "not-the-real-token"}
    )
    assert resp.status_code == 403


def test_accept_upsell_extends_cart_and_stays_approved(client, monkeypatch):
    _mock_payment_link(monkeypatch)

    body = client.post(
        "/acp/checkout_sessions",
        json={"agent_id": "buyer-6", "items": [{"item_id": "masala_dosa", "qty": 1}]},
    ).json()
    upsell = body["decision_detail"].get("upsell_suggestion")
    assert upsell is not None

    updated = client.post(f"/acp/checkout_sessions/{body['session_id']}/accept_upsell").json()
    assert updated["status"] == "ready_for_payment"
    assert updated["decision_detail"]["total_inr"] > body["decision_detail"]["total_inr"]
