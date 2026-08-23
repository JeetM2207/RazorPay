"""MCP adapter: Amma's Kitchen as tools a real AI assistant can call.

The other three adapters are spoken to by buyer agents we wrote. This one
is spoken to by somebody else's model -- a user adds this server as a
custom connector in their own Claude account, says "order me dinner from
Amma's Kitchen", and Claude negotiates and checks out through the same
`orchestrator.negotiate_and_record()` that ACP, AP2 and x402 go through.

That makes it the sharpest test of the project's central rule. An
external model chooses when to call these tools and what to put in them,
and it still cannot decide anything: APPROVE / COUNTER_OFFER / ESCALATE
comes back from plain Python in negotiation.py, which has no idea MCP
exists. The model proposes; the core disposes.

Shape notes
-----------
Stateless between calls, like adapter_x402.py and unlike ACP's sessions.
An MCP client may reconnect, retry after a timeout, or call `checkout`
having never called `get_catalog` -- there is no session object to lose,
and work is resumed by agent+cart fingerprint instead.

Everything here is translation. The catalog tool wraps catalog.py, trust
runs through trust.py via the orchestrator, checkout claims through the
same idempotency ledger the webhook handler and reconciler use, and audit
rows are written by the orchestrator exactly as for every other protocol
-- tagged `mcp:` so the source is visible in the trail.
"""

import hashlib
import json
import os

from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

import audit_log
import catalog
import idempotency
import merchant_config
import orchestrator

load_dotenv()

PROTOCOL = "mcp"

# Whatever a client calls itself, it is namespaced under `mcp:` so it can
# never present as an agent from another protocol and inherit its trust.
AGENT_PREFIX = "mcp:"
DEFAULT_CLIENT = "claude"

# Only an ESCALATE caused by the human-confirm threshold is something a
# human may wave through. A disallowed category or an unknown item is a
# hard merchant rule, not a second opinion. Same wording, and the same
# restriction, as the other three adapters.
_HUMAN_OVERRIDABLE_MARKER = "human confirmation threshold"


class CartItem(BaseModel):
    item_id: str = Field(description="Catalog item id exactly as returned by get_catalog.")
    qty: int = Field(ge=1, description="How many of this item.")


# ------------------------------------------------------------- internals

def _agent_id(client: str | None) -> str:
    name = (client or DEFAULT_CLIENT).strip() or DEFAULT_CLIENT
    return AGENT_PREFIX + name.removeprefix(AGENT_PREFIX)


def _cart_of(items: list[CartItem]) -> list[tuple[str, int]]:
    return [(item.item_id, item.qty) for item in items]


