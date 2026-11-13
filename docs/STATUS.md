# Status

_Last updated: Fri 13.11 (Helsinki)._

Live: production https://135-181-8-209.sslip.io (Verda CPU instance, Finland), staging https://staging.135-181-63-134.sslip.io. Both run `main` with MariaDB and the seeded demo mandates; human actions need the approver code.

## Vision (one product: Valtakirja)
Valio Aimo's front door for AI purchasing agents. Any buyer's agent (a hotel's, a hospital's) connects over **MCP or OpenAPI**, searches the catalog, gets a **signed quote** with live price and availability, and orders. Each agent carries a **mandate** (*valtakirja*) from its human: spend limits, allergen rules, substitution rules. Valio Aimo's gateway enforces the mandate in plain code and puts every order into one of four tiers:

- **Silent:** routine, ordered and logged.
- **Notify:** ordered, and the human gets an FYI.
- **Ask:** held until a human approves on their phone.
- **Block:** refused, with the rule that fired.

The LLM never decides; it only turns language into a structured request and explains results. Both the buyer and Valio Aimo read the same hash-chained audit log.

Pitch line: *"AI purchasing agents carry a signed mandate from their human, and Valio Aimo's gateway enforces it."*

## Works (on main)

**Catalog and quotes**
- Search over 17,546 products (fi/sv/en, SQLite FTS5 trigram) with fi/sv/en synonyms, a food prior and head-noun ranking; filters for claims, allergens, labels, temperature. No live API calls. First-result accuracy went from 56 % to 100 % on 39 fi/sv/en queries (`tests/test_search_quality.py`).
- Quotes: live price and availability for a shortlist only (at most 2 batched calls per uncached request; 0.95 per request on average with the cache), pack and lot sizes, VAT, cut-off checked against now, stock / Terminal / reliability / price-expiry issues, split across products when stock is short, alternatives with the reason each was rejected. Signed (Ed25519 JWS), expiring.
- Resilient to Valio Aimo's rate limits: honours `Retry-After` with backoff, a 429 pauses all calls briefly (circuit breaker), identical concurrent calls share one request. If a live call fails, the last-known copy (price ≤ 15 min, availability ≤ 5 min) is used and marked `DATA_STALE`, and such an order always goes to a human.
- Valio data handling: lot sizes in the split plan, naive times read as Helsinki time, a cut-off reported only for an earlier date flagged `CUTOFF_UNKNOWN`, transport errors become `valio_unavailable`.

**Mandate, orders, approvals**
- Mandate engine with the four tiers (silent / notify / ask / block); demo mandates in `scripts/seed.py` (hotel, hospital, school, Kaarna Catering). The seed reissues a mandate whose signature no longer verifies.
- Stricter allergens: allergen families work both ways (avoiding milk catches lactose, and avoiding lactose catches a product that only declares milk), and a product counts as declared only with a real allergen list or matching free-from codes. ~46 % of products have none: treated as unknown, so a human decides.
- Short known stock on a single product is `availability.partial_stock` (ask), never a silent order.
- Atomic orders: idempotency check, quote claim, daily spend and decision under one lock; one order per quote; a key reused for a different order is `idempotency_key_reused`.
- Only signed data is trusted: orders are built from the signed quote and the signed mandate, never the stored copies. A quote can only be ordered by the agent and mandate it was issued to (`quote_mismatch`).
- Approvals: approve/reject with a note, decided atomically. A late approval expires if a cut-off or the delivery date has passed, and is refused if the mandate was revoked or expired meanwhile. The receipt is re-signed after the human decision.

