"""Proof of Authorization: the record must say what WAS true, not what is.

The pack is evidence, so the tests that matter are the ones about
honesty -- that a snapshot survives the merchant editing her shop, and
that a field which genuinely does not exist is shown as missing rather
than filled in with something plausible.
"""

import pytest
from fastapi.testclient import TestClient

import adapter_ap2
import app as unified
import audit_log
import escalations
import evidence
import merchant_config
import notification_service
import orchestrator


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_log, "DEFAULT_DB_PATH", str(tmp_path / "audit.db"))
    monkeypatch.setattr(notification_service, "TWILIO_CONFIGURED", False)
    notification_service.clear_outbox()
    escalations.reset()
    adapter_ap2._INTENT_MANDATES.clear()
    adapter_ap2._CART_MANDATES.clear()
    return TestClient(unified.app)


def _shop(cap, confirm):
    merchant_config.save(
        profile_in={"shop_name": "Amma's Kitchen"},
        mandate_in={"budget_cap_inr": cap, "human_confirm_threshold_inr": confirm},
        menu_in=[
            {"title": "Veg Thali", "category": "meals", "price_inr": 150, "stock": 20},
            {"title": "Chicken Biryani", "category": "meals", "price_inr": 220, "stock": 15},
            {"title": "Masala Dosa", "category": "snacks", "price_inr": 80, "stock": 25},
        ],
    )


def _ap2_order(client, agent, items, buyer_limits=None):
    intent = {"items": items}
    if buyer_limits:
        intent["buyer_limits"] = buyer_limits
    body = client.post("/ap2/intent-mandates",
                       json={"agent_id": agent, "intent": intent}).json()
    return body["intent_mandate"]


# ------------------------------------------- the snapshot, which is the point

def test_the_record_still_says_what_the_limits_were_after_she_changes_them(client):
    """The reason this feature needed a fix before it could exist. An
    order that REFERENCED the live config started describing limits that
    were never applied to it the moment Amma edited her shop -- fine on a
    dashboard, fatal in a record someone is relying on."""
    _shop(cap=500, confirm=400)
    order = _ap2_order(client, "snap", [{"item_id": "chicken_biryani", "qty": 2}],
                       buyer_limits={"hard_cap_inr": 600, "soft_cap_inr": 300})
    order_id = order["decision_detail"]["event_id"]

    before = evidence.build_evidence_pack(order_id)
    assert before["limits_in_force"]["merchant"]["budget_cap_inr"] == 500
    assert before["limits_in_force"]["merchant"]["human_confirm_threshold_inr"] == 400
    assert before["limits_in_force"]["buyer"]["hard_cap_inr"] == 600

    # She raises her cap and her threshold the next day.
    _shop(cap=900, confirm=800)
    assert merchant_config.current_mandate().budget_cap_inr == 900

    after = evidence.build_evidence_pack(order_id)
    assert after["limits_in_force"]["merchant"]["budget_cap_inr"] == 500, "read live, not recorded"
    assert after["limits_in_force"]["merchant"]["human_confirm_threshold_inr"] == 400
    assert after["limits_in_force"]["buyer"]["hard_cap_inr"] == 600


def test_every_adapter_gets_the_snapshot_without_knowing_it_exists(client):
    """Written once at the orchestrator, so no adapter had to be taught."""
    _shop(cap=500, confirm=400)
    for agent, protocol in (("s-acp", "acp"), ("s-ap2", "ap2"),
                            ("s-x402", "x402"), ("s-mcp", "mcp")):
        detail = orchestrator.negotiate_and_record(agent, protocol, [("masala_dosa", 1)])
        pack = evidence.build_evidence_pack(detail["event_id"])
        merchant = pack["limits_in_force"]["merchant"]
        assert merchant["budget_cap_inr"] == 500, protocol
        assert merchant["allowed_categories"], protocol


# ------------------------------------------------------------- the two checks

