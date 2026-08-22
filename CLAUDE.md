# Amma's Kitchen — agentic commerce build (Razorpay AI Buildathon, Track 1)

## What this project is

We are building an AI agent system for a small home-run food business ("Amma's Kitchen")
that lets AI shopping assistants (buyer agents) discover the business, negotiate an order
when the exact request can't be fulfilled, and complete a bounded, auditable payment via
Razorpay's test-mode APIs.

The core idea: **one negotiation brain, reachable through multiple protocol-style adapters.**
The buyer-facing protocol shape (simple confirm/deny vs. structured mandate-style) should
never change the underlying decision logic. Only the translation layer at the edges differs.

## Track and bar we're being judged against

Razorpay AI Buildathon, Track 01 — AI Growth & Agentic Commerce.
"Build an agent that grows revenue for a merchant on Razorpay test-mode APIs, or that
makes a merchant transactable by an AI buyer end to end."

The bar: every money action must be explainable, bounded, and gated. We must show the
audit trail and one failure handled gracefully.

## Architecture

Two independent parties, each with its own limits, each enforced on its own side.
Nothing reaches Razorpay until both have said yes.

```
  human buyer                                              human merchant
      |                                                          |
      v                                                          v
  BUYER SIDE                                              MERCHANT SIDE
  buyer_mandate.py  --cleared-->  adapter_acp.py  \        trust.py
  (customer's own                 adapter_ap2.py   >--> orchestrator.py
   spending limits)               (protocol shape) /          |
      |                                                       v
      +-- refused here: the merchant is                 negotiation.py
          never contacted, and has no                   (APPROVE / COUNTER /
          record of it                                   ESCALATE)
                                                              |
                                                              v
                                                   audit_log.py + Razorpay
                                                              ^
                                                              |
                                            webhook_handler.py / reconcile_payments.py
```

- **Buyer's gate** (`buyer_mandate.py`): the customer's instructions to their shopping
  agent. Pure, deterministic, and run *before* any merchant is contacted. See "Two
  parties, two mandates" below — this distinction matters and was got wrong once.
