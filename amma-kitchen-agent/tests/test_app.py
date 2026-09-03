import re

import pytest

import merchant_auth
import merchants
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
    client = TestClient(unified.app)
    # These tests drive merchant surfaces, which now need a login. The
    # session is minted directly rather than posted through the form,
    # because the login itself is what tests/test_merchant_auth.py is
    # for -- and leaving these anonymous would only prove the guard
    # fires, which is already covered there.
    client.cookies.set(merchant_auth.COOKIE_NAME, merchant_auth.issue_cookie())
    return client


def test_all_human_facing_pages_render(client):
    for path in ("/", "/buyer", "/buyer/order", "/merchant", "/audit"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        # The PLATFORM's name, not one kitchen's. A customer choosing
        # between three kitchens is not on any one of their sites, and
        # asserting a single shop's name on every page is what a
        # single-tenant demo asserts.
        #
        # Read off Platform rather than hardcoded, so a rename is one
        # edit. The <title> carries it unsplit; the visible wordmark
        # splits it so "AI" can be lit, which is why this cannot just
        # grep the body for the whole string.
        assert merchants.Platform.name in resp.text, path


def test_the_wordmark_lights_the_ai_half(client):
    """Bhojnal + AI, with the AI carrying its own class.

    The lit half is the product claim: it is the part that is not a
    normal eatery. If a restyle ever flattens the wordmark back into one
    run of text this fails rather than quietly shipping."""
    for path in ("/buyer", "/buyer/order", "/merchant"):
        body = client.get(path).text
        assert 'class="wm-ai">AI</span>' in body, path
        assert "Bhojnal" in body, path


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
    # "pan" is checked as a whole word. As a bare substring it fires on
    # "paneer", "expand" and "company" -- and a guard that cries wolf on
    # a dish name is a guard somebody switches off.
    for forbidden in ("card_number", "cardnumber", "\"cvv\""):
        assert forbidden not in blob, f"an API surface accepts {forbidden}"
    assert not re.search(r"\bpan\b", blob), "an API surface accepts a PAN"


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


def test_demand_endpoint_surfaces_what_she_does_not_sell(client):
    """Collected signal that nothing displays is signal nobody acts on --
    the same mistake as an escalation that never reaches her queue."""
    import adapter_mcp

    for _ in range(2):
        adapter_mcp.propose_cart_impl([], "no reason given", None, ["2 pizzas"])
    adapter_mcp.propose_cart_impl([], "no reason given", None, ["tiramisu"])

    report = client.get("/api/demand").json()["demand"]
    assert [r["requested"] for r in report] == ["2 pizzas", "tiramisu"]
    assert report[0]["times"] == 2

    console = client.get("/merchant/orders").text
    assert "Asked for, but not on your menu" in console


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


def test_parse_cart_falls_back_to_the_menu_when_no_model_key_is_set(client, monkeypatch):
    """This used to answer 503 and stop the order dead, which is the wrong
    failure: the model only proposes a cart, and a proposal is not a
    decision. It now matches against the menu directly and SAYS it did --
    see tests/test_parse_fallback.py for the rest."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    resp = client.post("/api/parse-cart", json={"text": "two biryanis"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["parsed_by"] == "menu-matching"
    assert body["fallback_reason"] == "no model key is configured"


# ------------------------------------------- the AI Strategist is read-only

def test_insights_returns_the_numbers_even_with_no_model_key(client, monkeypatch):
    """The figures are the useful part; the prose is a convenience on top.
    A missing key must degrade to a dashboard, not an error page."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    body = client.get("/api/insights").json()

    assert body["insight"] is None
    assert "OPENROUTER_API_KEY" in body["note"]
    assert body["stats"]["window_hours"] == 24
    assert body["stats"]["revenue_inr"] == 0


def test_insights_survives_the_model_failing(client, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    import llm_client

    monkeypatch.setattr(llm_client, "generate_merchant_insights",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("provider down")))

    body = client.get("/api/insights").json()
    assert body["insight"] is None
    assert "provider down" in body["note"]
    assert "stats" in body


def test_insights_renders_the_model_answer(client, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    import llm_client

    monkeypatch.setattr(
        llm_client, "generate_merchant_insights",
        lambda stats, hours=24: {"observation": "Three people wanted pizza.",
                                 "action": "Put a pizza on the menu."},
    )

    body = client.get("/api/insights?hours=168").json()
    assert body["insight"]["action"] == "Put a pizza on the menu."
    assert body["stats"]["window_hours"] == 168


def test_the_insight_window_cannot_be_pushed_anywhere_silly(client):
    assert client.get("/api/insights?hours=0").json()["stats"]["window_hours"] == 1
    assert client.get("/api/insights?hours=99999").json()["stats"]["window_hours"] == 720


def test_growth_insights_never_reach_the_decision_path(client):
    """The whole feature is additive. If it vanished tomorrow, no order
    would come out differently -- so nothing in the decision or money path
    is allowed to import it."""
    import ast

    import negotiation
    import orchestrator

    for module in (negotiation, orchestrator):
        with open(module.__file__) as handle:
            source = handle.read()
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "llm_client" not in imported, f"{module.__name__} reaches a model"
        assert "growth_stats" not in source, f"{module.__name__} reads the insights"
        assert "generate_merchant_insights" not in source


def test_the_merchant_console_shows_the_panel(client):
    page = client.get("/merchant/orders").text
    assert "AI Strategist" in page
    assert "loadInsights" in page


def test_customer_written_demand_is_escaped_before_it_is_rendered(client):
    """unmatched_demand is free text typed by customers and relayed by
    somebody else's model, and so is the model's own prose. Both are
    rendered, so both go through esc()."""
    page = client.get("/merchant/orders").text
    assert "esc(d.requested)" in page or "esc(big)" in page
    assert "esc(data.insight.observation)" in page
    assert "esc(data.insight.action)" in page


def test_no_console_script_declares_the_same_name_twice():
    """A duplicate top-level `const` is a SyntaxError, and a SyntaxError
    kills the WHOLE script block -- so one careless redeclaration blanks
    every table on the page, not just the new feature.

    This exists because it happened: adding a second `rupee` helper to the
    merchant console left the page silently empty, while a test asserting
    the new function's name appeared in the HTML passed happily. Checking
    that a string is present cannot tell you the script parses.
    """
    import pathlib
    import re

    # `function` too, not just const/let: a duplicate function declaration
    # is the same SyntaxError, and it is how the picker's cartTotal()
    # collided with the agent's cartTotal() and blanked the whole page.
    pattern = re.compile(
        r"^(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=|^function\s+([A-Za-z_$][\w$]*)\s*\(",
        re.MULTILINE,
    )
    web = pathlib.Path(__file__).resolve().parent.parent / "web"
    for page in web.glob("*.html"):
        names = [a or b for a, b in pattern.findall(page.read_text(encoding="utf-8"))]
        duplicates = {n for n in names if names.count(n) > 1}
        assert not duplicates, f"{page.name} declares {duplicates} more than once"


def test_optimize_prices_endpoint_reprices_and_says_what_it_did(client):
    import merchant_config

    merchant_config.save(
        profile_in={"shop_name": "Amma's Kitchen"},
        mandate_in={"budget_cap_inr": 500, "human_confirm_threshold_inr": 400},
        menu_in=[
            {"title": "Veg Thali", "category": "meals", "price_inr": 200, "stock": 20},
            {"title": "Last Laddu", "category": "desserts", "price_inr": 40, "stock": 1},
        ],
    )

    body = client.post("/api/merchant/optimize-prices").json()

    assert body["discounted"] == 1
    assert [c["id"] for c in body["changed"]] == ["veg_thali"]
    assert body["changed"][0]["now_inr"] == 170

    # And the very next catalog fetch already carries it.
    item = {i["id"]: i for i in client.get("/catalog").json()["items"]}["veg_thali"]
    assert item["price"] == 170 and item["sale"] is True


def test_optimize_prices_is_safe_to_press_twice(client):
    import merchant_config

    merchant_config.save(
        profile_in={"shop_name": "Amma's Kitchen"},
        mandate_in={"budget_cap_inr": 500, "human_confirm_threshold_inr": 400},
        menu_in=[{"title": "Veg Thali", "category": "meals", "price_inr": 200, "stock": 20}],
    )

    assert client.post("/api/merchant/optimize-prices").json()["discounted"] == 1
    second = client.post("/api/merchant/optimize-prices").json()
    assert second["changed"] == [], "a second press moved a price again"


def test_pricing_never_reaches_the_decision_core(client):
    """The feature writes to the live config and nothing else. The core
    is handed a menu with prices on it, exactly as it always was."""
    import ast

    import negotiation

    with open(negotiation.__file__) as handle:
        source = handle.read()
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "merchant_config" not in imported
    for word in ("optimize_prices", "sale", "list_price"):
        assert word not in source, f"negotiation.py knows about {word}"


def test_the_console_offers_the_button(client):
    page = client.get("/merchant/orders").text
    assert "Optimize yield" in page
    assert "optimizeYield" in page


def test_order_outcomes_endpoint_is_bounded(client):
    assert client.get("/api/order-outcomes?minutes=0").status_code == 200
    assert client.get("/api/order-outcomes?minutes=999999").status_code == 200
    assert client.get("/api/order-outcomes").json()["outcomes"] == []


def test_the_terminal_marks_the_merchants_hard_limits(client):
    """The terminal is where a viewer either believes the limits are real
    arithmetic or does not, so the lines that quote one are marked."""
    page = client.get("/buyer/order").text

    assert "markLimits" in page
    for phrase in ("budget cap", "human confirm", "mandate"):
        assert phrase in page, phrase
    # Colour comes from the line's own kind, which the caller set from the
    # server's verdict -- the page must not be deciding pass/fail itself.
    assert "FAIL_KINDS" in page and "PASS_KINDS" in page


def test_the_buyer_screen_watches_for_a_refund(client):
    page = client.get("/buyer/order").text

    assert "refundToast" in page
    assert "/api/order-outcomes" in page
    assert "automatically refunded via the Razorpay API" in page
    # A reload must not replay the day's outcomes as if they just happened.
    assert "seenOutcomes" in page


def test_the_sms_feed_says_who_each_message_was_for(client, monkeypatch):
    import buyer_sms
    import escalations
    import notification_service

    monkeypatch.setattr(notification_service, "TWILIO_CONFIGURED", False)
    notification_service.clear_outbox()
    escalations.reset()
    buyer_sms.ask_approval(agent_id="a", phone="8306610707",
                           cart_label="2x Paneer Bhurji", total_inr=300, soft_cap_inr=300)

    body = client.get("/api/sms").json()
    assert body["outbox"][0]["audience"] == "customer"
    assert body["escalations"] == [], "nothing was ever asked of the merchant"


def test_her_console_shows_only_messages_addressed_to_her(client):
    """The earlier fix labelled the customer's questions and showed them
    anyway. That was the wrong call.

    Both parties reach the same outbox -- in a demo they are usually the
    same phone number -- but a merchant board should show what was said to
    HER. A question addressed to somebody else, with reply buttons under
    it that answer a different conversation, is noise at best. The
    customer answers in the buyer console, where their question is.
    """
    page = client.get("/merchant/orders").text

    assert "renderQuickReplies" in page
    # The filter is the whole point.
    assert '(m.audience || "merchant") !== "customer"' in page

    # No customer vocabulary on this screen at all any more: those
    # messages are not here to be answered.
    assert 'data-reply="YES"' not in page
    assert "answer in the buyer console" not in page

    # Hers still carry the single-use code, because a bare "1" no longer
    # moves anything -- see tests/test_reply_codes.py.
    assert 'data-reply="1 ${code}"' in page


def test_her_console_is_not_dressed_up_as_a_phone(client):
    """A fake handset around real content adds nothing a merchant needs
    and reads as a toy on a board she is meant to work from."""
    page = client.get("/merchant/orders").text
    for prop in ("phone-screen", "phone-av", "phone-who", "phone-feed", "phone-top"):
        assert prop not in page, f"the phone mock-up left {prop} behind"


# ------------------------------- pay-first, from the buyer console this time

def _escalating_ap2(client, agent):
    """Rs.440 -- over her Rs.400 confirmation threshold, under her cap."""
    mandate = client.post("/ap2/intent-mandates", json={
        "agent_id": agent, "intent": {"items": [{"item_id": "chicken_biryani", "qty": 2}]},
    }).json()["intent_mandate"]
    client.post(f"/ap2/intent-mandates/{mandate['id']}/settle-pending-confirmation")
    cart = client.post(f"/ap2/intent-mandates/{mandate['id']}/cart-mandate").json()["cart_mandate"]
    paid = client.post(f"/ap2/cart-mandates/{cart['id']}/execute-payment").json()["payment_mandate"]
    return mandate, paid


def test_the_paid_order_reaches_her_queue_tagged_with_its_own_protocol(client, monkeypatch):
    """The lifecycle is shared now, so her queue must say which door an
    order came through rather than calling everything MCP."""
    import mcp_orders

    _escalating_ap2(client, "pf-queue")

    mine = [p for p in client.get("/api/pending").json()["pending"]
            if p["agent_id"] == "pf-queue"]
    assert len(mine) == 1, "listed twice, or not at all"
    assert mine[0]["protocol"] == "ap2"
    assert mine[0]["decision_detail"]["already_paid"] is True


def test_declining_a_simulated_settlement_says_so_rather_than_claiming_a_refund(client):
    """A `sim_` reference has no Razorpay payment behind it -- asking to
    refund one is rejected as an invalid id. The order is still reversed
    and still closed, and the trail says which kind of reversal it was.
    Printing "refunded to your card" here would be a false statement
    about money."""
    import mcp_orders

    _, paid = _escalating_ap2(client, "pf-sim")
    assert paid["payment_id"].startswith("sim_")

    ref = [p for p in client.get("/api/pending").json()["pending"]
           if p["agent_id"] == "pf-sim"][0]["session_id"]
    result = client.post(f"/mcp-orders/{ref}/human_reject").json()

    assert result["status"] == mcp_orders.REFUNDED
    assert result["simulated"] is True
    outcome = [o for o in client.get("/api/order-outcomes").json()["outcomes"]
               if str(o["order_ref"]) == str(ref)][0]
    assert "no real money moved" in outcome["reason"]


def test_the_dish_picker_is_a_real_basket(client):
    """Tapping the same dish twice must produce "2 veg thali", not
    "1 veg thali, 1 veg thali" -- which is what the parser would have to
    untangle, and what a customer would rightly find odd."""
    page = client.get("/buyer/order").text

    assert "bumpDish" in page and "pickedText" in page
    assert 'data-bump="-1"' in page and 'data-bump="1"' in page
    assert "cartBar" in page
    # The quantity is merged into one line per dish.
    assert '`${qty} ${(dish ? dish.title : id.replace(/_/g, " ")).toLowerCase()}`' in page
    # Typing by hand wins; the tiles reset rather than showing stale counts.
    assert "pickerWroteBox" in page


def test_the_basket_does_not_shadow_the_agents_own_cart(client):
    """order.html already had cartTotal() for the cart the AGENT drafted.
    A second one for the picker's basket is a SyntaxError that blanks the
    whole page -- which is exactly what happened."""
    page = client.get("/buyer/order").text

    assert "function pickedTotal()" in page
    assert page.count("const cartTotal") + page.count("function cartTotal") == 1
