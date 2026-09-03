"""One kitchen's paid queue must not contain another kitchen's orders.

Found by signing into the grill house and seeing Amma's paid orders on
the board, each with a live Approve and a live Decline. Decline is not a
display action: it issues a real refund against a real payment. So an
unscoped queue handed one merchant the power to reverse another
merchant's completed sale, and the trust badge beside each row was
computed from platform-wide history, so an agent that had proved itself
at Amma's arrived on the grill house's board already wearing TRUSTED.

Both were invisible until a second kitchen existed to be wrong about --
the same shape as the seven leaks already recorded in CLAUDE.md, and the
reason this one gets a test of its own.
"""

import pytest

import adapter_mcp
import audit_log
import mcp_orders
import merchants


def _paid_order_at(kitchen: str, agent: str, total: int) -> int:
    """A cart that was decided, paid for, and is now waiting on a human."""
    ref = audit_log.record_event(
        agent_id=agent, protocol="ap2", cart=[{"item": "veg_thali", "qty": 2}],
        decision="ESCALATE", reason="over the merchant's confirmation threshold",
        total_inr=total, merchant_id=kitchen,
    )
    for status in ("AWAITING_PAYMENT", "PAID", mcp_orders.PENDING_MERCHANT_APPROVAL):
        audit_log.record_event(
            agent_id=agent, protocol="ap2", cart=[{"item": "veg_thali", "qty": 2}],
            decision=status, reason="lifecycle", total_inr=total,
            order_ref=ref, merchant_id=kitchen,
        )
    return ref


@pytest.fixture
def two_kitchens():
    default = merchants.default_id()
    other = next(m["id"] for m in merchants.all() if m["id"] != default)
    return default, other


def test_a_kitchen_is_offered_only_its_own_paid_orders(two_kitchens):
    mine, theirs = two_kitchens
    ours = _paid_order_at(mine, "agent-mine", 440)
    yours = _paid_order_at(theirs, "agent-yours", 620)

    at_mine = {o["id"] for o in mcp_orders.pending_orders(merchant_id=mine)}
    at_theirs = {o["id"] for o in mcp_orders.pending_orders(merchant_id=theirs)}

    assert ours in at_mine and ours not in at_theirs
    assert yours in at_theirs and yours not in at_mine


def test_the_unscoped_queue_is_still_the_whole_platform(two_kitchens):
    """The scheduler's expiry sweep passes no kitchen, deliberately: a
    customer who paid and was never answered is owed a refund whichever
    kitchen went quiet."""
    mine, theirs = two_kitchens
    ours = _paid_order_at(mine, "agent-mine", 440)
    yours = _paid_order_at(theirs, "agent-yours", 620)
    everything = {o["id"] for o in mcp_orders.pending_orders()}
    assert {ours, yours} <= everything


def test_the_trust_badge_on_her_board_is_her_own_history(two_kitchens):
    """An agent with a long clean record at one kitchen arrives at the
    next one as NEW, and the badge on the second kitchen's board has to
    say so."""
    mine, theirs = two_kitchens
    agent = "agent-well-known"
    for _ in range(12):
        audit_log.record_event(
            agent_id=agent, protocol="ap2", cart=[{"item": "veg_thali", "qty": 1}],
            decision="APPROVE", reason="within limits", total_inr=150,
            payment_id="pay_seed", merchant_id=mine,
        )
    _paid_order_at(mine, agent, 440)
    _paid_order_at(theirs, agent, 620)

    tier_at = lambda k: adapter_mcp.list_pending(merchant_id=k)["sessions"][0][
        "decision_detail"]["trust_tier"]
    assert tier_at(mine) != "NEW"
    assert tier_at(theirs) == "NEW"
