# Valtakirja trust protocol

Version 0.2 (Junction 2026). This document describes what the gateway in this repo does today, so an outside agent developer can build a buyer agent against it. Where the code makes a simplification for the demo, it says so.

The short version: a human issues a **mandate** to their purchasing agent. The agent asks the gateway for a **quote**. The gateway signs the quote and previews it against the mandate. The agent places an **order** on that quote. A deterministic rules engine puts the order into one of four **tiers**. Every step lands in a hash-chained **audit log** that both sides can read and verify.

## 1. Actors

| Actor | Who | What they do |
|---|---|---|
| Buyer human | e.g. a hotel F&B manager | Issues and revokes the mandate. Approves or rejects held orders. |
| Buyer agent | Any AI agent (Claude, Gemini, a script) | Searches, asks for quotes, places orders. Identified by `agent_id`. Can never approve its own orders. |
| Gateway | Valio Aimo's Valtakirja service (this repo) | Holds the catalog, calls Valio Aimo's price and availability APIs, signs mandates, quotes and receipts, enforces the mandate, writes the audit log. |
| Approver | The buyer human (or someone they delegate to) | Calls approve or reject on orders in `pending_approval`. |

| Valio Aimo's suggestions | `valtakirja/suggest.py`, actor `valio-aimo-suggestions` | Offers sponsored, mandate-checked suggestions to the buyer agent. Never quotes or orders. |

Human actions (issue or revoke a mandate, approve or reject an order, pause or resume a standing order, store simulated standing-order history, send a test notification) require the `X-Approver-Code` header when `VALTAKIRJA_APPROVER_CODE` is set. This is a demo stand-in for the buyer's real login (see [DECISIONS.md](DECISIONS.md)).

**Agent identity today** is the `agent_id` the agent sends plus the signed mandate issued to exactly that `agent_id`; quotes, orders and standing orders are bound to it. Opt-in bearer credentials for agents are being added. In production the agent would prove possession of a key bound to the mandate (mTLS, DPoP or a VC-bound key), see section 14.

## 2. Transports

The same operations are exposed twice, over one service layer (`valtakirja/service.py`):

- **REST / OpenAPI**: `GET /openapi.json`, interactive docs at `/docs`.
- **MCP** (Streamable HTTP) at `/mcp` and `/mcp/`, or stdio (`uv run python -m valtakirja.mcp_server`). Tools: `search_products`, `get_product`, `request_quote`, `suggest_substitutes`, `place_order`, `get_order`, `get_mandate`, `get_suggestions`, `create_standing_order`, `renew_standing_order`, `get_standing_order`. Each tool carries MCP annotations (read-only, idempotent, open-world). Resources: `valtakirja://allergens` (GS1 codes and families) and `valtakirja://mandates/{mandate_id}`. Prompt: `purchase(request, mandate_id, agent_id, delivery_date?)` walks a model through search, quote and order. The server also ships with usage instructions for the model.

| Operation | REST | MCP tool |
|---|---|---|
| Search catalog | `GET /products?q=&claims=&avoid_allergens=&labels=&temperature=&category=&limit=&agent_id=` | `search_products` |
| Product details | `GET /products/{id}` | `get_product` |
| Substitutes | `GET /products/{id}/substitutes?delivery_date=&amount=&unit=&mandate_id=` | `suggest_substitutes` |
| Quote | `POST /quotes`, `GET /quotes/{id}` | `request_quote` |
| Order | `POST /orders`, `GET /orders`, `GET /orders/{id}` | `place_order`, `get_order` |
| Order history | `GET /orders/{id}/history` | |
| Approve / reject | `POST /orders/{id}/approve`, `POST /orders/{id}/reject` (human) | none, on purpose |
| Mandates | `POST /mandates` (human), `GET /mandates`, `GET /mandates/{id}`, `GET /mandates/{id}/history`, `POST /mandates/{id}/revoke` (human) | `get_mandate` |
| Valio suggests | `GET /mandates/{id}/suggestions?agent_id=&delivery_date=&limit=&basis=` | `get_suggestions` |
| Standing orders | `POST /standing-orders`, `GET /standing-orders?mandate_id=`, `GET /standing-orders/{id}` | `create_standing_order`, `get_standing_order` |
| Renew a standing order | `POST /standing-orders/{id}/renew` `{delivery_date?, agent_id?, amounts?}` | `renew_standing_order` |
| Pause / resume (human) | `POST /standing-orders/{id}/pause`, `POST /standing-orders/{id}/resume` | none |
| Simulated history (demo, human) | `POST /standing-orders/{id}/simulate-history` | none |
| Notifications | `GET /notifications?buyer=&order_id=&limit=`, `POST /notifications/test` (human) | |
| Audit | `GET /audit?subject=&buyer=&limit=`, `GET /audit/verify` | |
| Public key | `GET /.well-known/jwks.json` | |
| Allergen codes | `GET /allergens` | resource `valtakirja://allergens` |
| Health | `GET /health` (Valio call counters, storage backend, whether the approver code is required) | |

