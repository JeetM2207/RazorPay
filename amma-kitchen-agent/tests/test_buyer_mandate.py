from buyer_mandate import BuyerDecision, BuyerMandate, check_cart
from mandate import MANDATE


def test_small_order_proceeds_without_asking_anyone():
    result = check_cart([("masala_dosa", 1)])
    assert result.decision == BuyerDecision.PROCEED
    assert result.total_inr == 80


def test_order_at_the_confirm_line_asks_the_customer():
    # confirm_above defaults to Rs.300; veg_thali + chicken_biryani = 370.
    result = check_cart([("veg_thali", 1), ("chicken_biryani", 1)])
    assert result.decision == BuyerDecision.ASK_USER
    assert "consulted" in result.reason


def test_order_over_the_hard_cap_is_refused_outright():
    # 3 chicken_biryani = Rs.660, over the Rs.600 cap.
    result = check_cart([("chicken_biryani", 3)])
    assert result.decision == BuyerDecision.REFUSE
    assert "will not send this to a merchant" in result.reason


def test_the_hard_cap_cannot_be_unlocked_by_confirmation():
    """There is deliberately no ASK_USER path above spend_cap: a customer
    who wants to spend more raises their own cap, they don't approve past
    it in the moment."""
    tight = BuyerMandate(spend_cap_inr=100, confirm_above_inr=50)
    result = check_cart([("chicken_biryani", 1)], mandate=tight)
    assert result.decision == BuyerDecision.REFUSE


def test_unknown_item_is_refused_rather_than_guessed():
    result = check_cart([("caviar", 1)])
    assert result.decision == BuyerDecision.REFUSE
    assert "unknown item" in result.reason


def test_buyer_and_merchant_limits_are_genuinely_independent():
    """The whole point of this module: the two sides gate separately.

    A cart can clear the buyer's mandate and still be stopped by the
    merchant's, and vice versa -- neither defers to the other.
    """
    # Rs.350 catering tray: fine by a permissive customer...
    permissive = BuyerMandate(spend_cap_inr=1000, confirm_above_inr=900)
    assert check_cart([("party_catering_tray", 1)], mandate=permissive).decision == BuyerDecision.PROCEED
    # ...but the merchant refuses that category outright.
    from negotiation import Decision, evaluate

    assert evaluate([("party_catering_tray", 1)]).decision == Decision.ESCALATE

    # And the reverse: a cart the merchant would happily auto-approve...
    assert evaluate([("veg_thali", 1)]).decision == Decision.APPROVE
    # ...can still be blocked by a strict customer.
    strict = BuyerMandate(spend_cap_inr=100, confirm_above_inr=50)
    assert check_cart([("veg_thali", 1)], mandate=strict).decision == BuyerDecision.REFUSE


def test_buyer_module_never_touches_payment_or_the_merchant_core():
    """Checks real imports, not mere mentions -- the docstring is
    expected to reference the merchant side, since explaining the
    separation is the point of the file."""
    import ast

    import buyer_mandate

    with open(buyer_mandate.__file__) as f:
        tree = ast.parse(f.read())

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "negotiation" not in imported, "the buyer's gate must not call the merchant's"
    assert "razorpay" not in imported
    assert "razorpay_client" not in imported
    assert "requests" not in imported
    assert "orchestrator" not in imported


def test_defaults_are_distinct_from_the_merchant_thresholds():
    """Guards against the two mandates silently converging into one
    number, which is what made the ownership confusing in the first place."""
    from buyer_mandate import DEFAULT_BUYER_MANDATE

    assert DEFAULT_BUYER_MANDATE.confirm_above_inr != MANDATE.human_confirm_threshold_inr
    assert DEFAULT_BUYER_MANDATE.spend_cap_inr != MANDATE.budget_cap_inr
