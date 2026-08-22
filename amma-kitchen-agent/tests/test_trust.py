import audit_log
import trust
from mandate import MANDATE


def _record_completed_order(agent_id: str, db_path: str) -> None:
    event_id = audit_log.record_event(
        agent_id=agent_id,
        protocol="acp",
        cart=[{"item": "veg_thali", "qty": 1}],
        decision="APPROVE",
        reason="within budget",
        total_inr=150,
        db_path=db_path,
    )
    audit_log.mark_paid(event_id, f"pay_{event_id}", db_path=db_path)


def test_unknown_agent_starts_at_new_tier(tmp_path):
    db_path = str(tmp_path / "audit.db")
    tier = trust.compute_trust_tier("brand-new-agent", db_path=db_path)
    assert tier == trust.TrustTier.NEW


def test_tier_upgrades_to_standard_after_one_completed_order(tmp_path):
    db_path = str(tmp_path / "audit.db")
    _record_completed_order("agent-a", db_path)
    assert trust.compute_trust_tier("agent-a", db_path=db_path) == trust.TrustTier.STANDARD


def test_tier_upgrades_to_trusted_after_five_completed_orders(tmp_path):
    db_path = str(tmp_path / "audit.db")
    for _ in range(trust.TRUSTED_MIN_COMPLETED):
        _record_completed_order("agent-a", db_path)
    assert trust.compute_trust_tier("agent-a", db_path=db_path) == trust.TrustTier.TRUSTED


def test_category_violation_forces_new_tier_regardless_of_history(tmp_path):
    db_path = str(tmp_path / "audit.db")
    for _ in range(trust.TRUSTED_MIN_COMPLETED):
        _record_completed_order("agent-a", db_path)
    audit_log.record_event(
        agent_id="agent-a",
        protocol="acp",
        cart=[{"item": "masala_dosa", "qty": 1}],
        decision="ESCALATE",
        reason="category not allowed: snacks (masala_dosa)",
        total_inr=80,
        db_path=db_path,
    )
    assert trust.compute_trust_tier("agent-a", db_path=db_path) == trust.TrustTier.NEW


def test_trust_never_widens_budget_cap_or_human_confirm_threshold(tmp_path):
    db_path = str(tmp_path / "audit.db")
    for _ in range(trust.TRUSTED_MIN_COMPLETED):
        _record_completed_order("agent-a", db_path)

    adjusted, tier = trust.trust_adjusted_mandate("agent-a", MANDATE, db_path=db_path)

    assert tier == trust.TrustTier.TRUSTED
    assert adjusted.flexible_margin_pct == trust.TIER_FLEXIBLE_MARGIN_PCT[trust.TrustTier.TRUSTED]
    assert adjusted.flexible_margin_pct > MANDATE.flexible_margin_pct
    # The two hard safety rails must be byte-for-byte unchanged.
    assert adjusted.budget_cap_inr == MANDATE.budget_cap_inr
    assert adjusted.human_confirm_threshold_inr == MANDATE.human_confirm_threshold_inr
    assert adjusted.allowed_categories == MANDATE.allowed_categories


def test_new_agent_gets_tighter_margin_than_default_mandate(tmp_path):
    db_path = str(tmp_path / "audit.db")
    adjusted, tier = trust.trust_adjusted_mandate("never-seen-before", MANDATE, db_path=db_path)
    assert tier == trust.TrustTier.NEW
    assert adjusted.flexible_margin_pct < MANDATE.flexible_margin_pct
