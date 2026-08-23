"""The whole system on one server.

Everything -- both protocol adapters, the Razorpay webhook receiver, the
agent-readable catalog, the audit dashboard, and the two human-facing web
consoles -- runs from a single process, so a demo needs one command
rather than five terminals.

    uvicorn app:app --port 8000 --reload

Then:
    /            landing page, links to everything
    /buyer       the buyer's console -- a human plays the customer
    /merchant    the merchant's console -- a human approves or refuses
    /audit       the full audit trail
    /catalog     the agent-readable product feed (JSON)
    /docs        API reference for both protocols
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

import adapter_acp
import adapter_ap2
import adapter_x402
import audit_log
import buyer_mandate
import buyer_sms
import catalog
import dashboard
import escalations
import notification_service
import trust
import webhook_handler
import merchant_config

WEB_DIR = Path(__file__).resolve().parent / "web"

app = FastAPI(title="Amma's Kitchen -- Agentic Commerce")

app.include_router(adapter_acp.router)
app.include_router(adapter_ap2.router)
app.include_router(adapter_x402.router)
app.include_router(webhook_handler.router)
app.include_router(escalations.router)
app.include_router(catalog.router)
app.include_router(dashboard.router)

app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


class CatalogItemIn(BaseModel):
    id: str
    title: str | None = None
    price_inr: int | None = None
    category: str | None = None
    agent_orderable: bool = True


class ParseCartRequest(BaseModel):
    text: str
    # What the buyer agent found when it read the merchant's catalog. It
    # sends this back so the parse is constrained to dishes that actually
    # exist -- the agent discovers the menu rather than the server
    # quietly assuming it. Omitted by the scripted buyer agents, which
    # fall back to the live menu.
    available_items: list[CatalogItemIn] | None = None


class BuyerCheckItem(BaseModel):
    item_id: str
    qty: int


class BuyerCheckRequest(BaseModel):
    items: list[BuyerCheckItem]
    spend_cap_inr: int
    confirm_above_inr: int


@app.get("/", response_class=HTMLResponse)
def landing() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/buyer", response_class=HTMLResponse)
def buyer_profile() -> FileResponse:
    """One-time account setup: who you are, the card, and the standing
    mandate. The card never reaches this server -- the page keeps only a
    last-4 and a token reference in the browser."""
    return FileResponse(WEB_DIR / "profile.html")


@app.get("/buyer/order", response_class=HTMLResponse)
def buyer_order() -> FileResponse:
    """Day-to-day: say what you want, watch the agent work."""
    return FileResponse(WEB_DIR / "order.html")


@app.get("/merchant", response_class=HTMLResponse)
def merchant_setup() -> FileResponse:
    """One-time shop setup: who you are, your limits, and your menu."""
    return FileResponse(WEB_DIR / "shop.html")


@app.get("/merchant/orders", response_class=HTMLResponse)
def merchant_console() -> FileResponse:
    """Day-to-day: the escalation queue, trust, and the decision log."""
    return FileResponse(WEB_DIR / "merchant.html")


@app.get("/api/merchant-config")
def get_merchant_config() -> dict:
    return merchant_config.as_dict()


class MerchantConfigRequest(BaseModel):
    profile: dict
    mandate: dict
    menu: list[dict]


@app.post("/api/merchant-config")
def save_merchant_config(req: MerchantConfigRequest) -> dict:
    """Save the shop. These values are what negotiation.py decides
    against from the next order onward -- the page is not decorative."""
    try:
        return merchant_config.save(req.profile, req.mandate, req.menu)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/menu")
def menu() -> dict:
    """What the buyer console renders. Includes items the merchant sells
    but agents may not order, flagged rather than hidden -- the buyer
    should be able to see the rule being applied, not just its result."""
    config = merchant_config.as_dict()
    return {
        "items": config["menu"],
        "mandate": config["mandate"],
        "merchant": config["profile"],
    }


@app.post("/api/parse-cart")
def parse_cart(req: ParseCartRequest) -> dict:
    """Natural language -> structured cart, via Claude with forced tool
    use. Runs server-side so the model key never reaches the browser.

    This is the ONLY place a model touches the flow, and all it does is
    turn words into a cart proposal. The APPROVE/COUNTER_OFFER/ESCALATE
    decision that follows is plain Python in negotiation.py.
    """
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise HTTPException(503, "OPENROUTER_API_KEY not configured; use the menu picker instead")

    if req.available_items:
        catalog_lines = [
            f"- {i.id}: {i.title or i.id}"
            + (f" (Rs.{i.price_inr})" if i.price_inr else "")
            + ("" if i.agent_orderable else " [in-person orders only]")
            for i in req.available_items
        ]
        item_ids = [i.id for i in req.available_items]
    else:
        menu = merchant_config.current_menu()
        catalog_lines = [f"- {name}: {item.name} (Rs.{item.price_inr})" for name, item in menu.items()]
        item_ids = list(menu.keys())

    # The menu goes in the prompt as well as the enum. The enum stops the
    # model inventing an id; the listing lets it tell the difference
    # between "not on this menu" and "close enough to something here".
    prompt = (
        "This merchant sells exactly these dishes:\n"
        + "\n".join(catalog_lines)
        + "\n\nThe customer asked for:\n"
        + req.text
        + "\n\nPut anything they asked for that this merchant does not sell into "
        "`unmatched`, using their own words. Do not substitute a different dish for it."
    )

    try:
        from llm_client import call_with_forced_tool

        args = call_with_forced_tool(
            prompt,
            tool_name="propose_cart",
            description=(
                "Convert the customer's request into a cart drawn only from this "
                "merchant's menu, and list anything they asked for that isn't on it."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "item_id": {"type": "string", "enum": item_ids},
                                "qty": {"type": "integer", "minimum": 1},
                            },
                            "required": ["item_id", "qty"],
                        },
                    },
                    "unmatched": {
                        "type": "array",
                        "description": "Things the customer asked for that this merchant does not sell.",
                        "items": {"type": "string"},
                    },
                },
                "required": ["items"],
            },
        )
        return {"items": args.get("items", []), "unmatched": args.get("unmatched", [])}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"could not parse that request: {exc}")


@app.post("/api/buyer-check")
def buyer_check(req: BuyerCheckRequest) -> dict:
    """The BUYER agent's own gate, run before any merchant is contacted.

    This enforces the customer's instructions to their agent, which are a
    different thing from the merchant's rules and belong to a different
    party. An order refused here never reaches Amma at all -- she has no
    say in it, and no record of it, because it was never her business.
    """
    mandate = buyer_mandate.BuyerMandate(
        spend_cap_inr=req.spend_cap_inr, confirm_above_inr=req.confirm_above_inr
    )
    cart = [(item.item_id, item.qty) for item in req.items]
    # Price against the merchant's LIVE menu, not the defaults -- a buyer
    # checking its own budget must use the prices actually being charged.
    result = buyer_mandate.check_cart(
        cart, mandate=mandate, menu=merchant_config.current_menu()
    )
    return {
        "decision": result.decision.value,
        "reason": result.reason,
        "total_inr": result.total_inr,
    }


@app.get("/api/buyer-mandate-defaults")
def buyer_mandate_defaults() -> dict:
    d = buyer_mandate.DEFAULT_BUYER_MANDATE
    return {"spend_cap_inr": d.spend_cap_inr, "confirm_above_inr": d.confirm_above_inr}


class AskBuyerRequest(BaseModel):
    agent_id: str
    phone: str
    original_request: str
    unmatched: list[str] = []


@app.post("/api/buyer-sms/ask")
def ask_buyer_what_instead(req: AskBuyerRequest) -> dict:
    """Message the customer about something the merchant doesn't sell.

    Goes to the number they gave at signup, and nowhere else -- the phone
    travels with the request from their own saved profile rather than
    being chosen here.
    """
    catalog = merchant_config.as_dict()
    try:
        conversation = buyer_sms.ask(
            agent_id=req.agent_id,
            phone=req.phone,
            original_request=req.original_request,
            unmatched=req.unmatched,
            available=catalog["menu"],
            shop_name=catalog["profile"]["shop_name"],
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return conversation.as_dict()


class ApproveBuyerRequest(BaseModel):
    agent_id: str
    phone: str
    cart_label: str
    total_inr: int
    soft_cap_inr: int


@app.post("/api/buyer-sms/approve")
def ask_buyer_to_approve(req: ApproveBuyerRequest) -> dict:
    """Ask the customer, on WhatsApp, to approve an order above their own
    soft cap -- rather than only offering the choice on a screen they may
    have walked away from."""
    catalog = merchant_config.as_dict()
    try:
        conversation = buyer_sms.ask_approval(
            agent_id=req.agent_id,
            phone=req.phone,
            cart_label=req.cart_label,
            total_inr=req.total_inr,
            soft_cap_inr=req.soft_cap_inr,
            shop_name=catalog["profile"]["shop_name"],
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return conversation.as_dict()


@app.get("/api/buyer-sms/status/{agent_id}")
def buyer_sms_status(agent_id: str) -> dict:
    """Polled by the waiting browser until the customer replies."""
    state = buyer_sms.status(agent_id)
    if state is None:
        raise HTTPException(404, "no open conversation for this agent")
    return state


@app.post("/api/buyer-sms/consume/{agent_id}")
def consume_buyer_reply(agent_id: str) -> dict:
    """Take the reply once, so a stale answer can't be reused on a later
    run of the same agent."""
    reply = buyer_sms.consume(agent_id)
    if reply is None:
        raise HTTPException(409, "no unconsumed reply for this agent")
    return {"reply": reply}


@app.get("/api/pending")
def pending() -> dict:
    """Everything awaiting a human decision, across BOTH protocols, in one
    merged queue. The merchant shouldn't have to care which protocol an
    order arrived on -- that's the whole architectural claim, made
    operational."""
    acp = adapter_acp.list_sessions(status="requires_human")["sessions"]
    ap2 = adapter_ap2.list_intent_mandates(status="requires_human")["sessions"]
    x402 = adapter_x402.list_orders(status="requires_human")["sessions"]
    return {"pending": acp + ap2 + x402}


@app.get("/api/sms")
def sms_state() -> dict:
    """What the merchant console shows in place of a real phone: the
    messages that went out, and what is still awaiting a reply."""
    return {
        "transport": "twilio" if notification_service.TWILIO_CONFIGURED else "mock",
        "outbox": notification_service.outbox(),
        "escalations": escalations.pending(),
    }


@app.get("/api/agents")
def agents() -> dict:
    db_path = audit_log.DEFAULT_DB_PATH
    events = audit_log.get_all_events(db_path=db_path, limit=1000)
    rows = []
    for agent_id in sorted({e["agent_id"] for e in events}):
        rows.append(
            {
                "agent_id": agent_id,
                "tier": trust.compute_trust_tier(agent_id, db_path=db_path).value,
                "completed": sum(1 for e in events if e["agent_id"] == agent_id and e["payment_id"]),
            }
        )
    return {"agents": rows}


@app.get("/api/events")
def events(limit: int = 40) -> dict:
    return {"events": audit_log.get_all_events(db_path=audit_log.DEFAULT_DB_PATH, limit=limit)}


@app.get("/api/payment-status/{payment_link_id}")
def payment_status(payment_link_id: str) -> dict:
    """Lets the buyer console show 'paid' without the user refreshing.
    Asks Razorpay directly rather than trusting local state."""
    import razorpay_client

    link = razorpay_client.fetch_payment_link(payment_link_id)
    return {"status": link["status"]}
