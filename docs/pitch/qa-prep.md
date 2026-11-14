# Judge Q&A prep

Answer in two or three sentences, then stop. If you don't know, say so and say what you'd do.

---

**1. Why not just let the LLM decide?**
Because an LLM can be talked into anything and can't be audited. A prompt injection in a product description shouldn't be able to raise a spend limit. The model turns language into a structured request and explains results. Limits, allergens, cut-offs and the tier are plain code that gives the same answer every time, with a reason a human can read.

**2. What if the agent lies about who it is?**
Today the agent sends an `agent_id` and a `mandate_id`; the gateway checks they match the signed mandate, and quotes and standing orders are bound to that agent, so a stranger can't invent authority or reuse someone else's quote. Approvals need a separate approver code. That is still hackathon-grade identity (see question 15). In production the agent proves a key bound to the mandate, so a stolen mandate ID is useless without the key.

**3. How does this scale to 700 suppliers and 25,000 products?**
Search runs on static catalog data in a local index (17,546 products in about 10 s to build), so it costs no backend calls. Live price and availability are fetched only for a shortlist: 2 batched calls per quote, up to 4 when substitutes are priced for the options (1.0 on average with the cache in our eval), against roughly 36 to poll the whole catalog. Supplier differences show up as data (Stock vs Terminal, reliability, cut-offs), not as code per supplier.

