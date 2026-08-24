"""Cancel stale UNPAID test payment links, so test mode has room again.

Razorpay's test mode caps an account at 30 payment links, and a project
that creates one per demo run reaches that fast:

    test mode limit of 30 reached for payment_link

There is no way to raise it, so the fix is to cancel links nobody is
going to pay. This only ever touches links in `created` -- a paid link is
evidence of a real settlement, and the audit trail, the reconciler and
the dashboard all still refer to it, so those are left alone. Run it
before a demo, not during one.
"""

import sys

import razorpay_client


def main(keep: int = 4) -> None:
    client = razorpay_client.client
    links = client.payment_link.all({"count": 100})["payment_links"]
    unpaid = [link for link in links if link["status"] == "created"]
    paid = [link for link in links if link["status"] == "paid"]

    print(f"{len(links)} links: {len(paid)} paid (kept), {len(unpaid)} unpaid")
    # Newest first, so the most recent unpaid link -- possibly one someone
    # is about to open -- survives.
    unpaid.sort(key=lambda link: link["created_at"], reverse=True)

    for link in unpaid[keep:]:
        try:
            client.payment_link.cancel(link["id"])
            print(f"  cancelled {link['id']}  Rs.{link['amount'] // 100}")
        except Exception as exc:
            print(f"  could not cancel {link['id']}: {exc}")

    print(f"kept the {min(keep, len(unpaid))} newest unpaid links")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 4)
