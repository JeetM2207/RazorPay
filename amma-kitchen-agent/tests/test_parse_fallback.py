"""Parsing when the model is unreachable.

The provider ran out of credit mid-demo and the buyer console stopped
dead at "Cannot draft a cart without interpreting the request". That is
the wrong failure: the model's only job here is turning words into a cart
proposal, and a proposal is not a decision. Every limit that matters is
plain Python underneath and none of it needs a model to run.

So the fallback exists, and these tests are mostly about the two ways it
could be dishonest -- guessing a dish nobody ordered, or being invisible.
"""

import merchant_config
import pytest
from fastapi.testclient import TestClient

import app


@pytest.fixture
def client(monkeypatch):
    # No key: the fallback path, without needing the provider to be down.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    merchant_config.save(
        profile_in={"shop_name": "Amma's Kitchen"},
        mandate_in={"budget_cap_inr": 500, "human_confirm_threshold_inr": 400},
        menu_in=[
            {"title": "Veg Thali", "category": "meals", "price_inr": 150, "stock": 20},
            {"title": "Paneer Bhurji", "category": "meals", "price_inr": 180, "stock": 10},
            {"title": "Tandoori Roti", "category": "meals", "price_inr": 20, "stock": 60},
            {"title": "Filter Coffee", "category": "beverages", "price_inr": 30, "stock": 40},
            {"title": "Party Catering Tray", "category": "bulk_catering", "price_inr": 350,
             "stock": 5, "agent_orderable": False},
        ],
    )
    return TestClient(app.app)


def _parse(client, text):
    r = client.post("/api/parse-cart", json={"text": text})
    assert r.status_code == 200, r.text
    return r.json()


def test_the_request_that_died_now_parses(client):
    """The exact sentence from the failed run."""
    out = _parse(client, "Order 1 paneer bhurji and 4 tandoori roti")
    assert out["items"] == [
        {"item_id": "paneer_bhurji", "qty": 1},
        {"item_id": "tandoori_roti", "qty": 4},
    ]
    assert out["unmatched"] == []


def test_it_says_which_parser_answered(client):
    """A demo that quietly degrades is worse than one that stops, because
    a viewer cannot tell which parser produced the cart."""
    out = _parse(client, "one veg thali")
    assert out["parsed_by"] == "menu-matching"
    assert out["fallback_reason"]


def test_a_miss_is_reported_never_substituted(client):
    """The one thing this must never do. Exact matching cannot tell 'not
    sold here' from 'close to something here', so it misses more than the
    model -- and a miss becomes a question asked over WhatsApp, while a
    wrong match would be a dish nobody ordered, silently in a cart."""
    out = _parse(client, "2 pizzas, a coke, and one veg thali")
    assert out["items"] == [{"item_id": "veg_thali", "qty": 1}]
    assert out["unmatched"] == ["2 pizzas", "a coke"]


def test_an_ambiguous_phrase_is_a_miss_not_a_guess(client):
    """'meals' could be any of three dishes. resolve_item returns None
    rather than picking one, and that conservatism is the whole design."""
    merchant_config.save(
        profile_in={"shop_name": "Amma's Kitchen"},
        mandate_in={"budget_cap_inr": 500, "human_confirm_threshold_inr": 400},
        menu_in=[
            {"title": "Veg Biryani", "category": "meals", "price_inr": 150, "stock": 20},
            {"title": "Chicken Biryani", "category": "meals", "price_inr": 220, "stock": 20},
        ],
    )
    out = _parse(client, "2 biryani")
    assert out["items"] == []
    assert out["unmatched"] == ["2 biryani"]


def test_quantities_in_words_and_digits_both_read(client):
    assert _parse(client, "three veg thali")["items"] == [{"item_id": "veg_thali", "qty": 3}]
    assert _parse(client, "3 veg thali")["items"] == [{"item_id": "veg_thali", "qty": 3}]
    assert _parse(client, "a couple of filter coffee")["items"] == [
        {"item_id": "filter_coffee", "qty": 2}]


def test_the_same_dish_twice_is_summed_not_repeated(client):
    """The same rule the console's basket follows: a cart line is a dish
    and a quantity, not one line per mention."""
    out = _parse(client, "1 veg thali and 2 veg thali")
    assert out["items"] == [{"item_id": "veg_thali", "qty": 3}]


def test_the_fallback_cannot_get_anything_past_the_merchants_rules(client):
    """The actual safety property. Whatever produced the cart, the gates
    below are unchanged -- so a disallowed category is refused exactly as
    it is when Claude parsed it, and no Razorpay call is made."""
    import orchestrator

    out = _parse(client, "1 party catering tray")
    assert out["items"] == [{"item_id": "party_catering_tray", "qty": 1}]

    result = orchestrator.negotiate_and_record(
        agent_id="agent-fallback", protocol="acp",
        cart=[(i["item_id"], i["qty"]) for i in out["items"]],
    )
    assert result["decision"] == "ESCALATE"
    assert result.get("payment_id") is None


def test_the_parser_makes_no_decision_of_its_own(client):
    """It proposes a cart and nothing else -- no price, no verdict, no
    limit anywhere in what it returns."""
    out = _parse(client, "3 veg thali")
    assert set(out) == {"items", "unmatched", "parsed_by", "fallback_reason"}
    assert not any(k in str(out) for k in ("budget_cap", "threshold", "APPROVE", "ESCALATE"))


def test_it_reads_the_live_menu_not_a_fixed_list(client):
    """She renames a dish; the parser follows without a code change."""
    merchant_config.save(
        profile_in={"shop_name": "Amma's Kitchen"},
        mandate_in={"budget_cap_inr": 500, "human_confirm_threshold_inr": 400},
        menu_in=[{"title": "Sunday Special", "category": "meals", "price_inr": 200, "stock": 5}],
    )
    assert _parse(client, "2 sunday special")["items"] == [
        {"item_id": "sunday_special", "qty": 2}]


def test_nothing_matched_still_answers_rather_than_erroring(client):
    """An empty cart is a real answer -- it is what triggers the 'what
    would you like instead?' message. It must not be a 502."""
    out = _parse(client, "2 pizzas and a burger")
    assert out["items"] == []
    assert out["unmatched"] == ["2 pizzas", "a burger"]


def test_the_fallback_is_pure_of_models_and_money(client):
    """merchant_config gained a parser; it must not have gained a model
    call or a payment import along with it."""
    import merchant_config as mc

    source = open(mc.__file__, encoding="utf-8").read()
    for forbidden in ("llm_client", "openai", "anthropic", "razorpay", "requests.post"):
        assert forbidden not in source, f"the offline parser reaches {forbidden}"
