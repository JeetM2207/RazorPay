"""Agent-readable product catalog -- the 'sellable to AI buyers' growth lever.

Modeled loosely on the product-feed half of the real Agentic Commerce
Protocol (ACP, OpenAI + Stripe): a structured, machine-fetchable feed any
AI buyer agent can discover and parse without a custom integration.

Also publishes the merchant's own order limits, so a well-behaved buyer
agent can self-limit its request before ever hitting the negotiation core
-- fewer wasted round-trips, fewer avoidable ESCALATEs.
"""

from fastapi import APIRouter, FastAPI

import merchant_config

router = APIRouter()


@router.get("/catalog")
def get_catalog() -> dict:
    return {
        "merchant": {"name": merchant_config.profile()["shop_name"], "currency": "INR"},
        "items": [
            {
                "id": item.name,
                "title": item.name.replace("_", " ").title(),
                "category": item.category,
                "price": item.price_inr,
                "currency": "INR",
                "availability": "in_stock" if item.stock > 0 else "out_of_stock",
                "stock": item.stock,
                # Published honestly: some items the merchant genuinely
                # sells are still not orderable by an autonomous agent.
                # Saying so here saves the agent a wasted round-trip.
                "agent_orderable": item.category in _m().allowed_categories,
            }
            for item in merchant_config.current_menu().values()
        ],
        "order_limits": {
            "max_order_inr": _m().budget_cap_inr,
            "human_confirm_at_inr": _m().human_confirm_threshold_inr,
            "allowed_categories": list(_m().allowed_categories),
        },
    }



def _m():
    return merchant_config.current_mandate()


app = FastAPI(title="Amma's Kitchen Agent Catalog")
app.include_router(router)