- **Negotiation core** (`negotiation.py`): pure, deterministic logic. Given a cart request
  and the merchant's mandate (budget cap, allowed categories, current inventory), it
  returns one of: APPROVE, COUNTER_OFFER (with 1-2 alternatives inside a small flexible
  margin), or ESCALATE (needs human confirmation because the gap exceeds the flexible
  margin, or the category isn't allowed at all).
- **IMPORTANT: the LLM never directly authorizes a payment.** The model is used to parse
  natural-language buyer requests into a structured cart proposal, and nothing else. The
  actual APPROVE / COUNTER_OFFER / ESCALATE decision is plain Python, unit-testable, with
  no model call in the loop. This is the single most important design rule in this project
  — do not let a prompt make the financial decision. Both `negotiation.py` and
  `buyer_mandate.py` have tests asserting they import nothing model- or payment-related.
- **Orchestrator** (`orchestrator.py`): shared plumbing every adapter reuses — trust
  lookup, call the core, write the audit event, and (only on approval) create the real
  Razorpay payment. It re-validates the cart at payment time as defense in depth. No
  business logic of its own; it never decides anything.
- **Adapters** (`adapter_acp.py`, `adapter_ap2.py`): thin translation layers. Each takes an
  incoming request in its own shape, converts it into the negotiation core's internal
  request format, calls the core, and formats the response back into that protocol's shape.
  Neither adapter contains any business logic of its own. ACP is modeled on the real
  OpenAI + Stripe protocol (stateful checkout sessions, single-use expiring delegate
  tokens); AP2 on Google's real Intent Mandate -> Cart Mandate -> Payment Mandate chain.
- **Buyer agent simulators** (`buyer_agent_a.py`, `buyer_agent_b.py`): small scripts that
  play the role of an external AI shopping assistant, each speaking through its respective
  adapter. These let us demo agent-to-agent commerce end to end without needing real
  ChatGPT/Gemini integration.
- **Human consoles** (`app.py` + `web/`): the same flows driven by real people instead of
  scripts. `/buyer` and `/merchant` are separate URLs so two humans can each take a side.
- **Razorpay integration** (`razorpay_client.py`, `webhook_handler.py`): real test-mode
  API calls, not mocked. Webhook handling MUST be idempotent — Razorpay delivers webhooks
  with at-least-once semantics, so the same event may arrive more than once. Never
  double-log or double-fulfill.
- **Audit trail** (`audit_log.py`): append-only, human-readable log of every decision —
  what was requested, what was checked, what passed/failed and why, what happened next.
- **Dashboard** (`dashboard.py`): renders the audit trail as a legible HTML table.

## Tech stack

- Python, FastAPI, vanilla JS/CSS for the consoles (no build step, no CDN)
- Claude (`anthropic/claude-sonnet-5`) for NL-to-cart parsing only, reached via
  **OpenRouter's** OpenAI-compatible endpoint (`llm_client.py`). This was a billing
  convenience during the buildathon, not an architectural choice — the model is still
  Claude, and it still only ever proposes a cart.
- SQLite for the audit log and the webhook idempotency ledger
- Razorpay test-mode API keys (Payment Links, Payments, Webhooks)

## Repo structure

```
amma-kitchen-agent/
  app.py                  # THE server: mounts everything, serves both consoles
  web/                    # buyer + merchant consoles (vanilla HTML/CSS/JS)
    index.html  buyer.html  merchant.html  shared.css

  buyer_mandate.py        # BUYER's limits — pure, runs before any merchant is contacted
  mandate.py              # MERCHANT's rules + today's menu (plain data)
  negotiation.py          # pure decision core + suggest_upsell(); no LLM, no I/O
  trust.py                # per-agent trust tier from audit history; widens margin only
  orchestrator.py         # shared plumbing: trust -> core -> audit -> Razorpay

  adapter_acp.py          # ACP-shaped: checkout sessions + delegate tokens
  adapter_ap2.py          # AP2-shaped: Intent -> Cart -> Payment mandate chain
  buyer_agent_a.py        # scripted ACP buyer (Claude parses NL to a cart)
  buyer_agent_b.py        # scripted AP2 buyer
  llm_client.py           # Claude via OpenRouter, forced tool use

  razorpay_client.py      # test-mode payment links / payments
  webhook_handler.py      # idempotent payment_link.paid / expired / cancelled
  idempotency.py          # the claim ledger both the webhook and reconciler share
  reconcile_payments.py   # safety net for webhooks that never arrived
  audit_log.py            # append-only log + queries
  catalog.py              # agent-readable product feed (ACP-style)
  dashboard.py            # audit trail as HTML

  demo.py                 # one-command scripted walkthrough (starts its own servers)
  human_confirm.py / human_reject.py          # merchant CLI, ACP
  human_confirm_ap2.py / human_reject_ap2.py  # merchant CLI, AP2
  simulate_webhook_delivery.py                # send the same webhook twice, locally
  scripts/                # early plumbing probes, kept for reference
  tests/                  # 94 tests; test_negotiation.py matters most
```

## How to run it

```
uvicorn app:app --port 8000     # everything: adapters, webhooks, consoles, audit
```

| Path | What it is |
| --- | --- |
| `/` | landing page + the two-person demo walkthrough |
| `/buyer` | a human plays the customer's AI agent |
| `/merchant` | a human plays Amma, deciding escalations from both protocols |
| `/audit` | the full audit trail |
| `/catalog` | agent-readable product feed (JSON) |
| `/docs` | both protocols' API reference |

`python demo.py` runs the whole story scripted instead, starting and stopping its own
servers on separate ports. `python -m pytest` runs the suite.

Payment testing uses Razorpay's **domestic** test card `4100 2800 0000 1007` (any future
expiry, any CVV, any 4-10 digit OTP). The commonly-quoted `4111 1111 1111 1111` is
rejected as an international card on a default test account.

## Two parties, two mandates (added after the consoles were built)

An early ambiguity worth recording, because it confused us and would confuse a
judge: `mandate.py` holds the **merchant's** rules, not the customer's. Amma's
budget cap and human-confirm threshold are hers — "I won't take agent orders over
Rs.500, and I want to see anything over Rs.400 before I commit to cooking it."
Legitimate, but they are not the customer's spending limits, and for a while they
were the only limits in the system, so an over-budget order went to the *merchant*
for approval when it should have gone to the *buyer's* own human.