def test_an_order_inside_both_limits_reads_clean(client):
    _shop(cap=500, confirm=400)
    order = _ap2_order(client, "clean", [{"item_id": "masala_dosa", "qty": 1}],
                       buyer_limits={"hard_cap_inr": 600, "soft_cap_inr": 300})
    pack = evidence.build_evidence_pack(order["decision_detail"]["event_id"])

    within, confirm = pack["checks"]
    assert within["result"] == "yes" and within["tone"] == "leaf"
    assert confirm["result"] == "not_required" and confirm["tone"] == "leaf"
    assert pack["confirmation_trail"]["answers"] == []
    assert "80" in within["detail"] and "600" in within["detail"]


def test_an_order_over_her_threshold_shows_the_confirmation_that_answered_it(client):
    _shop(cap=500, confirm=400)
    order = _ap2_order(client, "confirmed", [{"item_id": "chicken_biryani", "qty": 2}],
                       buyer_limits={"hard_cap_inr": 600, "soft_cap_inr": 300})
    order_id = order["decision_detail"]["event_id"]

    # Before she answers, the gap is shown as a gap.
    gap = evidence.build_evidence_pack(order_id)["checks"][1]
    assert gap["result"] == "missing" and gap["tone"] == "brick"

    client.post(f"/ap2/intent-mandates/{order['id']}/human-confirm", json={})

    pack = evidence.build_evidence_pack(order_id)
    confirm = pack["checks"][1]
    assert confirm["result"] == "confirmed" and confirm["tone"] == "rust"
    assert pack["confirmation_trail"]["answers"], "no human answer on the record"
    assert pack["confirmation_trail"]["answers"][-1]["outcome"] == "accepted"
    # And the message she was actually sent, while it is still in memory.
    assert any(f"Order #{order_id}" in m["body"]
               for m in pack["confirmation_trail"]["messages"])


def test_an_order_over_the_customers_own_cap_says_so(client):
    _shop(cap=900, confirm=800)
    order = _ap2_order(client, "overcap", [{"item_id": "chicken_biryani", "qty": 2}],
                       buyer_limits={"hard_cap_inr": 300, "soft_cap_inr": 200})
    within = evidence.build_evidence_pack(order["decision_detail"]["event_id"])["checks"][0]
    assert within["result"] == "no" and within["tone"] == "brick"
    assert "440" in within["detail"] and "300" in within["detail"]


# ------------------------------------------------- absence, shown as absence

def test_a_protocol_without_buyer_reasoning_says_so_rather_than_inventing_one(client):
    """Only the MCP tools require a reason. An ACP order genuinely has
    none, and a record with a marked hole is worth more than one with a
    plausible guess in it."""
    _shop(cap=500, confirm=400)
    detail = orchestrator.negotiate_and_record("acp-noreason", "acp", [("masala_dosa", 1)])
    pack = evidence.build_evidence_pack(detail["event_id"])

    assert pack["buyer_reasoning"]["available"] is False
    assert "ACP" in pack["buyer_reasoning"]["why"]
    assert "text" not in pack["buyer_reasoning"]


def test_a_buyer_reasoning_that_exists_is_carried_verbatim(client):
    _shop(cap=500, confirm=400)
    import adapter_mcp
    from adapter_mcp import CartItem

    adapter_mcp.propose_cart_impl(
        [CartItem(item_id="masala_dosa", qty=1)], "friend visiting who has not tried dosa")
    row = audit_log.get_events_for_agent("mcp:claude", db_path=audit_log.DEFAULT_DB_PATH)[0]

    pack = evidence.build_evidence_pack(row["id"])
    assert pack["buyer_reasoning"]["available"] is True
    assert pack["buyer_reasoning"]["text"] == "friend visiting who has not tried dosa"


def test_a_customer_limit_that_was_never_supplied_is_not_invented(client):
    _shop(cap=500, confirm=400)
    detail = orchestrator.negotiate_and_record("no-limits", "acp", [("masala_dosa", 1)])
    within = evidence.build_evidence_pack(detail["event_id"])["checks"][0]

    assert within["result"] == "not_recorded"
    assert "not supplied" in within["detail"]
    assert "numbers" not in within


