"""Stands in for a human ops person explicitly declining an escalated
order -- until the real dashboard (build order step 7) exists.

Distinct from just ignoring the order: this leaves a clear "a human said
no" entry in the audit trail, separate from "nobody has acted on it yet".

Run:
    python human_reject.py <session_id>
"""

import os
import sys
from pathlib import Path

import requests

import merchant_session
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv()

ACP_BASE_URL = os.environ.get("ACP_BASE_URL", "http://127.0.0.1:8000")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python human_reject.py <session_id>")
        sys.exit(1)
    session_id = sys.argv[1]

    merchant = merchant_session.login(ACP_BASE_URL)
    resp = merchant.post(f"{ACP_BASE_URL}/acp/checkout_sessions/{session_id}/human_reject")
    if resp.status_code != 200:
        print(f"Could not reject (HTTP {resp.status_code}): {resp.json()}")
        sys.exit(1)

    body = resp.json()
    print(f"Session {session_id} rejected. status={body['status']}")
    print(f"  reason: {body['decision_detail']['reason']}")
    print("No payment call was made. This is recorded in the audit trail as a human decision.")


if __name__ == "__main__":
    main()