`buyer_mandate.py` closes that gap. It is the customer's instructions to their
shopping agent ("never spend over Rs.600; check with me from Rs.300"), it is pure
and deterministic in exactly the way `negotiation.py` is, and it is enforced on the
buyer's side **before any merchant is contacted**. Consequences worth keeping:

- An order over the customer's cap is refused by their own agent. The merchant is
  never called and has no audit record of it — it was never her decision to make.
- There is deliberately no "confirm past the hard cap" path. A customer who wants
  to spend more raises their own cap; they don't approve past it in the moment.
- Neither side defers to the other. A cart can clear the customer's mandate and
  still be refused by the merchant's, or vice versa, or need a human on both sides.
- `buyer_mandate.py` must never import `negotiation.py`, `orchestrator.py`, or
  anything Razorpay — there is a test asserting this on real imports, not mentions.

This also makes AP2's Intent Mandate concept real rather than decorative: the
customer's spending authorization is data that travels with the request.

## The differentiator: Agent Trust Layer + agent-readable growth surface

The buildathon brief is an OR: grow the merchant's revenue, OR make the merchant
transactable by an AI buyer end to end. The negotiation core + adapters answer the
second half. To also answer the first half, and to stand out from teams that only
build two toy protocol shapes, we added three small, self-contained pieces:

- **`audit_log.py`** — SQLite-backed append-only event log (brought forward from
  original step 7). Every negotiation decision is recorded here: agent id, protocol,
  cart, decision, reason, total, payment id (filled in once Razorpay confirms).
- **`trust.py`** — the actual differentiator. NPCI's Unified Agent Protocol (UAP) is
  a real, still-unlaunched framework aiming to let merchants safely authenticate and
  authorize AI agents over UPI, starting with exactly our use case (low-value,
  high-frequency food/grocery orders). `trust.py` is a small working preview of that
  problem: every buyer agent gets a trust tier (NEW / STANDARD / TRUSTED) computed
  purely from this system's own audit history (never self-reported by the agent).
  Trust tier widens only the *flexible negotiation margin* (5% -> 10% -> 15%). It
  never touches the budget cap or the human-confirm threshold — those are the
  merchant's absolute limits and stay fixed regardless of trust. A single disallowed-
  category attempt resets an agent straight back to NEW. Critically, `trust.py` reads
  `audit_log` and produces an adjusted `Mandate`; it never imports or calls into
  `negotiation.py`, and `negotiation.py` has zero knowledge trust exists. The pure
  decision core is unmodified by this feature.
- **`negotiation.suggest_upsell()`** — a separate, optional, non-blocking pure
  function (not part of `evaluate()`) that, given an already-APPROVED cart, suggests
  at most one add-on item that keeps the order strictly below the human-confirm
  threshold. Never influences the APPROVE/COUNTER_OFFER/ESCALATE decision itself.
  This is the revenue-growth lever, and it's designed to be gated by trust tier later
  (e.g. only offer upsells to STANDARD+ agents) without any change to `evaluate()`.
- **`catalog.py`** — a small FastAPI app exposing `GET /catalog`: a structured,
  machine-fetchable product feed modeled loosely on the real Agentic Commerce
  Protocol's (ACP, OpenAI + Stripe) product-feed shape. It also publishes the
  merchant's own order limits (budget cap, human-confirm threshold, allowed
  categories) so a well-behaved buyer agent can self-limit its request before ever
  hitting the negotiation core, instead of wasting round-trips on requests that were
  always going to fail. This is the concrete answer to "agent-readable catalog" from
  the brief's example directions, and to "makes a merchant sellable to AI buyers."

Two further pieces were added while building steps 6-7, both worth calling out:

- **`idempotency.py` + `reconcile_payments.py`** — webhooks are the fast path for
  learning a payment completed, but they can be missed (server down, tunnel closed,
  every retry failed). Real payment systems keep a reconciliation job as the safety
  net, so this one does too: it asks Razorpay directly about any order that has a
  payment link but no recorded payment. Critically, the webhook handler and the
  reconciler claim through the SAME idempotency ledger, so the two independent paths
  to the same fact can never double-record it. This was not theoretical — running it
  for real recovered two genuinely-paid orders whose webhooks predated the webhook
  being configured at all.
