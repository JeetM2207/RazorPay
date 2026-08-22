"""Step 2 of the build order: prove the Razorpay plumbing works end to end
with ONE hardcoded cart, before any negotiation intelligence exists.

Run:
    python scripts/pay_hardcoded_cart.py

It creates a real (test-mode) Razorpay Payment Link, prints the URL, then
polls until the link is paid (or you Ctrl+C).
"""

import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mandate import MENU
from razorpay_client import create_payment_link, fetch_payment_link

# Hardcoded cart: 1 veg thali + 1 filter coffee.
CART = ["veg_thali", "filter_coffee"]


def main() -> None:
    total_inr = sum(MENU[item].price_inr for item in CART)
    description = " + ".join(CART)

    link = create_payment_link(
        amount_inr=total_inr,
        description=f"Amma's Kitchen order: {description}",
        reference_id=f"hardcoded-cart-{uuid.uuid4().hex[:8]}",
    )

    print(f"Cart: {CART}  Total: Rs.{total_inr}")
    print(f"Pay at: {link['short_url']}")
    print("Use a Razorpay DOMESTIC test card (e.g. Visa 4100 2800 0000 1007, "
          "any future expiry, any CVV). On the OTP screen enter any 4-10 "
          "digit number to succeed.\n")
    print("Polling payment link status (Ctrl+C to stop)...")

    while True:
        current = fetch_payment_link(link["id"])
        status = current["status"]
        print(f"  status = {status}")
        if status == "paid":
            print("\nPayment captured. Plumbing confirmed working.")
            break
        if status in ("cancelled", "expired"):
            print(f"\nLink ended in status={status}, not paid.")
            break
        time.sleep(3)


if __name__ == "__main__":
    main()
