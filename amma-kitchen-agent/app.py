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

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from html import escape
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

import adapter_acp
import adapter_ap2
import adapter_mcp
import adapter_x402
import audit_log
import buyer_mandate
import buyer_sms
import catalog
import dashboard
import escalations
import google_auth
import merchant_auth
import notification_service
import reply_auth
import scheduler
import trust
import webhook_handler
import merchant_config
import merchants

WEB_DIR = Path(__file__).resolve().parent / "web"

log = logging.getLogger("amma.app")

@asynccontextmanager
async def _lifespan(fastapi_app: FastAPI):
    """Run the MCP session manager alongside this app.

    A mounted sub-app's lifespan is NOT run by the parent, so without
    this the MCP endpoint 500s on the first request with "Task group is
    not initialized" -- the session manager never got started.
    """
    # Said once, loudly, because a missing token otherwise fails at the
    # worst possible moment: a live reply, mid-demo, 403'd in a way that
    # looks like a bug in Twilio.
    reply_auth.warn_if_misconfigured()
    merchant_auth.warn_if_misconfigured()
    # The clock. Started here so it lives exactly as long as the app does
    # -- and cancelled below, because a background task that outlives its
    # server is a task nobody is watching.
    ticker = asyncio.create_task(scheduler.run()) if scheduler.is_enabled() else None
    if ticker is None:
        log.info("scheduler: disabled (SCHEDULER_ENABLED=false)")

    try:
        async with adapter_mcp.app.router.lifespan_context(fastapi_app):
            yield
    finally:
        if ticker is not None:
            ticker.cancel()
            # Awaited rather than abandoned, so shutdown waits for the
            # tick in flight instead of tearing the database out from
            # under a refund that is half-written.
            try:
                await ticker
            except asyncio.CancelledError:
                pass


app = FastAPI(title="Amma's Kitchen -- Agentic Commerce", lifespan=_lifespan)

app.include_router(adapter_acp.router)
app.include_router(adapter_ap2.router)
app.include_router(adapter_x402.router)
app.include_router(adapter_mcp.router)
app.include_router(webhook_handler.router)
app.include_router(escalations.router)
app.include_router(catalog.router)
app.include_router(dashboard.router)

@app.exception_handler(merchant_auth._LoginRedirect)
async def _login_redirect(request: Request, exc: merchant_auth._LoginRedirect):
    """A dependency cannot return a response, so require_merchant raises
    the redirect and this sends it. Only a browser asking for a page gets
    here; a fetch gets a 401 it can act on."""
    return exc.response


@app.middleware("http")
async def _guard_adapter_decisions(request: Request, call_next):
    """The escalation accept/reject endpoints, which live in the adapters.

    Those are money actions and belong behind the login, but the adapters
    are not to be edited -- so they are matched by path here and checked
    with exactly the same `is_authenticated` the dependency uses. One
    rule, applied in two places, rather than two rules that can drift.
    """
    if merchant_auth.path_needs_merchant(request.url.path):
        if not merchant_auth.is_authenticated(request):
            return JSONResponse(
                {"detail": "This is a merchant surface. Log in at /merchant/login."},
                status_code=401,
            )
    return await call_next(request)


