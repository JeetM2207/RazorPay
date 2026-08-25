"""AP2 counterpart to human_reject.py -- stands in for a human ops person
explicitly declining an escalated Intent Mandate, until the real
dashboard (build order step 7) exists.

Run:
    python human_reject_ap2.py <intent_mandate_id>
"""

import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv()

# Defaults to the unified server on 8000. Each adapter used to run its
# own process on its own port; app.py mounts all of them now, and a
# default pointing at a port nothing listens on is a demo that fails on
# a fresh clone with a connection error rather than anything readable.
AP2_BASE_URL = os.environ.get("AP2_BASE_URL", "http://127.0.0.1:8000")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python human_reject_ap2.py <intent_mandate_id>")
        sys.exit(1)
    intent_id = sys.argv[1]

    resp = requests.post(f"{AP2_BASE_URL}/ap2/intent-mandates/{intent_id}/human-reject")
    if resp.status_code != 200:
        print(f"Could not reject (HTTP {resp.status_code}): {resp.json()}")
        sys.exit(1)

    mandate = resp.json()["intent_mandate"]
    print(f"Intent Mandate {intent_id} rejected. status={mandate['status']}")
    print(f"  reason: {mandate['decision_detail']['reason']}")
    print("No payment call was made. This is recorded in the audit trail as a human decision.")


if __name__ == "__main__":
    main()