**Beyond one order**
- Good / better / best substitutes that understand product type (sour milk is not milk), keep category, claims and allergen rules, show pack sizes and list each compromise.
- Standing orders with drift detection (`valtakirja/standing.py`): a weekly order is renewed by its agent (live quote) and compared with the median of its last 4 ordered renewals: quantity creep > +15 % or 3 rises in a row, total or price per L/KG > +10 %, product switched, new Terminal or low-reliability product. Any drift lifts the renewal to ask. Demo `scripts/demo_standing.py` (history marked simulated, priced at one real signed quote): milk +18 % vs the 4-week median, cost +€135 (+16 %), so the gateway asks before renewing.
- Valio suggests (`valtakirja/suggest.py`): cheaper / certified / more reliable / lower-CO₂ alternatives to what the buyer orders, seasonal and new products. Labelled sponsored by Valio Aimo, pre-checked against the mandate (nothing it would block is shown; unknown allergens are never pushed to a buyer with allergen rules), offers only, audited as `suggestions.offered`.
- Notifications (`valtakirja/notify.py`) for notify and ask orders and human decisions: in-app feed (`GET /notifications`), ntfy push with the approval link, HMAC-signed webhook. A failed notification never blocks an order.

**Audit and trust**
- Audit log: append-only, hash-chained, `/audit/verify`.
- MariaDB storage with system-versioned `mandate`, `purchase_order` and `audit_event`: `/orders/{id}/history` shows every version of an order and the mandate AS OF each decision; `/audit/verify` also checks MariaDB's history (any edit or delete of an audit row shows up). SQLite remains the fallback when `VALTAKIRJA_DB_URL` is unset.
- Hardening: approver code for human actions, per-IP rate limit, global cap on Valio API calls.

**Interfaces and agents**
- REST (`/docs`) and MCP (`/mcp`) expose the same operations. MCP has tool annotations, structured errors with a hint and a retryable flag, allergen and mandate resources and a `purchase` prompt.
- Real external run: Claude Code on a laptop, connected over MCP, ordered 1,000 L of lactose-free milk (Valio Eila 1 L × 1000, €1,400 ex VAT, notify tier, 2 Valio API calls).
- Valio Aimo UI (`/ui/`): approval inbox, orders with a Request → Lookup → Order → Approval timeline and the mandate as of the decision, standing orders with drift trends against the 4-week median, audit log with the chain and system-versioning badge, mandates, stats, live activity feed and new-order alerts.
- Reference buyer (`/buyer/`, built with Codex): editable purchase request, product matches and ranked substitutes, signed quote verified against JWKS in the browser, submit under a mandate, signed receipt or approval follow-up; multi-item baskets with requested vs quoted quantity per item and alternatives for shortages; pending orders refresh live; orders reopen by link. No model API key needed. See [buyer setup](../buyer/README.md).
- LLM buyer agent (`agent/`): any OpenAI-compatible model (Gemini, Verda Inference in the EU, self-hosted vLLM) turns a buyer's sentence into the gateway's MCP tool calls and explains the result; identity, mandate and idempotency are bound by the runtime. `scripts/llm_eval.py` runs the eval cases' sentences through it per model ([LLM_EVAL.md](LLM_EVAL.md), [VERDA.md](VERDA.md)).
- Demo: `scripts/demo.py` plays the video's scenes (hotel, hospital, Terminal cut-off, limits, Kaarna Catering) over REST; runbook in [demo-runbook.md](demo-runbook.md).

**CI and deployment**
- CI on CRACI runners (`CI_RUNNER=craci`): pytest with a MariaDB service; Docker build with a CycloneDX SBOM and a grype gate (fails on fixable criticals); compose smoke test under a deny-by-default egress policy; eval and LLM eval jobs with egress limited to PyPI, Valio and the model APIs. See [CI.md](CI.md).
- Deployed on Verda: gateway + MariaDB + Caddy on a CPU instance in Finland (production and staging).

## In progress
- Opt-in bearer credentials for agents, on top of `agent_id` + signed mandate.
- Per-model LLM eval: [LLM EVAL] numbers land in [LLM_EVAL.md](LLM_EVAL.md) when the model runs finish.

