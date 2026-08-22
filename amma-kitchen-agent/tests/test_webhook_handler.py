import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

import audit_log
import orchestrator
import webhook_handler

TEST_SECRET = "test_webhook_secret"


def _sign(body: bytes, secret: str = TEST_SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _paid_payload(payment_link_id: str, payment_id: str) -> bytes:
    return json.dumps(
        {
            "entity": "event",
            "event": "payment_link.paid",
            "payload": {
                "payment_link": {"entity": {"id": payment_link_id, "status": "paid"}},
                "payment": {"entity": {"id": payment_id, "status": "captured"}},
            },
        }
    ).encode()


def _expired_payload(payment_link_id: str) -> bytes:
    return json.dumps(
        {
            "entity": "event",
            "event": "payment_link.expired",
            "payload": {"payment_link": {"entity": {"id": payment_link_id, "status": "expired"}}},
        }
    ).encode()


def _make_paid_link_event(db_path: str, agent_id: str = "buyer-w1", payment_link_id: str = "plink_w1") -> int:
    detail = orchestrator.negotiate_and_record(agent_id, "acp", [("masala_dosa", 1)])
    audit_log.attach_payment_link(detail["event_id"], payment_link_id, db_path=db_path)
    return detail["event_id"]


@pytest.fixture
def client_and_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "audit.db")
    monkeypatch.setattr(audit_log, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(webhook_handler, "_WEBHOOK_SECRET", TEST_SECRET)
    return TestClient(webhook_handler.app), db_path


def test_valid_paid_event_marks_audit_event_paid(client_and_db):
    client, db_path = client_and_db
    event_id = _make_paid_link_event(db_path)

    body = _paid_payload("plink_w1", "pay_abc123")
    resp = client.post(
        "/webhooks/razorpay", content=body, headers={"X-Razorpay-Signature": _sign(body)}
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "processed"
    events = audit_log.get_events_for_agent("buyer-w1", db_path=db_path)
    matching = [e for e in events if e["id"] == event_id][0]
    assert matching["payment_id"] == "pay_abc123"


def test_duplicate_delivery_is_ignored_second_time(client_and_db):
    client, db_path = client_and_db
    _make_paid_link_event(db_path, agent_id="buyer-w2", payment_link_id="plink_w2")

    body = _paid_payload("plink_w2", "pay_xyz789")
    headers = {"X-Razorpay-Signature": _sign(body)}

    first = client.post("/webhooks/razorpay", content=body, headers=headers)
    second = client.post("/webhooks/razorpay", content=body, headers=headers)

    assert first.json()["status"] == "processed"
    assert second.json()["status"] == "duplicate_ignored"


def test_invalid_signature_is_rejected(client_and_db):
    client, db_path = client_and_db
    _make_paid_link_event(db_path, agent_id="buyer-w3", payment_link_id="plink_w3")

    body = _paid_payload("plink_w3", "pay_bad")
    resp = client.post(
        "/webhooks/razorpay", content=body, headers={"X-Razorpay-Signature": "not-a-real-signature"}
    )
    assert resp.status_code == 400


def test_unrecognized_event_type_is_ignored(client_and_db):
    client, db_path = client_and_db
    body = json.dumps({"entity": "event", "event": "payment.authorized", "payload": {}}).encode()
    resp = client.post(
        "/webhooks/razorpay", content=body, headers={"X-Razorpay-Signature": _sign(body)}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


def test_unmatched_payment_link_does_not_crash(client_and_db):
    client, db_path = client_and_db
    body = _paid_payload("plink_never_seen", "pay_orphan")
    resp = client.post(
        "/webhooks/razorpay", content=body, headers={"X-Razorpay-Signature": _sign(body)}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "processed_unmatched"


def test_expired_event_appends_new_row_without_mutating_original(client_and_db):
    client, db_path = client_and_db
    event_id = _make_paid_link_event(db_path, agent_id="buyer-w4", payment_link_id="plink_w4")

    body = _expired_payload("plink_w4")
    resp = client.post(
        "/webhooks/razorpay", content=body, headers={"X-Razorpay-Signature": _sign(body)}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "processed"

    events = audit_log.get_events_for_agent("buyer-w4", db_path=db_path)
    original = [e for e in events if e["id"] == event_id][0]
    assert original["payment_id"] is None  # untouched, append-only

    new_rows = [e for e in events if e["decision"] == "PAYMENT_NOT_COMPLETED"]
    assert len(new_rows) == 1
    assert "payment_link.expired" in new_rows[0]["reason"]