REST errors, including schema and HTTP errors, are `{"error": "<code>", "message": "<text>"}` with an HTTP status (plus `retry_after` on `valio_unavailable` when known). MCP tools return `{"error", "message", "hint", "retryable"}` as a tool result with `isError: true` instead of raising; `hint` tells the model what to do next (codes in section 7).

## 3. Signatures

Everything the gateway signs is a **compact JWS** (RFC 7515) with **Ed25519** (`alg: EdDSA`, RFC 8037).

- Header: `{"alg": "EdDSA", "kid": "<kid>", "typ": "<typ>"}`.
- Header and payload are serialised as canonical JSON (keys sorted, separators `,` and `:`, UTF-8, no ASCII escaping), then base64url without padding.
- `kid` is the first 16 hex characters of SHA-256 over the raw 32-byte public key.
- The public key is published as a JWKS at `GET /.well-known/jwks.json`:
  `{"keys": [{"kty": "OKP", "crv": "Ed25519", "x": "<base64url>", "kid": "<kid>", "alg": "EdDSA", "use": "sig"}]}`.

| `typ` | Payload |
|---|---|
| `valtakirja-mandate+jws` | The mandate object (section 4) |
| `valtakirja-quote+jws` | The quote object (section 5) |
| `valtakirja-order+jws` | The order receipt: `id, quote_id, mandate_id, status, total_ex_vat, lines, decision, approval, updated_at`. Re-signed after a human decision. |

The gateway trusts only signed payloads: an order is built from the quote's verified JWS payload and the mandate's verified JWS payload, never from the stored copies. A mandate whose signature no longer verifies gives `mandate_signature`; a quote, `quote_signature`.

The key is generated on first start and stored at `$VALTAKIRJA_DATA_DIR/gateway_ed25519.pem`. There is one key and no rotation yet.

Note that the gateway signs the mandate, not the human. The signature proves "the gateway recorded this mandate as issued by `issued_by`", authenticated by the approver code. A production version would have the human sign it (section 14).

## 4. The mandate

Source: `valtakirja/mandate.py`, class `Mandate`. All money is EUR excluding VAT.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `id` | string | required | Mandate id, e.g. `M-HOTEL-AURORA`. |
| `buyer` | string | required | Organisation the agent buys for. |
| `agent_id` | string | required | The only agent allowed to order under this mandate. |
| `issued_by` | string | required | The human who issued it. |
| `valid_until` | datetime or null | null | After this, every order is blocked. |
| `revoked` | bool | false | Set by `POST /mandates/{id}/revoke`. Blocks every order. |
| `notify_above_eur` | number or null | null | Above this, the order goes through and the human gets an FYI. |
| `auto_approve_limit_eur` | number | 500 | Up to this, no human is needed. Above it, the order is held. |
| `hard_limit_eur` | number or null | 5000 | Above this, the order is refused. |
| `daily_limit_eur` | number or null | null | Today's `ordered` total plus this order above the limit means a human must approve. |
| `allowed_categories` | list or null | null | Valio Aimo category codes. Null means any. |
| `avoid_allergens` | list | [] | GS1 allergen codes, e.g. `AM` milk, `AN` tree nuts, `AP` peanuts, `AW` gluten cereals. `AM`, `AN` and `AW` expand to their whole family (see `GET /allergens`). |
| `allow_may_contain` | bool | false | If false, "may contain" counts as contains. |
| `unknown_allergens` | `ask` or `block` | `ask` | What to do when a product has no allergen declaration. Never `silent`: unknown is never safe. |
| `required_claims` | list | [] | Claims every line must carry, e.g. `FREE_FROM_LACTOSE`. |
| `substitutions` | tier | `ask` | Tier for any line marked `substitute_for`. |
| `terminal_products` | tier | `silent` | Tier for products shipped from the supplier's warehouse (stock unknown). |
| `min_delivery_reliability` | number or null | 0.9 | Lines with historical full-delivery rate below this are held. |
| `later_delivery` | `notify`, `ask` or `block` | `ask` | Tier when the earliest delivery is after the requested date. |
| `max_comparison_price_eur` | map | {} | Cap per comparison unit, e.g. `{"L": 2.5}`. Above it, held. |