# ------------------------------------------------------------------ disputes

def test_marking_an_order_disputed_surfaces_it(client):
    _shop(cap=500, confirm=400)
    detail = orchestrator.negotiate_and_record("disputed-1", "acp", [("masala_dosa", 1)])
    order_id = detail["event_id"]

    assert client.get("/api/disputes").json()["disputes"] == []

    res = client.post(f"/api/orders/{order_id}/dispute").json()
    assert res["disputed"] is True
    assert res["evidence_url"] == f"/evidence/{order_id}"

    listed = client.get("/api/disputes").json()["disputes"]
    assert [d["order_id"] for d in listed] == [order_id]
    assert listed[0]["has_snapshot"] is True
    assert evidence.build_evidence_pack(order_id)["disputed_at"]


def test_marking_the_same_order_twice_is_not_an_error(client):
    _shop(cap=500, confirm=400)
    order_id = orchestrator.negotiate_and_record("twice", "acp", [("masala_dosa", 1)])["event_id"]
    assert client.post(f"/api/orders/{order_id}/dispute").status_code == 200
    assert client.post(f"/api/orders/{order_id}/dispute").status_code == 200
    assert len(client.get("/api/disputes").json()["disputes"]) == 1


def test_an_order_that_does_not_exist_is_a_404_not_a_blank_record(client):
    assert client.get("/api/evidence/999999").status_code == 404
    assert client.post("/api/orders/999999/dispute").status_code == 404


def test_a_lifecycle_row_resolves_to_the_order_it_belongs_to(client):
    """The trail is append-only, so an order has several rows. Asking for
    any of them must give the record for the order, not a fragment."""
    _shop(cap=500, confirm=400)
    order_id = orchestrator.negotiate_and_record("lifecycle", "mcp", [("masala_dosa", 1)])["event_id"]
    child = audit_log.record_event(
        agent_id="lifecycle", protocol="mcp", cart=[{"item": "masala_dosa", "qty": 1}],
        decision="PAID", reason="captured", total_inr=80,
        db_path=audit_log.DEFAULT_DB_PATH, order_ref=order_id,
    )
    assert evidence.build_evidence_pack(child)["order_id"] == order_id


# ------------------------------------------------------ what it must not be

def test_the_pack_states_facts_and_never_apportions_fault(client):
    """Evidence, not a verdict. Nothing here decides who owes whom, and
    no field is allowed to start implying it."""
    import json

    _shop(cap=500, confirm=400)
    order_id = orchestrator.negotiate_and_record("factual-only", "acp", [("masala_dosa", 1)])["event_id"]
    blob = json.dumps(evidence.build_evidence_pack(order_id)).lower()

    for word in ("liable", "liability", "fault", "verdict", "ruling", "guilty"):
        assert word not in blob, f"the pack implies a judgement: {word!r}"


def test_evidence_reads_and_never_writes(client):
    """It is assembled from the trail; assembling it must not add to it."""
    _shop(cap=500, confirm=400)
    order_id = orchestrator.negotiate_and_record("readonly", "acp", [("masala_dosa", 1)])["event_id"]
    before = len(audit_log.get_all_events(db_path=audit_log.DEFAULT_DB_PATH, limit=1000))

    for _ in range(3):
        evidence.build_evidence_pack(order_id)

    assert len(audit_log.get_all_events(db_path=audit_log.DEFAULT_DB_PATH, limit=1000)) == before


def test_evidence_never_reaches_the_decision_core():
    """It reads the trail and returns an object. If it vanished tomorrow
    no order would come out differently."""
    import ast

    import negotiation

    for module in (negotiation, orchestrator):
        with open(module.__file__) as handle:
            source = handle.read()
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "evidence" not in imported, module.__name__
