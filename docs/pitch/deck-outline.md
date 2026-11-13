# Deck outline (PDF, 13 slides)

One idea per slide. Big type, few words. Numbers must match `docs/STATUS.md` and `evals/results.json`. LLM numbers come from `docs/LLM_EVAL.md`.

---

### 1. Sous
- AI purchasing agents carry a signed mandate from their human, and Valio Aimo's gateway enforces it.
- Junction 2026 · Valio Aimo: Trusted Trade Agents · https://135-181-8-209.sslip.io · GitHub
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
- Agent → search (17,546 products, no API calls) → signed quote (live price + availability, ≤2 Valio API calls) → mandate check in code → order, held for a human, or refused.
- Four tiers: **silent** (just do it) · **notify** (do it, tell me) · **ask** (wait for me) · **block** (never).
- Notify and ask reach the human: in-app feed, phone push (ntfy), signed webhook.
- **Visual:** left-to-right flow diagram; the four tiers as coloured chips under "mandate check".

### 5. Demo in three orders
- Hotel: Claude Code on a laptop, over MCP: Valio Eila 1 L × 1000, €1,400 ex VAT → **notify**. 2 Valio API calls.
- Hospital: substitute with no allergen data → **ask** → approved on a phone.
- Terminal product past its cut-off → caught, Stock alternative picked.
- **Visual:** three order cards from the web UI, tier chip on each; phone screenshot of the approval.

### 6. Standing orders that notice drift
- A kitchen's weekly order renews itself, but the gateway compares each renewal with the median of its last 4.
- Quantity creep (> +15 % or 3 rises in a row), cost or price per L/KG > +10 %, a switched product, a new supply risk: each lifts the renewal to **ask**.
- Demo: milk +18 % vs the 4-week median, cost +€135 (+16 %) → ask before renewing.
- **Visual:** small sparkline of the 4 past weeks and this week, with the drift findings in plain words.

### 7. Valio suggests, inside the buyer's rules
- Valio Aimo's own selling agent offers cheaper, certified, more reliable or lower-CO₂ alternatives, plus seasonal and new products.
- Every suggestion is pre-checked against the buyer's mandate: nothing it would block is shown, unknown allergens are never pushed.
- Labelled "sponsored by Valio Aimo", offers only (never orders), written to the audit log.
- **Visual:** one suggestion card: "Valio Aimo suggests…", reason, mandate check chip, disclosure line.

### 8. Exceptions we found in the real API
- Terminal products show an order cut-off that has already passed while still promising Monday delivery.
- ~46 % of products have no allergen declaration. We treat unknown as unsafe: a human decides.
- Also handled: out of stock, later delivery date, short stock (split, or ask), price expiring before delivery, lot sizes, cut-off unknown for the requested date, rate limits (last-known data marked stale → human).
- **Visual:** annotated API response with the passed `closingTime` and `availableDate` highlighted.

### 9. Why you can trust it
- The LLM never decides. Limits, allergens, cut-offs, drift and tiers are deterministic code with plain-language reasons.
- Mandates, quotes and receipts are Ed25519-signed (JWS), verifiable by anyone via `/.well-known/jwks.json`. Orders use only the signed payloads; one order per quote; idempotent retries.
- Hash-chained audit log + MariaDB system-versioned tables: "what did the mandate say when this order was decided?" (`AS OF`).
- **Visual:** a decoded receipt JWS next to the audit chain; one `FOR SYSTEM_TIME AS OF` query.

### 10. Any model can be the buyer
- Our LLM buyer agent (`agent/`) speaks the gateway's MCP tools with any OpenAI-compatible model: Gemini, open-weight models on Verda Inference in the EU, self-hosted vLLM. Claude Code and Codex work as they are.
- The runtime binds agent id, mandate and idempotency key; the model only picks search terms, products, amount and date.
- Per-model eval on the same 45 cases (40 apply): Kimi K3.1 Pro (Verda, EU) 36/40 matched, 38/40 need understood · Gemini 4 Hydrogen 37/40, 39/40 · both 31/31 unsafe blocked or escalated · mandate violations auto-ordered: 0.
- **Visual:** a small table, one row per model: correct, unsafe stopped, tool calls, latency, where it runs.

### 11. Numbers
- **45/45** scripted buyer requests handled correctly with no manual fixes.
- **36/36** unsafe actions blocked or escalated · **≤2** Valio API calls per uncached request (0.95 average with the cache) vs ~36 to poll the whole catalog.
- **17,546** products searchable · search first-result accuracy 56 % → **100 %** on 39 fi/sv/en queries.
- **Visual:** four big numbers, no chart. Source line: "evals/results.json, run Fri 13.11".

### 12. Built and run like production
- CI on CRACI: tests with MariaDB, Docker build, CycloneDX SBOM, grype gate on fixable criticals.
- Smoke test of the real compose stack under deny-by-default egress; eval jobs may reach only PyPI, Valio and the model APIs. A gateway holding a supplier key must not talk to anything else.
- Live on Verda in Finland: production and staging, MariaDB, Caddy + TLS.
- **Visual:** the CI job graph with the egress policy per job; the CRACI network trace showing only `junction26.valioaimo.fi`.

### 13. Path to production
- **Identity:** today `agent_id` + signed mandate (bearer agent credentials being added). Next: buyer login via OIDC; agent keys bound to the mandate (mTLS, DPoP or VC-bound keys).
- **Orders and prices:** call Valio Aimo's real order API; per-customer prices and contracts instead of list prices.
- **Mandates as standards:** W3C Verifiable Credentials / AP2 mandates, signed by the buyer, so any wallet or agent platform can carry them.
- **Operations:** key rotation via JWKS `kid`, audit chain head anchored outside the gateway, per-agent quotas.
- **Open:** MCP + OpenAPI + JWS today. MIT-licensed. Any agent works, no lock-in.
- **Visual:** a three-step roadmap (Pilot with one customer → Real order API + bound agent keys → VC/AP2 mandates), ending with the one-liner.
