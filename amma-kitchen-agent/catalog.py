"""Agent-readable product catalog -- the 'sellable to AI buyers' growth lever.

Modeled loosely on the product-feed half of the real Agentic Commerce
Protocol (ACP, OpenAI + Stripe): a structured, machine-fetchable feed any
AI buyer agent can discover and parse without a custom integration.

Also publishes the merchant's own order limits, so a well-behaved buyer
agent can self-limit its request before ever hitting the negotiation core
-- fewer wasted round-trips, fewer avoidable ESCALATEs.
"""

from fastapi import FastAPI

from mandate import MANDATE, MENU

app = FastAPI(title="Amma's Kitchen Agent Catalog")


@app.get("/catalog")
def get_catalog() -> dict:
    return {
        "merchant": {"name": "Amma's Kitchen", "currency": "INR"},
        "items": [
            {
                "id": item.name,
                "title": item.name.replace("_", " ").title(),
                "category": item.category,
                "price": item.price_inr,
                "currency": "INR",
                "availability": "in_stock" if item.stock > 0 else "out_of_stock",
                "stock": item.stock,
            }
            for item in MENU.values()
        ],
        "order_limits": {
            "max_order_inr": MANDATE.budget_cap_inr,
            "human_confirm_at_inr": MANDATE.human_confirm_threshold_inr,
            "allowed_categories": list(MANDATE.allowed_categories),
        },
    }
