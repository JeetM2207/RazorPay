"""Debug helper: list recent payment links and their statuses directly
from Razorpay, so we don't have to trust a possibly-stale polling terminal.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from razorpay_client import client

links = client.payment_link.all({"count": 10})
for link in links["payment_links"]:
    print(
        f"id={link['id']}  ref={link.get('reference_id')}  "
        f"status={link['status']}  amount={link['amount'] / 100} "
        f"{link['currency']}  short_url={link['short_url']}"
    )