def _fingerprint(agent_id: str, cart: list[tuple[str, int]]) -> str:
    """Same shape as adapter_x402's, and for the same reason: a retry of
    the same request from the same agent must resolve to the same work
    rather than starting a second one."""
    payload = json.dumps([agent_id, sorted(cart)], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _unmatched(cart: list[tuple[str, int]]) -> list[str]:
    """Item ids the merchant does not sell, reported by name.

    The cart is still handed to the negotiation core unchanged -- it
    returns ESCALATE for an unknown item, and that is the real answer.
    This only names them so the assistant can tell its user what was
    wrong, instead of substituting something nobody asked for.
    """
    menu = merchant_config.current_menu()
    return [item for item, _qty in cart if item not in menu]


def _same_cart(event: dict, cart: list[tuple[str, int]]) -> bool:
    try:
        lines = json.loads(event["cart_json"])
    except (json.JSONDecodeError, TypeError):
        return False
    return sorted((line["item"], line["qty"]) for line in lines) == sorted(cart)


def _events_for(agent_id: str) -> list[dict]:
    """Newest first.

    The db path is read HERE rather than relying on the default argument
    of get_events_for_agent: that default is bound when audit_log is
    imported, so it would keep pointing at whatever the path was then.
    Every other module resolves it at call time for the same reason.
    """
    return list(reversed(audit_log.get_events_for_agent(agent_id, db_path=audit_log.DEFAULT_DB_PATH)))


def _settled_checkout(agent_id: str, cart: list[tuple[str, int]]) -> dict | None:
    """A checkout already done for this exact cart, if any.

    This is what makes a retried tool call safe: the client gets the
    original order back rather than a second one.
    """
    for event in _events_for(agent_id):
        if event["payment_link_id"] and _same_cart(event, cart):
            return event
    return None


def _approved_unpaid_event(agent_id: str, cart: list[tuple[str, int]]) -> dict | None:
    """An APPROVE already recorded for this cart and not yet paid for, so
    proposing then checking out doesn't record the same decision twice."""
    for event in _events_for(agent_id):
        if (
            event["decision"] == "APPROVE"
            and not event["payment_link_id"]
            and _same_cart(event, cart)
        ):
            return event
    return None


def _notify_merchant(detail: dict, cart: list[tuple[str, int]]) -> None:
    """Text Amma about an escalation, exactly as the other adapters do.

    The handle passed through is the audit event id rather than a session
    id: this adapter keeps no session, so the audit row IS the order.
    Isolated and swallowed for the same reason as everywhere else -- a
    notification problem must never stop an order being recorded.
    """
    try:
        import escalations

        escalations.notify(PROTOCOL, str(detail["event_id"]), detail, cart)
    except Exception:
        pass


def _is_resolved(agent_id: str, cart: list[tuple[str, int]], after_id: int) -> bool:
    """Has a human already decided this escalated cart?

    Resolution is a later APPROVE (a human override) or REJECTED for the
    same agent and cart. With no session to hold status on, the trail is
    the state.
    """
    for event in audit_log.get_events_for_agent(agent_id, db_path=audit_log.DEFAULT_DB_PATH):
        if event["id"] <= after_id:
            continue
        if event["decision"] in ("APPROVE", "REJECTED") and _same_cart(event, cart):
            return True
    return False


def _cart_from(event: dict) -> list[tuple[str, int]]:
    try:
        return [(line["item"], line["qty"]) for line in json.loads(event["cart_json"])]
    except (json.JSONDecodeError, TypeError, KeyError):
        return []


def list_pending() -> dict:
    """MCP escalations awaiting a human, in the shape the merchant console
    already understands.

    Rebuilt from the audit trail on each call rather than held in memory,
    so it survives a restart and a reconnecting client -- which is the
    whole point of this adapter being stateless.
    """
    import trust

    db_path = audit_log.DEFAULT_DB_PATH
    sessions = []
    for event in audit_log.get_all_events(db_path=db_path, limit=500):
        if not event["agent_id"].startswith(AGENT_PREFIX):
            continue
        if event["decision"] != "ESCALATE":
            continue
        cart = _cart_from(event)
        if not cart or _is_resolved(event["agent_id"], cart, event["id"]):
            continue

        sessions.append(
            {
                "session_id": str(event["id"]),
                "agent_id": event["agent_id"],
                "status": "requires_human",
                "protocol": PROTOCOL,
                "cart": [{"item": name, "qty": qty} for name, qty in cart],
                "decision_detail": {
                    "event_id": event["id"],
                    "agent_id": event["agent_id"],
                    "decision": "ESCALATE",
                    "reason": event["reason"],
                    "total_inr": event["total_inr"],
                    "trust_tier": trust.compute_trust_tier(
                        event["agent_id"], db_path=db_path
                    ).value,
                    "alternatives": [],
                    "buyer_reasoning": event.get("buyer_reasoning"),
                },
            }
        )
    return {"sessions": sessions}


def _escalated_event(order_id) -> dict:
    event_id = int(order_id)
    for session in list_pending()["sessions"]:
        if session["decision_detail"]["event_id"] == event_id:
            return session
    raise HTTPException(409, f"order #{event_id} is not awaiting a decision")


def human_confirm(order_id, items: list[CartItem] | None = None) -> dict:
    """A human approves an escalated MCP order.

    Same restriction as every other adapter: only a threshold escalation
    can be waved through. A disallowed category is a hard merchant rule
    and no human overrides it here.
    """
    session = _escalated_event(order_id)
    detail = session["decision_detail"]
    cart = [(line["item"], line["qty"]) for line in session["cart"]]

    if _HUMAN_OVERRIDABLE_MARKER not in detail["reason"]:
        raise HTTPException(
            403,
            "this escalation is a hard merchant rule (disallowed category, "
            "unknown item, or over the flexible margin) and cannot be "
            "human-overridden here",
        )

    new_event_id = orchestrator.record_human_override(
        session["agent_id"], PROTOCOL, cart, detail
    )
    return {
        "order_id": new_event_id,
        "status": "approved",
        "reason": f"human override: {detail['reason']}",
    }


def human_reject(order_id) -> dict:
    session = _escalated_event(order_id)
    detail = session["decision_detail"]
    cart = [(line["item"], line["qty"]) for line in session["cart"]]

    new_event_id = orchestrator.record_human_rejection(
        session["agent_id"], PROTOCOL, cart, detail
    )
    return {
        "order_id": new_event_id,
        "status": "rejected",
        "reason": f"human rejected: {detail['reason']}",
    }


# Merchant-side REST, separate from the MCP protocol surface: these are
# for Amma's console and the SMS resolver, not for the assistant.
router = APIRouter()


@router.get("/mcp-orders")
def list_mcp_orders() -> dict:
    return list_pending()


@router.post("/mcp-orders/{order_id}/human_confirm")
def confirm_mcp_order(order_id: str) -> dict:
    return human_confirm(order_id)


@router.post("/mcp-orders/{order_id}/human_reject")
def reject_mcp_order(order_id: str) -> dict:
    return human_reject(order_id)


def _decision_response(detail: dict, cart: list[tuple[str, int]]) -> dict:
    """The same object every other adapter returns, differently wrapped."""
    response = {
        "decision": detail["decision"],
        "reason": detail["reason"],
        "total_inr": detail["total_inr"],
        "trust_tier": detail["trust_tier"],
        "order_id": detail["event_id"],
        "alternatives": detail["alternatives"],
    }
    if detail.get("upsell_suggestion"):
        response["upsell_suggestion"] = detail["upsell_suggestion"]
    unmatched = _unmatched(cart)
    if unmatched:
        response["unmatched_items"] = unmatched
    return response


# ---------------------------------------------------------- tool bodies
# Kept as plain functions so they are directly testable without standing
# up a transport. The MCP registrations below are thin wrappers.

def get_catalog_impl() -> dict:
    """Wraps catalog.py rather than re-deriving the feed, and trims the
    per-item currency repetition -- custom connector responses have a
    token ceiling and none of it should go on saying "INR" 20 times."""
    feed = catalog.get_catalog()
    return {
        "merchant": feed["merchant"],
        "items": [
            {
                "id": item["id"],
                "title": item["title"],
                "category": item["category"],
                "price_inr": item["price"],
                "in_stock": item["availability"] == "in_stock",
                "agent_orderable": item["agent_orderable"],
            }
            for item in feed["items"]
        ],
        "order_limits": feed["order_limits"],
        "note": (
            "Items with agent_orderable=false are sold in person only and will be "
            "refused if ordered. Orders at or above human_confirm_at_inr wait for "
            "the merchant to approve them."
        ),
    }


def propose_cart_impl(
    items: list[CartItem], reasoning: str, client: str | None = None
) -> dict:
    agent_id = _agent_id(client)
    cart = _cart_of(items)
    if not cart:
        return {
            "decision": "ESCALATE",
            "reason": "empty cart: nothing to price",
            "total_inr": 0,
            "trust_tier": "NEW",
            "order_id": None,
            "alternatives": [],
        }

    # Required by the schema, but checked here too: a schema stops a
    # well-behaved client, a server check stops everything else.
    if not (reasoning or "").strip():
        return {
            "decision": "ESCALATE",
            "reason": "the customer's reason for this order is required and was empty",
            "total_inr": 0,
            "trust_tier": "NEW",
            "order_id": None,
            "alternatives": [],
        }

    detail = orchestrator.negotiate_and_record(agent_id, PROTOCOL, cart)
    # Written after the orchestrator's row rather than through it, so the
    # shared decision path stays identical for every protocol.
    audit_log.attach_buyer_reasoning(
        detail["event_id"], reasoning.strip(), db_path=audit_log.DEFAULT_DB_PATH
    )
    if detail["decision"] == "ESCALATE":
        _notify_merchant(detail, cart)
    response = _decision_response(detail, cart)
    response["buyer_reasoning"] = reasoning.strip()
    return response


def checkout_impl(
    items: list[CartItem],
    delivery_name: str,
    delivery_phone: str,
    delivery_address: str,
    client: str | None = None,
) -> dict:
    agent_id = _agent_id(client)
    cart = _cart_of(items)
    if not cart:
        return {"status": "refused", "reason": "empty cart: nothing to pay for"}

    # The schema marks these required, which is what makes a real client
    # go and ask the user for them. Re-checked here because a schema
    # constrains a cooperative caller and nothing else -- and an order
    # with nobody to deliver it to is not an order.
    missing = [
        field
        for field, value in (
            ("delivery_name", delivery_name),
            ("delivery_phone", delivery_phone),
            ("delivery_address", delivery_address),
        )
        if not (value or "").strip()
    ]
    if missing:
        return {
            "status": "refused",
            "reason": (
                "cannot place an order without "
                + ", ".join(missing)
                + " -- ask the customer for their delivery details and call again"
            ),
            "missing_fields": missing,
        }

    # A retried tool call must return the original order, not make another.
    already = _settled_checkout(agent_id, cart)
    if already:
        return {
            "status": "already_placed",
            "order_id": already["id"],
            "amount_inr": already["total_inr"],
            "payment_link_id": already["payment_link_id"],
            "duplicate": True,
            "reason": "this exact cart was already checked out; returning the original order",
        }

    # Reuse an APPROVE already on record for this cart -- including one a
    # human granted for an escalated order -- rather than re-deciding and
    # writing a second audit row for the same decision.
    approved = _approved_unpaid_event(agent_id, cart)
    if approved is None:
        detail = orchestrator.negotiate_and_record(agent_id, PROTOCOL, cart)
        if detail["decision"] == "ESCALATE":
            _notify_merchant(detail, cart)
        if detail["decision"] != "APPROVE":
            # No claim was made, so a legitimate retry after the merchant
            # approves is still able to check out.
            refusal = _decision_response(detail, cart)
            refusal["status"] = "refused"
            return refusal
        event_id = detail["event_id"]
        human_approved = False
    else:
        event_id = approved["id"]
        human_approved = "human override" in (approved["reason"] or "")

    # Claim through the SAME ledger the webhook handler and reconciler
    # use. Claimed only once the order is genuinely going to be created.
    db_path = audit_log.DEFAULT_DB_PATH
    if not idempotency.claim_event("mcp.checkout", _fingerprint(agent_id, cart), db_path):
        replay = _settled_checkout(agent_id, cart)
        if replay:
            return {
                "status": "already_placed",
                "order_id": replay["id"],
                "amount_inr": replay["total_inr"],
                "payment_link_id": replay["payment_link_id"],
                "duplicate": True,
                "reason": "this exact cart was already checked out; returning the original order",
            }
        return {
            "status": "in_progress",
            "reason": "a checkout for this cart is already underway; do not retry immediately",
        }

    # orchestrator re-runs the whole check here as defense in depth, so a
    # client claiming "this was approved" is never taken at its word.
    # skip_reevaluation only for a cart a human explicitly waved through,
    # which would otherwise re-escalate forever.
    link = orchestrator.create_payment_for_cart(
        agent_id, event_id, cart, skip_reevaluation=human_approved
    )
    audit_log.attach_delivery(
        event_id,
        delivery_name.strip(),
        delivery_phone.strip(),
        delivery_address.strip(),
        db_path=db_path,
    )
    total = sum(
        merchant_config.current_menu()[name].price_inr * qty for name, qty in cart
    )
    return {
        "status": "placed",
        "order_id": event_id,
        "amount_inr": total,
        "payment_link_id": link["id"],
        # A link the CUSTOMER opens. Everything that actually authorises
        # money -- OTP, UPI PIN, CVV -- happens on Razorpay's page, typed
        # by the human. This adapter cannot do that step and holds no
        # credential with which to try.
        "payment_url": link["short_url"],
        "delivery": {
            "name": delivery_name.strip(),
            "phone": delivery_phone.strip(),
            "address": delivery_address.strip(),
        },
        "duplicate": False,
        "next_step": (
            "Give the customer the payment_url. They complete payment on Razorpay's "
            "own page; you cannot pay on their behalf."
        ),
    }


# ------------------------------------------------------------ MCP server

mcp_server = MCPServer(
    name="ammas-kitchen",
    title="Amma's Kitchen",
    version="1.0.0",
    instructions=(
        "Amma's Kitchen is a home kitchen that accepts orders from AI assistants. "
        "Always call get_catalog first so you order real items at real prices. "
        "Then call propose_cart to have the kitchen price and check the order — it "
        "may approve it, counter with alternatives, or hold it for the cook to "
        "confirm. Only call checkout once propose_cart has returned APPROVE. "
        "You cannot approve an order yourself; the kitchen decides."
    ),
)


@mcp_server.tool(
    name="get_catalog",
    title="Get Amma's Kitchen menu",
    description=(
        "Fetch the current menu for Amma's Kitchen: every dish with its id, price in "
        "rupees, whether it is in stock, and whether an AI assistant is allowed to "
        "order it at all. Also returns the kitchen's own limits — the largest order "
        "it will accept, and the amount above which the cook must confirm by hand. "
        "Call this before proposing a cart so you use real item ids and real prices."
    ),
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
)
def get_catalog() -> dict:
    return get_catalog_impl()


@mcp_server.tool(
    name="propose_cart",
    title="Price and check an order",
    description=(
        "Send a cart of items and quantities to Amma's Kitchen for pricing and "
        "checking. Returns one of three answers: APPROVE (the kitchen will make it, "
        "and you may then call checkout), COUNTER_OFFER (it cannot do exactly that, "
        "with alternatives that would work), or ESCALATE (the cook has to confirm "
        "this one by hand, or it breaks a rule of hers). The reason is always "
        "included. Item ids must come from get_catalog; anything the kitchen does "
        "not sell is named back to you rather than substituted. This does not take "
        "any payment. Also pass along the context the user gave for wanting this order: "
        "the kitchen can see the cart and the price, but has no other way to know who "
        "it is for or what the occasion is."
    ),
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
)
def propose_cart(
    items: list[CartItem],
    reasoning: str = Field(
        description=(
            "The user's actual stated intent or context for this order -- the occasion, "
            "preference or need they mentioned. Do NOT restate prices, caps or "
            "thresholds; those are tracked separately. Just capture why the human wants "
            "this, e.g. 'Working late and wants something light that isn't spicy' or "
            "'Friend visiting who has not tried South Indian food before'. If they gave "
            "no reason, say so plainly rather than inventing one."
        )
    ),
    client: str | None = None,
) -> dict:
    return propose_cart_impl(items, reasoning, client)


@mcp_server.tool(
    name="checkout",
    title="Place and pay for the order",
    description=(
        "Place an order that propose_cart has already APPROVED, creating a real "
        "payment for it. The kitchen re-checks the cart against its own rules before "
        "taking anything, so a cart that is no longer acceptable is refused here even "
        "if it was approved a moment ago. Calling this twice with the same cart is "
        "safe: the original order is returned rather than a second one being placed. "
        "You must collect the customer's name, phone and delivery address from them "
        "first — ask in conversation, never invent them. This returns a payment link "
        "for the customer to open; you cannot pay on their behalf and hold no card or "
        "UPI details to try."
    ),
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
    ),
)
def checkout(
    items: list[CartItem],
    delivery_name: str = Field(
        description="Full name of the person receiving the order. Ask the user; never invent one."
    ),
    delivery_phone: str = Field(
        description="Contact phone number for the delivery. Ask the user; never invent one."
    ),
    delivery_address: str = Field(
        description=(
            "Full delivery address including flat/house number, street, area and city. "
            "Ask the user; never invent or guess one."
        )
    ),
    client: str | None = None,
) -> dict:
    return checkout_impl(items, delivery_name, delivery_phone, delivery_address, client)


