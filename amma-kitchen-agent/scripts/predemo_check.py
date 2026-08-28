"""Everything that has silently broken at least once, checked in one command.

Run this before a demo, not during one:

    python scripts/predemo_check.py

Each line is PASS, WARN or FAIL. WARN means it works but will bite you
later; FAIL means the demo does not work right now. Nothing here changes
any state -- it sends no WhatsApp, creates no payment link and writes no
audit row. The one message it does send is a webhook `ping`, an event the
handler ignores before it claims anything.

Written because every bug in this project's history was invisible until a
real request went through a real service: a stale webhook secret held by a
running process, a tunnel whose domain was not in MCP_ALLOWED_HOSTS, tool
descriptions that contradicted the code, a lock left behind by a failed
checkout. None of those show up in the unit suite.
"""

import asyncio
import hashlib
import hmac
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests
from dotenv import dotenv_values

ENV = dotenv_values(str(Path(__file__).resolve().parent.parent / ".env"))
LOCAL = "http://127.0.0.1:8000"

_RESULTS = []


def report(status: str, label: str, detail: str = "") -> None:
    mark = {"PASS": "  ok  ", "WARN": " warn ", "FAIL": " FAIL "}[status]
    print(f"[{mark}] {label}" + (f"\n           {detail}" if detail else ""))
    _RESULTS.append(status)


# --------------------------------------------------------------- the server

def check_server() -> bool:
    try:
        code = requests.get(f"{LOCAL}/api/pending", timeout=8).status_code
    except Exception as exc:
        report("FAIL", "server on :8000", f"not reachable ({type(exc).__name__}) -- start uvicorn")
        return False
    report("PASS" if code == 200 else "FAIL", "server on :8000", "" if code == 200 else f"HTTP {code}")
    return code == 200


def check_routes() -> None:
    """Is the RUNNING server running the code that is on disk?

    Python routes are registered at import, so a server started before an
    endpoint was written serves FastAPI's own bare 404 for it -- which
    looks exactly like a bug in the page that called it. HTML and CSS are
    read per request and update without a restart, so a half-updated
    server is entirely possible and entirely confusing: the new screen
    appears and the endpoint behind it does not.

    This is the third time a stale process has cost an hour (a webhook
    secret held from before .env was edited, then a whole feature's
    routes), so it is checked rather than remembered.
    """
    try:
        live = set(requests.get(f"{LOCAL}/openapi.json", timeout=10).json()["paths"])
    except Exception as exc:
        report("WARN", "server code freshness", f"could not read the route table: {str(exc)[:60]}")
        return

    try:
        import app

        on_disk = {r.path for r in app.app.routes if getattr(r, "path", None)}
    except Exception as exc:
        report("WARN", "server code freshness", f"could not import app.py: {str(exc)[:60]}")
        return

    missing = sorted(p for p in on_disk if p.startswith(("/api/", "/evidence")) and p not in live)
    if missing:
        report("FAIL", "server code freshness",
               f"{len(missing)} endpoint(s) exist in the code but not in the running server "
               f"-- restart uvicorn. First: {missing[0]}")
    else:
        report("PASS", "server code freshness", "the running server has every endpoint in the code")


def check_tunnel() -> str | None:
    try:
        tunnels = requests.get("http://127.0.0.1:4040/api/tunnels", timeout=5).json()["tunnels"]
    except Exception:
        report("FAIL", "ngrok", "not running -- Razorpay webhooks and the MCP connector both need it")
        return None
    if not tunnels:
        report("FAIL", "ngrok", "agent running but no tunnel open")
        return None

    url = tunnels[0]["public_url"]
    host = url.split("://", 1)[-1]
    allowed = [h.strip() for h in (ENV.get("MCP_ALLOWED_HOSTS") or "").split(",") if h.strip()]
    if host in allowed:
        report("PASS", "ngrok", url)
    else:
        # The MCP SDK rejects any host it was not told about, with a 421
        # that looks nothing like a configuration problem.
        report("FAIL", "ngrok",
               f"{url} is NOT in MCP_ALLOWED_HOSTS -- Claude will get 421 Invalid Host header")
    return url


# ------------------------------------------------------------ the webhook

