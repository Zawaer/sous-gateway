# Judge Q&A prep

Answer in two or three sentences, then stop. If you don't know, say so and say what you'd do.

---

**1. Why not just let the LLM decide?**
Because an LLM can be talked into anything and can't be audited. A prompt injection in a product description shouldn't be able to raise a spend limit. The model turns language into a structured request and explains results. Limits, allergens, cut-offs and the tier are plain code that gives the same answer every time, with a reason a human can read.

**2. What if the agent lies about who it is?**
Today the agent sends an `agent_id` and a `mandate_id`; the gateway checks they match, and a human must have issued and signed that mandate, so a stranger can't invent authority. Approvals need a separate approver code. That is still hackathon-grade identity. In production the agent authenticates with OIDC client credentials (or mTLS), and the mandate is bound to the agent's key, so a stolen mandate ID is useless without the key.

**3. How does this scale to 700 suppliers and 25,000 products?**
Search runs on static catalog data in a local index (17,546 products in about 10 s to build), so it costs no backend calls. Live price and availability are fetched only for a shortlist: 2 batched calls per quote, against roughly 36 to poll the whole catalog. Supplier differences show up as data (Stock vs Terminal, reliability, cut-offs), not as code per supplier.

**4. What about Valio Aimo's confidential data and the marketplace roadmap?**
Agents never touch internal systems. They see the catalog and the quotes they ask for, nothing else. The gateway calls the backend on their behalf, with a global cap on API calls and per-IP rate limits. Nothing about ranking, margins or the roadmap is exposed. In production, per-customer prices would only go to an authenticated buyer.

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
A real order API instead of our order store, per-customer prices and contracts, OIDC identity for buyers and agents, mandates as W3C VCs / AP2, push and SMS notifications for notify and ask, and signing-key rotation through JWKS `kid`. The shape doesn't change: those plug into the same quote, mandate and audit flow.

---

## Backup facts
- 17,546 products, fi/sv/en. Search: SQLite FTS5 trigram, no API calls.
- Quote: 2 batched Valio API calls (price + availability). Signed, expiring.
- Hotel run: Valio Eila 1 L × 1,000 at €1.40/L, €1,400 ex VAT, notify tier, 2 API calls total.
- Hotel mandate: notify above €1,000, auto-approve to €2,500, hard limit €10,000.
- Hospital mandate: auto-approve to €400, avoids milk / nuts, unknown allergens → ask, substitutions → ask.
- Tiers: silent / notify / ask / block. Late approvals expire after the cut-off.
- Signatures: Ed25519 JWS; public keys at `/.well-known/jwks.json`.
- Evals: `[EVAL: % correct]` correct, `[EVAL: % unsafe blocked]` unsafe blocked, `[EVAL: API calls per request]` calls per request, on `[EVAL: N]` requests.
