# Judge Q&A prep

Answer in two or three sentences, then stop. If you don't know, say so and say what you'd do.

---

**1. Why not just let the LLM decide?**
Because an LLM can be talked into anything and can't be audited. A prompt injection in a product description shouldn't be able to raise a spend limit. The model turns language into a structured request and explains results. Limits, allergens, cut-offs and the tier are plain code that gives the same answer every time, with a reason a human can read.

**2. What if the agent lies about who it is?**
Today the agent sends an `agent_id` and a `mandate_id`; the gateway checks they match the signed mandate, and quotes and standing orders are bound to that agent, so a stranger can't invent authority or reuse someone else's quote. Approvals need a separate approver code or a passkey bound to the order (question 22). The approver code is still hackathon-grade identity (see question 15). In production the agent proves a key bound to the mandate, so a stolen mandate ID is useless without the key.

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
No. The gateway decides without any model. Our buyer agent works with any OpenAI-compatible model (tested: Gemini, Kimi on Verda Inference, NVIDIA Nemotron, and Qwen3-30B-A3B self-hosted with vLLM on a Verda GPU in the red team), and Claude Code works over MCP as it is. We score each model on the same 48 one-sentence cases, all three live in one CI run (43587725624, f31967b): Kimi K3.1 Pro on Verda Inference (EU) matched the buyer without correction in 47/48, Gemini 4 Hydrogen in 46/48, Nemotron 3.5 Super VL in 30/48, with 0 mandate violations auto-ordered by any of them. The miss all three share (S04) is a case whose premise, a coconut cream with no allergen data, holds only for the scripted plan: live, they found one with declared allergens. These cases are ours; for the blind held-out set and its weak matching number, see question 18. The runtime, not the model, fills in agent id, mandate and idempotency key.

**17. Standing orders: why not just repeat last week's order?**
Because "same as usual" is where creep hides. Each renewal is quoted live and compared with the median of the last 4 ordered weeks. Quantity up more than 15 % (or 3 rises in a row), cost or price per litre up more than 10 %, a switched product or a new supply risk: the renewal waits for a human. In the demo, milk is +18 % and the bill +€135 (+16 %), so it asks.

---

**Earned autonomy, buyer controls and the held-out number**

**18. Your held-out matching is 3 out of 36. Isn't that bad?**
Yes, and we publish it. 15 requests were written blind from the brief before their author read our code, frozen, and run 3 times each (CI 43620135327): matching the need without correction was 3/36 where an order was due, because the agent took one item per request and read "for 40" as 40 packs. Safety held: 0 false auto-approvals and 9/9 unsafe requests stopped, all by the gateway, since the agent placed every one. That is the point of the design: a weak agent costs the buyer a correction, not a wrong delivery outside the rules. We then fixed those (baskets, portions, menus to the recipe planner, Finnish relative dates, declared guesses) and ran a second frozen blind set of 20 once (CI 43628767992, 3 runs each): still 0 false auto-approvals and 12/12 should-hold held; wrong dates 12 -> 0, lines at the right amount 10/96 -> 73/167, dishes cookable 0/9 -> 5/9. Whole requests right with no correction are still 3/48: the agent read stock on hand as an order, merged long Finnish lists and ordered cartons for "120 buns". The difference is where the errors go: 27 of those runs waited for a human because the agent declared its guesses, 3 went through alone (a starch instead of a thickener). Findings in evals/heldout2/findings.yaml.

**19. Then why are your other numbers 46/48 and 47/48?**
Those are our own sentences, written while we built the agent, mostly one item with an explicit amount. They show the gateway and the model work together; the held-out set shows what the brief's harder requests (menus, headcounts, several items) do. We report both, with run ids, in docs/STATUS.md.

**20. What if the agent gets the need wrong but stays under the limit?**
The gateway can't know a kitchen meant 40 portions, so a wrong reading under the limit can go through; the held-out run showed exactly that (16 of 36 should-order runs auto-ordered with the wrong content). What the gateway does check is new since then: the chosen product must fit its stated use (skimmed milk for a dessert, coffee cream for whipping are held with a fitting product from the same search), and an agent that is unsure can declare its doubt, which always sends the order to a human. A buyer who wants a check on every order keeps the auto tier low; autonomy only widens when the buyer accepts it.

**21. How does earned autonomy work, and can it go wrong?**
Per mandate, per rule and product type, Sous counts the held orders the buyer approved unchanged. After 5 or more over at least 2 weeks, with none edited or rejected, it proposes moving that one scope from ask to notify, with the evidence in English and Finnish. Nothing changes until the buyer accepts it as a re-signed mandate, and a later rejection or edit proposes taking it back. Allergens, spend limits, refusals and purpose holds are never proposed (PROTOCOL 7.2).