def check_webhook_secret(public_url: str | None) -> None:
    """Signs a `ping`, an event type the handler ignores before claiming
    anything -- so this proves the secret without writing a row.

    The secret is read at import, so a server started before .env was
    edited holds the old one. That failed silently once: every Razorpay
    delivery rejected, and nothing on any screen said so.
    """
    secret = ENV.get("RAZORPAY_WEBHOOK_SECRET") or ""
    if not secret:
        report("FAIL", "webhook secret", "RAZORPAY_WEBHOOK_SECRET is empty in .env")
        return

    body = b'{"event":"ping"}'
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {"Content-Type": "application/json", "X-Razorpay-Signature": sig}

    for label, base in (("local", LOCAL), ("public", public_url)):
        if not base:
            continue
        try:
            resp = requests.post(f"{base}/webhooks/razorpay", data=body, headers=headers, timeout=25)
        except Exception as exc:
            report("FAIL", f"webhook signature ({label})", str(exc)[:90])
            continue
        if resp.status_code == 200:
            report("PASS", f"webhook signature ({label})")
        elif resp.status_code == 400:
            report("FAIL", f"webhook signature ({label})",
                   "rejected -- the RUNNING server holds a different secret than .env; restart it")
        else:
            report("FAIL", f"webhook signature ({label})", f"HTTP {resp.status_code} {resp.text[:80]}")


# ------------------------------------------------------------------ Razorpay

def check_razorpay(public_url: str | None) -> None:
    import razorpay_client

    try:
        links = razorpay_client.client.payment_link.all({"count": 100})["payment_links"]
    except Exception as exc:
        report("FAIL", "Razorpay keys", str(exc)[:110])
        return

    unpaid = sum(1 for link in links if link["status"] == "created")
    # Test mode caps an account at 30 links, and this project makes one per
    # run. Past that, checkout fails in a way that looks like an adapter bug.
    headroom = 30 - len(links)
    if headroom >= 5:
        report("PASS", "Razorpay payment links", f"{len(links)}/30 used, {headroom} free")
    elif headroom > 0:
        report("WARN", "Razorpay payment links",
               f"{len(links)}/30 used -- {headroom} left; free some with "
               f"scripts/free_payment_links.py ({unpaid} unpaid)")
    else:
        report("FAIL", "Razorpay payment links",
               f"30/30 used -- checkout WILL fail; run scripts/free_payment_links.py "
               f"({unpaid} cancellable)")

    try:
        hooks = requests.get(
            "https://api.razorpay.com/v1/webhooks",
            auth=(ENV["RAZORPAY_KEY_ID"], ENV["RAZORPAY_KEY_SECRET"]),
            timeout=20,
        ).json().get("items", [])
    except Exception as exc:
        report("WARN", "Razorpay webhook", f"could not list: {str(exc)[:80]}")
        return

    live = [h for h in hooks if h.get("active")]
    if not live:
        report("FAIL", "Razorpay webhook",
               "none registered -- a paid order will not confirm itself; register one, "
               "or run reconcile_payments.py by hand after paying")
        return
    urls = [h["url"] for h in live]
    if public_url and not any(url.startswith(public_url) for url in urls):
        report("FAIL", "Razorpay webhook",
               f"registered for {urls[0]} but the tunnel is {public_url} -- deliveries go nowhere")
    else:
        report("PASS", "Razorpay webhook", urls[0])

    # Issuing a refund returns immediately; whether the money reaches the
    # customer is settled afterwards. Without these two events a refund
    # Razorpay went on to fail reads as REFUNDED forever.
    subscribed = {name for hook in live for name, on in (hook.get("events") or {}).items() if on}
    for event in ("payment_link.paid", "refund.processed", "refund.failed"):
        if event not in subscribed:
            report("WARN", f"webhook event {event}",
                   "not subscribed -- tick it in Razorpay > Settings > Webhooks")
    if {"payment_link.paid", "refund.processed", "refund.failed"} <= subscribed:
        report("PASS", "webhook events", "capture and both refund outcomes subscribed")


# ------------------------------------------------------------------ messaging