**4. What about Valio Aimo's confidential data and the marketplace roadmap?**
Agents never touch internal systems. They see the catalog and their own mandate, quotes and orders, nothing else: every read of mandates, orders, notifications and the audit log needs a credential (the approver code for everything, an agent's bearer token for its own mandate, a sandbox token for that sandbox), and anything else answers 404 as if it didn't exist. Only the curated demo buyers are public, so you can browse the UI without logging in; the audit chain's integrity check is public, its contents aren't. The gateway calls the backend on the agents' behalf, with a global cap on API calls and per-IP rate limits. Nothing about ranking, margins or the roadmap is exposed. In production the approver code becomes the buyer's login, and per-customer prices would only go to that authenticated buyer.

**5. Why MCP?**
It's what today's agents (Claude, ChatGPT, Codex, Cursor) speak natively, so a buyer's agent works with zero integration code. We demoed it: Claude Code on a laptop connected and ordered 1,000 L. But MCP is only a transport. The same operations are on OpenAPI, and the trust lives in signed JWS artifacts, so if the standard changes, the guarantees don't.

**6. Why not ACP or AP2?**
They're about payment and checkout between an agent and a merchant. Food-service wholesale runs on invoices and contracts, so payment isn't the hard part; the authority to buy is. Our mandate is a signed, structured statement of that authority, which maps cleanly to an AP2 mandate or a W3C Verifiable Credential. That's on our path to production.

**7. What happens with partial availability or out of stock?**
The quote splits across products when one can't cover the quantity, and lists every alternative with the reason it was rejected. The substitutes endpoint ranks good / better / best options in the same category, keeping claims and allergen rules and listing each compromise. Whether a substitution goes through alone is the buyer's call in the mandate: the hotel allows it with an FYI, the hospital wants to approve.

**8. Your API says Monday. Why do you say it can't be delivered?**
In the live mock, Terminal (supplier-shipped) products have an order cut-off that already passed (Thursday) while still promising Monday. We trust the cut-off over the promise, block that line and pick a stocked product. It's listed as an assumption, and we'd ask Valio Aimo which field is authoritative.

**9. 30 % of food products (46 % of all products) have no allergen data. Isn't treating them all as unsafe too strict?**
For a hotel ordering milk, allergen rules don't apply, so nothing is held. For a hospital or school with allergen rules, "we don't know" is not "safe". That order goes to a human with the reason, or is blocked if the mandate says so. Strict here is cheap: one tap on a phone. A wrong guess isn't.

**10. How do you know nobody changed the record afterwards?**
Two independent layers. The audit log is hash-chained, so editing any event breaks the chain (`/audit/verify`). And the tables are MariaDB system-versioned, so the database itself keeps every past version. We can show what the mandate said at the moment an order was decided, and any edit or delete of an audit row shows up even if someone rewrote the chain.

**11. Who is the customer, and why would Valio Aimo build this?**
Valio Aimo runs it; buyers' agents use it. It gives Valio Aimo an open, documented agent channel on its own terms, before a third-party platform defines it and sits between Valio Aimo and 20,000 kitchens. For buyers, routine orders happen in seconds and the unusual ones reach a human with the reason attached.

**12. What's missing before production?**
A real order API instead of our order store, per-customer prices and contracts, OIDC login for buyers and keys bound to agents, mandates as W3C VCs / AP2, and signing-key rotation through JWKS `kid`. Notifications already exist (feed, phone push, signed webhook). The shape doesn't change: the rest plugs into the same quote, mandate and audit flow.

**13. What if Valio's API rate-limits you?**
We already hit it: the shared key returned HTTP 429 during CI. The client honours `Retry-After`, backs off, and on a 429 pauses all calls briefly so we don't hammer Valio. If a live call still fails, the quote uses the last copy we fetched (price up to 15 min old, availability up to 5 min), marks it `DATA_STALE`, and that order always goes to a human. With no recent copy, the agent gets `valio_unavailable` with a retry time. We never auto-order on stale data.

**14. Why should a seller be allowed to push suggestions to someone else's agent?**
Because a good sales rep does it today, and the buyer's rules still apply. Every suggestion is checked against the buyer's signed mandate first: nothing it would block is shown, and products with unknown allergens are never pushed to a buyer with allergen rules. It's labelled "sponsored by Valio Aimo", it's only an offer (the buyer's agent must quote and order it like anything else), and every batch is in the audit log both sides read.

**15. How do you authenticate agents?**
Today: the `agent_id` plus a mandate signed by the gateway and issued to exactly that `agent_id`; quotes, orders and standing orders are bound to it. Opt-in bearer credentials for agents exist (per mandate, or for every mandate with `SOUS_REQUIRE_AGENT_AUTH=1`, which staging has on). In production the agent proves possession of a key: mTLS, DPoP-bound tokens, or a key named in the mandate as a Verifiable Credential. A leaked mandate id then buys nothing.

**16. Does it depend on one LLM?**
No. The gateway decides without any model. Our buyer agent works with any OpenAI-compatible model (tested: Gemini, Kimi on Verda Inference and NVIDIA Nemotron; self-hosted vLLM is documented but not run), and Claude Code works over MCP as it is. We score each model on the same 48 one-sentence cases, all three live in one CI run (43587725624, 11d8448): Kimi K3.1 Pro on Verda Inference (EU) matched the buyer without correction in 47/48, Gemini 4 Hydrogen in 46/48, Nemotron 3.5 Super VL in 30/48, with 0 mandate violations auto-ordered by any of them. The miss all three share (S04) is a case whose premise, a coconut cream with no allergen data, holds only for the scripted plan: live, they found one with declared allergens. The cases are ours, not a held-out set. The runtime, not the model, fills in agent id, mandate and idempotency key.

**17. Standing orders: why not just repeat last week's order?**
Because "same as usual" is where creep hides. Each renewal is quoted live and compared with the median of the last 4 ordered weeks. Quantity up more than 15 % (or 3 rises in a row), cost or price per litre up more than 10 %, a switched product or a new supply risk: the renewal waits for a human. In the demo, milk is +18 % and the bill +€135 (+16 %), so it asks.

---

## Backup facts
- 17,546 products, fi/sv/en. Search: SQLite FTS5 trigram, no API calls.
- Quote: 2 batched Valio API calls (price + availability), up to 4 with substitute options; 1.0 per request on average with the cache. Signed, expiring.
- Search first-result accuracy 56 % → 100 % on 39 fi/sv/en queries.
- 30 % of food products (4,118 of 13,546) have no allergen declaration: unknown → human.
- Hotel run: Valio Eila 1 L × 1,000 at €1.40/L, €1,400 ex VAT, notify tier, 2 API calls total.
- Hotel mandate: notify above €1,000, auto-approve to €2,500, hard limit €10,000, daily limit €8,000.
- Hospital mandate: auto-approve to €400, avoids milk / nuts, unknown allergens → ask, substitutions → ask.
- Tiers: silent / notify / ask / block. Late approvals expire after the cut-off or delivery date.
- Signatures: Ed25519 JWS; public keys at `/.well-known/jwks.json`.
- Scripted eval (no LLM, live Valio, our cases; docs/STATUS.md): 60/60, 43/43 unsafe actions blocked or escalated, human-approval rate 34.5 % of 58 order decisions, 0.47 Valio calls per request (CI on CRACI, full run 43587725624 on 11d8448).
- Conformance kit (`scripts/conformance.py`, 17 black-box checks over HTTP): 17/17 on staging at 7685e81 (14.11 10:34Z, docs/CONFORMANCE.md), again 17/17 on staging 06a858f (agent board 11:37Z). Check-the-checker: 47/47 planted violations caught, 12/12 controls quiet (docs/EVAL_INTEGRITY.md).
- Monday rush (docs/ROBUSTNESS.md): 60 agents on 400 ST of one product, 400/400 allocated first-come, 397/400 pro-rata, 0 over-allocated in every run.
- Spend so far: $29.77 known (docs/COSTS.md).
- Mandate drafting from plain language: Gemini 4 Hydrogen 23/23, rule parser 23/23 (CI run 43587725624, as in 43566010358; was 15/19 in 43562850006, the misses became validator rules). docs/GEMINI.md.
- Nemotron 3.5 Super VL photo reading: 0 silent errors, 100 % auto-correct over 32 page reads (CI run 43530170621); baseline prompt 16 silent errors. Rows from different runs (docs/NEMOTRON.md).
- New controls since Friday: approve-by = earliest cut-off − 15 min (`approval_deadline_passed`), response options incl. `next_date`, product holds/recalls (`product.held`), stock as buckets, scoped reads (404 like a missing id), `/rules`, `/ui/break.html` (seven attacks in a sandbox).
- 3-minute stage pitch and the 8 most likely questions: [stage-pitch.md](stage-pitch.md).
- LLM eval per model (48 one-sentence cases, all live in CI 43587725624, 11d8448): Kimi K3.1 Pro (Verda, EU) 47/48 (p50 9.9 s, ~$0.058/request at list price); Gemini 4 Hydrogen 46/48 (p50 14.8 s, $0.046/request); Nemotron 3.5 Super VL (NVIDIA, free tier) 30/48 (p50 8.2 s). Unsafe blocked or escalated 34/34, 34/34, 33/34; 0 mandate violations auto-ordered by any. Misses: S04 for all three (scripted-only premise; Nemotron's order of a coconut cream with declared allergens went through, hence its 33/34); Gemini's H02 (a milk drink for milk); Nemotron's are understanding and persistence (gave up after 12 tool calls, stopped at a preview). Earlier: 0b0ad68 (CI 43557209740) Kimi 48/49, Gemini 46/49.
- Standing-order demo: milk +18 % vs 4-week median, cost +€135 (+16 %) → ask before renewing.
- Live: production, the judge URL, https://135-181-8-209.sslip.io (Verda, Finland; at a880f90 on 14.11 11:50Z, redeploy of current `main` requested); staging https://staging.135-181-63-134.sslip.io (8624b18, near current `main`, the fallback). What is on which: docs/STATUS.md#deployed-now.
- CI on CRACI: SBOM + grype gate; deny-by-default egress per job, with a signed egress proof per run (PASS in 43587725624; docs/CRACI.md).
