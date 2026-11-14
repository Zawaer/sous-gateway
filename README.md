# Sous

Sous is Valio Aimo's front door for AI purchasing agents. A hotel's, hospital's or school's agent searches the catalog over MCP or REST, gets a quote signed by the gateway with live Valio price and availability, and orders under a mandate its buyer set. **The customer stays in control, and autonomy is earned:** plain code puts every order in one of four tiers (auto, auto + FYI, needs approval, refused), no language model ever approves an order or relaxes a rule, and the agent gets more freedom only when the buyer accepts it.

Built at Junction 2026 for Valio Aimo's challenge "Trusted Trade Agents".

**The flow, in Valio's four steps:**

1. **Request.** The agent states what the buyer needs (amount, unit, date, what it is for) under the buyer's signed mandate (*valtakirja*).
2. **Lookup.** Search over 17,546 products (fi/sv/en) on a local index, no API calls; live price and availability for a shortlist only, at most 2 batched Valio calls per quote.
3. **Order.** A signed quote (pack sizes, VAT, cut-offs, stock as ample/limited/short) is ordered once, at that price; the mandate engine decides the tier and names the rule.
4. **Approval.** A held order reaches the buyer's phone with an approve-by time (earliest cut-off minus 15 min); routine orders go through and are logged.

**What keeps the buyer in control:**