## Next
- Record the demo video before Sun 15.11 17:00 (after that the mock's Stock cut-off has passed).
- Run the LLM eval on Verda Inference and Gemini; put the numbers in the pitch.
- Self-hosted vLLM on a Verda GPU as a third model in the eval.
- Production identity: buyer login (OIDC) instead of the approver code; agent keys bound to the mandate (mTLS, DPoP or VC-bound keys).
- Signing-key rotation via JWKS `kid`; anchor the audit chain head outside the gateway.

## Evaluation
`uv run python scripts/eval.py` runs the 45 scripted buyer requests in `evals/cases.yaml` (hotel, hospital, school and 6 ad-hoc mandates: happy path, splits, partial stock, ask/hard limits, price cap, allergen contains / may contain / unknown, substitutes, Terminal cut-off, out of stock, undeliverable, later delivery, wrong agent, revoked/expired mandate, daily budget, double order, expired quote, idempotent retry, late approval after revocation) through `valtakirja.service` with live Valio Aimo prices and availability, and writes `evals/results.json`. Uses a fresh SQLite state file, never `data/state.db`. Needs `data/` and the API key, and the mock's cut-off of Sun 15.11 17:00 must still be ahead (after that every Stock product reads as cut-off passed). CI runs it on `main` (job `eval`, egress limited to PyPI and Valio).

Run of Fri 13.11 ~22:00 (main at 2931d2d + eval):

| Metric | Result |
|---|---|
| Cases passed | 45 / 45 |
| Handled without manual correction | 100 % |
| Unsafe actions blocked or escalated | 36 / 36 = 100 % |
| Human-approval rate | 26.7 % of 45 order decisions (silent 12, notify 2, ask 12, block 19) |
| Valio API calls per request | at most 2 uncached; avg 0.95 with the cache (42 live calls for 45 requests; 128 cache hits) |
| Latency per request | mean ~0.8 s (quote ~0.6 s, almost all of it the live Valio call) |

LLM buyer agent per model: [LLM EVAL] (see [LLM_EVAL.md](LLM_EVAL.md)). The oracle run, which replays the scripted plan through the agent runtime, passes all 40 cases that apply to an LLM.

Before 8d9efc1 the set caught two gateway bugs (now fixed, cases kept as regressions): **P11**, a single product with less stock than needed was ordered silently (200 L ordered, 131 in stock); **H19**, another agent sending an idempotency key the hotel had used got the hotel's order back.

## Assumptions (shown to judges)
- Unknown allergen data is treated as unsafe: never auto-ordered for a mandate with allergen rules.
- CO₂ is a product-group average × quantity, an estimate, not a product measurement.
- Content per pack comes from the pack size in the name, confirmed against Valio Aimo's price / comparison price ratio; the ratio wins when they disagree.
- A passed `closingTime` means the quoted `availableDate` can't be met, even if the API still reports it.
- Stock `availableAmount` 9999999999 = plenty; Terminal 999999 = unknown.
- The mock ignores the requested date (earliest date is always the same); delivery date = max(requested, earliest).
- Seasonal and new products for Valio suggests are proxies: name keywords for the delivery date, and Synkka's lastUpdated within the last 30 days of the dataset. Every suggestion says so.
- Standing-order demo history is simulated (marked `simulated`), priced at one real signed quote; no Valio response is invented.

## Findings about the mock API
- Terminal products: `closingTime` already in the past (Thu 12.11) while `availableDate` is Mon 16.11.
- Some Stock products also had a cut-off today 13:00 (passed).
- Out of stock = Stock with `availableAmount` 0 and no date; undeliverable = empty `scheduleLines` and no logistic model.
- ~46 % of products have no allergen declaration at all.
- The shared API key can return HTTP 429 when several jobs call at once; CI's LLM eval runs after the eval at 30 Valio calls/min.

## Open questions for the Valio Aimo mentors
1. Is it right that the order endpoint, approvals and audit are ours to design? (We assume yes.)
2. Is the passed cut-off on Terminal products intentional, and which field is authoritative: `closingTime` or `availableDate`?
3. What must always need human sign-off in your view? Our default: unknown allergens, substitutions, over-limit spend, drift on standing orders.
4. Would Valio Aimo push suggestions to buyers' agents, and under which rules? Ours: labelled sponsored, mandate-checked, offers only, audited.
5. Is there a real launch or season date per product? Seasonal and new suggestions use name keywords and Synkka's lastUpdated as proxies.
6. What rate limit would the real price and availability APIs give each partner?

Answers: _(not asked yet)_