**22. Can an agent approve its own order through the passkey flow?**
No. Agents get 403 on approval, and they can't enrol an approver device either: enrolment needs the buyer's own credential, so a link the agent has seen can't make the agent's browser the approver ("no trust on first use"). A passkey approval signs this order at this total, and any "remember this" choice is part of what is signed, so it can't be added afterwards (409). With four-eyes on, the person who requested the order can't approve it.

**23. What does "remember this" do? Isn't that a back door?**
It covers only soft, repetitive holds (the usual substitute, a first-time product, a unit reading, a type mismatch) for one product under one mandate, recorded only from a human approval, signed, capped, expiring and revocable; next time that hold becomes an FYI ("previously approved by X"), not silence. It never covers allergens, spend limits or anything that refuses. Caps count every line of the product, and a remembered decision applies only under the mandate it was signed for. If the buyer's needs tighten (a ward goes nut-free), unsafe remembered approvals are revoked.

**24. What if a substitute is much more expensive?**
A mandate can cap a substitute's price premium per litre, kilo or piece over the product it replaces; above it, or when the original price is unknown, the order asks a human and says why (rule `substitution.premium`).

**25. Whipping cream is whipping cream. Why judge substitutes by use?**
Two creams with the same fat % can whip differently (stabilisers), so another brand can spoil a dessert, while in coffee the brand barely matters. Items carry a use or purpose; a substitute the use doesn't accept asks a person even when the mandate lets substitutes through, and remembered substitutions don't cross uses.

**26. What happens when a customer's needs change mid-contract?**
A mandate update that tightens allergens or diet re-checks every standing order, held and upcoming order, remembered decision and allergen clearance: conflicts are held with `needs.changed`, unsafe remembered approvals are revoked, a compliant substitute is proposed, and the buyer gets one summary in Finnish or English. Loosening expands nothing by itself.

