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

```
Buyer agent A (ACP-style, flat request)   \
                                            >--> Adapter --> Negotiation core --> Audit + Razorpay test API
Buyer agent B (AP2-style, mandate object) /
```

- **Negotiation core** (`negotiation.py`): pure, deterministic logic. Given a cart request
  and the merchant's mandate (budget cap, allowed categories, current inventory), it
  returns one of: APPROVE, COUNTER_OFFER (with 1-2 alternatives inside a small flexible
  margin), or ESCALATE (needs human confirmation because the gap exceeds the flexible
  margin, or the category isn't allowed at all).
- **IMPORTANT: the LLM never directly authorizes a payment.** The LLM (Claude, via the
  Messages API with tool use) is used to parse natural-language buyer requests into a
  structured cart proposal, and to phrase responses back in natural language. The actual
  APPROVE / COUNTER_OFFER / ESCALATE decision is plain Python, unit-testable, with no model
  call in the loop. This is the single most important design rule in this project — do not
  let a prompt make the financial decision.
- **Adapters** (`adapter_acp.py`, `adapter_ap2.py`): thin translation layers. Each takes an
  incoming request in its own shape, converts it into the negotiation core's internal
  request format, calls the core, and formats the response back into that protocol's shape.
  Neither adapter contains any business logic of its own.
- **Buyer agent simulators** (`buyer_agent_a.py`, `buyer_agent_b.py`): small scripts (can
  use Claude too) that play the role of an external AI shopping assistant, each speaking
  through its respective adapter. These let us demo agent-to-agent commerce end to end
  without needing real ChatGPT/Gemini integration.
- **Razorpay integration** (`razorpay_client.py`, `webhook_handler.py`): real test-mode
  Orders API and Payments API calls, not mocked. Webhook handling MUST be idempotent —
  Razorpay delivers webhooks with at-least-once semantics, so the same `payment.captured`
  event may arrive more than once. Never double-log or double-fulfill.
- **Audit trail** (`audit_log.py`): append-only, human-readable log of every decision —
  what was requested, what was checked, what passed/failed and why, what happened next.
  This is shown live in the demo video.
- **Dashboard** (`dashboard.py`): one FastAPI route rendering the audit trail as a simple
  HTML table. Not fancy — legible on camera is the only requirement.

## Tech stack

- Python, FastAPI
- Claude API (Messages API + tool use) for NL parsing and response phrasing only
- SQLite for mandate config + audit log
- Razorpay test-mode API keys (Orders API, Payments API, Webhooks)

## Repo structure

```
amma-kitchen-agent/
  negotiation.py         # pure decision logic, unit tested, no LLM calls
  mandate.py              # mandate schema + loading (budget, categories, thresholds)
  adapter_acp.py            # thin ACP-style translation layer
  adapter_ap2.py              # thin AP2-style (mandate object) translation layer
  buyer_agent_a.py               # simulated ACP-style buyer
  buyer_agent_b.py                 # simulated AP2-style buyer
  razorpay_client.py                 # test-mode order/payment calls
  webhook_handler.py                   # idempotent payment.captured / failed handling
  audit_log.py                           # append-only log + query
  dashboard.py                             # FastAPI route rendering the audit trail
  tests/
    test_negotiation.py                      # the tests that actually matter most
  README.md
```

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

Planned next (not yet built): reshape the two protocol adapters to be spec-accurate
to the real named protocols in the brief's "why now" line, and add a third. ACP-style
adapter should mirror real ACP's product-feed + stateful checkout + delegate-token
shape. AP2-style adapter should mirror Google's real Intent Mandate -> Cart Mandate
-> Payment Mandate chain. A third, x402-style adapter would simulate the HTTP 402
challenge/response flow (server replies 402 Payment Required with a price, buyer
agent retries with a payment proof) but settle via Razorpay test-mode instead of
stablecoins — demonstrating the same negotiation core bridging a Web3-native agent
payment UX to India's real payment rails. This proves genuine protocol-agnosticism
against three protocols judges will actually recognize by name, not two invented ones.

## Build order (do NOT skip ahead — each phase should work before starting the next)

1. **Scope lock**: confirm the mandate schema (budget cap, category allow-list, human
   confirm threshold, flexible negotiation margin) and today's menu/inventory for Amma's
   Kitchen. Write it down in `mandate.py` as plain data, not prose.
2. **Catalog + real Razorpay test-mode payment, no guardrails yet**: get one hardcoded cart
   successfully paid through Razorpay's real test-mode API. Prove the plumbing works before
   adding intelligence.
3. **Negotiation core**: implement APPROVE / COUNTER_OFFER / ESCALATE as pure functions.
   Write unit tests for this before wiring it to anything else — this is the highest-value
   part of the whole project.
4. **Adapter A (ACP-style) + buyer agent A**: wire the simplest protocol shape first.
5. **Adapter B (AP2-style) + buyer agent B**: prove the same negotiation core serves a
   structurally different request shape with zero changes to negotiation.py.
6. **Webhook handling with idempotency**: this is the real, non-obvious hard part. Test by
   deliberately sending the same webhook event twice.
7. **Audit trail dashboard**: make it presentable, not just correct.
8. **Deliberate failure demo**: trigger a real rejection (over-budget or disallowed
   category) end to end and confirm it's logged with a clear reason, and that no payment
   call was ever made for it.
9. **Polish + record the 5-minute pitch video.**

## What "done" looks like for the pitch

- Live demo: buyer agent A orders successfully. Buyer agent B, using a completely
  different message shape, orders successfully through the SAME negotiation core.
- Live demo: a request that breaks the mandate gets rejected before any Razorpay call is
  made, with a clear logged reason.
- The audit trail is shown on screen, human-readable, not a raw log dump.
- The pitch explains the actual insight in one line: the intelligence is protocol-agnostic;
  only the adapters are protocol-specific.