- **Only the buyer approves.** Held orders are decided on the buyer side; agents get 403 (audited), and Valio staff see them read-only.
- **No agent can become its own approver.** Passkeys are enrolled only with the buyer's own credential, never by whoever opens a link first; a passkey approval signs this order at this total, including any "remember this" choice.
- **Autonomy is earned and can be taken back.** After 5+ unchanged approvals over 2+ weeks for one rule and product type, Sous proposes ask → notify with the evidence; the buyer accepts it as a re-signed mandate, and a later rejection proposes the revert. Never allergens or spend limits ([PROTOCOL 7.2](docs/PROTOCOL.md)).
- **The right product for its use.** Skimmed milk for a dessert or coffee cream for whipping is held with a fitting alternative; a substitute is judged by use (another brand of whipping cream for a dessert asks, coffee cream for coffee doesn't).
- **Needs change mid-contract.** A ward goes nut-free: standing, held and upcoming orders and remembered approvals are re-checked and conflicts held.
- **Buyer controls.** Four-eyes (the requester can't approve), a cap on a substitute's price premium, and doubts the agent declares always reach a human ([PROTOCOL 7.3](docs/PROTOCOL.md)).
- **A record both sides can check.** Every rule at `/rules` (en/fi), signed quotes and receipts (Ed25519 JWS), a hash-chained audit log plus MariaDB system versioning: what the mandate said when each order was decided.

**Try it:** watch agents order live at [135-181-8-209.sslip.io](https://135-181-8-209.sslip.io/) · your own sandbox with ready prompts in 5 minutes: [docs/pitch/try-it.md](docs/pitch/try-it.md) · approve as the buyer: `/ui/buyer.html` · try seven attacks: `/ui/break.html` · connect Claude, ChatGPT or any MCP client: [docs/CONNECT.md](docs/CONNECT.md) · honest numbers, held-out set included: [docs/STATUS.md](docs/STATUS.md#evaluation).

Download a verification bundle from an order in the reference buyer, then verify it with independently trusted public keys (Node 22+, no dependencies):

```sh
node scripts/verify_buyer_bundle.mjs order-proof.json trusted-jwks.json
node scripts/verify_buyer_bundle.mjs --tamper order-proof.json trusted-jwks.json
```

The second command demonstrates rejection of an altered receipt and preserves the saved proof.

## Judge path in 5 minutes

The demo server is production, `https://135-181-8-209.sslip.io`, set as `SOUS` below. It is redeployed from `main` until the Sun 15.11 08:00 Helsinki freeze. Staging, `https://staging.135-181-63-134.sslip.io`, is the fallback if production is ever down and usually runs newer code (on Sat 14.11 14:11Z `/health` showed prod at a431ff6, staging at ea4cd19); [docs/STATUS.md](docs/STATUS.md#deployed-now) says what runs where. Needs `curl` and `jq`.

**Dates:** Valio's mock gives Stock products a cut-off of Sun 15.11 17:00 for Monday delivery. After that the quickstart's Monday date is refused (`availability.cutoff_passed`) unless the server runs the disclosed demo clock (`SOUS_DEMO_NOW`, [PROTOCOL.md section 21](docs/PROTOCOL.md); shown in `/health` as `clock.mode`) or you ask for a later date.

1. **Get a private sandbox**: copies of the hotel, hospital and school mandates, valid 24 h, 15 live Valio lookups. The two tokens in the response are shown once.
   ```bash
   export SOUS=https://135-181-8-209.sslip.io
   curl -s -X POST $SOUS/sandbox | tee sandbox.json | jq '{sandbox, mandates: [.mandates[] | {mandate_id, rules}]}'
   ```
2. **Run the quickstart**: the response's `quickstart.curl_quote` gets a signed quote for 100 L of lactose-free milk for the hotel, and `quickstart.curl_order` orders it.
   ```bash
   Q=$(jq -r .quickstart.curl_quote sandbox.json | sh | jq -r .quote.id)
   jq -r .quickstart.curl_order sandbox.json | sed "s/<quote.id>/$Q/g" | sh | jq 'if .error then . else {id, status, tier: .decision.outcome, rules: [.decision.findings[].rule]} end'
   ```
   Within the hotel's limits it is `ordered`, tier `silent` (auto), rule `mandate.ok`. Every finding carries a `rule_url`. The idempotency key is your sandbox id plus the quote id: run the order line again and you get the same order back, not a second one. Keys are scoped per agent, so no two judges clash.
3. **Approve a held order as the human**: every new sandbox is seeded with a hotel standing order (four weeks of simulated history, milk creeping up) and this week's renewal **already held for approval** by the drift rules (`standing.quantity_creep`, `standing.quantity_trend`, `standing.cost_drift`); `seeded` in the response names it. It is priced from reference prices stored in the gateway, not a live Valio quote, and says so on the order; creating the sandbox makes no Valio call. Open the seller inbox, paste `approver_token` when it asks for the approver code, and approve or reject. The token works only on your sandbox's orders.
   ```bash
   jq '.seeded | {held_order_id, why, reasons}' sandbox.json
   jq -r .quickstart.seller_ui sandbox.json     # $SOUS/ui/?sandbox=SBX-....#/inbox
   ```
4. **Check the rules and the audit chain**:
   ```bash
   curl -s $SOUS/rules | jq '.tiers, .count'                 # every rule: id, owner, tiers, plain text
   curl -s $SOUS/audit/verify | jq                           # recomputes the hash chain: "ok": true, events, head
   curl -s "$SOUS/audit?sandbox=$(jq -r .sandbox.id sandbox.json)&limit=5" \
     -H "Authorization: Bearer $(jq -r .agent_token sandbox.json)" | jq '.events[] | {seq, event, hash, prev_hash}'
   ```
5. **Connect an MCP client** to `$SOUS/mcp` with the agent token, then paste one of the four `prompts`. Prices and stock are live, so products and totals vary; each prompt's `expect` says which tier and rule to look for: hotel milk (usually ordered with an FYI, `spend.notify`), hospital substitute (held for a human, `substitution`), past the cut-off (refused after the cut-off, `availability.cutoff_passed`), school over budget (refused above €1,500, `spend.hard_limit`).
   ```bash
   jq -r .quickstart.claude_code sandbox.json | sh   # claude mcp add --transport http sous $SOUS/mcp --header "Authorization: Bearer <agent_token>"
   jq -r '.prompts[] | .title, .prompt, ""' sandbox.json
   ```
   **From claude.ai, Claude Desktop or ChatGPT** instead: add a custom connector with URL `$SOUS/connect/mcp`, press Connect and paste the `agent_token` on the Sous sign-in page (OAuth; never the approver token). Cursor, VS Code and the details: [docs/CONNECT.md](docs/CONNECT.md).

Evaluation, as [docs/STATUS.md](docs/STATUS.md#evaluation) states it. **Held-out** ([LLM_EVAL.md](docs/LLM_EVAL.md#held-out), CI 43620135327): 15 buyer requests written blind before reading the code, Gemini 4 Hydrogen + Sous agent, 3 runs each: **0 false auto-approvals**, 9/9 unsafe requests stopped (all by the gateway, the agent placed every one), but only 3/36 should-order runs matched the need (one item per request, headcounts read as pack counts); fixes in progress, a second frozen set of 20 waits for the re-run. The rest are our own cases. From one full CI run on CRACI, [43587725624](https://github.com/Zawaer/sous/actions/runs/43587725624) on 11d8448: scripted cases (no LLM, live Valio prices) **60/60**, 43/43 unsafe actions blocked or escalated; LLM buyer agent on 48 one-sentence cases: Kimi K3.1 Pro (Verda Inference) 47/48, Gemini 4 Hydrogen 46/48, Nemotron 3.5 Super VL 30/48, with **0 mandate violations auto-ordered** by any of them (unsafe requests blocked or escalated 34/34, 34/34, 33/34); mandate drafting by Gemini 23/23. Conformance kit 17/17 on staging ([CONFORMANCE.md](docs/CONFORMANCE.md)); check-the-checker catches 47/47 planted violations ([EVAL_INTEGRITY.md](docs/EVAL_INTEGRITY.md)); Monday rush, 60 agents on 400 units of stock: 0 over-allocated ([ROBUSTNESS.md](docs/ROBUSTNESS.md)). Recipe planner, 7 menus (CI [43595658113](https://github.com/Zawaer/sous/actions/runs/43595658113), after the R05 crash fix): recipe table 7/7, Gemini 5/7, Kimi 5/7, every miss a quantity outside our 35 % bar. Red team on a Verda GPU ([REDTEAM.md](docs/REDTEAM.md), 51b139d): 2,000 generated requests through a self-hosted Qwen3-30B-A3B (vLLM), 976/997 unsafe held or refused; it found 3 order-slicing misses in the gateway (demo mandates had the split window off), fixed with a 60-minute split window (c71bdde; Gemma 4 re-run on 100 slicing requests: 0 gateway violations). Spend so far: $38.26 known ([COSTS.md](docs/COSTS.md)).

## Valio's brief → where it is in Sous

| Valio's brief | In Sous | Number or link |
|---|---|---|
| **Flow** Request → Lookup → Order → Approval | `POST /sandbox` quickstart above: search → `POST /quotes` → `POST /orders` → `/ui/approve.html` | CI eval: 60/60 scripted cases end to end ([STATUS](docs/STATUS.md#evaluation)) |
| **1. Lookup**: structured search of products, prices, availability | `GET /products` / MCP `search_products` (local index, 0 API calls); live price + availability only on the quote | 17,546 products fi/sv/en; avg **0.47** Valio calls per request, max 2 (CI 43587725624) |
| **2. Request and order** at a fixed price | `POST /quotes` returns an Ed25519-signed quote with an expiry; `POST /orders` accepts only that signed payload, once | 20 parallel orders on one quote → 1 ordered, 19 rejected |
| **3. Permissions and approval**: agent alone vs human sign-off | Signed mandate → tiers silent / notify / ask / block, every rule at `GET /rules`; optional buyer controls: a cap on a substitute's price premium over the product it replaces, the agent's own declared doubts always reach a human, and four-eyes (`separate_approver`: the requester can't approve; PROTOCOL 7.3); the buyer's humans approve on the buyer side (`/ui/buyer.html`, or the passkey page `/ui/approve.html`); Valio's seller side (`/ui/`) sees held orders read-only, agents get 403; **earned autonomy**: after a clean approval record per rule and product type (≥5 unchanged approvals over ≥2 weeks, none edited or rejected) Sous proposes ask → notify, the buyer accepts it as a re-signed mandate, and a later rejection proposes taking it back (`GET /mandates/{id}/autonomy`, PROTOCOL 7.2) | 43/43 unsafe blocked or escalated; red team 976/997 ([REDTEAM.md](docs/REDTEAM.md)) |
| **Bonus**: more than price | Quote lines carry delivery reliability, labels/certifications, CO₂ estimate (range across options), origin | low reliability (< 90 %) is flagged on the line |
| **Bonus**: exceptions | Partial availability → signed options (part now + rest later, later date, substitute), type-aware substitutes, **use-aware substitution** (a substitute is judged for what the product is used for: another brand of whipping cream for a dessert asks a person, coffee cream for coffee doesn't; rule `substitution.use_sensitive`), post-order amendments | eval cases O01–O06; [PROTOCOL 5.1, 5.1.1](docs/PROTOCOL.md); tests/test_use_sensitivity.py |
| **Bonus**: exceptions, the buyer's needs change | A ward goes nut-free mid-contract: the mandate update re-checks standing orders, held and upcoming orders and remembered approvals; conflicts are held with a reason (`needs.changed`), unsafe remembered approvals revoked, a safe substitute proposed, one fi/en summary; loosening expands nothing | tests/test_needs.py; [PROTOCOL 8.1](docs/PROTOCOL.md#81-needs-changed-mid-contract-sousneedspy) |
| **Bonus**: explain decisions | Every finding has a rule id, plain-language reason and `rule_url`; hash-chained audit at `GET /audit` (`/audit/verify`); reasons also in Finnish (`/rules?lang=fi`) | `/audit/verify` ok on prod at a431ff6 (365 events, 13:09Z) |
| **Good to know**: not a chatbot; query APIs only when needed | MCP/OpenAPI for any agent; Valio is called only for a quote's shortlist, cached, with a shared budget (Aiven Valkey) | 0.47 calls/request; never a full-catalog fetch |
| **Good to know**: not production-ready in 48 h | Demo-grade identity, list prices, ERP hand-off stub labelled simulated | [Known limits](docs/STATUS.md#known-limits) |

## Where to read more

- [docs/pitch/try-it.md](docs/pitch/try-it.md): the sandbox walk-through as a one-page handout.
- [docs/STATUS.md](docs/STATUS.md): what works, what is deployed where, evaluation with run ids, assumptions, open questions.
- [docs/PROTOCOL.md](docs/PROTOCOL.md): the full protocol. Start with [6. Tiers and rules](docs/PROTOCOL.md#6-tiers-and-rules), [12. Audit log](docs/PROTOCOL.md#12-audit-log) and [18. Judge sandbox](docs/PROTOCOL.md#18-judge-sandbox).
- [docs/DECISIONS.md](docs/DECISIONS.md): technical choices and the path to production.
- [docs/INTEGRATION.md](docs/INTEGRATION.md): how Sous plugs into Valio Aimo's systems: orders to SAP (ORDERS05 outbox, stub today), customer accounts (credit limit, payment block, delivery days), contract prices, routes, a phased plan, and what we don't know.
- [docs/CONFORMANCE.md](docs/CONFORMANCE.md): one command (`scripts/conformance.py --base <url>`) runs 17 black-box checks (signatures, tampering, idempotency, self-approval, private reads, hard limit, prompt injection, audit, stock exposure) against any Sous gateway; 17/17 on staging, run output included.
- [docs/STANDARDS.md](docs/STANDARDS.md): mapping to AP2, ACP, A2A, MCP server cards, schema.org, W3C VC 2.0, JOSE, and Peppol BIS Ordering 3.0 (UBL Order and Order Response per order).
- [docs/EVAL_INTEGRITY.md](docs/EVAL_INTEGRITY.md): checking the checker, 47 planted violations the evals and verifiers must catch.
- [docs/ROBUSTNESS.md](docs/ROBUSTNESS.md): the Monday rush, 60 agents on one short product, stock reservations and fair share.
- [docs/CAPACITY.md](docs/CAPACITY.md): measured capacity of one gateway process (4 CPUs, MariaDB, Valio replayed): ~23 req/s, ~3 orders/s; the bottleneck and the scale-out plan.
- [docs/CRACI.md](docs/CRACI.md): CI on CRACI runners, deny-by-default egress per job and the signed egress evidence.
- [docs/COSTS.md](docs/COSTS.md): every paid model run and running service.
- [docs/LLM_EVAL.md](docs/LLM_EVAL.md) (per-model LLM eval, case by case), [docs/CI.md](docs/CI.md), [docs/VERDA.md](docs/VERDA.md), [docs/GEMINI.md](docs/GEMINI.md), [docs/NEMOTRON.md](docs/NEMOTRON.md), [docs/AIVEN.md](docs/AIVEN.md), [docs/demo-runbook.md](docs/demo-runbook.md).
- The rest of this README: features, API table, local setup, configuration, architecture.

## What it does

- **Search** 17,546 products in Finnish, Swedish and English, with filters for claims, allergens, labels, temperature and country of origin. Search uses a local index and costs no Valio API calls. Type-aware ranking (6e547ce): 'laktoositon maito' ranks plain lactose-free milk above coffee milk; precision@3 90.5 % → 99.3 % on our own 49 tuning queries.
- **Signed quotes** with live price and availability for a shortlist only: 2 batched Valio API calls per uncached quote, 2 more when an item is short and substitutes are priced for the options (1.0 on average with the cache in `evals/results.json`). Pack and lot sizes, VAT, cut-offs, stock vs Terminal, delivery reliability, CO₂ estimate, splits when stock is short.
- **Mandate engine** in plain code. Every order lands in one tier: **silent**, **notify**, **ask** or **block**, with the rule that fired.
- **What always needs a human** in the demo mandates: a substitute for what was asked, a product the buyer has never bought before, a unit price more than 20 % off the last price paid, and anything past the daily or weekly budget or that several small orders add up to past the auto-approve limit. **Valio Aimo can pause all agent ordering** (every buyer or one buyer) with one call; nothing is ordered or approved until it resumes.
- **Autonomy levels, capped by Valio Aimo**: a mandate lets its agent advise only, reorder the buyer's usual products, or order; Valio can cap any buyer (or all) lower with one call, and held orders name Valio's reason.
- **Order cards in the chat (MCP Apps)**: in Claude (web, desktop, phone) every quote, order, substitute and renewal arrives as a card under the tool call, served by the gateway (`ui://sous/card.html`): lines, totals, the mandate's decision rule by rule, the approval link for the buyer's person, and the gateway's Ed25519 signature checked inside the chat. Without Claude: `/ui/cards.html` draws real demo orders with the same card.
- **Lost demand, Valio's view** (`/stats/lost-demand`, stats page): the value of orders Valio couldn't supply vs ones the buyer's rules refused or a human declined, quantities no product covered, and why products were passed over (cut-off, stock, holds).
- **Peppol BIS Ordering 3.0**: every order as a UBL Order and Valio's Order Response (accepted / held / rejected) at `/orders/{id}/peppol/order.xml` and `/order-response.xml`, for buyers' ERP and e-procurement systems. Validated against the official UBL 2.1 XSD and OpenPeppol BIS schematron in CI (2d2d6a5).
- **Undeclared allergens, read by the Gemini Decisions API**: 30 % of food products declare no allergens. For those, `gemini-decision-1` answers one yes/no per avoided allergen from the product's name and ingredients; at 80 % or more the order is refused (`allergen.likely`). Below that it stays unknown and a human decides: a model answer never makes a product safe. Accuracy on products whose allergens are known is on `/proof`.
- **Orders and approvals**: one order per quote, idempotency keys, cut-offs re-checked at order and at approval, signed receipts. A human approves or rejects from a phone link before an approve-by time: the earliest cut-off minus 15 minutes, capped by the mandate's and sandbox's expiry (e24b0cd). Explanations and rule texts also in Finnish (`/rules?lang=fi`, 31d0d32). With no real order API, the ERP hand-off is a stub and labelled `simulated` (611ab8b).
- **Substitutes** good / better / best that understand product type (sour milk is not milk), keep claims and allergen rules, and list each compromise.
- **Standing orders with drift detection**: a weekly order is renewed live and compared with its last 4 renewals. Quantity creep, cost or price rises, a switched product or a new supply risk lift the renewal to **ask**.
- **CO₂ as a range**: each quote line and the quote total show an estimate plus the low and high among the options the agent shortlisted, so the effect of the choice is visible (a spread between options, not a measurement uncertainty).
- **Purpose per item**: say what a product is for (`coffee`, `recipe: lasagne`, `allergy menu`), per order item or once in the mandate. Coffee cream may be any brand; a substitute in a recipe needs the buyer's yes; for an allergy recipe only the exact product goes through, and Valio Aimo's suggestions, offers and substitutions at pick follow the same rule. See [PROTOCOL.md section 32](docs/PROTOCOL.md).
- **Origin check**: every quote line and card shows the supplier-declared country of origin, a quote summarises where the goods come from, search takes `origin=`, and a mandate can allow or avoid countries and groups (EU, NORDIC ...). Unknown origin is never treated as allowed. See [PROTOCOL.md section 30](docs/PROTOCOL.md).
- **Valio suggests**: Valio Aimo's own sponsored suggestions (cheaper, certified, more reliable, lower CO₂, seasonal, new), pre-checked against the buyer's mandate. Offers only, never orders, and audited.
- **Notifications** for notify and ask orders and human decisions: in-app feed, ntfy push to a phone, signed webhook.
- **Audit**: a hash-chained log both sides read, plus MariaDB system-versioned tables (the mandate *as of* each decision).
- **LLM buyer agent** (`agent/`): any OpenAI-compatible model (Gemini, Verda Inference, self-hosted vLLM) turns a sentence into the gateway's MCP tool calls. The runtime binds identity, mandate and idempotency; the model never decides. Scored per model by `scripts/llm_eval.py`.
- **Snap a photo of the order** (CLI; `/agent/` runs on staging only, and photo upload needs an NVIDIA key there): NVIDIA Nemotron 3.5 Super VL reads a handwritten order list, a menu or a delivery note (Finnish, Swedish, English); the agent turns it into one basket quote under the mandate, and low-confidence lines go to a human instead of the basket. See [docs/NEMOTRON.md](docs/NEMOTRON.md).
- **Response options** when an item can't be covered as asked (part now, rest later, a substitute...), each with a mandate preview. **Rule register** at `GET /rules`.
- **Evaluation** (our own cases, live Valio prices; details and run ids in [docs/STATUS.md](docs/STATUS.md#evaluation)), all from CI run 43587725624 (11d8448) on CRACI: scripted, no LLM: 60/60 cases, 43/43 unsafe actions blocked or escalated (incl. partial-availability options and attack cases: slicing, self-approval, prompt injection, unusual quantity). LLM agent, 48 one-sentence cases, all three models in the same run: Kimi K3.1 Pro 47/48, Gemini 4 Hydrogen 46/48, Nemotron 3.5 Super VL 30/48; 0 mandate violations auto-ordered with any of them. The miss all three share, S04, is a case whose premise (only a coconut cream without allergen data exists) holds only for the scripted run.

## Try it yourself

The demo server (`$SOUS`, see [Judge path](#judge-path-in-5-minutes); production is a Verda CPU instance in Finland) and staging both serve `/docs`, `/mcp` (also `/mcp/`), `/ui/` and `/buyer/`; human actions need the approver code. Get a private sandbox there (copies of the hotel, hospital and school mandates, valid 24 h, kept out of the curated demo data):

```bash
curl -s -X POST $SOUS/sandbox | jq
```

The response has your sandbox id (e.g. `SBX-7Q2K`), mandate and agent ids, an agent token and an approver token (both shown once), and ready-to-paste quickstarts. Then:

1. Connect Claude Code with the `quickstart.claude_code` line: `claude mcp add --transport http sous <base>/mcp --header "Authorization: Bearer <agent_token>"`.
2. Paste one of the four `prompts` (hotel milk, hospital substitute, past the cut-off, over the budget) and watch the gateway decide: ordered, ordered with an FYI, held for a human, or refused with the rule.
3. Open `quickstart.seller_ui` (`/ui/?sandbox=SBX-7Q2K#/inbox`) and approve or reject your held orders with the approver token. It works only on your sandbox's orders.
4. Open `quickstart.break_ui` (`/ui/break.html?sandbox=SBX-7Q2K`), paste your agent token and try seven attacks (self-approval, slicing, prompt injection, a tampered quote, replay, over the limit, a forbidden allergen): each shows the verdict, the rule and the audit events, with no Valio call.

Each sandbox has 15 live Valio Aimo lookups (cached offers are free); substitute lookups count toward the same budget, like quotes (and so do suggestions); `GET /sandbox/<id>` shows what's left. One-page handout: [docs/pitch/try-it.md](docs/pitch/try-it.md); details in [PROTOCOL.md section 18](docs/PROTOCOL.md#18-judge-sandbox).

## API and MCP at a glance

| Area | REST | MCP tool |
|---|---|---|
| Catalog | `GET /products`, `GET /products/{id}`, `GET /allergens` | `search_products`, `get_product` |
| Substitutes | `GET /products/{id}/substitutes` | `suggest_substitutes` |
| Quotes | `POST /quotes`, `GET /quotes/{id}` | `request_quote` |
| Orders | `POST /orders`, `GET /orders`, `GET /orders/{id}`, `GET /orders/{id}/history` | `place_order`, `get_order` |
| Approvals (human) | `POST /orders/{id}/approve`, `POST /orders/{id}/reject` | none, on purpose |
| Mandates | `POST /mandates` (human), `GET /mandates[/{id}[/history]]`, `POST /mandates/{id}/revoke` (human) | `get_mandate` |
| Suggestions | `GET /mandates/{id}/suggestions` | `get_suggestions` |
| Standing orders | `POST /standing-orders`, `GET /standing-orders[/{id}]`, `POST /standing-orders/{id}/renew`, `.../pause`, `.../resume` (human) | `create_standing_order`, `renew_standing_order`, `get_standing_order` |
| Notifications | `GET /notifications`, `POST /notifications/test` (human) | |
| Audit and trust | `GET /audit`, `GET /audit/verify`, `GET /.well-known/jwks.json`, `GET /health` | |
| Discovery and standards | `GET /.well-known/agent-card.json` (A2A), `GET /.well-known/mcp/server-card.json` (MCP), `GET /schemas/{mandate,quote,order,receipt}.json`, `GET /mandates/{id}/ap2`, `GET /quotes/{id}/ap2`, `GET /quotes/{id}/schema-org`, `GET /orders/{id}/ap2`, `GET /orders/{id}/schema-org` | |
| Judge sandbox | `POST /sandbox`, `GET /sandbox/{id}`, `DELETE /sandbox/{id}` (human); `?sandbox=<id>` on the list endpoints | |

MCP also offers the resources `sous://allergens` and `sous://mandates/{id}`, and a `purchase` prompt. Full details, error codes and rule ids: [docs/PROTOCOL.md](docs/PROTOCOL.md). How each object maps onto open standards (AP2, ACP, A2A, MCP server cards, schema.org, W3C VC 2.0, JOSE), what is exported and what is still missing: [docs/STANDARDS.md](docs/STANDARDS.md).

## Quick start

Needs Python 3.11+, [uv](https://docs.astral.sh/uv/) and a Valio Aimo API key (from the Junction challenge portal).

```sh
uv sync
cp .env.example .env                    # set VALIO_AIMO_API_KEY
uv run python -m scripts.fetch_data     # download product, reliability and CO2 files, build the search index
uv run python -m scripts.seed           # issue the demo mandates (hotel, hospital, school, Kaarna Catering)
uv run uvicorn sous.api:app --port 8000
```

Then open:

- http://localhost:8000/docs for the REST API (OpenAPI schema at `/openapi.json`)
- http://localhost:8000/ui/ for Valio Aimo's view: approval inbox, orders, mandates, audit log
- http://localhost:8000/buyer/ for the reference buyer (see [buyer/README.md](buyer/README.md))

Connect Claude Code as the buyer agent over MCP:

```sh
claude mcp add --transport http sous http://localhost:8000/mcp
```

Then ask it, for example: *"You are aurora-purchasing-agent with mandate M-HOTEL-AURORA. Buy 1,000 litres of lactose-free milk for delivery on Monday."* Against the demo server, use `$SOUS/mcp` instead.

Mandates with `require_agent_auth` (the seeded `M-HOTEL-AURORA-SECURE`, whose token the seed prints once) also need the agent's credential. Pass it as a header:

```sh
claude mcp add --transport http sous http://localhost:8000/mcp --header "Authorization: Bearer <token>"
```

Without it, quotes and orders on that mandate get `agent_auth_required`. See [PROTOCOL.md, section 16](docs/PROTOCOL.md#16-agent-authentication).

The MCP server also runs over stdio: `uv run python -m sous.mcp_server`.

Demos and evaluation (each takes `--base <gateway URL>`; see [docs/demo-runbook.md](docs/demo-runbook.md)):

```sh
uv run python scripts/demo.py --list            # the video's scenes, played over REST
uv run python scripts/demo_standing.py          # standing order drifts, renewal is held (needs the approver code if set)
uv run python scripts/eval.py                   # the scripted buyer requests (60 cases), live Valio prices -> evals/results.json
uv run python -m agent.buyer "We need 1,000 litres of lactose-free milk delivered on Monday." \
    --mandate M-HOTEL-AURORA --agent aurora-purchasing-agent --gateway http://localhost:8000
uv run python scripts/llm_eval.py --provider verda   # the eval sentences through the LLM agent, per model
```

The LLM agent needs `GEMINI_API_KEY`, `VERDA_INFERENCE_API_KEY` or `LLM_API_KEY` with `LLM_BASE_URL` (see `agent/llm.py`).

### From a menu to one order (recipe planner)

A chef says what they are cooking, not what to buy. `agent/recipe.py` turns that into one basket quote:

```sh
uv run python -m agent.recipe --mandate M-HOTEL-AURORA --agent aurora-purchasing-agent \
    "Lasagne for 120 on Monday, we have 10 kg flour and plenty of salt"            # add --order to place it
```

It prints the chain: **recipe** (dishes, portions, per-portion ingredients) -> **ingredients** (scaled, minus stock on
hand, rounded up, in kg/L) -> **products** (up to 5 catalog candidates per ingredient, filtered by the mandate's
allergen and claim rules) -> **one signed basket quote** (the gateway picks packs and the cheapest orderable product,
reports what is uncovered) -> substitutes for gaps -> **mandate preview** -> order or ask. Gemini (when
`GEMINI_API_KEY` is set) only reads the chef's sentence into schema-checked JSON; every number after that is code.
Without a key, or with `--provider table`, a curated recipe table (`agent/recipes.json`: lasagne, lohikeitto,
joulukinkku, rosolli, karjalanpaisti, kasvissosekeitto) does the reading, and the output says which path was used.
`agent.buyer` routes menu-shaped requests here automatically. Eval: `uv run python scripts/recipe_eval.py --providers
table gemini` (7 cases, `evals/recipe_cases.yaml`). See [docs/GEMINI.md](docs/GEMINI.md) and
[PROTOCOL.md, section 20](docs/PROTOCOL.md#20-buyer-side-recipe-planning-reference-agent).

Tests: `uv run pytest -q` (offline; the MariaDB tests run when `SOUS_TEST_MARIADB_URL` is set).

## Configuration

Read from the environment, or from `.env` in development. Never commit `.env`. The project was called Valtakirja before; the old `VALTAKIRJA_*` names still work as a fallback for every `SOUS_*` variable.

| Variable | Default | Purpose |
|---|---|---|
| `VALIO_AIMO_API_KEY` | (empty) | Key for Valio Aimo's price, availability and download APIs. Required. |
| `VALIO_AIMO_API_BASE` | `https://junction26.valioaimo.fi/api` | Valio Aimo API base URL. |
| `VALIO_AIMO_CUSTOMER` | `123` | Customer number sent to Valio Aimo (the mock ignores it). |
| `SOUS_DATA_DIR` | `./data` | Data files, catalog index, SQLite state and the Ed25519 signing key. |
| `SOUS_STATE_DB` | `$DATA_DIR/state.db` | SQLite state file, used when no MariaDB URL is set. |
| `SOUS_DB_URL` | (empty) | `mysql://user:pass@host:3306/db` to use MariaDB with system-versioned tables. |
| `SOUS_QUOTE_TTL` | `900` | Quote lifetime in seconds (shortened further by order cut-offs). |
| `SOUS_APPROVER_CODE` | (empty) | If set, human actions (approve, reject, issue or revoke a mandate) need it in the `X-Approver-Code` header. |
| `SOUS_REQUIRE_AGENT_AUTH` | (empty) | `1` = every mandate needs the agent's bearer credential for quotes, orders and renewals, not only those with `require_agent_auth`. |
| `SOUS_PUBLIC_BASE_URL` | (empty) | Public URL, used to build `order_url` and `approval_url` links. |
| `SOUS_RATE_LIMIT_PER_MIN` | `60` | POST requests per client IP per minute. |
| `SOUS_AIMO_CALLS_PER_MIN` | `60` | Per-process cap on live Valio Aimo API calls per minute. |
| `SOUS_VALKEY_URL` | (empty) | Aiven for Valkey (`rediss://...`): one Valio call budget, circuit breaker and offer cache shared by every deployment ([docs/AIVEN.md](docs/AIVEN.md)). Empty = per-process only. |
| `SOUS_DEPLOYMENT_ROLE` | `dev` | `prod`, `staging`, `ci`, `demo` or `dev`. Only `prod` may use the whole shared budget; the others stop at half of it. |
| `SOUS_VALIO_GLOBAL_CALLS_PER_MIN` | `60` | Shared Valio Aimo calls per minute across all deployments (sliding window in Valkey). |
| `MARIADB_PASSWORD` | (empty) | Used by `compose.yaml` for the MariaDB service and to build `SOUS_DB_URL`. |
| `SOUS_TRUST_CF_HEADER` | (empty) | `1` to take the client IP from `CF-Connecting-IP` (only behind Cloudflare). |
| `SOUS_NTFY_TOPIC` | (empty) | ntfy topic for push notifications to the buyer's phone. Optional `SOUS_NTFY_SERVER` (default `https://ntfy.sh`) and `SOUS_NTFY_TOKEN`. |
| `SOUS_NOTIFY_WEBHOOK_URL` | (empty) | Webhook that receives notifications as JSON. With `SOUS_NOTIFY_WEBHOOK_SECRET` the body is HMAC-SHA256 signed. |
| `GEMINI_API_KEY`, `VERDA_INFERENCE_API_KEY` | (empty) | Model keys for the LLM buyer agent (`agent/`) and `scripts/llm_eval.py`. Not used by the gateway. |
| `LLM_PROVIDER`, `LLM_MODEL`, `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_REASONING_EFFORT` | (presets) | Override the agent's model: `gemini`, `verda` or `local` (any OpenAI-compatible server). |

## Docker

```sh
cp .env.example .env   # set VALIO_AIMO_API_KEY, MARIADB_PASSWORD (any random string), SOUS_APPROVER_CODE
docker compose up -d --build
```

This starts the gateway on port 8000 and MariaDB 11.4. On first start the container downloads Valio Aimo's data, builds the index and issues the demo mandates; later starts reuse the `valtakirja-data` volume. State lives in the `mariadb-data` volume.

## Architecture

```mermaid
flowchart LR
    subgraph Buyer side
        H[Buyer human]
        A[Buyer agent<br/>Claude Code, agent/ LLM agent, scripts]
        B[Reference buyer<br/>/buyer]
    end
    subgraph Sous gateway
        MCP[MCP server<br/>/mcp]
        REST[REST + OpenAPI<br/>/docs]
        SVC[service.py<br/>search, quote, order, approve]
        CAT[(Catalog index<br/>SQLite FTS5 trigram)]
        Q[quote.py<br/>packs, lots, cut-offs, issues]
        M[mandate.py<br/>rules engine, 4 tiers]
        SO[standing.py<br/>standing orders, drift]
        SG[suggest.py<br/>Valio suggests]
        N[notify.py<br/>feed, ntfy, webhook]
        SIG[signing.py<br/>Ed25519 JWS, JWKS]
        ST[(store.py<br/>MariaDB system-versioned<br/>or SQLite)]
        UI[Valio Aimo UI<br/>/ui]
    end
    V[Valio Aimo APIs<br/>price, availability]

    A --> MCP --> SVC
    B --> REST
    A --> REST --> SVC
    H -- approve / reject --> UI --> REST
    SVC --> CAT
    SVC --> Q -- shortlist only --> V
    SVC --> M
    SO --> SVC
    SG --> M
    SVC --> N -- notify / ask --> H
    SVC --> SIG
    SVC --> ST
```

## Repo layout

```
sous/             the gateway
  api.py          REST API, mounts MCP, UI and buyer
  mcp_server.py   MCP tools, resources and prompt (same operations as REST)
  service.py      search, quote, order, approve, substitutes, history
  catalog.py      static catalog and FTS5 trigram search, allergen checks
  quote.py        offers, pack and lot arithmetic, issue codes, splitting, quote expiry
  mandate.py      mandate model and the deterministic rules engine
  substitutes.py  good / better / best substitutes, product-type aware
  standing.py     standing orders and drift detection (standing_store.py: storage)
  suggest.py      Valio suggests: sponsored, mandate-checked suggestions
  notify.py       notifications: feed, ntfy push, signed webhook (notifications_store.py)
  signing.py      Ed25519 compact JWS and JWKS
  store.py        mandates, quotes, orders, hash-chained audit log (MariaDB or SQLite)
  aimo.py         Valio Aimo API client: batching, caching, call budget
  config.py       settings
agent/            LLM buyer agent: buyer.py (MCP tool loop), llm.py (OpenAI-compatible model presets)
                  recipe.py (menu -> basket), vision.py (photo -> basket, Nemotron VL)
scripts/          fetch_data.py, seed.py, demo.py, demo_standing.py, eval.py, llm_eval.py
evals/            cases.yaml (60 cases), results.json, llm_results.*.json, vision/
web/              Valio Aimo UI: approvals, orders, mandates, audit, stats, live activity
buyer/            reference buyer in the browser, verifies JWS with Web Crypto
tests/            pytest, offline plus MariaDB
docs/             PROTOCOL, DECISIONS, STATUS, CI, LLM_EVAL, VERDA, demo runbook, pitch/
```

## License

MIT, see [LICENSE](LICENSE). Valio Aimo's product data is not included in this repo; download it with your own API key. Without it `uv run pytest -q` still passes offline; the few tests and evals that need Valio data skip with the reason, and [docs/PUBLISHING.md](docs/PUBLISHING.md#valio-data) lists the commands that rebuild each file.
