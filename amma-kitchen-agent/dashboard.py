"""Audit trail dashboard (build order step 7).

Renders every decision the system has made as a human-readable page --
what was requested, what was checked, what passed or failed and why, and
what happened next. This is what gets shown on camera in the pitch: the
proof that every money action is explainable, bounded and gated.

Deliberately self-contained (no external CSS/JS) so it renders identically
anywhere, and auto-refreshes so you can run buyer agents in another
terminal and watch rows appear live during a demo.

Run:
    uvicorn dashboard:app --port 8003
"""

import html
import json

from fastapi import APIRouter, FastAPI
from fastapi.responses import HTMLResponse

import audit_log
import trust
import merchant_config

router = APIRouter()

# Decisions that must never have resulted in a Razorpay call. The
# dashboard highlights these specifically, because "no payment call was
# ever made for a rejected order" is a claim the audit trail should let
# a viewer verify at a glance rather than take on trust.
_NON_PAYING_DECISIONS = ("ESCALATE", "REJECTED", "COUNTER_OFFER", "PAYMENT_NOT_COMPLETED")

# Which stamp each decision gets. The mapping is the project's four
# semantics, unchanged: cleared, waiting on a human, refused by a hard
# rule, informational -- only the ink is new.
_DECISION_STAMPS = {
    "APPROVE": ("leaf", "APPROVE"),
    "PAID": ("leaf", "PAID"),
    "AUTO_CONFIRMED": ("leaf", "AUTO CONFIRMED"),
    "MERCHANT_ACCEPTED": ("leaf", "ACCEPTED"),
    "COUNTER_OFFER": ("rust", "COUNTER OFFER"),
    "ESCALATE": ("rust", "ESCALATE"),
    "AWAITING_PAYMENT": ("rust", "AWAITING PAYMENT"),
    "PENDING_MERCHANT_APPROVAL": ("rust", "PENDING HER OK"),
    "REJECTED": ("brick", "REJECTED (human)"),
    "MERCHANT_REJECTED": ("brick", "REJECTED"),
    "REFUNDED": ("brick", "REFUNDED"),
    "REFUND_FAILED": ("brick", "REFUND FAILED"),
    "MERCHANT_TIMEOUT_REFUNDED": ("brick", "TIMED OUT"),
    "PAYMENT_NOT_COMPLETED": ("steel", "PAYMENT NOT COMPLETED"),
    "UNMATCHED_DEMAND": ("steel", "ASKED FOR"),
}

_TIER_STAMPS = {"NEW": "steel", "STANDARD": "gold", "TRUSTED": "leaf"}


def _format_cart(cart_json: str) -> str:
    try:
        lines = json.loads(cart_json)
    except (json.JSONDecodeError, TypeError):
        return html.escape(str(cart_json))
    if not lines:
        return "<span class='muted'>empty</span>"
    return html.escape(
        ", ".join(f"{line['qty']}x {line['item'].replace('_', ' ')}" for line in lines)
    )


def _decision_badge(decision: str) -> str:
    tone, label = _DECISION_STAMPS.get(decision, ("steel", decision.replace("_", " ")))
    return f"<span class='stamp {tone}'>{html.escape(label)}</span>"


def _tier_badge(tier: str) -> str:
    tone = _TIER_STAMPS.get(tier, "steel")
    return f"<span class='stamp {tone}'>{html.escape(tier)}</span>"


def _payment_cell(event: dict) -> str:
    if event["payment_id"]:
        # A `sim_` reference was asserted by us, not settled by Razorpay.
        # It must never render the same as money that actually moved.
        simulated = event["payment_id"].startswith("sim_")
        label = "SIMULATED" if simulated else "PAID"
        tone = "rust" if simulated else "leaf"
        return (
            f"<span class='stamp {tone}'>{label}</span>"
            f"<div class='mono muted'>{html.escape(event['payment_id'])}</div>"
        )
    if event["payment_link_id"]:
        return (
            "<span class='stamp rust'>awaiting payment</span>"
            f"<div class='mono muted'>{html.escape(event['payment_link_id'])}</div>"
        )
    if event["decision"] in _NON_PAYING_DECISIONS:
        return "<span class='stamp brick'>no Razorpay call</span>"
    return "<span class='muted'>&mdash;</span>"


