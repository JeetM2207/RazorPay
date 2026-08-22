import pytest
from fastapi.testclient import TestClient

import adapter_ap2
import audit_log
import autonomous_payment
import orchestrator


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_log, "DEFAULT_DB_PATH", str(tmp_path / "audit.db"))
    adapter_ap2._INTENT_MANDATES.clear()
    adapter_ap2._CART_MANDATES.clear()
    monkeypatch.setattr(
        autonomous_payment.razorpay_client,
        "create_order",
        lambda **kwargs: {"id": "order_TESTORDER123"},
    )
    # Default: S2S unavailable, which is the real situation on a standard
    # test account. Individual tests override it.
    monkeypatch.setattr(autonomous_payment, "_try_s2s", lambda *a, **k: None)
    return TestClient(adapter_ap2.app)


def _locked_cart(client, agent_id="auto-buyer"):
    mandate = client.post(
        "/ap2/intent-mandates",
        json={"agent_id": agent_id, "intent": {"items": [{"item_id": "masala_dosa", "qty": 1}]}},
    ).json()["intent_mandate"]
    return client.post(f"/ap2/intent-mandates/{mandate['id']}/cart-mandate").json()["cart_mandate"]


def test_settles_without_a_browser(client):
    cart = _locked_cart(client)
    pm = client.post(f"/ap2/cart-mandates/{cart['id']}/execute-payment").json()["payment_mandate"]

    assert pm["amount_inr"] == 80
    assert pm["order_id"] == "order_TESTORDER123"
    assert "payment_link_url" not in pm, "autonomous settlement must not hand back a link to click"


def test_a_simulated_capture_is_never_disguised_as_a_real_one(client):
    """The load-bearing guarantee. A real Razorpay payment id starts
    `pay_`; anything we asserted ourselves must not."""
    cart = _locked_cart(client)
    pm = client.post(f"/ap2/cart-mandates/{cart['id']}/execute-payment").json()["payment_mandate"]

    assert pm["simulated"] is True
    assert pm["payment_id"].startswith("sim_")
    assert not pm["payment_id"].startswith("pay_")
    assert "not enabled" in pm["method"]


def test_a_real_s2s_charge_is_reported_as_real(client, monkeypatch):
    monkeypatch.setattr(autonomous_payment, "_try_s2s", lambda *a, **k: "pay_realcharge99")
    cart = _locked_cart(client)
    pm = client.post(f"/ap2/cart-mandates/{cart['id']}/execute-payment").json()["payment_mandate"]

    assert pm["simulated"] is False
    assert pm["payment_id"] == "pay_realcharge99"
    assert pm["method"] == "Razorpay S2S card charge"


def test_settlement_is_written_to_the_audit_trail(client):
    cart = _locked_cart(client, agent_id="auto-audited")
    pm = client.post(f"/ap2/cart-mandates/{cart['id']}/execute-payment").json()["payment_mandate"]

    events = audit_log.get_events_for_agent("auto-audited", db_path=audit_log.DEFAULT_DB_PATH)
    assert events[0]["payment_id"] == pm["payment_id"]
    assert autonomous_payment.is_simulated(events[0]["payment_id"])


def test_autonomous_settlement_is_still_single_use(client):
    """Autonomous must not mean less checked."""
    cart = _locked_cart(client)
    assert client.post(f"/ap2/cart-mandates/{cart['id']}/execute-payment").status_code == 200
    assert client.post(f"/ap2/cart-mandates/{cart['id']}/execute-payment").status_code == 409


def test_an_expired_cart_mandate_cannot_be_charged(client):
    cart = _locked_cart(client)
    adapter_ap2._CART_MANDATES[cart["id"]]["expires_at"] = 0
    assert client.post(f"/ap2/cart-mandates/{cart['id']}/execute-payment").status_code == 403


def test_link_and_autonomous_routes_are_mutually_exclusive(client, monkeypatch):
    """Both consume the same single-use cart mandate, so a cart cannot be
    settled twice by taking one route then the other."""
    monkeypatch.setattr(
        adapter_ap2.orchestrator.razorpay_client,
        "create_payment_link",
        lambda **kwargs: {"id": "plink_x", "short_url": "https://rzp.io/x"},
    )
    cart = _locked_cart(client)
    assert client.post(f"/ap2/cart-mandates/{cart['id']}/execute-payment").status_code == 200
    assert client.post(f"/ap2/cart-mandates/{cart['id']}/payment-mandate").status_code == 409


def test_is_simulated_helper_is_the_single_source_of_truth():
    assert autonomous_payment.is_simulated("sim_abc") is True
    assert autonomous_payment.is_simulated("pay_abc") is False
    assert autonomous_payment.is_simulated(None) is False
    assert autonomous_payment.is_simulated("") is False


def test_simulated_revenue_never_inflates_the_dashboard_total(tmp_path, monkeypatch):
    """A simulated settlement appears in the log but must not be counted
    as money the merchant received."""
    import dashboard

    db_path = str(tmp_path / "audit.db")
    monkeypatch.setattr(audit_log, "DEFAULT_DB_PATH", db_path)

    real = orchestrator.negotiate_and_record("rev-a", "ap2", [("masala_dosa", 1)])
    audit_log.mark_paid(real["event_id"], "pay_genuine", db_path=db_path)
    fake = orchestrator.negotiate_and_record("rev-b", "ap2", [("veg_thali", 1)])
    audit_log.mark_paid(fake["event_id"], "sim_pretend", db_path=db_path)

    stats = dashboard._summary(audit_log.get_all_events(db_path=db_path))
    assert stats["captured_inr"] == 80, "simulated settlement leaked into revenue"
    assert stats["captured_count"] == 1
