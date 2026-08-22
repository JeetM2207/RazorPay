from mandate import MANDATE, MENU, Mandate
from negotiation import CartLine, Decision, NegotiationResult, evaluate, suggest_upsell


def test_approve_small_order_within_budget():
    result = evaluate([("masala_dosa", 1), ("filter_coffee", 1)])
    assert result.decision == Decision.APPROVE
    assert result.total_inr == 110
    assert result.alternatives == ()


def test_escalate_at_human_confirm_threshold_even_within_budget():
    # 2 chicken_biryani = Rs.440: under the Rs.500 cap, but at/above the
    # Rs.400 human-confirm threshold, so it must still escalate.
    result = evaluate([("chicken_biryani", 2)])
    assert result.decision == Decision.ESCALATE
    assert result.total_inr == 440
    assert "human confirmation threshold" in result.reason


def test_counter_offer_when_over_cap_but_within_flexible_margin():
    # chicken_biryani(2) + masala_dosa(1) = 440 + 80 = 520.
    # Cap is 500, margin is 10% -> ceiling 550. 520 is over cap but under
    # ceiling, so this must counter-offer, not escalate.
    result = evaluate([("chicken_biryani", 2), ("masala_dosa", 1)])
    assert result.decision == Decision.COUNTER_OFFER
    assert result.total_inr == 520
    assert len(result.alternatives) >= 1
    for alt in result.alternatives:
        alt_total = sum(MENU[line.item].price_inr * line.qty for line in alt)
        assert alt_total <= MANDATE.budget_cap_inr


def test_counter_offer_produces_distinct_drop_and_scale_alternatives():
    # Same cart as above: dropping the whole biryani line vs. trimming its
    # quantity should yield two different alternatives.
    result = evaluate([("chicken_biryani", 2), ("masala_dosa", 1)])
    assert result.decision == Decision.COUNTER_OFFER
    assert len(result.alternatives) == 2
    assert (CartLine("masala_dosa", 1),) in result.alternatives
    assert (CartLine("chicken_biryani", 1), CartLine("masala_dosa", 1)) in result.alternatives


def test_escalate_when_over_cap_beyond_flexible_margin():
    # chicken_biryani(2) + veg_thali(1) = 440 + 150 = 590, which is beyond
    # the 550 ceiling (500 * 1.10) -> must escalate, not counter-offer.
    result = evaluate([("chicken_biryani", 2), ("veg_thali", 1)])
    assert result.decision == Decision.ESCALATE
    assert result.total_inr == 590
    assert "exceeds budget cap" in result.reason


def test_escalate_on_disallowed_category():
    strict_mandate = Mandate(allowed_categories=("meals",))
    result = evaluate([("masala_dosa", 1)], mandate=strict_mandate)
    assert result.decision == Decision.ESCALATE
    assert "category not allowed" in result.reason


def test_escalate_on_unknown_item():
    result = evaluate([("mystery_item", 1)])
    assert result.decision == Decision.ESCALATE
    assert "unknown item" in result.reason


def test_counter_offer_when_quantity_exceeds_stock():
    # masala_dosa stock is 25; requesting 100 can't be fulfilled as asked.
    result = evaluate([("masala_dosa", 100)])
    assert result.decision == Decision.COUNTER_OFFER
    assert "exceeds available stock" in result.reason
    assert result.alternatives == ((CartLine("masala_dosa", 25),),)


def test_upsell_suggests_highest_value_item_that_fits_headroom():
    # masala_dosa alone = Rs.80. Headroom to the Rs.400 threshold is large,
    # so the priciest still-fitting item (chicken_biryani, Rs.220) should
    # be suggested.
    suggestion = suggest_upsell([("masala_dosa", 1)])
    assert suggestion is not None
    assert suggestion.name == "chicken_biryani"


def test_upsell_respects_tight_headroom():
    # veg_thali + masala_dosa + filter_coffee = 150+80+30 = 260.
    # Headroom to 400 is 139: chicken_biryani (220) no longer fits,
    # gulab_jamun (60) does.
    suggestion = suggest_upsell([("veg_thali", 1), ("masala_dosa", 1), ("filter_coffee", 1)])
    assert suggestion is not None
    assert suggestion.name == "gulab_jamun"


def test_upsell_returns_none_when_no_headroom_left():
    # chicken_biryani + veg_thali + filter_coffee = 220+150+30 = 400,
    # already at the human-confirm threshold -- no room to suggest anything.
    suggestion = suggest_upsell([("chicken_biryani", 1), ("veg_thali", 1), ("filter_coffee", 1)])
    assert suggestion is None


def test_upsell_never_appears_in_evaluate_result():
    # suggest_upsell is a separate, optional hook -- evaluate()'s own
    # result type has no knowledge of it.
    result = evaluate([("masala_dosa", 1)])
    assert not hasattr(result, "upsell")
    assert "upsell" not in NegotiationResult.__dataclass_fields__


def test_no_payment_side_effects_from_negotiation_module():
    # The negotiation core must never import or call anything Razorpay-
    # or network-related. This is the single most important design rule.
    import negotiation

    source = negotiation.__file__
    with open(source) as f:
        contents = f.read()
    assert "razorpay" not in contents.lower()
    assert "requests" not in contents.lower()