- **`dashboard.py`** — renders the audit trail with the merchant mandate stated at the
  top, per-agent trust tiers, and an explicit "no Razorpay call made" marker on every
  gated decision, so a viewer can *verify* the "rejected orders never touched money"
  claim at a glance instead of taking it on trust. Auto-refreshes, so buyer agents can
  be run in another terminal and rows appear live on camera.

Pitch line: *"We built the trust layer NPCI hasn't shipped yet — and we did it
without touching the file that makes the actual money decision."*

Both adapters were subsequently built spec-accurate to the real named protocols in the
brief's "why now" line, rather than as invented shapes: ACP with a product feed,
stateful checkout sessions and single-use delegate tokens; AP2 with Google's real
Intent -> Cart -> Payment mandate chain. Adding AP2 required **zero** changes to
`negotiation.py` or `orchestrator.py` — `git log` shows that commit adding only new
files, and a test asserts both adapters share the identical orchestrator module object.

**Still unbuilt, and the strongest remaining idea:** a third x402-style adapter. x402
(Coinbase) is the most-used agentic payment protocol by volume and uses the literal
HTTP 402 status code — the server replies `402 Payment Required` with a price, and the
agent retries carrying a signed payment proof. Implementing that challenge/response
flow but settling via **Razorpay test-mode instead of stablecoins** would show the same
negotiation core bridging a Web3-native agent payment UX onto India's real payment
rails. Three protocols judges recognize by name, one unchanged brain.

## Build order — status

Each phase had to work before the next was started. Steps 1-8 are done; only the pitch
itself remains.

1. **DONE** — Scope lock: mandate schema and menu written into `mandate.py` as plain data.
2. **DONE** — Real Razorpay test-mode payment for one hardcoded cart, proving the plumbing
   before adding any intelligence.
3. **DONE** — Negotiation core as pure functions, unit tested before being wired to
   anything. Still the highest-value part of the project.
4. **DONE** — Adapter A (ACP) + buyer agent A, wired end to end to a real payment.
5. **DONE** — Adapter B (AP2) + buyer agent B, serving a structurally different request
   shape with zero changes to `negotiation.py`.
6. **DONE** — Idempotent webhook handling, enforced by a DB-level UNIQUE constraint rather
   than a check-then-write. Verified with duplicate deliveries, and with real Razorpay
   deliveries over an ngrok tunnel.
7. **DONE** — Audit trail dashboard, plus `reconcile_payments.py` as the safety net for
   webhooks that never arrive.
8. **DONE** — Deliberate failure demo. Required adding `party_catering_tray` to the menu:
   before it, every category was allowed, so the category rule could never actually fire
   and the "failure" had to be faked with an unknown item. At Rs.350 it sits under both
   the budget cap and the confirm threshold and is in stock, so the *only* thing refusing
   it is its category — which makes the demo unambiguous. A test asserts Razorpay is never
   called for it, rather than leaving that to narration.
9. **REMAINING** — Polish + record the 5-minute pitch video.

Built beyond the original plan: the two-sided mandate model, the agent trust layer, the
upsell hook, the agent-readable catalog, payment reconciliation, the one-command
`demo.py`, and the two human web consoles.

## What "done" looks like for the pitch

- Live demo: buyer agent A orders successfully. Buyer agent B, using a completely
  different message shape, orders successfully through the SAME negotiation core.
- Live demo: a request that breaks the mandate gets rejected before any Razorpay call is
  made, with a clear logged reason.
- The audit trail is shown on screen, human-readable, not a raw log dump.
- The pitch explains the actual insight in one line: the intelligence is protocol-agnostic;
  only the adapters are protocol-specific.

Two beats worth adding to that list, both now demonstrable live with two people:

- An order stopped by the **buyer's own agent** before the merchant is ever contacted —
  showing bounded autonomy is not just something merchants impose on agents.
- An escalation appearing in the merchant's queue and, on approval, the buyer's screen
  **unblocking itself** — the handoff between two humans and two agents, in one shot.
