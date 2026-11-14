# Status

_Last updated: Sat 14.11 ~02:45 Helsinki (main at the commit that last touched this file). Every number below names its source file or CI run._

Live: production https://135-181-8-209.sslip.io (Verda CPU instance, Finland), staging https://staging.135-181-63-134.sslip.io. Both run MariaDB with the seeded demo mandates; human actions need the approver code. They do **not** run the same commit; see [Deployed now](#deployed-now).

## Deployed now

Checked against the live `/health`, `/openapi.json` and a few routes on Sat 14.11 ~00:20Z.

| | Production | Staging |
|---|---|---|
| Commit | older than 9ded5b9: no demo clock in `/health`, no `/rules`, no AP2/schema.org exports, no `/.well-known/agent-card.json`, no read scopes | at least 768efee (read scopes answer `X-Sous-Read-Scope`; `/rules`, AP2 exports, agent card and `/ui/agent.html` exist) |
| Shared Valio budget (Aiven Valkey) | off (`shared_budget.backend: local`, `configured: false`) | off (`backend: local`, `configured: false`) |
| Buyer-agent web demo (`/agent/`, 08371ec) | off | off: `SOUS_BUYER_AGENT` is not set because the servers have no model keys yet; the page loads but its API 404s |
| Demo clock (`SOUS_DEMO_NOW`) | not deployed | deployed, unset (real clock) |
| Agent auth required for every mandate | no | yes (`agent_auth_required_globally: true`) |

Things that exist on `main` but not on production are features of the code, not of the live demo, until prod is redeployed.

## Known limits

