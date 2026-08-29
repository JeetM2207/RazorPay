"""One-command end-to-end demo (build order steps 8 + 9).

Walks the whole story in a single take, so the pitch video doesn't need
six terminals juggled live:

  Scene 1  ACP buyer agent orders, gets an upsell, pays
  Scene 2  AP2 buyer agent -- a structurally different protocol -- orders
           through the SAME negotiation core, unchanged
  Scene 3  THE DELIBERATE FAILURE: a request that breaks the merchant's
           mandate is refused, and we verify from the audit trail that no
           Razorpay call was ever made for it
  Scene 4  A human explicitly rejects it, closing it out on the record
  Scene 5  Razorpay delivers the same webhook twice; the second is ignored

Starts its own adapter/webhook servers on dedicated ports and tears them
down afterwards, so it won't collide with anything you're already running.

Run:
    python demo.py
"""

import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

import merchant_session
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
load_dotenv()

import audit_log

ACP_PORT, AP2_PORT, WEBHOOK_PORT = 8010, 8011, 8012
ACP = f"http://127.0.0.1:{ACP_PORT}"
AP2 = f"http://127.0.0.1:{AP2_PORT}"
WEBHOOK = f"http://127.0.0.1:{WEBHOOK_PORT}/webhooks/razorpay"

RUN_ID = time.strftime("%H%M%S")
AGENT_A = f"demo-acp-{RUN_ID}"
AGENT_B = f"demo-ap2-{RUN_ID}"
AGENT_BAD = f"demo-violator-{RUN_ID}"


# ---------------------------------------------------------------- output

def scene(number: int, title: str) -> None:
    print(f"\n\n{'=' * 72}\n  SCENE {number}   {title}\n{'=' * 72}")


def say(text: str = "") -> None:
    print(text)


def step(text: str) -> None:
    print(f"  -> {text}")


def pause() -> None:
    """Let the presenter control pacing; skipped with --no-pause."""
    if "--no-pause" not in sys.argv:
        input("\n     [enter to continue] ")


# ---------------------------------------------------------------- servers