Issuing (`POST /mandates`) re-signs and stores the mandate and writes `mandate.issued` to the audit log. Re-issuing with the same id replaces it; with MariaDB the old version stays queryable (section 12).

## 5. The quote

`POST /quotes` with:

```json
{
  "agent_id": "aurora-purchasing-agent",
  "mandate_id": "M-HOTEL-AURORA",
  "delivery_date": "2026-11-16",
  "items": [
    {"product_ids": ["<id>", "<id>"], "amount": 1000, "unit": "L", "substitute_for": null, "note": null}
  ]
}
```

- `unit` is `L`, `KG` or `SALES_UNIT` (packs). With L or KG the gateway converts to packs and rounds up to the allowed lot size.
- Several `product_ids` for one item means "these are candidates". The gateway prices all of them, ranks orderable ones by comparison price (EUR per L or KG), and fills the need cheapest first, splitting across products when stock is short.
- One `product_id` that can't be ordered is kept in the quote, so the reasons are visible and the mandate blocks it.
- At most 50 items and 200 candidate ids per quote. Price and availability are fetched in two batched calls per item (price cached 10 min, availability 60 s), so a one-item quote costs at most 2 live calls.

Response: `{"quote": {...}, "jws": "<compact JWS of quote>"}`. Quote fields:

| Field | Meaning |
|---|---|
| `id` | `Q-XXXXXXXX` |
| `agent_id`, `mandate_id` | As requested. |
| `issued_at` | Gateway time (Europe/Helsinki, ISO 8601, ms). |
| `expires_at` | `min(issued_at + VALTAKIRJA_QUOTE_TTL, earliest order_cutoff of any orderable line)`. Default TTL 900 s. |
| `requested_delivery_date` | As requested. |
| `items[]` | The requested items, signed with the quote, so a buyer can match each line to its need. |
| `currency` | `EUR` |
| `lines[]` | One per chosen product (below). |
| `total_ex_vat`, `total_inc_vat` | Sums of line totals. |
| `uncovered[]` | Needs that couldn't be filled: `{item, item_index, uncovered, unit}` or `{item, item_index, uncovered, reason}`. |
| `alternatives_considered[]` | Up to 6 per item: `{product_id, name, item_index, comparison_price, orderable, why_not[]}`. |
| `valio_api_calls` | Live calls this quote cost: `{price_calls, availability_calls, cache_hits}`. |
| `mandate_preview` | Only with `mandate_id`: `{outcome, findings[]}`, what will happen if this quote is ordered now. |

Each line has: `product_id, name, brand, sales_unit, content_per_sales_unit {amount, unit, source}, quantity, covers {amount, unit}, unit_price_ex_vat, unit_price_inc_vat, vat_rate, comparison_price {ex_vat, per}, list_price_ex_vat, line_total_ex_vat, line_total_inc_vat, logistic_model (Stock|Terminal), available_amount, stock (known|plenty|unknown), available_date, delivery_date, order_cutoff, delivery_reliability, co2_estimate_kg, co2_note, labels, claims, allergens, temperature, orderable, issues[], item_index` (which entry of `items[]` it covers), and `substitute_for` when set.

`delivery_date = max(requested, available_date)`. `co2_estimate_kg` is a product-group average times quantity, labelled as an estimate.

### Issue codes (`valtakirja/quote.py`)

Each issue is `{code, severity, message}`. A line with any `blocking` issue has `orderable: false`.

