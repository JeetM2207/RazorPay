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
import catalog
import dashboard
import trust
import webhook_handler
from mandate import MANDATE, MENU

WEB_DIR = Path(__file__).resolve().parent / "web"

app = FastAPI(title="Amma's Kitchen -- Agentic Commerce")

app.include_router(adapter_acp.router)
app.include_router(adapter_ap2.router)
app.include_router(adapter_x402.router)
app.include_router(webhook_handler.router)
app.include_router(catalog.router)
app.include_router(dashboard.router)

app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


class ParseCartRequest(BaseModel):
    text: str


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
def buyer_console() -> FileResponse:
    return FileResponse(WEB_DIR / "buyer.html")


@app.get("/merchant", response_class=HTMLResponse)
def merchant_console() -> FileResponse:
    return FileResponse(WEB_DIR / "merchant.html")


@app.get("/api/menu")
def menu() -> dict:
    """What the buyer console renders. Includes items the merchant sells
    but agents may not order, flagged rather than hidden -- the buyer
    should be able to see the rule being applied, not just its result."""
    return {
        "items": [
            {
                "id": item.name,
                "title": item.name.replace("_", " ").title(),
                "category": item.category,
                "price_inr": item.price_inr,
                "stock": item.stock,
                "agent_orderable": item.category in MANDATE.allowed_categories,
            }
            for item in MENU.values()
        ],
        "mandate": {
            "budget_cap_inr": MANDATE.budget_cap_inr,
            "human_confirm_threshold_inr": MANDATE.human_confirm_threshold_inr,
            "allowed_categories": list(MANDATE.allowed_categories),
        },
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
    try:
        from llm_client import call_with_forced_tool

        args = call_with_forced_tool(
            req.text,
            tool_name="propose_cart",
            description=(
                "Convert the buyer's natural language food order into a structured "
                "cart of catalog item ids and quantities."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "item_id": {"type": "string", "enum": list(MENU.keys())},
                                "qty": {"type": "integer", "minimum": 1},
                            },
                            "required": ["item_id", "qty"],
                        },
                    }
                },
                "required": ["items"],
            },
        )
        return {"items": args["items"]}
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
    result = buyer_mandate.check_cart(cart, mandate=mandate)
    return {
        "decision": result.decision.value,
        "reason": result.reason,
        "total_inr": result.total_inr,
    }


@app.get("/api/buyer-mandate-defaults")
def buyer_mandate_defaults() -> dict:
    d = buyer_mandate.DEFAULT_BUYER_MANDATE
    return {"spend_cap_inr": d.spend_cap_inr, "confirm_above_inr": d.confirm_above_inr}


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