def check_messaging() -> None:
    sms_enabled = (ENV.get("SMS_ENABLED") or "true").strip().lower() not in ("false", "0", "no")
    twilio_ready = all(ENV.get(k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM"))

    if not sms_enabled:
        report("WARN", "messaging",
               "SMS_ENABLED=false -- the MOCK transport is on. Right for testing; "
               "set it to true for the demo.")
        return
    if not twilio_ready:
        report("WARN", "messaging", "Twilio not configured -- falling back to the mock outbox")
        return

    try:
        from twilio.rest import Client

        sid = ENV["TWILIO_ACCOUNT_SID"]
        account = Client(sid, ENV["TWILIO_AUTH_TOKEN"]).api.accounts(sid).fetch()
    except Exception as exc:
        report("FAIL", "Twilio", str(exc)[:110])
        return

    if account.type == "Trial":
        # There is no remaining-messages counter in Twilio's console or its
        # API, so this cannot be checked -- only stated.
        report("WARN", "Twilio",
               "TRIAL account: 50 messages/day, and free-form WhatsApp only sends within "
               "24h of you last messaging the sandbox. Send the join code before demoing.")
    else:
        report("PASS", "Twilio", f"{account.type} account")


# ------------------------------------------------------------------- MCP

def check_mcp(public_url: str | None) -> None:
    if not public_url:
        return
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async def probe():
        async with streamable_http_client(f"{public_url}/mcp/") as (reader, writer):
            async with ClientSession(reader, writer) as session:
                init = await session.initialize()
                tools = {t.name: t for t in (await session.list_tools()).tools}
                return init.instructions or "", tools

    try:
        instructions, tools = asyncio.run(probe())
    except Exception as exc:
        report("FAIL", "MCP over the public URL", str(exc)[:110])
        return

    missing = [name for name in ("get_catalog", "propose_cart", "checkout") if name not in tools]
    if missing:
        report("FAIL", "MCP tools", f"missing: {missing}")
        return
    report("PASS", "MCP over the public URL", f"{len(tools)} tools")

    # The descriptions ARE load-bearing code here: the caller is somebody
    # else's model, and wording that contradicted the flow once cost a sale
    # with every layer below it working correctly.
    problems = []
    if "Only call checkout once propose_cart has returned APPROVE" in instructions:
        problems.append("server instructions still describe the pre-payment flow")
    if "payable" not in instructions:
        problems.append("server instructions never mention `payable`")
    if "has already APPROVED" in tools["checkout"].description:
        problems.append("checkout still says it only takes APPROVED carts")
    for phrase in ("largest order", "confirm by hand"):
        if phrase in tools["get_catalog"].description:
            problems.append("get_catalog advertises the merchant's limits")
            break
    report("FAIL" if problems else "PASS", "MCP tool wording", "; ".join(problems))


# ----------------------------------------------------------- leftover state

def check_state() -> None:
    import adapter_mcp
    import audit_log
    import unstick_checkouts

    # Same detector the fixer uses, so the check and the fix can never
    # disagree about what counts as stuck.
    stuck = unstick_checkouts.find_stuck(audit_log.DEFAULT_DB_PATH)
    if stuck:
        report("FAIL", "stuck checkout locks",
               f"{len(stuck)} cart(s) unbuyable -- run scripts/unstick_checkouts.py --release")
    else:
        report("PASS", "stuck checkout locks", "none")

    pending = adapter_mcp.list_pending()["sessions"]
    if pending:
        refs = ", ".join(
            f"#{s['session_id']} Rs.{s['decision_detail']['total_inr']}" for s in pending
        )
        report("WARN", "paid orders awaiting her answer", f"{refs} -- decide these before demoing")
    else:
        report("PASS", "paid orders awaiting her answer", "none")


def main() -> int:
    print("Amma's Kitchen -- pre-demo check\n")
    if not check_server():
        print("\nStart the server first:  uvicorn app:app --port 8000")
        return 1

    check_routes()
    public_url = check_tunnel()
    check_webhook_secret(public_url)
    check_razorpay(public_url)
    check_messaging()
    check_mcp(public_url)
    check_state()

    fails = _RESULTS.count("FAIL")
    warns = _RESULTS.count("WARN")
    print(f"\n{len(_RESULTS)} checks: {_RESULTS.count('PASS')} ok, {warns} warn, {fails} FAIL")
    if fails:
        print("Fix the FAILs before demoing -- each one is something that has broken before.")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