| Code | Severity | Meaning |
|---|---|---|
| `UNIT_MISMATCH` | blocking | Need is in L but the product is measured in KG, or the reverse. |
| `CONTENT_UNKNOWN` | blocking | Can't tell how many L or KG one pack holds, so quantity can't be computed. |
| `NO_PRICE` | blocking | Valio Aimo returned no price. |
| `NO_AVAILABILITY` | blocking | Valio Aimo returned no availability. |
| `SALES_BLOCK` | blocking | Product is sales-blocked. |
| `UNDELIVERABLE` | blocking | No schedule lines: no delivery date at all. |
| `OUT_OF_STOCK` | blocking | No available date, or available amount 0. |
| `CUTOFF_PASSED` | blocking | The order cut-off (`closingTime`) for the quoted date is in the past, even if the API still reports the date. Also added at order time if the cut-off passed after the quote was issued. |
| `CUTOFF_UNKNOWN` | warning | Valio Aimo reported a cut-off only for an earlier available date, so the cut-off for the requested date isn't known. The line gets no `order_cutoff` and doesn't shorten the quote's expiry. |
| `LOT_SIZE` | warning | Sold in lots; quantity rounded up. |
| `PRICE_EXPIRES` | warning | Price `validTo` is before the delivery date. |
| `LATER_DATE` | warning | Earliest delivery is after the requested date. |
| `STOCK_UNKNOWN` | warning | Terminal product: supplier stock is unknown. |
| `PARTIAL_STOCK` | warning | Known stock is below the quantity. Removed from lines that `plan` sized to stock; where it stays, the rule `availability.partial_stock` holds the order for a human. |
| `LOW_RELIABILITY` | warning | Historical full-delivery rate below 0.9. |
| `DATA_STALE` | warning | Valio Aimo couldn't be reached live (rate limit, error, or the gateway paused calls after a 429), so the line uses the last copy fetched: price up to 15 min old, availability up to 5 min. The cut-off is still checked against now. Always sends the order to a human (`availability.data_stale`). |

## 6. Tiers and rules

The rules engine (`mandate.evaluate`) is plain Python. No LLM is involved. It returns a list of findings `{rule, outcome, message, product_id}` and the overall tier is the strictest finding.

| Tier | Order status | Meaning |
|---|---|---|
| `silent` | `ordered` | Routine. Ordered and logged. |
| `notify` | `ordered` | Ordered; the human gets an FYI. |
| `ask` | `pending_approval` | Held until a human approves or rejects. |
| `block` | `refused` | Refused, with the rule that fired. |

If the tier is `silent` or `notify`, a final `mandate.ok` finding is added with the total.

| Rule id | Tier | Fires when |
|---|---|---|
| `mandate.revoked` | block | Mandate is revoked. |
| `mandate.expired` | block | Now is after `valid_until`. |
| `mandate.agent` | block | Calling `agent_id` is not the mandate's `agent_id`. |
| `order.empty` | block | The quote has no lines. |
| `spend.hard_limit` | block | Total > `hard_limit_eur`. |
| `spend.auto_limit` | ask | Total > `auto_approve_limit_eur` (and not over the hard limit). |
| `spend.notify` | notify | Total > `notify_above_eur` (and within the auto limit). |
| `spend.daily_limit` | ask | Today's ordered total + this total > `daily_limit_eur`. |
| `need.partial` | ask | Part of the need is uncovered. |
| `availability.<code>` | block | Any blocking issue on a line, code lowercased: `availability.unit_mismatch`, `availability.content_unknown`, `availability.no_price`, `availability.no_availability`, `availability.sales_block`, `availability.undeliverable`, `availability.out_of_stock`, `availability.cutoff_passed`. |
| `availability.partial_stock` | ask | Line still has `PARTIAL_STOCK`: known stock is below the quantity ordered. |
| `delivery.later_date` | mandate's `later_delivery` | Line has `LATER_DATE`. |
| `price.expires` | ask | Line has `PRICE_EXPIRES`. |
| `availability.data_stale` | ask | Line has `DATA_STALE`: never ordered on last-known data without a human. |
| `product.category` | block | Category not in `allowed_categories`. |
| `product.claims` | block | Line lacks a `required_claims` claim. |
| `allergen.contains` | block | Line contains an avoided allergen. |
| `allergen.may_contain` | block | Line may contain one and `allow_may_contain` is false. |
| `allergen.unknown` | mandate's `unknown_allergens` (ask or block) | Mandate has allergen rules and the product has no allergen declaration. |
| `substitution` | mandate's `substitutions` | Line is marked `substitute_for`. |
| `supply.terminal` | mandate's `terminal_products` | Terminal product (only emitted when that setting isn't `silent`). |
| `supply.reliability` | ask | `delivery_reliability` < `min_delivery_reliability`. |
| `price.comparison_cap` | ask | Comparison price above `max_comparison_price_eur[per]`. |
| `mandate.ok` | silent or notify | All checks passed. |

Standing-order renewals add drift rules (section 8): `standing.quantity_creep`, `standing.quantity_trend`, `standing.cost_drift`, `standing.price_drift`, `standing.product_switched`, `standing.new_terminal`, `standing.new_reliability`, each tier ask.

