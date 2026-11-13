# Status

_Last updated: Fri 13.11 ~21:45 (Helsinki)._ Live demo URL: not deployed yet.

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
- Reference buyer (`/buyer/`): fill an editable purchase request, review product matches and ranked substitutes, verify the signed quote against JWKS, submit under a mandate, and check the signed receipt or follow a human approval. Runs in a browser without a model API key. See [buyer setup](../buyer/README.md).
- Good / better / best substitutes (same category, claims and allergen rules kept; compromises listed).
- Valio suggests (proactive selling, `GET /mandates/{id}/suggestions`, MCP `get_suggestions`): cheaper / certified / more reliable / lower-CO2 alternatives to what the buyer orders, seasonal (name keywords for the delivery date) and new (Synkka lastUpdated proxy) products; labelled sponsored by Valio Aimo, pre-checked against the mandate (nothing it would block is shown), offers only, audited as `suggestions.offered`.
- MariaDB storage with system-versioned `mandate`, `purchase_order` and `audit_event`: `/orders/{id}/history` shows every version of an order and the mandate AS OF each decision; `/audit/verify` checks the hash chain and MariaDB's history (any edit or delete of an audit row shows up). SQLite remains the fallback when `VALTAKIRJA_DB_URL` is unset.
- Hardening: approver code for human actions, per-IP rate limit, global cap on Valio API calls.
- CI: pytest (offline + MariaDB service) on every push; runner switchable to CRACI via repo variable `CI_RUNNER`.

## In progress
- Web UI in `web/` (Rene's agent): approval inbox, orders, audit view, mandate viewer.

## Next
- Public deployment for judges.
- Eval in CI (needs Valio data + API key as CI secrets; see Evaluation).
- Notifications (notify / ask) to the human's phone.

## Evaluation
`uv run python scripts/eval.py` runs the 45 scripted buyer requests in `evals/cases.yaml` (hotel, hospital, school and 6 ad-hoc mandates: happy path, splits, partial stock, ask/hard limits, price cap, allergen contains / may contain / unknown, substitutes, Terminal cut-off, out of stock, undeliverable, later delivery, wrong agent, revoked/expired mandate, daily budget, double order, expired quote, idempotent retry, late approval after revocation) through `valtakirja.service` with live Valio Aimo prices and availability, and writes `evals/results.json`. Uses a fresh SQLite state file, never `data/state.db`. Needs `data/` and the API key, and the mock's cut-off of Sun 15.11 17:00 must still be ahead (after that every Stock product reads as cut-off passed), so it isn't in CI yet.

Run of Fri 13.11 ~22:00 (main at 2931d2d + eval):

| Metric | Result |
|---|---|
| Cases passed | 45 / 45 |
| Handled without manual correction | 100 % |
| Unsafe actions blocked or escalated | 36 / 36 = 100 % |
| Human-approval rate | 26.7 % of 45 order decisions (silent 12, notify 2, ask 12, block 19) |
| Valio API calls per request | avg 0.95, max 2 (42 live calls for 45 requests; 128 cache hits) |
| Latency per request | mean ~0.8 s (quote ~0.6 s, almost all of it the live Valio call) |

Before 8d9efc1 the set caught two gateway bugs (now fixed, cases kept as regressions): **P11**, a single product with less stock than needed was ordered silently (200 L ordered, 131 in stock); **H19**, another agent sending an idempotency key the hotel had used got the hotel's order back.

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
