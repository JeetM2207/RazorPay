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

_DECISION_STYLES = {
    "APPROVE": ("#0b7a3b", "#e6f6ec", "APPROVE"),
    "COUNTER_OFFER": ("#8a5a00", "#fdf3e0", "COUNTER OFFER"),
    "ESCALATE": ("#9a4a00", "#fdeee0", "ESCALATE"),
    "REJECTED": ("#a01b2b", "#fdeaec", "REJECTED (human)"),
    "PAYMENT_NOT_COMPLETED": ("#4a4a52", "#eeeef1", "PAYMENT NOT COMPLETED"),
}

_TIER_STYLES = {
    "NEW": ("#4a4a52", "#eeeef1"),
    "STANDARD": ("#0b5f9a", "#e5f1fa"),
    "TRUSTED": ("#0b7a3b", "#e6f6ec"),
}


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
    fg, bg, label = _DECISION_STYLES.get(decision, ("#4a4a52", "#eeeef1", decision))
    return f"<span class='badge' style='color:{fg};background:{bg}'>{html.escape(label)}</span>"


def _tier_badge(tier: str) -> str:
    fg, bg = _TIER_STYLES.get(tier, ("#4a4a52", "#eeeef1"))
    return f"<span class='badge' style='color:{fg};background:{bg}'>{html.escape(tier)}</span>"


def _payment_cell(event: dict) -> str:
    if event["payment_id"]:
        # A `sim_` reference was asserted by us, not settled by Razorpay.
        # It must never render the same as money that actually moved.
        simulated = event["payment_id"].startswith("sim_")
        label = "SIMULATED" if simulated else "PAID"
        cls = "pending" if simulated else "paid"
        return (
            f"<span class='{cls}'>{label}</span>"
            f"<div class='mono muted'>{html.escape(event['payment_id'])}</div>"
        )
    if event["payment_link_id"]:
        return (
            "<span class='pending'>awaiting payment</span>"
            f"<div class='mono muted'>{html.escape(event['payment_link_id'])}</div>"
        )
    if event["decision"] in _NON_PAYING_DECISIONS:
        return "<span class='none'>no Razorpay call made</span>"
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
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 32px;
    font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #f6f7f9; color: #1a1a1f;
  }}
  h1 {{ margin: 0 0 4px; font-size: 26px; letter-spacing: -0.3px; }}
  h2 {{ margin: 32px 0 12px; font-size: 15px; text-transform: uppercase;
        letter-spacing: 0.6px; color: #5a5a66; }}
  .sub {{ margin: 0 0 24px; color: #5a5a66; }}
  .mandate {{
    display: inline-block; padding: 8px 14px; margin-bottom: 20px;
    background: #fff; border: 1px solid #e2e4e9; border-radius: 8px;
    font-size: 13px; color: #4a4a52;
  }}
  .mandate b {{ color: #1a1a1f; }}
  .cards {{ display: flex; flex-wrap: wrap; gap: 12px; }}
  .card {{
    flex: 1 1 150px; background: #fff; border: 1px solid #e2e4e9;
    border-radius: 10px; padding: 16px 18px;
  }}
  .card .num {{ font-size: 24px; font-weight: 650; letter-spacing: -0.5px; }}
  .card .lbl {{ font-size: 12px; color: #5a5a66; margin-top: 2px; }}
  .wrap {{ overflow-x: auto; background: #fff;
           border: 1px solid #e2e4e9; border-radius: 10px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 13.5px; }}
  th {{
    text-align: left; padding: 11px 14px; background: #fafbfc;
    border-bottom: 1px solid #e2e4e9; font-size: 11.5px;
    text-transform: uppercase; letter-spacing: 0.5px; color: #5a5a66;
    white-space: nowrap;
  }}
  td {{ padding: 11px 14px; border-bottom: 1px solid #f0f1f4; vertical-align: top; }}
  tr:last-child td {{ border-bottom: none; }}
  .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
           font-size: 12px; }}
  .muted {{ color: #8a8a96; }}
  .nowrap {{ white-space: nowrap; }}
  .num-cell {{ text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }}
  .reason {{ max-width: 400px; color: #3a3a44; }}
  .buyer-said, .deliver-to {{
    margin-top: 6px; padding-left: 9px; font-size: 12.5px; line-height: 1.45;
    border-left: 2px solid #d8d5cd; color: #55524c;
  }}
  .buyer-said {{ border-left-color: #b9a6e0; }}
  .deliver-to {{ border-left-color: #a8c6dd; }}
  .who {{
    font-size: 10.5px; font-weight: 700; letter-spacing: .5px;
    text-transform: uppercase; color: #8a857c; margin-right: 4px;
  }}
  .badge {{
    display: inline-block; padding: 3px 9px; border-radius: 20px;
    font-size: 11.5px; font-weight: 600; white-space: nowrap;
  }}
  .proto {{
    display: inline-block; padding: 3px 8px; border-radius: 5px;
    background: #eef0f4; color: #4a4a52; font-size: 11px;
    font-weight: 650; letter-spacing: 0.4px;
  }}
  .paid {{ color: #0b7a3b; font-weight: 650; font-size: 12px; }}
  .pending {{ color: #8a5a00; font-size: 12px; }}
  .none {{ color: #a01b2b; font-size: 12px; font-weight: 600; }}
  .note {{ margin-top: 14px; font-size: 13px; color: #5a5a66; }}
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
          <th>Cart</th><th>Total</th><th>Decision</th><th>Reason</th><th>Payment</th>
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