class _RevalidatingStatic(StaticFiles):
    """Serve the console's CSS and assets, but make browsers check.

    Without this a browser holds the old stylesheet and shows a
    half-restyled page: the pages' inline rules reference design tokens
    the stale sheet has never heard of, so they resolve to nothing and,
    for instance, the merchant's board comes out white instead of dark.
    `no-cache` still allows caching -- it only requires revalidation --
    so the cost is one 304, and the failure mode it removes is somebody
    reloading mid-demo onto a page that looks broken.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", _RevalidatingStatic(directory=str(WEB_DIR)), name="static")

# The MCP endpoint an external assistant connects to. Mounted rather than
# routed because it is a Starlette app of its own, speaking Streamable
# HTTP instead of plain REST.
app.mount("/mcp", adapter_mcp.app)


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
    # Which kitchen this request is for, so the fallback paths read the
    # right menu. The agent normally sends available_items and this is
    # belt and braces -- but the model-unavailable fallback matches
    # against the live menu, and matching against the WRONG kitchen's
    # would put a dish nobody sells into a cart.
    merchant_id: str | None = None


class BuyerCheckItem(BaseModel):
    item_id: str
    qty: int


class BuyerCheckRequest(BaseModel):
    items: list[BuyerCheckItem]
    spend_cap_inr: int
    confirm_above_inr: int
    merchant_id: str | None = None


def _console(path: Path) -> HTMLResponse:
    """Serve a console page with the internal reply credential in it.

    The two pages with reply boxes post to /webhook/sms-reply, which is
    now authenticated. They are not Twilio-signed, so they carry
    INTERNAL_REPLY_TOKEN instead -- and the token is stamped into the
    page at serve time rather than fetched from an endpoint, because an
    endpoint that hands the credential to whoever asks is not a
    credential. `no-store` keeps it out of the disk cache.

    This is a stamp, not a login: see reply_auth.py on exactly how far it
    goes and where it stops.
    """
    html = path.read_text(encoding="utf-8").replace(
        "__INTERNAL_REPLY_TOKEN__", reply_auth.internal_token()
    )
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


def _login_page(next_path: str, error: str = "") -> HTMLResponse:
    html = (WEB_DIR / "login.html").read_text(encoding="utf-8")
    banner = f'<p class="login-err">{escape(error)}</p>' if error else ""
    html = html.replace("__NEXT__", escape(next_path or "/merchant/orders"))
    html = html.replace("__KITCHENS__", "".join(
        f'<option value="{escape(m["id"])}">{escape(m["name"])}</option>'
        for m in merchants.all()))
    html = html.replace("__ERROR__", banner)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/merchant/login", response_class=HTMLResponse)
def merchant_login_page(request: Request, next: str = "/merchant/orders") -> HTMLResponse:
    if merchant_auth.is_authenticated(request):
        return RedirectResponse(next, status_code=303)
    return _login_page(next)


@app.post("/merchant/login")
def merchant_login(
    password: str = Form(default=""),
    next: str = Form(default="/merchant/orders"),
    merchant_id: str = Form(default=""),
):
    """Neither the password nor the cookie value is logged, here or
    anywhere else -- a credential in a log file is a credential.

    `merchant_id` says which kitchen is signing in. It is validated
    against the register rather than trusted, and it is then baked INTO
    the signed cookie -- so from here on the answer to "whose orders may
    this session read" comes from something the caller cannot edit.
    """
    if merchant_id and not merchants.exists(merchant_id):
        return _login_page(next, "That kitchen is not on the platform.")
    # Checked as a PAIR. The kitchen chosen in the dropdown and the
    # password have to belong to each other, or the dropdown would be an
    # invitation to sign in as somebody else.
    if not merchant_auth.password_is_correct(password, merchant_id or None):
        return _login_page(next, "That is not the password for that kitchen.")

    # Only ever a path on this site: an open redirect would turn the
    # login into a way to send somebody somewhere else.
    destination = next if next.startswith("/") and not next.startswith("//") \
        else "/merchant/orders"
    response = RedirectResponse(destination, status_code=303)
    response.set_cookie(
        merchant_auth.COOKIE_NAME,
        merchant_auth.issue_cookie(merchant_id=merchant_id or None),
        max_age=merchant_auth.SESSION_SECONDS,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


class GoogleCredential(BaseModel):
    credential: str


@app.get("/api/google/config")
def google_config() -> dict:
    """What the sign-in buttons need to render themselves.

    The client id is public by design -- it identifies the app to Google
    and is embedded in every page that offers the button. The SECRET is
    not here and is not needed: this flow verifies an ID token's
    signature, it does not exchange an authorization code.
    """
    return {
        "enabled": google_auth.is_enabled(),
        "client_id": google_auth.client_id(),
        "merchant_enabled": google_auth.merchant_google_enabled(),
    }


@app.post("/merchant/login/google")
def merchant_login_google(req: GoogleCredential):
    """Sign the merchant in with Google, if she is on the list.

    The allowlist is the whole point. Google sign-in with no further check
    would be strictly worse than the password: anyone with a Google
    account could open her shop. See google_auth.verify_merchant.
    """
    try:
        person = google_auth.verify_merchant(req.credential)
    except google_auth.NotConfigured as exc:
        raise HTTPException(503, str(exc))
    except google_auth.NotAllowed as exc:
        raise HTTPException(403, str(exc))

    response = JSONResponse({"ok": True, "email": person["email"], "name": person["name"]})
    response.set_cookie(
        merchant_auth.COOKIE_NAME,
        merchant_auth.issue_cookie(),
        max_age=merchant_auth.SESSION_SECONDS,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


@app.post("/api/buyer/login/google")
def buyer_login_google(req: GoogleCredential) -> dict:
    """Identify the customer, and name their agent after them.

    No allowlist here, deliberately: any Google account is a legitimate
    customer. What this buys is a real name for the agent id, so the
    audit trail reads "Jeet's Agent" rather than five random characters.

    It sets no cookie. The buyer console keeps its profile in the
    browser, which is the existing design and the reason the card never
    reaches this server -- this returns who they are and the page stores
    it exactly as it stores a typed name.
    """
    try:
        person = google_auth.verify(req.credential)
    except google_auth.NotConfigured as exc:
        raise HTTPException(503, str(exc))
    except google_auth.NotAllowed as exc:
        raise HTTPException(403, str(exc))

    return {
        "name": person["name"],
        "email": person["email"],
        "picture": person["picture"],
        "agent_id": google_auth.agent_name_for(person["name"], person["email"]),
    }


@app.post("/merchant/logout")
def merchant_logout():
    response = RedirectResponse("/merchant/login", status_code=303)
    response.delete_cookie(merchant_auth.COOKIE_NAME, path="/")
    return response


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
def buyer_order() -> HTMLResponse:
    """Day-to-day: say what you want, watch the agent work."""
    return _console(WEB_DIR / "order.html")


@app.get("/merchant", response_class=HTMLResponse, dependencies=[Depends(merchant_auth.require_merchant)])
def merchant_setup() -> FileResponse:
    """One-time shop setup: who you are, your limits, and your menu."""
    return FileResponse(WEB_DIR / "shop.html")


@app.get("/merchant/orders", response_class=HTMLResponse, dependencies=[Depends(merchant_auth.require_merchant)])
def merchant_console() -> HTMLResponse:
    """Day-to-day: the escalation queue, trust, and the decision log."""
    return _console(WEB_DIR / "merchant.html")


@app.get("/api/merchant-config", dependencies=[Depends(merchant_auth.require_merchant)])
def get_merchant_config(request: Request) -> dict:
    return merchant_config.as_dict(merchant_auth.signed_in_merchant(request))


class MerchantConfigRequest(BaseModel):
    profile: dict
    mandate: dict
    menu: list[dict]
    # Optional so an older client, or a script that only means to edit the
    # menu, keeps her existing rate limits rather than silently resetting
    # them to the shipped defaults.
    velocity: dict | None = None


@app.post("/api/merchant-config", dependencies=[Depends(merchant_auth.require_merchant)])
def save_merchant_config(request: Request, req: MerchantConfigRequest) -> dict:
    """Save the shop. These values are what negotiation.py decides
    against from the next order onward -- the page is not decorative."""
    try:
        return merchant_config.save(
            req.profile, req.mandate, req.menu, req.velocity,
            merchant_id=merchant_auth.signed_in_merchant(request))
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/restaurants")
def restaurants() -> dict:
    """Every kitchen on the platform, for the customer to choose from.

    Public and unauthenticated on purpose: this is a directory, and a
    directory nobody can read is a marketplace nobody can shop at. It
    carries no limits -- see /api/menu, and the note in adapter_mcp
    about why a merchant's numbers are not published to the person on
    the other side of the negotiation.
    """
    out = []
    for entry in merchants.all():
        profile = merchant_config.profile(entry["id"])
        menu = merchant_config.current_menu(entry["id"])
        out.append({
            **entry,
            "shop_name": profile.get("shop_name") or entry["name"],
            "dishes": len(menu),
            "from_inr": min((i.price_inr for i in menu.values()), default=0),
        })
    return {
        "platform": {"name": merchants.Platform.name,
                     "tagline": merchants.Platform.tagline,
                     "blurb": merchants.Platform.blurb},
        "restaurants": out,
    }


@app.get("/api/menu")
def menu(merchant_id: str | None = None) -> dict:
    """What the buyer console renders, for the kitchen it is showing.

    Includes items the merchant sells but agents may not order, flagged
    rather than hidden -- the buyer should be able to see the rule being
    applied, not just its result.
    """
    config = merchant_config.as_dict(merchant_id)
    return {
        "items": config["menu"],
        "mandate": config["mandate"],
        "merchant": config["profile"],
        "merchant_id": merchant_id or merchants.default_id(),
        "merchant_name": merchants.name_of(merchant_id),
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
        return _parse_without_a_model(req.text, "no model key is configured", merchant_id=req.merchant_id)

    if req.available_items:
        catalog_lines = [
            f"- {i.id}: {i.title or i.id}"
            + (f" (Rs.{i.price_inr})" if i.price_inr else "")
            + ("" if i.agent_orderable else " [in-person orders only]")
            for i in req.available_items
        ]
        item_ids = [i.id for i in req.available_items]
    else:
        menu = merchant_config.current_menu(req.merchant_id)
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
        return {
            "items": args.get("items", []),
            "unmatched": args.get("unmatched", []),
            "parsed_by": "claude",
        }
    except HTTPException:
        raise
    except Exception as exc:
        return _parse_without_a_model(req.text, _why_the_model_failed(exc, merchant_id=req.merchant_id))


def _why_the_model_failed(exc: Exception) -> str:
    """One short sentence, because the raw provider error is a wall of
    JSON that says the same thing five times and reads like a crash."""
    text = str(exc)
    if "402" in text or "credits" in text.lower():
        return "the model provider is out of credit"
    if "401" in text or "invalid api key" in text.lower():
        return "the model key was rejected"
    if "timeout" in text.lower() or "timed out" in text.lower():
        return "the model did not answer in time"
    return "the model could not be reached"


def _parse_without_a_model(text: str, why: str,
                           merchant_id: str | None = None) -> dict:
    """Fall back to matching the request against the menu directly.

    The model's only job in this project is turning words into a cart
    proposal -- it has never decided anything, and every gate after this
    point is plain Python. So when it is unreachable the honest response
    is to do that one job worse rather than to refuse the order: match
    what can be matched exactly, and report the rest as off-menu through
    the same path a model-reported miss already takes.

    It is labelled on the wire and said out loud in the buyer's terminal.
    A demo that quietly degrades is worse than one that stops, because
    the viewer cannot tell which parser answered.
    """
    parsed = merchant_config.parse_request(text, merchant_id)
    return {
        "items": parsed["items"],
        "unmatched": parsed["unmatched"],
        "parsed_by": "menu-matching",
        "fallback_reason": why,
    }


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
    # Price against the LIVE menu of the kitchen being ordered from, not
    # the defaults and not some other shop's -- a buyer checking its own
    # budget must use the prices actually about to be charged. Getting
    # this wrong priced a grill-house cart against a South Indian menu
    # and refused it as "unknown item" before the grill house was ever
    # asked, which is a refusal by the wrong party for the wrong reason.
    result = buyer_mandate.check_cart(
        cart, mandate=mandate,
        menu=merchant_config.current_menu(req.merchant_id),
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
    """Polled by the waiting browser until the customer replies.

    "Nothing to answer" is a 200, not a 404. The buyer console now polls
    this from page load rather than only inside a live order, so the
    normal state is having no open question -- and answering that with an
    error meant a browser console full of red on an idle page, which is
    exactly where somebody looks for a real problem during a demo.
    """
    state = buyer_sms.status(agent_id)
    return state if state is not None else {"agent_id": agent_id, "open": False}


@app.post("/api/buyer-sms/consume/{agent_id}")
def consume_buyer_reply(agent_id: str) -> dict:
    """Take the reply once, so a stale answer can't be reused on a later
    run of the same agent."""
    reply = buyer_sms.consume(agent_id)
    if reply is None:
        raise HTTPException(409, "no unconsumed reply for this agent")
    return {"reply": reply}


@app.get("/api/pending")
def pending(request: Request) -> dict:
    """Everything awaiting a human decision, across ALL protocols, in one
    merged queue. The merchant shouldn't have to care which protocol an
    order arrived on -- that's the whole architectural claim, made
    operational.

    Two kinds of entry sit here side by side. An adapter session waiting
    on a human BEFORE payment is held in that adapter's memory. An order
    that has already been PAID for and is waiting on her answer is rebuilt
    from the audit trail instead -- so it survives a restart, and so a
    stateless adapter can have a queue at all.

    An order in the paid lifecycle is deliberately not also listed by its
    own adapter: it is no longer waiting for permission to proceed, it is
    waiting for her verdict on something already bought.

    Scoped to the signed-in kitchen, read off her session cookie and
    never off a query string. This is the queue every entry of which
    carries a Decline, and Decline refunds a real customer -- an
    unscoped one put Amma's paid orders on the grill house's board with
    live buttons under them. ACP and x402 do not carry a kitchen yet and
    still resolve to the default, which is the documented adapter gap;
    they are therefore only offered to the default kitchen rather than to
    everybody.
    """
    shop = merchant_auth.signed_in_merchant(request)
    default = merchants.default_id()

    paid = adapter_mcp.list_pending(merchant_id=shop)["sessions"]
    settled_refs = {str(s["session_id"]) for s in paid}

    def unsettled(sessions):
        return [s for s in sessions if str(s.get("session_id")) not in settled_refs]

    single_tenant = shop is None or shop == default
    acp = unsettled(adapter_acp.list_sessions(status="requires_human")["sessions"])         if single_tenant else []
    x402 = unsettled(adapter_x402.list_orders(status="requires_human")["sessions"])         if single_tenant else []
    ap2 = unsettled(adapter_ap2.list_intent_mandates(
        status="requires_human", merchant_id=shop)["sessions"])
    return {"pending": acp + ap2 + x402 + paid}


@app.get("/api/demand")
def unmatched_demand(request: Request) -> dict:
    """What agents asked for that the merchant doesn't sell.

    Surfaced because a signal nobody can see is a signal nobody acts on --
    which is the same mistake as logging an escalation that never reaches
    her queue.
    """
    return {"demand": audit_log.get_unmatched_demand(
        db_path=audit_log.DEFAULT_DB_PATH,
        merchant_id=merchant_auth.signed_in_merchant(request))}


class RoutineItemIn(BaseModel):
    item_id: str
    qty: int


class RoutineIn(BaseModel):
    items: list[RoutineItemIn]
    days: list[str]
    time: str
    agent_id: str
    phone: str | None = None
    routine_cap_inr: int | None = None
    window_minutes: int = 45
    # Which kitchen it repeats at. Its dishes, the prices drift is
    # measured against and the rules it is checked by all belong to that
    # shop -- so a routine without one is a routine for nobody.
    merchant_id: str | None = None
    # The customer's own offset from UTC. Their "08:00" means eight where
    # THEY are; without this the gate measured it against UTC and a
    # routine outside that zone could never fire.
    utc_offset_minutes: int | None = None


@app.get("/api/routines")
def list_routines(agent_id: str | None = None,
                  merchant_id: str | None = None) -> dict:
    """One kitchen's standing orders.

    Filtered, because the grill house was listing a repeat order in
    dishes it does not sell, from a routine that belongs to another shop
    entirely.
    """
    import routines as routines_mod

    rows = routines_mod.for_merchant(merchant_id, agent_id)
    return {"routines": rows, "price_drift_tolerance": routines_mod.PRICE_DRIFT_TOLERANCE}


@app.post("/api/routines")
def create_routine(req: RoutineIn) -> dict:
    """Turn a standing order on. Always explicit -- nothing in this system
    creates one on the customer's behalf."""
    import routines as routines_mod

    try:
        return routines_mod.create(
            items=[{"item_id": i.item_id, "qty": i.qty} for i in req.items],
            days=req.days, at_time=req.time, agent_id=req.agent_id, phone=req.phone,
            routine_cap_inr=req.routine_cap_inr, window_minutes=req.window_minutes,
            utc_offset_minutes=req.utc_offset_minutes,
            merchant_id=req.merchant_id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/routines/{routine_id}/status")
def set_routine_status(routine_id: str, status: str) -> dict:
    import routines as routines_mod

    try:
        updated = routines_mod.set_status(routine_id, status)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if updated is None:
        raise HTTPException(404, "no such standing order")
    return updated


@app.delete("/api/routines/{routine_id}")
def delete_routine(routine_id: str) -> dict:
    import routines as routines_mod

    return {"deleted": routines_mod.delete(routine_id)}


@app.post("/api/routines/{routine_id}/simulate")
def simulate_routine(routine_id: str, at: str | None = None) -> dict:
    """Run the confidence gate now, and fire or ask.

    There is no scheduler in this project -- see CLAUDE.md. Something has
    to call this, and for the demo that something is a button. `at` lets a
    future occurrence be simulated without waiting for the day to come
    round.
    """
    import routines as routines_mod

    when = None
    if at:
        try:
            when = datetime.fromisoformat(at)
        except ValueError:
            raise HTTPException(400, "`at` should be an ISO timestamp")
    try:
        return routines_mod.check_and_fire(routine_id, now=when)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.post("/api/routines/{routine_id}/confirm")
def confirm_routine(routine_id: str, approved: bool = True) -> dict:
    """The customer answered the prompt a gate failure raised."""
    import routines as routines_mod

    try:
        return routines_mod.confirm_pending(routine_id, approved=approved)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.get("/api/routines/suggestions")
def routine_suggestions(agent_id: str) -> dict:
    """Carts ordered repeatedly. A suggestion, never a routine."""
    import routines as routines_mod

    return {"suggestions": routines_mod.suggest_from_history(agent_id)}


@app.get("/evidence/{order_id}", dependencies=[Depends(merchant_auth.require_merchant)])
def evidence_page(order_id: int) -> FileResponse:
    """The Proof of Authorization page for one order.

    The id is in the path so the page is linkable and printable; the page
    fetches the pack itself from the API below.
    """
    return FileResponse(WEB_DIR / "evidence.html")


@app.get("/api/evidence/{order_id}", dependencies=[Depends(merchant_auth.require_merchant)])
def evidence_pack(order_id: int) -> dict:
    """The complete factual record of one order, assembled read-only.

    Not a ruling and not a liability finding -- the two checks it carries
    are arithmetic against the limits recorded at the time, stated with
    their numbers shown.
    """
    import evidence

    pack = evidence.build_evidence_pack(order_id, db_path=audit_log.DEFAULT_DB_PATH)
    if pack is None:
        raise HTTPException(404, f"no order #{order_id} in the audit trail")
    return pack


@app.post("/api/orders/{order_id}/raise-dispute")
def raise_dispute(order_id: int) -> dict:
    """The CUSTOMER says they did not authorise this order.

    Deliberately not behind the merchant login, because the merchant is
    not the person who disputes a charge. In the real world the customer
    raises it with their bank, the merchant receives the notice, and the
    merchant is the one who has to produce evidence. The merchant console
    keeps its own entry point for logging a dispute that arrived by that
    route -- this is the other end of the same fact.

    Safe to leave open because it moves nothing. It writes one timestamp
    that marks a record as contested and makes its evidence pack the
    featured view. No money, no status change, no notification.

    It is honestly unauthenticated rather than pretending otherwise: this
    project has no per-customer accounts (the buyer profile lives in the
    browser's own localStorage), so there is nobody to check the claimant
    against. Real multi-tenancy would need authentication nothing here
    has, and that is a known gap rather than an oversight.
    """
    if audit_log.get_event(order_id, db_path=audit_log.DEFAULT_DB_PATH) is None:
        raise HTTPException(404, f"no order #{order_id} in the audit trail")
    audit_log.mark_disputed(order_id, db_path=audit_log.DEFAULT_DB_PATH)
    return {"order_id": order_id, "disputed": True}


@app.post("/api/orders/{order_id}/dispute", dependencies=[Depends(merchant_auth.require_merchant)])
def mark_order_disputed(order_id: int) -> dict:
    """Flag an order as disputed. One timestamp, no workflow.

    All this does is make the order's evidence pack the featured view for
    it, and list it in the merchant's Disputes tab. Nothing is notified
    and nothing else changes.
    """
    if audit_log.get_event(order_id, db_path=audit_log.DEFAULT_DB_PATH) is None:
        raise HTTPException(404, f"no order #{order_id} in the audit trail")
    audit_log.mark_disputed(order_id, db_path=audit_log.DEFAULT_DB_PATH)
    return {"order_id": order_id, "disputed": True, "evidence_url": f"/evidence/{order_id}"}


@app.get("/api/disputes", dependencies=[Depends(merchant_auth.require_merchant)])
def disputes(request: Request) -> dict:
    """Orders someone has asked for the record on, newest first."""
    rows = audit_log.get_disputed(db_path=audit_log.DEFAULT_DB_PATH,
                                  merchant_id=merchant_auth.signed_in_merchant(request))
    return {
        "disputes": [
            {
                "order_id": r["id"],
                "disputed_at": r["disputed_at"],
                "placed_at": r["ts"],
                "agent_id": r["agent_id"],
                "protocol": r["protocol"],
                "cart_json": r["cart_json"],
                "total_inr": r["total_inr"],
                "decision": r["decision"],
                "has_snapshot": bool(r.get("limits_snapshot")),
            }
            for r in rows
        ]
    }


@app.get("/api/transactions")
def transactions(limit: int = 60) -> dict:
    """Money out and money back, for the customer's own statement.

    Read-only, and derived from the audit trail rather than a second
    ledger -- a separate table could disagree with the record, and then
    one of them would be lying.
    """
    limit = max(1, min(int(limit), 200))
    rows = audit_log.transactions(db_path=audit_log.DEFAULT_DB_PATH, limit=limit)
    return {
        "transactions": rows,
        "totals": {
            # Only a real capture counts as money out. A `sim_` reference
            # is an assertion of ours, and it is totalled separately so it
            # can never be mistaken for takings.
            "paid_inr": sum(r["amount_inr"] for r in rows
                            if r["direction"] == "out" and r["kind"] == "payment"),
            "returned_inr": sum(r["amount_inr"] for r in rows
                                if r["direction"] == "in" and r["kind"] == "refund"),
            "simulated_inr": sum(r["amount_inr"] for r in rows
                                 if r["kind"] == "simulated"),
        },
    }


@app.get("/api/order-outcomes")
def order_outcomes(minutes: int = 30, merchant_id: str | None = None) -> dict:
    """Orders that finished recently, so a screen can say so.

    Read-only, and the buyer console is the caller: under pay-first an
    order is decided by the kitchen AFTER the money has moved, so the
    customer's own screen has no other way to learn that she declined and
    the refund has already gone back.

    `merchant_id` here is the kitchen the customer is ORDERING FROM, sent
    by the buyer console. Unlike everywhere on the merchant side, taking
    it from the caller is safe: this grants nothing and moves nothing, it
    narrows a read of already-public outcomes. It is also as far as the
    narrowing can go -- there is still no per-customer identity in this
    project, so this reports the kitchen's recent outcomes rather than
    one person's, and a customer watching two kitchens sees both.
    """
    import mcp_orders

    minutes = max(1, min(int(minutes), 60 * 24))
    if merchant_id and not merchants.exists(merchant_id):
        raise HTTPException(404, "unknown kitchen")
    return {"outcomes": mcp_orders.recent_outcomes(minutes, merchant_id=merchant_id)}


@app.post("/api/merchant/optimize-prices", dependencies=[Depends(merchant_auth.require_merchant)])
def optimize_prices(request: Request) -> dict:
    """Discount what is piling up; restore what is running out.

    Writes only to the live merchant config, through the same
    merchant_config.save() the setup page uses -- so every validation
    rule she is already protected by still runs. negotiation.py is
    untouched and unaware: it receives a menu with prices on it, exactly
    as before, and a sale price is just a price.

    Because catalog.py reads that config, a buyer agent sees the new
    prices on its very next fetch. Nothing is scheduled -- she presses
    the button.
    """
    try:
        return merchant_config.optimize_prices(
            merchant_auth.signed_in_merchant(request))
    except ValueError as exc:
        # save() refused. Her shop is untouched, and she gets the reason.
        raise HTTPException(400, str(exc))


@app.get("/api/insights", dependencies=[Depends(merchant_auth.require_merchant)])
def growth_insights(request: Request, hours: int = 24) -> dict:
    """Read-only growth insights: her own numbers, plus two sentences of
    advice drawn from them.

    Isolated on purpose. It reads the audit log and calls a model, and
    that is all -- nothing here can reach an order, a price or a
    decision, and no other code reads its output back.

    The numbers are returned whether or not the model answers. They are
    the useful part; the prose is a convenience on top, and a missing API
    key or a slow provider should degrade to a dashboard rather than to
    an error page.
    """
    hours = max(1, min(int(hours), 24 * 30))
    stats = audit_log.growth_stats(hours, db_path=audit_log.DEFAULT_DB_PATH,
                                   merchant_id=merchant_auth.signed_in_merchant(request))

    if not os.environ.get("OPENROUTER_API_KEY"):
        return {"stats": stats, "insight": None,
                "note": "OPENROUTER_API_KEY not configured; showing the numbers only"}
    try:
        from llm_client import generate_merchant_insights

        return {"stats": stats, "insight": generate_merchant_insights(stats, hours)}
    except Exception as exc:
        return {"stats": stats, "insight": None, "note": f"insight unavailable: {exc}"}


# Beyond the list this task named, and deliberately: this returns the
# outbox, which carries the customer's phone number AND the single-use
# code in every escalation message. Leaving it open would hand an
# attacker the code, which is the one thing standing between knowing an
# order number and approving the order.
@app.get("/api/sms", dependencies=[Depends(merchant_auth.require_merchant)])
def sms_state() -> dict:
    """What the merchant console shows in place of a real phone: the
    messages that went out, and what is still awaiting a reply."""
    return {
        "transport": ("textbee" if notification_service.TEXTBEE_CONFIGURED
                      else "meta" if notification_service.META_CONFIGURED
                      else "twilio" if notification_service.TWILIO_CONFIGURED
                      else "mock"),
        "outbox": notification_service.outbox(),
        "escalations": escalations.pending(),
    }


@app.get("/api/agents")
def agents(request: Request) -> dict:
    db_path = audit_log.DEFAULT_DB_PATH
    events = audit_log.get_all_events(
        db_path=db_path, limit=1000,
        merchant_id=merchant_auth.signed_in_merchant(request))
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
def events(request: Request, limit: int = 40) -> dict:
    return {"events": audit_log.get_all_events(
        db_path=audit_log.DEFAULT_DB_PATH, limit=limit,
        merchant_id=merchant_auth.signed_in_merchant(request))}


@app.get("/api/payment-status/{payment_link_id}")
def payment_status(payment_link_id: str) -> dict:
    """Lets the buyer console show 'paid' without the user refreshing.

    Asks Razorpay directly rather than trusting local state -- and RECORDS
    the answer, which it used to leave undone.

    That gap was invisible until someone tested without a tunnel. The
    webhook is what normally writes a capture to the trail, and it cannot
    reach a laptop with no ngrok running: the customer paid, Razorpay
    said "paid", this endpoint said "paid", the screen said "paid", and
    the audit row still had no payment id. So the order never appeared in
    the customer's statement, never entered the pay-first lifecycle, and
    Amma was never asked about the ones that needed her. Everything
    correct except the one thing that mattered.

    The recording goes through the SAME claim and the SAME follow-up the
    webhook and the reconciler use, so this is a third path to the fact
    and not a third version of it -- whichever gets there first wins and
    the others no-op.
    """
    import razorpay_client

    link = razorpay_client.fetch_payment_link(payment_link_id)
    status = link["status"]

    if status == "paid":
        try:
            _record_capture(payment_link_id, link)
        except Exception:
            # Reporting the status must not fail because recording it did;
            # the reconciler is still the safety net behind this.
            log.exception("could not record the capture for %s", payment_link_id)

    return {"status": status}


def _record_capture(payment_link_id: str, link: dict) -> None:
    """Write a capture the webhook never delivered."""
    import idempotency
    import mcp_orders

    original = audit_log.get_event_by_payment_link(
        payment_link_id, db_path=audit_log.DEFAULT_DB_PATH
    )
    if original is None or original["payment_id"]:
        return

    payments = link.get("payments") or []
    payment_id = next(
        (p.get("payment_id") or p.get("id") for p in payments
         if p.get("status") == "captured"), None
    )
    if not payment_id:
        return

    # The same ledger and the same key the webhook claims under, so a
    # webhook that arrives late finds the work already done rather than
    # doing it twice.
    if not idempotency.claim_event(
        "payment_link.paid", payment_link_id, audit_log.DEFAULT_DB_PATH
    ):
        return

    audit_log.mark_paid(original["id"], payment_id, db_path=audit_log.DEFAULT_DB_PATH)
    mcp_orders.follow_up_after_capture(original, payment_id)
