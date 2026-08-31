"""Give the other kitchens on the platform a shop of their own.

Each one gets its own menu, its own limits and its own velocity gate,
because that is the whole claim being made: a merchant on a marketplace
sets her own rules and the platform enforces hers, not an average.

Deliberately different from each other in ways a demo can point at:

  * Bombay Tiffin Room is a cheap high-volume counter. Low prices, a low
    budget cap, and a confirmation threshold that a normal order will
    never reach -- almost everything goes straight through.
  * Lahori Grill House is the opposite. Expensive charcoal cooking, a
    high cap, and a threshold low enough relative to its prices that a
    single main course needs a human. Its `catering_platter` is in a
    category it does not sell to agents at all, which is how the
    category refusal is demonstrable on a kitchen other than Amma's.

Amma's Kitchen is NOT touched. It points at the original
merchant_config.json, which holds a real configured shop with real
history behind it.

    python seed_merchants.py            # write the two new shops
    python seed_merchants.py --force    # overwrite them if they exist
"""

from __future__ import annotations

import argparse
import json
import sys

import merchants

SHOPS = {
    "bombay-tiffin": {
        "profile": {
            "shop_name": "Bombay Tiffin Room",
            "address": "Ghatkopar East, Mumbai",
            "phone": "",
            "configured": True,
        },
        "mandate": {
            "budget_cap_inr": 400,
            "human_confirm_threshold_inr": 300,
            "allowed_categories": ["tiffin", "snacks", "beverages", "sweets"],
            "flexible_margin_pct": 0.05,
        },
        "velocity": {"max_orders_per_hour": 8, "max_spend_per_day_inr": 2500},
        "menu": {
            "vada_pav":      {"name": "vada_pav",      "category": "snacks",    "price_inr": 25,  "stock": 60, "list_price_inr": 25,  "sale": False},
            "misal_pav":     {"name": "misal_pav",     "category": "tiffin",    "price_inr": 90,  "stock": 30, "list_price_inr": 90,  "sale": False},
            "poha":          {"name": "poha",          "category": "tiffin",    "price_inr": 60,  "stock": 40, "list_price_inr": 60,  "sale": False},
            "sabudana_khich":{"name": "sabudana_khich","category": "tiffin",    "price_inr": 80,  "stock": 25, "list_price_inr": 80,  "sale": False},
            "pav_bhaji":     {"name": "pav_bhaji",     "category": "tiffin",    "price_inr": 110, "stock": 22, "list_price_inr": 110, "sale": False},
            "cutting_chai":  {"name": "cutting_chai",  "category": "beverages", "price_inr": 15,  "stock": 90, "list_price_inr": 15,  "sale": False},
            "solkadhi":      {"name": "solkadhi",      "category": "beverages", "price_inr": 40,  "stock": 35, "list_price_inr": 40,  "sale": False},
            "shrikhand":     {"name": "shrikhand",     "category": "sweets",    "price_inr": 70,  "stock": 28, "list_price_inr": 70,  "sale": False},
        },
    },
    "lahori-grill": {
        "profile": {
            "shop_name": "Lahori Grill House",
            "address": "Navrangpura, Ahmedabad",
            "phone": "",
            "configured": True,
        },
        "mandate": {
            "budget_cap_inr": 900,
            "human_confirm_threshold_inr": 500,
            # `catering` is absent on purpose -- see the platter below.
            "allowed_categories": ["grill", "mains", "breads", "beverages"],
            "flexible_margin_pct": 0.05,
        },
        "velocity": {"max_orders_per_hour": 5, "max_spend_per_day_inr": 4000},
        "menu": {
            "seekh_kebab":     {"name": "seekh_kebab",     "category": "grill",     "price_inr": 280, "stock": 18, "list_price_inr": 280, "sale": False},
            "malai_tikka":     {"name": "malai_tikka",     "category": "grill",     "price_inr": 320, "stock": 14, "list_price_inr": 320, "sale": False},
            "mutton_biryani":  {"name": "mutton_biryani",  "category": "mains",     "price_inr": 340, "stock": 12, "list_price_inr": 340, "sale": False},
            "dal_makhani":     {"name": "dal_makhani",     "category": "mains",     "price_inr": 190, "stock": 20, "list_price_inr": 190, "sale": False},
            "butter_naan":     {"name": "butter_naan",     "category": "breads",    "price_inr": 45,  "stock": 70, "list_price_inr": 45,  "sale": False},
            "roomali_roti":    {"name": "roomali_roti",    "category": "breads",    "price_inr": 35,  "stock": 55, "list_price_inr": 35,  "sale": False},
            "lassi":           {"name": "lassi",           "category": "beverages", "price_inr": 60,  "stock": 40, "list_price_inr": 60,  "sale": False},
            # The refusal demo, on a kitchen that is not Amma's. In stock,
            # under the cap, and in a category no agent may order -- so the
            # ONLY thing refusing it is the category, which makes the point
            # unambiguous.
            "catering_platter":{"name": "catering_platter","category": "catering",  "price_inr": 850, "stock": 4,  "list_price_inr": 850, "sale": False},
        },
    },
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true",
                    help="overwrite a shop that already exists")
    args = ap.parse_args()

    for merchant_id, shop in SHOPS.items():
        path = merchants.config_path(merchant_id)
        if path.exists() and not args.force:
            print(f"  {merchant_id:<16} already set up (--force to replace)")
            continue
        path.write_text(json.dumps(shop, indent=2), encoding="utf-8")
        print(f"  {merchant_id:<16} {shop['profile']['shop_name']}: "
              f"{len(shop['menu'])} dishes, cap Rs.{shop['mandate']['budget_cap_inr']}, "
              f"asks from Rs.{shop['mandate']['human_confirm_threshold_inr']}")

    print(f"\n  {merchants.Platform.name} now has {len(merchants.all())} kitchens.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
