# Deck outline (PDF, 10 slides)

One idea per slide. Big type, few words. Numbers must match `docs/STATUS.md` and `evals/results.json`.

---

### 1. Valtakirja
- AI purchasing agents carry a signed mandate from their human, and Valio Aimo's gateway enforces it.
- Junction 2026 · Valio Aimo: Trusted Trade Agents · public URL · GitHub
- **Visual:** name large, one-liner below, small QR code to the live demo.

### 2. Before: 13 minutes and a phone call
- A general shopping agent (Puntari) was asked for 1,000 L of lactose-free milk by Monday.
- 13 minutes of research. Conclusion: phone a wholesaler on Saturday. Prices and stock aren't readable by agents.
- **Visual:** screenshot of Puntari's final answer with a "13:00" timer, next to our receipt with "seconds".

### 3. Who defines the interface?
- Agents will buy from wholesalers either way. The question is whose rules they follow.
- If Valio Aimo doesn't define an open, safe interface, someone else's platform will.
- **Visual:** two arrows into Valio Aimo: "someone else's platform" (greyed) vs "Valio Aimo's own front door" (highlighted).

### 4. How it works
- Agent → search (17,546 products, no API calls) → signed quote (live price + availability, 2 Valio API calls) → mandate check in code → order, held for a human, or refused.
- Four tiers: **silent** (just do it) · **notify** (do it, tell me) · **ask** (wait for me) · **block** (never).
- **Visual:** left-to-right flow diagram; the four tiers as coloured chips under "mandate check".

### 5. Demo in three orders
- Hotel: 1,000 × Valio Eila 1 L at €1.40/L, €1,400 ex VAT → **notify**. Placed by Claude Code over MCP from a laptop.
- Hospital: substitute with no allergen data → **ask** → approved on a phone.
- Terminal product past its cut-off → caught, Stock alternative picked.
- **Visual:** three order cards from the web UI, tier chip on each; phone screenshot of the approval.

### 6. Exceptions we found in the real API
- Terminal products show an order cut-off that has already passed while still promising Monday delivery.
- ~46% of products have no allergen declaration. We treat unknown as unsafe: a human decides.
- Also handled: out of stock, later delivery date, short stock (split across products), price expiring before delivery, lot sizes.
- **Visual:** annotated API response with the passed `closingTime` and `availableDate` highlighted.

### 7. Why you can trust it
- The LLM never decides. Limits, allergens, cut-offs and tiers are deterministic code with plain-language reasons.
- Mandates, quotes and receipts are Ed25519-signed (JWS), verifiable by anyone via `/.well-known/jwks.json`.
- Hash-chained audit log + MariaDB system-versioned tables: "what did the mandate say when this order was decided?" (`AS OF`).
- **Visual:** a decoded receipt JWS next to the audit chain; one `FOR SYSTEM_TIME AS OF` query.

### 8. Numbers
- `[EVAL: % correct]` of `[EVAL: N]` scripted requests handled correctly with no manual fixes.
- `[EVAL: % unsafe blocked]` of unsafe orders stopped · `[EVAL: API calls per request]` Valio API calls per request (vs ~36 to poll the whole catalog).
- `[EVAL: % without human]` handled without a human · 17,546 products searchable.
- **Visual:** four big numbers, no chart. Source line: "evals/results.json, run on <date>".

### 9. Assumptions we made
- Unknown allergen data = unsafe. A passed `closingTime` = the quoted date can't be met.
- CO₂ is a product-group average × quantity: an estimate, labelled as such.
- Pack contents from the product name, checked against price / comparison price; Terminal quantity 999999 = unknown.
- Valio Aimo has no order API in the mock: orders, approvals and the audit log are ours to design.
- **Visual:** plain list, two columns. Honest and short.

### 10. Path to production
- **Identity:** buyer and agent identity via OIDC (client credentials), mandates bound to the agent's key.
- **Orders and prices:** call Valio Aimo's real order API; per-customer prices and contracts instead of list prices.
- **Mandates as standards:** W3C Verifiable Credentials / AP2 mandates, so any wallet or agent platform can carry them.
- **Operations:** push/SMS notifications for notify and ask, key rotation via JWKS `kid`, rate limits and per-agent quotas.
- **Open:** MCP + OpenAPI + JWS today. MIT-licensed. Any agent works, no lock-in.
- **Visual:** a three-step roadmap (Pilot with one customer → Real order API + OIDC → VC/AP2 mandates), ending with the one-liner.