# The SDK validates the Host header by default, to stop a browser on the
# user's machine being tricked into driving a localhost MCP server (DNS
# rebinding). That protection also rejects any public hostname it wasn't
# told about, so a tunnel or a deploy returns 421 "Invalid Host header"
# until its domain is listed here.
#
# MCP_ALLOWED_HOSTS is a comma-separated list, e.g.
#   MCP_ALLOWED_HOSTS=abc123.ngrok-free.dev,ammas-kitchen.onrender.com
# Left unset, only localhost works -- which is the safe default, and the
# reason this is configuration rather than a hardcoded domain.
_EXTRA_HOSTS = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]

_ALLOWED_HOSTS = ["127.0.0.1", "localhost", "127.0.0.1:*", "localhost:*", *_EXTRA_HOSTS]
_ALLOWED_ORIGINS = [
    "http://127.0.0.1:*", "http://localhost:*",
    *[f"https://{h}" for h in _EXTRA_HOSTS],
]

# Stateless HTTP: no per-connection session to lose when a client
# reconnects, which is also what lets this sit behind a tunnel or a
# multi-instance deploy without sticky routing.
app = mcp_server.streamable_http_app(
    streamable_http_path="/",
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        allowed_hosts=_ALLOWED_HOSTS,
        allowed_origins=_ALLOWED_ORIGINS,
    ),
)