Warnings `LOT_SIZE`, `CUTOFF_UNKNOWN`, `STOCK_UNKNOWN` and `LOW_RELIABILITY` have no rule of their own. Terminal and reliability are judged from the line's `logistic_model` and `delivery_reliability` fields against the mandate instead.

## 7. Orders

`POST /orders` with `{agent_id, mandate_id, quote_id, idempotency_key?}`. The gateway, in order:

All steps run under one lock, so concurrent requests can't double order, overrun the daily limit, or both approve and reject the same order.

1. If `idempotency_key` matches an existing order with the same `agent_id`, `mandate_id` and `quote_id`, returns that order unchanged. Use one key per quote so retries never order twice. The same key with any other agent, mandate or quote is `idempotency_key_reused`.
2. Loads the mandate and verifies its signature (`mandate_signature`); the signed payload is what the engine uses.
3. Loads the quote and verifies its signature (`quote_signature`); everything below works from the signed payload.
4. Checks the quote was issued to this `agent_id` and `mandate_id` (`quote_mismatch`).
5. Rejects a quote already used (`quote_used`): **one order per quote**, enforced by an atomic `UPDATE ... WHERE order_id IS NULL`. If storing the order fails, the quote is released again.
6. Rejects an expired quote (`quote_expired`, audit event `order.rejected`).
7. Re-checks every line's cut-off against the clock (no API call) and adds `CUTOFF_PASSED` where needed.
8. Re-runs the mandate engine with today's spend, maps the tier to a status, signs a receipt, stores the order, and sends a notification for notify and ask (section 10).

Order fields: `id (PO-XXXXXXXX), quote_id, mandate_id, agent_id, buyer, status, total_ex_vat, total_inc_vat, delivery_date, lines[], decision {outcome, findings[], by: "mandate-engine", at}, approval, order_url, approval_url, idempotency_key, created_at, updated_at, receipt_jws`.

### Statuses

```mermaid
stateDiagram-v2
    [*] --> ordered: silent / notify
    [*] --> pending_approval: ask
    [*] --> refused: block
    pending_approval --> ordered: approved
    pending_approval --> rejected: rejected by human
    pending_approval --> expired: approved after a cut-off or the delivery date
    pending_approval --> refused: approved, but mandate revoked or expired meanwhile
```

| Status | Meaning |
|---|---|
| `ordered` | Accepted. (In this demo there is no downstream Valio Aimo order API; see DECISIONS.) |
| `pending_approval` | Waiting for a human. `approval_url` points to the UI. |
| `refused` | Blocked by the mandate, or approved after the mandate was revoked or expired. |
| `rejected` | A human rejected it. |
| `expired` | A human approved it, but a line's cut-off or the delivery date had already passed. |

Approve or reject: `POST /orders/{id}/approve` or `/reject` with `{"by": "<name>", "note": "..."}` and the approver header. Only `pending_approval` orders can be decided (`not_pending` otherwise, also when a concurrent decision won). The result is stored in `approval {by, approved, note, at, result_reason}`.

The `receipt_jws` is signed when the order is placed and **re-signed after the human decision**, so the latest receipt covers `status`, `approval` and `updated_at`. The engine's original decision stays in `decision`, in the audit log and in the order history.

### Error codes

| Code | HTTP | When | Retry as-is? |
|---|---|---|---|
| `invalid_argument` | 400 | A field is malformed (standing `amounts` not one positive number per item; over MCP also bad dates). | no |
| `invalid_request` | 422 | REST request doesn't match the schema; `message` names each field, `details` has the raw list. | no |
| `unauthorized` | 401 | Human action without the right `X-Approver-Code`. | no |
| `too_many_candidates` | 400 | More than 200 candidate ids in a quote. | no |
| `mandate_not_found`, `quote_not_found`, `order_not_found`, `product_not_found`, `standing_not_found` | 404 | Unknown id. | no |
| `agent_mismatch` | 403 | Suggestions asked for by an agent that doesn't hold the mandate. | no |
| `standing_agent_mismatch` | 403 | A standing order created or renewed by an agent other than the mandate's / standing order's. | no |
| `mandate_signature` | 409 | The stored mandate's signature doesn't verify. | no |
| `quote_signature` | 409 | The stored quote's signature doesn't verify. | no |
| `quote_mismatch` | 409 | The quote was issued to another agent or mandate. | no |
| `quote_used` | 409 | The quote already has an order. | no |
| `quote_expired` | 409 | Past `expires_at`; ask for a new quote. | no |
| `idempotency_key_reused` | 409 | The key belongs to a different order. Use a fresh key. | no |
| `not_pending` | 409 | Approve or reject on an order that isn't `pending_approval`. | no |
| `standing_paused` | 409 | Renewal of a paused standing order. | no |
| `already_renewed` | 409 | This standing order already has an ordered or pending renewal for that delivery date. | no |
| `rate_limited` | 429 | Per client IP, on POST (`VALTAKIRJA_RATE_LIMIT_PER_MIN`). | after a minute |
| `valio_unavailable` | 503 | Valio Aimo error, timeout, rate limit or malformed reply, or the gateway's per-minute budget of live calls is used up, and no recent last-known copy of the data exists (otherwise the quote is served with `DATA_STALE`). Carries `retry_after` seconds in the body and a `Retry-After` header when known. | yes, after `retry_after` |
| `internal_error` | (MCP only) | Unexpected error. | yes, once |

