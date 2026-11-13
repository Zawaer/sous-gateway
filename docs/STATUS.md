# Status

_Last updated: Fri 13.11 ~21:10 (Helsinki)._ Live demo URL: not deployed yet.

## Vision (one product: Valtakirja)
Valio Aimo's front door for AI purchasing agents. Any buyer's agent (a hotel's, a hospital's) connects over **MCP or OpenAPI**, searches the catalog, gets a **signed quote** with live price and availability, and orders. Each agent carries a **mandate** (*valtakirja*) from its human: spend limits, allergen rules, substitution rules. Valio Aimo's gateway enforces the mandate in plain code and puts every order into one of four tiers:

- **Silent:** routine, ordered and logged.
- **Notify:** ordered, and the human gets an FYI.
- **Ask:** held until a human approves on their phone.
- **Block:** refused, with the rule that fired.

The LLM never decides; it only turns language into a structured request and explains results. Both the buyer and Valio Aimo read the same hash-chained audit log.

Pitch line: *"AI purchasing agents carry a signed mandate from their human, and Valio Aimo's gateway enforces it."*

## Works (on main)
- Catalog search over 17.5k products (fi/sv/en, SQLite FTS5 trigram), filters for claims, allergens, labels, temperature. No live API calls.
- Quotes: live price + availability for a shortlist only (2 batched calls), pack sizes and lot sizes, VAT, cut-off checked against now, stock / Terminal / reliability / price-expiry issues, split across products when stock is short, alternatives with the reason each was rejected. Signed (Ed25519 JWS), expiring.
- Mandate engine with the four tiers; demo mandates in `scripts/seed.py` (hotel, hospital, school).
- Orders: idempotency keys, one order per quote, cut-off re-checked at order and at approval time, signed receipts.
- Approvals: approve/reject with a note; late approvals expire if the cut-off has passed.
- Audit log: append-only, hash-chained, `/audit/verify`.
- REST (`/docs`) and MCP (`/mcp`) expose the same operations.

## In progress
- Web UI in `web/` (Rene's agent): approval inbox, orders, audit view, mandate viewer.
- Demo buyer agent (chat where the chef types the request) — needs an LLM API key in `.env`.

## Next
- Public deployment for judges.
- MariaDB for the audit log (system-versioned tables) for the MariaDB tech challenge.
- Substitutes: good / better / best ranking within the category, allergen-safe.
- Eval set (~30 scripted requests): % correct, % unsafe blocked, API calls per request.
- Notifications (notify / ask) to the human's phone.

## Assumptions (shown to judges)
- Unknown allergen data is treated as unsafe: never auto-ordered for a mandate with allergen rules.
- CO₂ is a product-group average × quantity, an estimate, not a product measurement.
- Content per pack comes from the pack size in the name, confirmed against Valio Aimo's price / comparison price ratio; the ratio wins when they disagree.
- A passed `closingTime` means the quoted `availableDate` can't be met, even if the API still reports it.
- Stock `availableAmount` 9999999999 = plenty; Terminal 999999 = unknown.
- The mock ignores the requested date (earliest date is always the same); delivery date = max(requested, earliest).

## Findings about the mock API
- Terminal products: `closingTime` already in the past (Thu 12.11) while `availableDate` is Mon 16.11.
- Some Stock products also had a cut-off today 13:00 (passed).
- Out of stock = Stock with `availableAmount` 0 and no date; undeliverable = empty `scheduleLines` and no logistic model.
- ~46 % of products have no allergen declaration at all.

## Open questions for the Valio Aimo mentors
1. Is it right that the order endpoint, approvals and audit are ours to design? (We assume yes.)
2. MCP vs OpenAPI vs a commerce standard (ACP/AP2)? We offer MCP + OpenAPI over the same operations.
3. What must always need human sign-off in your view?
4. Is the passed cut-off on Terminal products intentional?
5. When do the webshop test accounts arrive, and do they matter for judging?

Answers: _(not asked yet)_