def _reasons_cell(event: dict) -> str:
    """Two reasons, side by side and never merged.

    The system's is why the order was allowed or refused -- caps,
    categories, thresholds. The buyer's is the human context behind it:
    the occasion or need the customer gave, which nothing else in this
    system can see. Only present for protocols that ask for it, currently
    MCP. A merchant reading an agent order wants both.
    """
    parts = [f"<div>{html.escape(event['reason'])}</div>"]

    buyer_reasoning = event.get("buyer_reasoning")
    if buyer_reasoning:
        parts.append(
            "<div class='buyer-said'><span class='who'>customer wanted:</span> "
            f"{html.escape(buyer_reasoning)}</div>"
        )

    if event.get("delivery_name"):
        recipient = " &middot; ".join(
            html.escape(v)
            for v in (event.get("delivery_name"), event.get("delivery_phone"))
            if v
        )
        parts.append(
            f"<div class='deliver-to'><span class='who'>deliver to:</span> {recipient}"
            f"<br>{html.escape(event.get('delivery_address') or '')}</div>"
        )

    return "".join(parts)


def _summary(events: list[dict]) -> dict:
    # Revenue counts only genuine Razorpay captures. A simulated
    # settlement is shown in the log but never inflates the total.
    captured = [e for e in events if e["payment_id"] and not e["payment_id"].startswith("sim_")]
    return {
        "total": len(events),
        "approved": sum(1 for e in events if e["decision"] == "APPROVE"),
        "escalated": sum(1 for e in events if e["decision"] == "ESCALATE"),
        "rejected": sum(1 for e in events if e["decision"] == "REJECTED"),
        "captured_count": len(captured),
        "captured_inr": sum(e["total_inr"] for e in captured),
        "gated_without_payment": sum(
            1
            for e in events
            if e["decision"] in _NON_PAYING_DECISIONS and not e["payment_link_id"]
        ),
    }


def _agent_tiers(events: list[dict], db_path: str) -> list[tuple[str, str, int]]:
    agents = sorted({e["agent_id"] for e in events})
    rows = []
    for agent_id in agents:
        tier = trust.compute_trust_tier(agent_id, db_path=db_path)
        completed = sum(
            1 for e in events if e["agent_id"] == agent_id and e["payment_id"]
        )
        rows.append((agent_id, tier.value, completed))
    return rows


def _render(events: list[dict], db_path: str, refresh: int) -> str:
    _mandate = merchant_config.current_mandate()
    stats = _summary(events)
    refresh_tag = f"<meta http-equiv='refresh' content='{refresh}'>" if refresh > 0 else ""

    stat_cards = "".join(
        f"<div class='card'><div class='num'>{value}</div><div class='lbl'>{label}</div></div>"
        for label, value in [
            ("decisions logged", stats["total"]),
            ("approved", stats["approved"]),
            ("escalated to human", stats["escalated"]),
            ("rejected by human", stats["rejected"]),
            ("payments captured", stats["captured_count"]),
            ("revenue captured", f"&#8377;{stats['captured_inr']}"),
        ]
    )

    tier_rows = "".join(
        f"<tr><td class='mono'>{html.escape(agent_id)}</td>"
        f"<td>{_tier_badge(tier)}</td>"
        f"<td class='num-cell'>{completed}</td></tr>"
        for agent_id, tier, completed in _agent_tiers(events, db_path)
    ) or "<tr><td colspan='3' class='muted'>no agents yet</td></tr>"

    event_rows = "".join(
        f"<tr>"
        f"<td class='mono muted'>{event['id']}</td>"
        f"<td class='mono muted nowrap'>{html.escape(event['ts'][:19].replace('T', ' '))}</td>"
        f"<td class='mono'>{html.escape(event['agent_id'])}</td>"
        f"<td><span class='proto'>{html.escape(event['protocol'].upper())}</span></td>"
        f"<td>{_format_cart(event['cart_json'])}</td>"
        f"<td class='num-cell'>&#8377;{event['total_inr']}</td>"
        f"<td>{_decision_badge(event['decision'])}</td>"
        f"<td class='reason'>{_reasons_cell(event)}</td>"
        f"<td>{_payment_cell(event)}</td>"
        # Every order has a Proof of Authorization record whether or not
        # anyone has asked for it; this is the shortest way to reach one.
        f"<td class='nowrap'><a href='/evidence/{event['id']}'>view record</a></td>"
        f"</tr>"
        for event in events
    ) or "<tr><td colspan='9' class='muted'>No decisions recorded yet. Run a buyer agent.</td></tr>"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{refresh_tag}