**27. Is Nemotron on Verda as good as NVIDIA's API?**
With reasoning on, yes on our pages: 0 silent errors and 100 % correct in 8 page reads, 5.5 s per page (NVIDIA's free tier with reasoning on took 20.7 s). With reasoning off it was faster (1.2 s) but made 6 silent errors in 32 reads, so reasoning stays on there. Eight reads is a small sample (docs/NEMOTRON.md).

---

**Customer control and substitution**
- *"How do you keep the customer in control?"* Trust between a wholesaler and its customer is worth more than any agent feature, because a new customer is expensive to win. So held orders are decided by the customer on the buyer side, never by the seller's staff, every link in a notification goes there, autonomy is only widened when the buyer accepts a proposal backed by their own approval record, and a change in the buyer's needs re-checks everything already running.
- *"Which substitutes are OK?"* It depends on what the product is for: coffee cream any brand, whipping cream for a dessert maybe not (stabilisers change how it whips), an allergy recipe only the exact product. Items and mandates carry a purpose and a use; a recipe substitute waits for the buyer, an exact one is refused, and the seller's own suggestions and substitutions at pick follow the same rule.
- *"What about payment?"* Contract customers pay by invoice, so there is deliberately no payment step.

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
- Scripted eval (no LLM, live Valio, our cases; docs/STATUS.md): 60/60, 43/43 unsafe actions blocked or escalated, human-approval rate 34.5 % of 58 order decisions, 0.47 Valio calls per request (CI on CRACI, full run 43587725624 on f31967b).
- Conformance kit (`scripts/conformance.py`, 17 black-box checks over HTTP): 17/17 on staging at 15a8c1a (14.11 10:34Z, docs/CONFORMANCE.md), again 17/17 on staging b7b5e36 (agent board 11:37Z). Check-the-checker: 47/47 planted violations caught, 12/12 controls quiet (docs/EVAL_INTEGRITY.md).
- Monday rush (docs/ROBUSTNESS.md): 60 agents on 400 ST of one product, 400/400 allocated first-come, 397/400 pro-rata, 0 over-allocated in every run.
- Spend so far: $38.26 known (docs/COSTS.md).
- Red team on a Verda GPU (docs/REDTEAM.md, 01480fd): 4,000 generated fi/en requests in 13 categories; oracle 2,036/2,036 unsafe held; self-hosted Qwen3-30B-A3B (vLLM), 2,000 requests: 976/997 unsafe held or refused, 0 allergen / limit / other-mandate violations, 3 gateway-side order-slicing misses (demo mandates had the split window off; fixed with a 60-minute split window, ad366ce). Gemma 4 26B-A4B on a Verda A100, 600 requests: 289/301 unsafe held, 2 slicing misses; re-run on 100 slicing requests after the fix: 0 gateway violations. On CRACI's 32 vCPUs (run 43611589764) all 4,000 scripted red-team requests ran in 15.4 s, 2,036/2,036 unsafe held.
- Recipe planner, 7 menus (CI 43595658113, after the R05 crash fix aa67faa): recipe table 7/7, Gemini 5/7, Kimi 5/7; all misses are quantities outside our 35 % bar, 0 allergen-rule violations.
- Search type-aware ranking (9eb15ec): precision@3 90.5 % → 99.3 %, top-1 89.8 % → 100 % on our own 49 tuning queries (a regression test, not held out).
- claude.ai connector (`/connect/mcp`, OAuth): Toivo ordered 1,000 L of lactose-free milk from the real claude.ai on staging (PO-356567FC, €1,560 ex VAT, notify).
- Mandate drafting from plain language: Gemini 4 Hydrogen 23/23, rule parser 23/23 (CI run 43587725624, as in 43566010358; was 15/19 in 43562850006, the misses became validator rules). docs/GEMINI.md.
- Nemotron 3.5 Super VL photo reading: 0 silent errors, 100 % auto-correct over 32 page reads (CI run 43530170621); baseline prompt 16 silent errors. Rows from different runs (docs/NEMOTRON.md).
- New controls since Friday: approve-by = earliest cut-off − 15 min, capped by mandate/sandbox expiry (497be36; `approval_deadline_passed`), Peppol Order/Order Response validated by the official XSD + schematron in CI (9e5ed51), Finnish rule texts (59fcce3), ERP stub labelled simulated (3509492), response options incl. `next_date`, product holds/recalls (`product.held`), stock as buckets, scoped reads (404 like a missing id), `/rules`, `/ui/break.html` (seven attacks in a sandbox).
- 3-minute stage pitch and the 8 most likely questions: [stage-pitch.md](stage-pitch.md).
- LLM eval per model (48 one-sentence cases, all live in CI 43587725624, f31967b): Kimi K3.1 Pro (Verda, EU) 47/48 (p50 9.9 s, ~$0.058/request at list price); Gemini 4 Hydrogen 46/48 (p50 14.8 s, $0.046/request); Nemotron 3.5 Super VL (NVIDIA, free tier) 30/48 (p50 8.2 s). Unsafe blocked or escalated 34/34, 34/34, 33/34; 0 mandate violations auto-ordered by any. Misses: S04 for all three (scripted-only premise; Nemotron's order of a coconut cream with declared allergens went through, hence its 33/34); Gemini's H02 (a milk drink for milk); Nemotron's are understanding and persistence (gave up after 12 tool calls, stopped at a preview). Earlier: 0424796 (CI 43557209740) Kimi 48/49, Gemini 46/49.
- Standing-order demo: milk +18 % vs 4-week median, cost +€135 (+16 %) → ask before renewing.
- Live: production, the judge URL, https://sous.saarikko.dev (Verda, Finland; at cdb7670 on 14.11 14:11Z); staging https://staging.sous.saarikko.dev (ede8518, near current `main`, the fallback). What is on which: docs/STATUS.md#deployed-now.
- CI on CRACI: SBOM + grype gate; deny-by-default egress per job, with a signed egress proof per run (PASS in 43587725624; docs/CRACI.md).
- LLM eval, 5 models x generic / Sous agent (local runs, 48 own sentences, 08144fb, docs/STATUS.md): Sous agent Gemini 4 Hydrogen 48/48, Gemma 4 31B 48/48, Kimi K3.1 Pro 47/48, Gemini 3.8 Flash 46/48, Gemma 4 26B-A4B 40/48; 0 mandate violations auto-ordered in all 10 runs, 34/34 unsafe handled in each.
- Gemini Decisions allergen check (docs/GEMINI.md): 483 of 488 "likely contains" answers confirmed by the declaration (99.0 %) on 600 declared food products; 468 of 4,118 undeclared food products caught.
- Held-out (CI 43620135327, Gemini 4 Hydrogen + Sous agent, 15 blind cases x 3): 0 false auto-approvals, should-hold 9/9 (all by the gateway; no-gateway floor 9), matched 12/45 overall, should-order 3/36, menus cookable 0/9, 12 wrong dates. evals/heldout/findings.yaml. Set 2 (CI 43628767992 @ 6d1b06b, after the fixes, 20 blind cases x 3): matched 15/60, should-order 3/48, false auto-approvals 0, should-hold 12/12 (no-gateway floor 19), cookable 5/9, lines at the right amount 73/167, wrong dates 0, $4.57. evals/heldout2/findings.yaml.
- Earned autonomy: ≥5 unchanged approvals over ≥2 ISO weeks, 0 edited, 0 rejected → proposal ask → notify (PROTOCOL 7.2, sous/ramp.py).
- CRACI vulnerability management (run 43611583279): CRACI's job SBOMs, 201 packages, 0 findings; image 0 Critical, 61 High (2 fixable); gate on fixable Criticals (docs/CRACI.md).