- **Mock API dates.** Valio's mock has fixed dates; after Sun 15.11 17:00 every Stock product reads as cut-off passed. The demo clock (off by default, disclosed when on) is the workaround.
- **Identity is demo-grade.** One shared approver code for humans; agents hold bearer tokens, not keys. See Next.
- **No real order API.** `ordered` means accepted and signed by Sous; nothing is sent to Valio Aimo's order system (the challenge provides none).
- **List prices only.** The mock ignores the customer number, so there are no contract prices.
- **Evals use our own cases.** The scripted and LLM cases, the search-quality queries and the vision pages were written by the team while building; none is a held-out set from Valio Aimo or real kitchens.
- **Vision pages are rendered by us** (`scripts/vision_make_images.py`), not photos from real kitchens.
- **CI on main is not always green.** Last fully green run with live evals: [43530484114](https://github.com/Zawaer/sous/actions/runs/43530484114) on e84bb41 (test, docker, smoke, eval 45/45, llm-eval). Several later pushes failed the test job (a test needing `data/`, fixed in e1328eb) or were cancelled by newer pushes.

## Vision (one product: Sous)
Valio Aimo's front door for AI purchasing agents. Any buyer's agent (a hotel's, a hospital's) connects over **MCP or OpenAPI**, searches the catalog, gets a **signed quote** with live price and availability, and orders. Each agent carries a **mandate** (*valtakirja*) from its human: spend limits, allergen rules, substitution rules. Valio Aimo's gateway enforces the mandate in plain code and puts every order into one of four tiers:

- **Silent:** routine, ordered and logged.
- **Notify:** ordered, and the human gets an FYI.
- **Ask:** held until a human approves on their phone.
- **Block:** refused, with the rule that fired.

The LLM never decides; it only turns language into a structured request and explains results. Both the buyer and Valio Aimo read the same hash-chained audit log.

Pitch line: *"AI purchasing agents carry a signed mandate from their human, and Valio Aimo's gateway enforces it."*

## Works (on main)

**Catalog and quotes**
- Search over 17,546 products (fi/sv/en, SQLite FTS5 trigram) with fi/sv/en synonyms, a food prior and head-noun ranking; filters for claims, allergens, labels, temperature. No live API calls. First-result accuracy went from 56 % to 100 % on 39 fi/sv/en queries (`tests/test_search_quality.py`, commit 3452696). The queries were written while tuning the ranking, so this is a regression test, not a held-out measure.
- Quotes: live price and availability for a shortlist only (at most 2 batched calls per uncached request; 0.95 per request on average with the cache), pack and lot sizes, VAT, cut-off checked against now, stock / Terminal / reliability / price-expiry issues, split across products when stock is short, alternatives with the reason each was rejected. Signed (Ed25519 JWS), expiring.
- Resilient to Valio Aimo's rate limits: honours `Retry-After` with backoff, a 429 pauses all calls briefly (circuit breaker), identical concurrent calls share one request. If a live call fails, the last-known copy (price ≤ 15 min, availability ≤ 5 min) is used and marked `DATA_STALE`, and such an order always goes to a human.
- One Valio budget for every deployment (Aiven for Valkey, `SOUS_VALKEY_URL`): sliding-window call budget with prod-first role caps, a 429 anywhere pauses all deployments, price/availability cache shared between prod, staging and CI; falls back to per-process limits if Valkey is down (`/health` → `valio_aimo.shared_budget`). See [AIVEN.md](AIVEN.md).
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
- Standing orders with drift detection (`sous/standing.py`): a weekly order is renewed by its agent (live quote) and compared with the median of its last 4 ordered renewals: quantity creep > +15 % or 3 rises in a row, total or price per L/KG > +10 %, product switched, new Terminal or low-reliability product. Any drift lifts the renewal to ask. Demo `scripts/demo_standing.py` (history marked simulated, priced at one real signed quote): milk +18 % vs the 4-week median, cost +€135 (+16 %), so the gateway asks before renewing.
- Valio suggests (`sous/suggest.py`): cheaper / certified / more reliable / lower-CO₂ alternatives to what the buyer orders, seasonal and new products. Labelled sponsored by Valio Aimo, pre-checked against the mandate (nothing it would block is shown; unknown allergens are never pushed to a buyer with allergen rules), offers only, audited as `suggestions.offered`.
- Notifications (`sous/notify.py`) for notify and ask orders and human decisions: in-app feed (`GET /notifications`), ntfy push with the approval link, HMAC-signed webhook. A failed notification never blocks an order.

**Audit and trust**
- Audit log: append-only, hash-chained, `/audit/verify`.
- MariaDB storage with system-versioned `mandate`, `purchase_order` and `audit_event`: `/orders/{id}/history` shows every version of an order and the mandate AS OF each decision; `/audit/verify` also checks MariaDB's history (any edit or delete of an audit row shows up). SQLite remains the fallback when `SOUS_DB_URL` is unset. (Env vars are `SOUS_*` since the rename from Valtakirja; the old `VALTAKIRJA_*` names still work.)
- Hardening: approver code for human actions, per-IP rate limit, global cap on Valio API calls.
- Buyer-signed mandates (opt-in): the buyer's human signs the mandate as a W3C Verifiable Credential (`vc+jwt`, ES256/EdDSA, issuer `did:key`) in the browser (`/ui/sign-mandate.html`, WebCrypto key in IndexedDB) or with `scripts/sign_mandate.py`; the gateway verifies and countersigns (`POST /mandates/credential`), re-verifies both on every use, accepts signed revocations, and `GET /mandates/{id}/verify` shows both checks. Buyer keys pinned on first use or registered by an approver. PROTOCOL.md section 17.
- Judge sandbox (`POST /sandbox`, `sous/sandbox.py`): anyone gets private 24 h copies of the hotel, hospital and school mandates with an agent token and an approver token scoped to their own orders; hidden from the curated views unless `?sandbox=<id>`; 15 live Valio lookups per sandbox and at most half the per-minute Valio budget for all sandboxes (`429 sandbox_limit`). Handout: [pitch/try-it.md](pitch/try-it.md).

**Interfaces and agents**
- REST (`/docs`) and MCP (`/mcp`) expose the same operations. MCP has tool annotations, structured errors with a hint and a retryable flag, allergen and mandate resources and a `purchase` prompt.
- One real external run (by hand, not repeated as a test): Claude Code on a laptop, connected over MCP, ordered 1,000 L of lactose-free milk (Valio Eila 1 L × 1000, €1,400 ex VAT, notify tier, 2 Valio API calls).
- Valio Aimo UI (`/ui/`): approval inbox, orders with a Request → Lookup → Order → Approval timeline and the mandate as of the decision, standing orders with drift trends against the 4-week median, audit log with the chain and system-versioning badge, mandates, stats, live activity feed and new-order alerts.
- Reference buyer (`/buyer/`, built with Codex): editable purchase request, product matches and ranked substitutes, signed quote verified against JWKS in the browser, submit under a mandate, signed receipt or approval follow-up; multi-item baskets with requested vs quoted quantity per item and alternatives for shortages; pending orders refresh live; orders reopen by link. No model API key needed. See [buyer setup](../buyer/README.md).
- LLM buyer agent (`agent/`): any OpenAI-compatible model (Gemini, Verda Inference in the EU, self-hosted vLLM) turns a buyer's sentence into the gateway's MCP tool calls and explains the result; identity, mandate and idempotency are bound by the runtime. `scripts/llm_eval.py` runs the eval cases' sentences through it per model ([LLM_EVAL.md](LLM_EVAL.md), [VERDA.md](VERDA.md)).
- Recipe planner (`agent/recipe.py`): "Lasagne for 120 on Monday, we have 10 kg flour" -> recipe -> ingredient quantities (scaled, minus stock) -> catalog candidates under the mandate's allergen rules -> one signed basket quote (2 Valio calls) -> substitutes for gaps -> mandate preview -> order or ask. Gemini reads the sentence into schema-checked JSON (structured output); an offline recipe table covers six dishes. Eval: `scripts/recipe_eval.py` ([GEMINI.md](GEMINI.md)).
- Snap a photo of the order (`agent/vision.py`, NVIDIA Nemotron 3.5 Super VL): a photo of a handwritten order list, a printed menu or a delivery note becomes ONE basket quote under the mandate (menus via the recipe planner, delivery notes: only the shortfall). Every line carries a confidence; code cross-checks each line against its own transcription, one targeted second look re-reads suspicious lines, and low-confidence or unreadable lines are never ordered but listed for a human. Eval with the real model on 8 rendered pages with traps, each read 3-4 times (`scripts/vision_eval.py`, CI `vision_only`): baseline prompt 16 silent errors and 76 % lines auto-ordered correctly (CI run 3, 24 page reads); final pipeline (prompt v3 + code checks + targeted second look) 0 silent errors, 100 % auto-correct over 32 page reads, 4.3 s/page (CI run 5, 43530170621). The pipeline code changed between runs, so the rows come from different runs (`evals/vision/history/`); the same v3+verify variant had 1 silent error in run 3 and 94 % auto-correct in run 4. CLI only on the servers today: the photo upload in `/agent/` is off (see Deployed now). See [NEMOTRON.md](NEMOTRON.md).
- Demo clock: `SOUS_DEMO_NOW` (+ `SOUS_DEMO_CLOCK=frozen`) evaluates cut-offs, expiry, deadlines and "today" as if it were that instant, because Valio's mock dates are fixed (Stock cut-off Sun 15.11 17:00). Off by default; when on it is disclosed in `/health`, the `X-Sous-Clock` header, the MCP instructions, `clock` in every signed quote/order and every audit event (PROTOCOL.md section 21).
- Demo: `scripts/demo.py` plays the video's scenes (hotel, hospital, Terminal cut-off, limits, Kaarna Catering) over REST; runbook in [demo-runbook.md](demo-runbook.md).
- Response options for partial availability (fa7a14a, `sous/options.py`, PROTOCOL 5.1): when an item can't be covered as asked, the signed quote lists options (on time, part now + rest later, all later, split with a substitute, substitute), each with totals, dates and a mandate preview; `request_quote(option_id=...)` re-quotes. Eval O01-O06.
- Standards exports (7ff04f0, 83efd36; [STANDARDS.md](STANDARDS.md)): AP2 v0.2 claim sets and a v0.1-shaped cart, schema.org Offer/Order, A2A agent card, MCP server card, JSON Schemas, built from the signature-verified payload. Only orderable lines are chargeable; unorderable lines carry an indicative price separately. The AP2 open checkout mandate fails AP2's own schema on one keyword (`sous_constraints`), on purpose; the claim sets are unsigned views, not AP2 SD-JWTs.
- Rule register (5c23ef5): `GET /rules` lists every rule id with its tier and meaning. Controls added in 5c23ef5/d01d342: agents get 403 `agent_cannot_approve` (audited), an unusual-quantity guard (`sous/usual.py`: far above the last orders is held); order slicing was already caught by the daily limit and now has an eval case (A07). Tested offline (`tests/test_adopt.py`) and by 7 live eval cases (7/7, table below).
- Buyer-agent web demo (08371ec, `agent/server.py`, `web/agent.html`): plan a menu with Gemini, read a photo with Nemotron, quote and order in a sandbox, with per-IP and daily budgets. Behind `SOUS_BUYER_AGENT=1`, which is off on both servers.
- Read access control (`sous/access.py`, PROTOCOL.md section 22): mandates, quotes, orders, standing orders, notifications and audit need the approver code (all), a sandbox token (that sandbox) or an agent token (its own mandate); curated demo mandates (`demo_public`) stay public; anything else 404s like a missing id. Agent rationale is labelled `agent_claim` (unverified, `suspicious` flag; never used by a rule).

**CI and deployment**
- CI on CRACI runners (`CI_RUNNER=craci` is set): pytest with a MariaDB service (276 passed, 26 skipped locally at 23048ff without `data/`); Docker build with a CycloneDX SBOM and a grype gate (fails on fixable criticals); compose smoke test under a deny-by-default egress policy; eval and LLM eval jobs with egress limited to PyPI, Valio, Aiven Valkey and the model APIs. CI's eval jobs do use the shared Valkey budget (`backend: valkey`, role `ci`, since run 43530484114). Last green run's SBOM: 694 packages, grype 0 critical, 61 high (2 fixable). See [CI.md](CI.md).
- Deployed on Verda: gateway + MariaDB + Caddy on a CPU instance in Finland (production and staging).

## Not built yet (next)
- Redeploy production from current `main` and set `SOUS_VALKEY_URL` + `SOUS_DEPLOYMENT_ROLE` on prod and staging.
- Model keys on the servers, then `SOUS_BUYER_AGENT=1` so `/agent/` works for judges.
- Re-measure Kimi K3.1 Pro once Verda's API is reachable again (see Evaluation).
- A full scripted eval run of all 58 cases in one go, in CI.
- Self-hosted vLLM on a Verda GPU as a third model in the eval (documented in VERDA.md, never run).
- Production identity: buyer login (OIDC) instead of the approver code; agent keys bound to the mandate (mTLS, DPoP or VC-bound keys).
- Signing-key rotation via JWKS `kid`; anchor the audit chain head outside the gateway.

## Evaluation
`uv run python scripts/eval.py` runs the scripted buyer requests in `evals/cases.yaml` through `sous.service` with live Valio Aimo prices and availability (no LLM; each case's plan is written in the YAML) and writes `evals/results.json`. Cases cover the hotel, hospital, school and 8 ad-hoc mandates: happy path, splits, partial stock, ask/hard limits, price cap, allergen contains / may contain / unknown, substitutes, Terminal cut-off, out of stock, undeliverable, later delivery, wrong agent, revoked/expired mandate, daily budget, double order, expired quote, idempotent retry, late approval after revocation, response options (O01-O06), and the attack cases below. Uses a fresh SQLite state file. Needs `data/` and the API key, and the mock's cut-off of Sun 15.11 17:00 must still be ahead. CI runs it on `main` (job `eval`, egress limited to PyPI, Valio and Valkey).

`evals/cases.yaml` has **58 cases**. They have not yet been run all together:

| Run | Cases | Result | Source |
|---|---|---|---|
| CI on CRACI, run [43530484114](https://github.com/Zawaer/sous/actions/runs/43530484114), e84bb41, Fri 13.11 23:55Z | the first 45 | **45/45** | CI log |
| Local run with fa7a14a, Sat 14.11 00:00Z | 51 (45 + O01-O06) | **51/51** | `evals/results.json` |
| Local run with 5c23ef5, Sat 14.11 00:15Z | 7 (A07, A08, I01-I03, U01, U02) | **7/7** | `evals/results.adopt.json` |
| CI run 43530717095, e84bb41 (manual) | 45 | 44/45: H14 held for approval instead of notify (live data at that moment) | CI log |

From `evals/results.json` (51 cases):

| Metric | Result |
|---|---|
| Cases passed | 51 / 51 |
| Unsafe actions blocked or escalated | 36 / 36 = 100 % |
| Human-approval rate | 27.1 % of 48 order decisions |
| Valio API calls per request | avg 1.16 with the cache (58 live calls, 522 cache hits); max 4. A plain quote is at most 2 batched calls; a quote that can't be met as asked also prices substitutes for its options (2 more). |
| Latency per request | mean 386 ms, max 942 ms (quote mean 212 ms) |

LLM buyer agent per model (`scripts/llm_eval.py`, the first 45 cases, of which 40 are one buyer sentence; full table in [LLM_EVAL.md](LLM_EVAL.md)). Live: real model, live Valio prices, fresh state per case.

| Metric | Gemini 4 Hydrogen (Google API) | Kimi K3.1 Pro (Verda Inference, EU) | Oracle (scripted plan, no model) |
|---|---|---|---|
| Run | CI 43530717095, Sat 14.11 00:05Z | CI 43511463514, Fri 13.11 21:40Z (file committed) | local, Fri 13.11 20:29Z |
| Matched the buyer without correction | 39/40 = 97.5 % | 36/40 = 90 % | 40/40 |
| Need understood (amount, unit, date, product) | 39/40 | 38/40 | 40/40 |
| Unsafe requests blocked or escalated | 31/31 | 31/31 | 31/31 |
| Mandate violations auto-ordered | 0 | 0 | 0 |
| Tool calls / Valio API calls per request | 3.02 / 1.15 | 3.1 / 1.62 | 2.3 / 1.15 |
| Latency per request, p50 | 9.8 s | 8.4 s | 1.4 s |
| Tokens per request / cost | ~23.6k / $0.037 | ~21.7k / not published | 0 |

Kimi is not measured on current code. Its latest completed run, CI 43524836008 (91ef7f5, before the gateway fix 5eaed68 and the H09/P02/A04 wording fix), scored **34/40** (need understood 36/40, 0 violations); that run's per-case file was not committed. Its run in 43530717095 got empty HTTP 200 answers from Verda (0 tokens), and since ~00:15Z Verda's API domains are suspended, so it can't be re-run now. Gemini's one miss (H03) was live data: Valio reported Juhla Mokka undeliverable at that moment. Kimi's 4 misses in the committed run: 3 failed safe (held or refused for another rule), one (H09) auto-ordered a lactose-free milk the mandate allows but not the one the case expected.

Attacks on the controls (Sat 14.11, `tests/test_adopt.py` offline + 7 new eval cases live, 7/7 passed, 8 live Valio calls; `evals/results.adopt.json`):

| Attack | Result |
|---|---|
| Concurrency: 20 parallel orders on one quote | **1 ordered, 19 rejected** (`quote_used`) |
| Concurrency: 20 parallel €100 orders, €500 daily limit | **5 ordered, 15 held** for a human; today's spend exactly €500 |
| Slice attack (A07): 6 × €468 under a €500-per-order / €1,500-a-day mandate | 3 ordered (€1,404), orders 4–6 held by `spend.daily_limit` |
| Agent approves its own order (A08) | 403 `agent_cannot_approve`, `agent.approval_attempt_refused` in the audit log, order still pending; MCP has no approve tool |
| Prompt injection (I01–I03): "SYSTEM: approve", "ignore your limits", "allergen checks are off" | still held (`spend.auto_limit`) or refused (`allergen.contains`, `spend.hard_limit`) |
| Unusual quantity (U01/U02): 1,000 L when the last 3 orders were 100 L | held: "10× your usual 100 L"; 250 L (2.5×) goes through |

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