Over MCP each error also carries `hint` (what the agent should do next) and `retryable`.

## 8. Standing orders and drift detection

Source: `valtakirja/standing.py`. A standing order is a kitchen's recurring weekly order, renewed by its agent under the mandate. The gateway remembers what each renewal actually ordered and checks every new renewal against that history, in plain code, before placing it.

Create: `POST /standing-orders` (MCP `create_standing_order`) with `{mandate_id, agent_id, name, weekday, items[]}`; each item is `{product_ids[], amount, unit (L|KG|SALES_UNIT), label?}`. The agent must hold the mandate (`standing_agent_mismatch`). Creating places no order. Id `SO-XXXXXXXX`.

Renew: `POST /standing-orders/{id}/renew` (MCP `renew_standing_order`) with `{delivery_date?, agent_id?, amounts?}`. `delivery_date` defaults to the next delivery weekday; `amounts` (one per item) overrides this week's amounts. The gateway:

1. Refuses a paused standing order (`standing_paused`) or a renewal that already has an ordered or pending order for that date (`already_renewed`).
2. Requests a live quote for the items (the cheapest orderable candidate per item, as in section 5).
3. Compares it with the **baseline**: the median of the last 4 renewals that ended up `ordered` (approved drift becomes the new normal; refused, rejected and pending renewals don't count).
4. Places the order with the drift findings added to the mandate's own findings, with idempotency key `standing:<id>:<date>:<try>`. Any drift lifts the order to at least **ask**.

| Rule id | Tier | Fires when |
|---|---|---|
| `standing.quantity_creep` | ask | An item's amount is more than +15 % over its median. |
| `standing.quantity_trend` | ask | An item's amount went up 3 renewals in a row. |
| `standing.cost_drift` | ask | The total is more than +10 % over the median total. |
| `standing.price_drift` | ask | An item's price per L / KG / unit is more than +10 % over its median. |
| `standing.product_switched` | ask | A different product was chosen than last time. |
| `standing.new_terminal` | ask | A Terminal (supplier-stock) product appears where the last renewal had none. |
| `standing.new_reliability` | ask | A low-reliability product appears where the last renewal had none. |

The response is `{renewal, order, drift {findings, summary, baseline}}`; `drift.summary` is one plain sentence for the human. `GET /standing-orders/{id}` (MCP `get_standing_order`) returns the standing order with its renewals (newest first) and the baseline. Pause and resume are human actions.

Demo history: `POST /standing-orders/{id}/simulate-history` (human) stores past renewals marked `simulated: true`. Only the amounts are made up; they are priced at a real signed quote under the same mandate, never at an invented Valio response. With `scripts/demo_standing.py`, the hotel's milk is +18 % over its 4-week median and the cost +€135 (+16 %), so the renewal is held for a human.

## 9. Valio suggests

Source: `valtakirja/suggest.py`. The seller's side of the conversation: Valio Aimo offers the buyer's agent products it may want. `GET /mandates/{id}/suggestions?agent_id=&delivery_date=&limit=5&basis=<product id>...` (MCP `get_suggestions`). Only the mandate's agent may ask (`agent_mismatch`).

Kinds:

- `alternative`: a better deal on something the buyer already orders (from its order history or `basis`): same category, measure and temperature, cheaper per L/KG, delivered in full more often, a certification label, or a lower CO₂ estimate at a small premium at most.
- `seasonal`: a seasonal product for the delivery date, found by name keywords (proxy: the data has no season dates).
- `new`: recently (re)listed in a category the buyer orders from: its Synkka record was updated in the last 30 days of the dataset (proxy: no launch date).

Guardrails, all deterministic:

- **Offers only.** Nothing is quoted or ordered. Each suggestion has `how_to_accept`: request a quote, show the buyer, order only if they agree.
- **Mandate first.** Anything the mandate would block (allergens contained or possibly contained, missing required claims, disallowed category, over a comparison-price cap) or that can't be delivered is never shown. Products with unknown allergen data are never pushed to a buyer with allergen rules. Each suggestion carries `mandate_check {outcome, findings}`: the tier it would get at the usual quantity, from the same engine.
- **Labelled.** `sponsored_by: "Valio Aimo"` and a `disclosure`: a commercial suggestion from the seller, not neutral advice. Agents are told to present it as "Valio Aimo suggests...".
- **Audited.** Every batch is written as `suggestions.offered`.
- **Cheap.** One price call and one availability call for all candidates together; the response reports `valio_api_calls`.

## 10. Notifications

Source: `valtakirja/notify.py`. Sent when an order lands in notify (`order.notify`) or ask (`order.ask`, to the buyer's human), and when a human decides (`order.decided`, to the agent side). Every notification goes to the in-app feed (`GET /notifications`), then in the background to:

- **ntfy** push to the buyer's phone: `VALTAKIRJA_NTFY_TOPIC` (optional `VALTAKIRJA_NTFY_SERVER`, `VALTAKIRJA_NTFY_TOKEN`). Ask pushes carry the approval link as a click action.
- **Webhook**: JSON POST to `VALTAKIRJA_NOTIFY_WEBHOOK_URL`. With `VALTAKIRJA_NOTIFY_WEBHOOK_SECRET` the body is signed: `X-Valtakirja-Signature: sha256=<hex HMAC-SHA256 of the raw body>`.

A failed notification never blocks or fails an order; each delivery is audited as `notification.sent` or `notification.failed`. `POST /notifications/test {mandate_id}` (human) sends a test through every configured channel.

## 11. Sequence

```mermaid
sequenceDiagram
    actor H as Buyer human
    participant A as Buyer agent
    participant G as Valtakirja gateway
    participant V as Valio Aimo APIs
    participant L as Audit log

    H->>G: POST /mandates (approver code)
    G->>L: mandate.issued
    G-->>H: mandate + JWS
    H->>A: "1,000 l lactose-free milk by Monday"
    A->>G: search_products(query, claims, avoid_allergens)
    G->>L: catalog.search (0 Valio calls)
    G-->>A: product cards (no price)
    A->>G: request_quote(candidate ids, 1000 L, date, mandate_id)
    G->>V: POST /price (batched shortlist)
    G->>V: POST /availability (batched shortlist)
    G->>G: packs, lots, cut-offs, split, mandate preview
    G->>L: quote.issued
    G-->>A: quote + JWS (expires_at, mandate_preview)
    A->>A: verify JWS against /.well-known/jwks.json
    A->>G: place_order(quote_id, idempotency_key)
    G->>G: re-check cut-offs, evaluate mandate
    G->>L: order.ordered / order.pending_approval / order.refused
    G-->>A: order + receipt JWS
    alt status pending_approval
        A-->>H: reasons + approval_url
        H->>G: POST /orders/{id}/approve (approver code)
        G->>G: re-check revoked mandate and cut-offs
        G->>L: order.approved
        G-->>H: ordered / expired / refused
    end
```

## 12. Audit log

Every operation appends one event. Events: `mandate.issued`, `mandate.revoked`, `catalog.search`, `quote.issued`, `substitutes.suggested`, `order.ordered`, `order.pending_approval`, `order.refused`, `order.rejected` (expired quote), `order.approved`, `order.rejected_by_human`, `standing.created`, `standing.renewal_checked` (with the drift findings and baseline), `standing.renewed`, `standing.paused`, `standing.resumed`, `standing.history_simulated`, `suggestions.offered`, `notification.sent`, `notification.failed`.

Each row: `seq, ts, actor, actor_type (agent|human|system), event, subject, buyer, data, prev_hash, hash`.

**Hash chain** (`store.audit`):

```
rec  = {"ts", "actor", "actor_type", "event", "subject", "buyer", "data", "prev_hash"}
hash = sha256( canonical_json(rec) ).hexdigest()
prev_hash of the first event = "0" * 64
prev_hash of event n = hash of event n-1 (by seq)
```

`canonical_json` is the same as for signatures: `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)` encoded as UTF-8. `data` is the parsed JSON object, not its stored string.

**Verify**: `GET /audit/verify` walks all events in `seq` order, recomputes each hash and checks each `prev_hash`. It returns `{ok, events, head}` or `{ok: false, broken_at_seq}`. A third party can do the same from `GET /audit` output (newest first, so reverse it), and can pin `head` to detect a later rewrite of the whole chain.

**Second check (MariaDB)**: with `VALTAKIRJA_DB_URL` set, `mandate`, `purchase_order` and `audit_event` are `WITH SYSTEM VERSIONING` tables. The database keeps every past row version on its own. `audit_event` is append-only, so any row with a closed `ROW_END` means someone ran UPDATE or DELETE. `/audit/verify` adds `system_versioning {rows_changed_or_deleted, seqs}` and fails if it's non-zero. The same history powers `GET /orders/{id}/history` (every status version, plus the mandate `FOR SYSTEM_TIME AS OF` the decision and approval times, and which fields changed since) and `GET /mandates/{id}/history`. With SQLite there is no versioning; history shows only the current row.

## 13. Verifying offline

A buyer can check a quote or receipt with only the JWKS, without trusting the gateway's other answers:

1. Fetch `GET /.well-known/jwks.json` once over HTTPS and pin it.
2. Split the compact JWS into `header.payload.signature`.
3. Decode the header. Check `alg == "EdDSA"`, `kid` matches the pinned key, and `typ` is the one you expect (`valtakirja-quote+jws` for a quote).
4. Verify the Ed25519 signature over the ASCII bytes `header.payload` (the two base64url strings joined by a dot).
5. Decode the payload. For a quote, check `expires_at` is in the future, `agent_id` and `mandate_id` are yours, and the totals and lines are what you will commit to.
6. For a receipt, check `quote_id` matches the quote you ordered, and read `status` and `decision.findings`.

```python
import base64, json, httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

def b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

def verify(token: str, jwks: dict, typ: str) -> dict:
    h, p, s = token.split(".")
    header = json.loads(b64d(h))
    key = next(k for k in jwks["keys"] if k["kid"] == header["kid"])
    assert header["alg"] == "EdDSA" and header["typ"] == typ
    Ed25519PublicKey.from_public_bytes(b64d(key["x"])).verify(b64d(s), f"{h}.{p}".encode())  # raises if forged
    return json.loads(b64d(p))

base = "http://localhost:8000"
jwks = httpx.get(f"{base}/.well-known/jwks.json").json()
r = httpx.post(f"{base}/quotes", json={...}).json()
quote = verify(r["jws"], jwks, "valtakirja-quote+jws")
assert quote == r["quote"]
```

The reference buyer in `buyer/` does the same in the browser with Web Crypto.

## 14. Known limits of v0.2

- The gateway signs mandates on the human's behalf; the human authenticates with a shared approver code.
- Agents are identified by `agent_id` plus the signed mandate issued to it. Opt-in bearer credentials are being added; production would bind a key to the mandate (mTLS, DPoP or a VC-bound key) so a leaked mandate id is useless without the key.
- One signing key, no rotation, no `exp` or `iat` JWS claims (expiry is inside the quote payload).
- Prices are the mock's catalog prices; the mock ignores the customer number.
- No real order is sent to Valio Aimo; `ordered` is the end state.
- The order lock is per process: the gateway runs as one process. Several replicas would need the lock in the database.
- Seasonal and new suggestions use proxies (name keywords, Synkka lastUpdated); standing-order demo history is simulated and marked so.

## 15. Implementation notes for agent developers

- Always pass `mandate_id` to `request_quote` and read `mandate_preview` before ordering. It is the same engine that will decide the order, so a `block` preview will be refused.
- The preview can differ from the final decision: cut-offs are re-checked at order time and the daily spend may have changed.
- Every MCP tool, `request_quote` included, returns errors as `{error, message, hint, retryable}` with `isError: true`. Follow `hint`; retry as-is only when `retryable` is true (`valio_unavailable`, `internal_error`).
- Never reuse an idempotency key across quotes. Retry with the same key and the same quote; a new quote needs a new key.
- For a standing order, renew it each week; if the result is `pending_approval`, show the buyer `drift.summary` and the `approval_url`.
- Present `get_suggestions` results as Valio Aimo's sponsored suggestions, never as your own advice, and order one only when the buyer asks.
- Search never costs live API calls. Quote only the products you would actually buy.