<title>Amma's Kitchen &mdash; Audit Trail</title>
<style>
  /* Same tokens as web/shared.css, inlined because this page is rendered
     server-side and loads no stylesheet. Every family is a system stack:
     nothing here reaches the network. */
  :root {{
    --paper:#F1ECDF; --paper-card:#FBF8F0; --paper-border:#DAD0B8;
    --ink:#2B1D14; --ink-soft:#6B5940;
    --coffee:#2E1B0E; --gold:#B8791A; --gold-deep:#8F5C10;
    --leaf:#4F7942; --rust:#A85C2A; --brick:#9B3A2C; --steel:#5C7A8A;
    --radius-chit: 3px 16px 3px 16px;
    --font-display: Georgia, 'Iowan Old Style', Charter, 'Times New Roman', serif;
    --font-body: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    --font-mono: ui-monospace, 'SF Mono', 'Cascadia Mono', Consolas, 'Liberation Mono', monospace;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 32px;
    font: 15px/1.55 var(--font-body);
    background: var(--paper); color: var(--ink);
  }}
  h1 {{ margin: 0 0 4px; font-family: var(--font-display); font-size: 30px; letter-spacing: -.01em; }}
  h2 {{ margin: 32px 0 12px; font-family: var(--font-mono); font-size: 11px;
        text-transform: uppercase; letter-spacing: .12em; color: var(--gold-deep); }}
  .sub {{ margin: 0 0 24px; color: var(--ink-soft); }}
  .mandate {{
    display: inline-block; padding: 8px 14px; margin-bottom: 20px;
    background: var(--paper-card); border: 1px solid var(--paper-border); border-radius: 8px;
    font-size: 13px; color: var(--ink-soft);
  }}
  .mandate b {{ color: var(--ink); }}
  .cards {{ display: flex; flex-wrap: wrap; gap: 12px; }}
  .card {{
    flex: 1 1 150px; background: var(--paper-card); border: 1px solid var(--paper-border);
    border-radius: var(--radius-chit); padding: 16px 18px; position: relative;
  }}
  .card::before {{
    content: ''; position: absolute; top: 0; left: 18px; right: 18px; height: 0;
    border-top: 1.5px dashed var(--paper-border);
  }}
  .card .num {{ font-family: var(--font-display); font-size: 26px; font-weight: 700;
                letter-spacing: -.02em; }}
  .card .lbl {{ font-size: 12px; color: var(--ink-soft); margin-top: 2px; }}
  .wrap {{ overflow-x: auto; background: var(--paper-card);
           border: 1px solid var(--paper-border); border-radius: var(--radius-chit); }}
  table {{ border-collapse: collapse; width: 100%; font-size: 13.5px; }}
  th {{
    text-align: left; padding: 11px 14px; background: #F6F1E5;
    border-bottom: 1px solid var(--paper-border); font-size: 11.5px;
    text-transform: uppercase; letter-spacing: 0.5px; color: var(--ink-soft);
    white-space: nowrap;
  }}
  td {{ padding: 11px 14px; border-bottom: 1px solid var(--paper-border); vertical-align: top; }}
  tr:last-child td {{ border-bottom: none; }}
  .mono {{ font-family: var(--font-mono); font-size: 12px; }}
  /* Every raw technical value reads as one: ids, references, timestamps. */
  td.mono, td .mono, .num-cell {{ font-family: var(--font-mono); }}
  .muted {{ color: #8B7A61; }}
  .nowrap {{ white-space: nowrap; }}
  .num-cell {{ text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }}
  .reason {{ max-width: 400px; color: var(--ink); }}
  .buyer-said, .deliver-to {{
    margin-top: 6px; padding-left: 9px; font-size: 12.5px; line-height: 1.45;
    border-left: 2px solid var(--paper-border); color: var(--ink-soft);
  }}
  .buyer-said {{ border-left-color: var(--gold); }}
  .deliver-to {{ border-left-color: var(--steel); }}
  .who {{
    font-size: 10.5px; font-weight: 700; letter-spacing: .5px;
    text-transform: uppercase; color: var(--ink-soft); margin-right: 4px;
  }}
  /* The stamp: a rubber-stamped verdict, same component as the consoles. */
  .stamp {{
    display: inline-flex; align-items: center; gap: 6px;
    font-family: var(--font-mono); text-transform: uppercase; letter-spacing: .07em;
    font-size: 10.5px; font-weight: 700;
    padding: 4px 11px; border-radius: 999px;
    border: 1.5px solid currentColor;
    transform: rotate(-2.5deg); white-space: nowrap;
  }}
  .stamp.leaf {{ color: var(--leaf); }}
  .stamp.rust {{ color: var(--rust); }}
  .stamp.brick {{ color: var(--brick); }}
  .stamp.steel {{ color: var(--steel); }}
  .stamp.gold {{ color: var(--gold-deep); }}
  .proto {{
    display: inline-block; padding: 2px 8px; border-radius: 5px;
    background: #FAF1E0; border: 1px solid #E7CE9F; color: var(--gold-deep);
    font-family: var(--font-mono); font-size: 10px;
    font-weight: 700; letter-spacing: .07em;
  }}
  .note {{ margin-top: 14px; font-size: 13px; color: var(--ink-soft); }}
</style>
</head>
<body>
  <h1>Amma's Kitchen &mdash; Agent Audit Trail</h1>
  <p class="sub">Every decision this system has made, and whether money moved.</p>

  <div class="mandate">
    Merchant mandate in force &mdash;
    budget cap <b>&#8377;{_mandate.budget_cap_inr}</b> &middot;
    human confirmation at <b>&#8377;{_mandate.human_confirm_threshold_inr}</b> &middot;
    allowed categories <b>{html.escape(", ".join(_mandate.allowed_categories))}</b>
  </div>

  <div class="cards">{stat_cards}</div>

  <h2>Buyer agents &amp; earned trust</h2>
  <div class="wrap">
    <table>
      <thead><tr><th>Agent</th><th>Trust tier</th><th>Completed orders</th></tr></thead>
      <tbody>{tier_rows}</tbody>
    </table>
  </div>
  <p class="note">
    Trust is computed from this system's own audit history &mdash; never self-reported by
    the agent &mdash; and widens only the flexible negotiation margin. The budget cap and
    the human-confirmation threshold are never adjusted by trust.
  </p>

  <h2>Decision log</h2>
  <div class="wrap">
    <table>
      <thead>
        <tr>
          <th>#</th><th>Time (UTC)</th><th>Agent</th><th>Protocol</th>
          <th>Cart</th><th>Total</th><th>Decision</th><th>Reason</th><th>Payment</th><th></th>
        </tr>
      </thead>
      <tbody>{event_rows}</tbody>
    </table>
  </div>
  <p class="note">
    <b>{stats["gated_without_payment"]}</b> decision(s) were gated before any Razorpay call
    was made &mdash; shown above as &ldquo;no Razorpay call made&rdquo;. The same negotiation
    core produced every row, whichever protocol the request arrived on.
  </p>
</body>
</html>"""


@router.get("/audit", response_class=HTMLResponse)
def dashboard(limit: int = 200, refresh: int = 5) -> HTMLResponse:
    db_path = audit_log.DEFAULT_DB_PATH
    events = audit_log.get_all_events(db_path=db_path, limit=limit)
    return HTMLResponse(_render(events, db_path, refresh))


app = FastAPI(title="Amma's Kitchen -- Audit Trail")
app.include_router(router)


@app.get("/", response_class=HTMLResponse)
def _root_alias(limit: int = 200, refresh: int = 5) -> HTMLResponse:
    """Convenience only, for `uvicorn dashboard:app` standalone. On the
    unified server (app.py) the audit trail lives at /audit and `/` is
    the landing page."""
    return dashboard(limit=limit, refresh=refresh)
