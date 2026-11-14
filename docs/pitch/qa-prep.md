# Judge Q&A prep

Answer in two or three sentences, then stop. If you don't know, say so and say what you'd do.

---

**1. Why not just let the LLM decide?**
Because an LLM can be talked into anything and can't be audited. A prompt injection in a product description shouldn't be able to raise a spend limit. The model turns language into a structured request and explains results. Limits, allergens, cut-offs and the tier are plain code that gives the same answer every time, with a reason a human can read.

**2. What if the agent lies about who it is?**
Today the agent sends an `agent_id` and a `mandate_id`; the gateway checks they match the signed mandate, and quotes and standing orders are bound to that agent, so a stranger can't invent authority or reuse someone else's quote. Approvals need a separate approver code. That is still hackathon-grade identity (see question 15). In production the agent proves a key bound to the mandate, so a stolen mandate ID is useless without the key.

**3. How does this scale to 700 suppliers and 25,000 products?**
Search runs on static catalog data in a local index (17,546 products in about 10 s to build), so it costs no backend calls. Live price and availability are fetched only for a shortlist: at most 2 batched calls per request (0.95 on average with the cache in our eval), against roughly 36 to poll the whole catalog. Supplier differences show up as data (Stock vs Terminal, reliability, cut-offs), not as code per supplier.

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

**9. ~46% of products have no allergen data. Isn't treating them all as unsafe too strict?**
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
Today: the `agent_id` plus a mandate signed by the gateway and issued to exactly that `agent_id`; quotes, orders and standing orders are bound to it. Opt-in bearer credentials for agents are being added. In production the agent proves possession of a key: mTLS, DPoP-bound tokens, or a key named in the mandate as a Verifiable Credential. A leaked mandate id then buys nothing.

**16. Does it depend on one LLM?**
No. The gateway decides without any model. Our buyer agent works with any OpenAI-compatible model (Gemini, open-weight models on Verda Inference in the EU, self-hosted vLLM), and Claude Code works over MCP as it is. We score each model on the same 45 cases: Kimi K3.1 Pro on Verda Inference (EU) matched the buyer without correction in 36/40 cases (90 %), Gemini 4 Hydrogen in 37/40 (92.5 %); both stopped 31/31 unsafe requests (blocked or held for a human) with 0 mandate violations auto-ordered. The runtime, not the model, fills in agent id, mandate and idempotency key.

**17. Standing orders: why not just repeat last week's order?**
Because "same as usual" is where creep hides. Each renewal is quoted live and compared with the median of the last 4 ordered weeks. Quantity up more than 15 % (or 3 rises in a row), cost or price per litre up more than 10 %, a switched product or a new supply risk: the renewal waits for a human. In the demo, milk is +18 % and the bill +€135 (+16 %), so it asks.

---

## Backup facts
- 17,546 products, fi/sv/en. Search: SQLite FTS5 trigram, no API calls.
- Quote: at most 2 batched Valio API calls (price + availability); 0.95 per request on average with the cache. Signed, expiring.
- Search first-result accuracy 56 % → 100 % on 39 fi/sv/en queries.
- ~46 % of products have no allergen declaration: unknown → human.
- Hotel run: Valio Eila 1 L × 1,000 at €1.40/L, €1,400 ex VAT, notify tier, 2 API calls total.
- Hotel mandate: notify above €1,000, auto-approve to €2,500, hard limit €10,000, daily limit €8,000.
- Hospital mandate: auto-approve to €400, avoids milk / nuts, unknown allergens → ask, substitutions → ask.
- Tiers: silent / notify / ask / block. Late approvals expire after the cut-off or delivery date.
- Signatures: Ed25519 JWS; public keys at `/.well-known/jwks.json`.
- Eval: 45/45 cases pass; 36/36 unsafe actions blocked or escalated; human-approval rate 26.7 % (silent 12, notify 2, ask 12, block 19).
- LLM eval per model (40 cases apply): Kimi K3.1 Pro on Verda Inference (EU) 36/40 matched (90 %), need understood 95 %; Gemini 4 Hydrogen 37/40 (92.5 %), 97.5 %; both 31/31 unsafe blocked or escalated, 0 mandate violations auto-ordered; ~3.1 tool calls and 1.62 Valio calls per request; p50 8.4 s (Kimi) / 11.3 s (Gemini); ~22-24k tokens per request. 6 of 7 misses failed safe; the 7th was an in-mandate order for a different milk.
- Standing-order demo: milk +18 % vs 4-week median, cost +€135 (+16 %) → ask before renewing.
- Live: production https://135-181-8-209.sslip.io (Verda, Finland); staging https://staging.135-181-63-134.sslip.io.
- CI on CRACI: SBOM + grype gate; smoke under deny-by-default egress.
