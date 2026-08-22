"""Pure, deterministic negotiation core. No LLM calls, no I/O.

Given a cart request and the merchant's mandate, returns exactly one of:
APPROVE, COUNTER_OFFER (with 1-2 in-budget alternatives), or ESCALATE.

This is the only place the APPROVE / COUNTER_OFFER / ESCALATE decision is
made. Adapters and buyer agents must never re-implement or bypass this.
"""

from dataclasses import dataclass
from enum import Enum

from mandate import MANDATE, MENU, Mandate, MenuItem


class Decision(str, Enum):
    APPROVE = "APPROVE"
    COUNTER_OFFER = "COUNTER_OFFER"
    ESCALATE = "ESCALATE"


@dataclass(frozen=True)
class CartLine:
    item: str
    qty: int


@dataclass(frozen=True)
class NegotiationResult:
    decision: Decision
    reason: str
    total_inr: int
    alternatives: tuple[tuple[CartLine, ...], ...] = ()


def _cart_total(cart: tuple[CartLine, ...], menu: dict[str, MenuItem]) -> int:
    return sum(menu[line.item].price_inr * line.qty for line in cart)


def _cap_to_stock(
    cart: tuple[CartLine, ...], menu: dict[str, MenuItem]
) -> tuple[CartLine, ...]:
    capped = []
    for line in cart:
        qty = min(line.qty, menu[line.item].stock)
        if qty > 0:
            capped.append(CartLine(line.item, qty))
    return tuple(capped)


def _reduce_to_budget(
    cart: tuple[CartLine, ...], menu: dict[str, MenuItem], budget: int
) -> tuple[CartLine, ...]:
    """Drop whole line items, most expensive first, until under budget."""
    remaining = sorted(
        cart, key=lambda line: menu[line.item].price_inr * line.qty, reverse=True
    )
    while remaining and _cart_total(tuple(remaining), menu) > budget:
        remaining.pop(0)
    return tuple(remaining)


def _scale_to_budget(
    cart: tuple[CartLine, ...], menu: dict[str, MenuItem], budget: int
) -> tuple[CartLine, ...]:
    """Trim quantities one unit at a time (costliest line first) until under budget."""
    scaled = list(cart)
    while scaled and _cart_total(tuple(scaled), menu) > budget:
        idx = max(
            range(len(scaled)),
            key=lambda i: menu[scaled[i].item].price_inr * scaled[i].qty,
        )
        line = scaled[idx]
        if line.qty > 1:
            scaled[idx] = CartLine(line.item, line.qty - 1)
        else:
            scaled.pop(idx)
    return tuple(scaled)


def evaluate(
    cart: list[CartLine] | list[tuple[str, int]],
    mandate: Mandate = MANDATE,
    menu: dict[str, MenuItem] = MENU,
) -> NegotiationResult:
    lines = tuple(
        line if isinstance(line, CartLine) else CartLine(*line) for line in cart
    )

    for line in lines:
        if line.item not in menu:
            return NegotiationResult(Decision.ESCALATE, f"unknown item: {line.item}", 0)

    for line in lines:
        category = menu[line.item].category
        if category not in mandate.allowed_categories:
            return NegotiationResult(
                Decision.ESCALATE,
                f"category not allowed: {category} ({line.item})",
                _cart_total(lines, menu),
            )

    stock_capped = _cap_to_stock(lines, menu)
    if stock_capped != lines:
        return NegotiationResult(
            Decision.COUNTER_OFFER,
            "requested quantity exceeds available stock",
            _cart_total(lines, menu),
            alternatives=(stock_capped,) if stock_capped else (),
        )

    total = _cart_total(lines, menu)
    cap = mandate.budget_cap_inr
    margin_ceiling = round(cap * (1 + mandate.flexible_margin_pct))

    if total > margin_ceiling:
        return NegotiationResult(
            Decision.ESCALATE,
            f"total Rs.{total} exceeds budget cap Rs.{cap} by more than the "
            f"{mandate.flexible_margin_pct:.0%} margin",
            total,
        )

    if total > cap:
        alternatives = []
        for alt in (_reduce_to_budget(lines, menu, cap), _scale_to_budget(lines, menu, cap)):
            if alt and alt not in alternatives:
                alternatives.append(alt)
        return NegotiationResult(
            Decision.COUNTER_OFFER,
            f"total Rs.{total} exceeds budget cap Rs.{cap}, within flexible margin",
            total,
            alternatives=tuple(alternatives),
        )

    if total >= mandate.human_confirm_threshold_inr:
        return NegotiationResult(
            Decision.ESCALATE,
            f"total Rs.{total} at/above human confirmation threshold "
            f"Rs.{mandate.human_confirm_threshold_inr}",
            total,
        )

    return NegotiationResult(Decision.APPROVE, "within budget and below human confirm threshold", total)