def start_servers() -> list[subprocess.Popen]:
    say("Starting adapters and webhook handler...")
    procs = []
    for module, port in (
        ("adapter_acp:app", ACP_PORT),
        ("adapter_ap2:app", AP2_PORT),
        ("webhook_handler:app", WEBHOOK_PORT),
    ):
        procs.append(
            subprocess.Popen(
                [sys.executable, "-m", "uvicorn", module, "--port", str(port), "--log-level", "warning"],
                cwd=str(HERE),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )

    for url in (f"{ACP}/docs", f"{AP2}/docs", f"http://127.0.0.1:{WEBHOOK_PORT}/docs"):
        for _ in range(40):
            try:
                requests.get(url, timeout=1)
                break
            except requests.RequestException:
                time.sleep(0.25)
        else:
            raise RuntimeError(f"server did not come up: {url}")

    say("All three servers up.\n")
    return procs


def stop_servers(procs: list[subprocess.Popen]) -> None:
    for proc in procs:
        proc.terminate()
    for proc in procs:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ---------------------------------------------------------------- scenes

def scene_1_acp() -> None:
    scene(1, "ACP buyer agent -- flat checkout session, delegate token")
    say("A buyer agent speaking OpenAI/Stripe's Agentic Commerce Protocol shape.")

    resp = requests.post(
        f"{ACP}/acp/checkout_sessions",
        json={"agent_id": AGENT_A, "items": [{"item_id": "masala_dosa", "qty": 1}]},
    ).json()
    detail = resp["decision_detail"]
    step(f"asked for 1x masala dosa (Rs.{detail['total_inr']})")
    step(f"trust tier: {detail['trust_tier']}  ->  decision: {detail['decision']}")
    step(f"reason: {detail['reason']}")

    upsell = detail.get("upsell_suggestion")
    if upsell:
        step(f"merchant suggests adding {upsell['item']} (Rs.{upsell['price_inr']})")
        resp = requests.post(
            f"{ACP}/acp/checkout_sessions/{resp['session_id']}/accept_upsell"
        ).json()
        step(f"agent accepts -> new total Rs.{resp['decision_detail']['total_inr']}")
        step("note: the larger cart was re-run through the negotiation core, not waved through")

    complete = requests.post(
        f"{ACP}/acp/checkout_sessions/{resp['session_id']}/complete",
        json={"delegate_token": resp["delegate_token"]},
    ).json()
    step(f"real Razorpay link: {complete['payment_link_url']}  (Rs.{complete['amount_inr']})")
    return complete


def scene_2_ap2() -> None:
    scene(2, "AP2 buyer agent -- mandate chain, SAME negotiation core")
    say("A completely different protocol shape: Google's AP2 Intent -> Cart -> Payment")
    say("mandate chain. negotiation.py and orchestrator.py are byte-for-byte unchanged.")

    resp = requests.post(
        f"{AP2}/ap2/intent-mandates",
        json={
            "agent_id": AGENT_B,
            "intent": {"items": [{"item_id": "veg_thali", "qty": 1}], "auto_confirm_limit_inr": 250},
        },
    ).json()["intent_mandate"]
    detail = resp["decision_detail"]
    step(f"Intent Mandate created: 1x veg thali (Rs.{detail['total_inr']})")
    step(f"trust tier: {detail['trust_tier']}  ->  decision: {detail['decision']}")

    cart = requests.post(f"{AP2}/ap2/intent-mandates/{resp['id']}/cart-mandate").json()["cart_mandate"]
    step(f"Cart Mandate locked: {cart['id'][:12]}...  Rs.{cart['total_inr']}")

    payment = requests.post(
        f"{AP2}/ap2/cart-mandates/{cart['id']}/payment-mandate"
    ).json()["payment_mandate"]
    step(f"Payment Mandate: hash {payment['matched_mandate_hash'][:16]}...")
    step(f"real Razorpay link: {payment['payment_link_url']}  (Rs.{payment['amount_inr']})")


def scene_3_failure() -> str:
    scene(3, "THE DELIBERATE FAILURE -- a request the mandate forbids")
    say("Amma's Kitchen really does sell bulk catering. She just never lets an")
    say("autonomous agent book it -- those need a human conversation first.")
    say("So this request is affordable, in stock, and still must be refused.")

    resp = requests.post(
        f"{ACP}/acp/checkout_sessions",
        json={"agent_id": AGENT_BAD, "items": [{"item_id": "party_catering_tray", "qty": 1}]},
    ).json()
    detail = resp["decision_detail"]

    step(f"asked for 1x party catering tray (Rs.{detail['total_inr']})")
    step(f"that is UNDER the Rs.500 budget cap and UNDER the Rs.400 confirm threshold")
    step(f"decision: {detail['decision']}")
    step(f"reason: {detail['reason']}")

    say("\n  Verifying against the audit trail that no money call was made...")
    event = [
        e for e in audit_log.get_events_for_agent(AGENT_BAD) if e["id"] == detail["event_id"]
    ][0]
    assert event["payment_link_id"] is None, "a payment link was created for a refused order!"
    assert event["payment_id"] is None, "a payment was recorded for a refused order!"
    step(f"audit event #{event['id']}: payment_link_id = None, payment_id = None")
    step("VERIFIED: Razorpay was never called for this order.")

    say("\n  Trying to force it through anyway, as a merchant human would...")
    merchant = merchant_session.login(ACP)
    forced = merchant.post(f"{ACP}/acp/checkout_sessions/{resp['session_id']}/human_confirm")
    step(f"human_confirm -> HTTP {forced.status_code}")
    step(f"refused: {forced.json()['detail'][:96]}...")
    step("a category rule is a hard merchant rule; not even a human can wave it through here.")

    return resp["session_id"]


def scene_4_reject(session_id: str) -> None:
    scene(4, "A human closes it out on the record")
    say("Doing nothing would leave this indistinguishable from 'nobody looked at it'.")

    merchant = merchant_session.login(ACP)
    rejected = merchant.post(
        f"{ACP}/acp/checkout_sessions/{session_id}/human_reject"
    ).json()
    step(f"status: {rejected['status']}")
    step(f"reason: {rejected['decision_detail']['reason']}")
    step("recorded as its own terminal audit entry, distinct from the machine's ESCALATE.")


def scene_5_webhook() -> None:
    scene(5, "Razorpay delivers the same webhook twice")
    say("Razorpay guarantees at-least-once delivery, so duplicates are normal,")
    say("not an error case. Double-fulfilling on one would be the bug.")
    say("")
    say("Note: this replays a delivery against a SYNTHETIC link id, not the real")
    say("unpaid order from scene 1. We won't write a payment that never happened")
    say("into our own audit trail just to make a demo look tidier -- which is")
    say("why the first delivery reports 'unmatched' rather than 'paid'.")

    secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")
    if not secret or "xxxx" in secret:
        step("RAZORPAY_WEBHOOK_SECRET not set in .env -- skipping this scene.")
        return

    body = json.dumps(
        {
            "entity": "event",
            "event": "payment_link.paid",
            "payload": {
                "payment_link": {"entity": {"id": f"plink_synthetic_{RUN_ID}", "status": "paid"}},
                "payment": {"entity": {"id": f"pay_synthetic_{RUN_ID}", "status": "captured"}},
            },
        }
    ).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Razorpay-Signature": hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(),
    }

    first = requests.post(WEBHOOK, data=body, headers=headers).json()
    step(f"delivery #1 -> {first['status']}  (accepted, signature verified)")
    second = requests.post(WEBHOOK, data=body, headers=headers).json()
    step(f"delivery #2 -> {second['status']}")

    if second["status"] == "duplicate_ignored":
        step("VERIFIED: claimed exactly once, enforced by a DB constraint, not a lookup.")

    say("\n  And a delivery with a bad signature, for completeness:")
    bad = requests.post(WEBHOOK, data=body, headers={**headers, "X-Razorpay-Signature": "forged"})
    step(f"forged signature -> HTTP {bad.status_code} (rejected)")


def closing() -> None:
    scene(6, "The audit trail")
    events = audit_log.get_all_events(limit=500)
    mine = [e for e in events if e["agent_id"].endswith(RUN_ID)]
    say(f"This run added {len(mine)} decisions to the permanent audit trail.")
    say("")
    for event in reversed(mine):
        if event["payment_id"]:
            outcome = "PAID"
        elif event["payment_link_id"]:
            outcome = "link issued, awaiting payment"
        elif event["decision"] in ("ESCALATE", "REJECTED", "COUNTER_OFFER"):
            outcome = "NO RAZORPAY CALL -- gated"
        else:
            # An APPROVE with no link: superseded by a larger cart (the
            # upsell) before it was ever taken to payment.
            outcome = "superseded by a later cart"
        say(f"  #{event['id']:<4} {event['protocol'].upper():<4} {event['decision']:<12} Rs.{event['total_inr']:<5} {outcome}")
    say("")
    say("  Full dashboard:  uvicorn dashboard:app --port 8003   ->   http://127.0.0.1:8003")


def main() -> None:
    say("\n  AMMA'S KITCHEN -- agentic commerce, bounded and auditable")
    say("  Razorpay AI Buildathon, Track 1\n")

    procs = start_servers()
    try:
        scene_1_acp()
        pause()
        scene_2_ap2()
        pause()
        session_id = scene_3_failure()
        pause()
        scene_4_reject(session_id)
        pause()
        scene_5_webhook()
        pause()
        closing()
    finally:
        stop_servers(procs)
        say("\n  (demo servers stopped)")


if __name__ == "__main__":
    main()
