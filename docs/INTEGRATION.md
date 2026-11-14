# Integrating Sous with Valio Aimo's systems

Sous is the gateway that AI purchasing agents talk to. It checks every order against the buyer's signed mandate and against Valio Aimo's own rules. This page explains how Sous would connect to the systems Valio Aimo already runs: the order system, the customer master, credit, prices and delivery routes. It covers what is built today, what is mocked, and what we don't know.

**What we don't know.** We have not seen Valio Aimo's internals. We assume an SAP ERP (ECC or S/4HANA) behind the Aimo web shop, because that is common for Finnish food wholesale. We don't know that for sure. The only Valio interface we have used is the hackathon's mock price/availability API. It ignores the customer number (`VALIO_AIMO_CUSTOMER`, default `123`). Every SAP name below (IDoc types, tables, fields) is the standard SAP one. Each is something to confirm with Valio Aimo's integration team, not a claim about how Valio has set things up.

## Where the line is

| Stays in Sous (seller-side gateway) | Stays in Valio Aimo's systems |
|---|---|
| Mandates (buyer-signed VCs, countersigned), agent credentials | Customer master: who the customer is, sales area, ship-to addresses |
| Mandate rules and tiers (silent / notify / ask / block), human approvals | Credit limits, exposure, payment blocks (credit management) |
| Signed quotes and receipts, the hash-chained audit log | Prices: list prices and customer-specific conditions |
| Holds and pauses for agent ordering (Valio's kill switch over agents) | Stock, ATP, delivery routes, cut-offs, sales blocks |
| An outbox that hands every accepted order to the ERP exactly once | Sales orders, deliveries, invoices |

Sous never becomes the system of record for orders. An order that is `ordered` in Sous was accepted under a mandate, and Sous then hands it to the ERP. The ERP's sales order number comes back as `erp.confirmation_id`.

## What is built now (mock backends, real interfaces)

### 1. Customer account check (`sous/account.py`)

- **Interface.** `CustomerAccount` has two methods: `customer_number_for(buyer)` and `get(customer_number)`. Each returns an `Account`: customer number, buyer, credit limit, open balance, payment block (with reason), delivery weekdays, and the delivery route with its cut-off. The production version is an adapter with the same two methods, backed by Valio's customer master and credit management.
- **Mock.** The mock is a `customer_account` table (SQLite or MariaDB; system-versioned on MariaDB). It is seeded on first use from `DEMO_ACCOUNTS`: one account for each demo buyer, and a separate account for each judge sandbox copy (`<number>-SBX-XXXX`), so visitors never use the demo buyer's credit. The approver can change an account with `GET` / `PATCH /accounts/{customer_number}`, which is how the demo shows a payment block or a tight credit limit. In production these facts come from SAP, not from that call.
- **Which account.** The mandate has an optional `customer_number` field (Valio's customer number for the buyer; SAP `KUNNR` in production). It is part of what the buyer signs, so an agent can't move an order onto another account. If the field is empty, the buyer name is looked up in the account table. With `SOUS_REQUIRE_ACCOUNT=1`, a buyer without an account is blocked (`account.unknown`). Without it, the account checks are skipped.
- **Rules.** All three are owned by Valio and listed at `GET /rules`. They are checked at quote preview, at order time and again at approval:
  - `account.payment_block` (block): the account has a payment or delivery block.
  - `account.credit_limit` (ask): the order, incl. VAT, is more than the credit left. Credit left = limit − open balance − this customer's Sous orders that went through and are still to be delivered. A human can confirm it. Valio's own credit check in the ERP still has the last word (see below).
  - `account.delivery_day` (block): the delivery date isn't one of the customer's delivery weekdays, or the route's cut-off for that date has passed (e.g. 20:00 the day before). This comes on top of the per-product cut-offs that Valio's availability API already returns.
- **Demo values.** Credit is ample, and Monday (the demo's delivery day) is a delivery day for every demo buyer, so no account rule fires on the README judge path. The school kitchens get deliveries Mon–Fri, the hotel and Kaarna Mon–Sat, the hospital every day. The customer numbers are made up.

### 2. Outbound order adapter (`sous/erp.py`)

- **When it runs.** An order is handed over when it becomes `ordered`: right away for silent and notify orders, or when a human approves an ask order. Standing-order renewals go through the same path. Refused, rejected, expired and pending orders are never sent.
- **Outbox and retries.** The outbox row (keyed by order id, so each order is queued only once) is written in the same database transaction as the order itself, at placement and at approval, on SQLite and MariaDB alike. If the outbox write fails, the order isn't stored either: the agent gets an error, and a retry with the same idempotency key works. As a second safety net, every sweep (at startup and then every minute) queues any `ordered` order that has no outbox row and audits it as `order.erp_reconciled`. Orders that were already `ordered` when the adapter was first switched on (`erp_meta.activated_at`) are marked `legacy` and never sent. Once the row is written, Sous tries to send the order. If the send fails, the order is not lost. It stays in the outbox, and the gateway's background sweep (every minute) retries it after 30 s, 1, 2, 5, 10 and 30 min, then hourly. After 10 attempts Sous gives up and writes `order.erp_gave_up` to the audit log for operations.
- **No duplicates.** The same order always produces the same payload. The Sous order id goes out as the customer purchase-order number (E1EDK02 qualifier 001) and as the `Idempotency-Key` header, so the ERP's duplicate-PO check can catch a resend. The stub returns the same SIM- number for every resend.
- **Where it sends.** The target is set by `SOUS_ERP_URL`. Empty or `stub` uses an in-process stub: nothing leaves Sous (Valio Aimo has no order API in this challenge), so the order is marked `simulated` with a `SIM-` prefixed number, never `confirmed`. `stub:fail` always fails, for demonstrating the outbox. Any URL gets an HTTP POST of the JSON document; a 2xx response with `confirmation_id` (or `sales_order`) marks the order `confirmed`, and a 2xx without one marks it `sent`. Sandbox orders always go to the stub, whatever the setting.
- **What the order shows.** `GET /orders/{id}` (and MCP `place_order` / `get_order`) includes `erp: {adapter: stub|http, simulated, status: pending|sent|confirmed|simulated|failed, confirmation_id, payload_hash, attempts, last_error, next_retry_at, gave_up, sent_at, confirmed_at, simulated_at, note}`. With the stub: `adapter: "stub"`, `simulated: true`, `status: "simulated"`, `confirmation_id: "SIM-…"`, `note: "Simulated hand-off: no real order was sent to Valio Aimo (no order API in this challenge). The ORDERS05 payload is what would be sent."` Stub rows written before this label existed are shown the same way. MCP tells assistants to say "placed with Sous; Valio hand-off simulated in this demo". `GET /orders/{id}/erp` returns the exact document sent. The `erp` block is unsigned operational data, read from the outbox. Signed receipts are never re-issued for it, and the stored order doesn't change.
- **Audit log.** Each step is logged as `order.erp_sent` and `order.erp_confirmed` (a real endpoint only), `order.erp_simulated` (the stub, with `simulated: true`, the SIM- id and the note; no `erp_sent`), `order.erp_failed` (one per attempt) and `order.erp_gave_up`, each with the payload's SHA-256.

#### Mapping: Sous order → ORDERS05 (JSON)

The document uses the standard ORDERS05 inbound sales order layout, written as JSON with SAP segment and field names. A middleware (SAP CPI / PI/PO, or Valio's own integration layer) would turn it into a real IDoc, or call `BAPI_SALESORDER_CREATEFROMDAT2` or the S/4HANA Sales Order OData API instead.

| ORDERS05 segment / field | Value from Sous | Notes / to confirm with Valio |
|---|---|---|
| EDI_DC40 IDOCTYP / MESTYP | `ORDERS05` / `ORDERS` | Control record. SNDPRN `SOUS` and RCVPRN `VALIOAIMO` are placeholders (`SOUS_ERP_SENDER`, `SOUS_ERP_RECEIVER`), as is the partner profile. |
| EDI_DC40 REFINT | Sous order id | Interchange reference. |
| E1EDK01 BELNR, CURCY | Sous order id, `EUR` | Header. |
| E1EDK14 QUALF 012 ORGID | `ZAGT` | Sales document type: a placeholder for an "agent order" type Valio would define. Sales org, distribution channel and division (QUALF 008/007/006) are left out because they depend on Valio's setup. |
| E1EDK03 IDDAT 002 / 022 | Requested delivery date / order date (YYYYMMDD) | |
| E1EDKA1 PARVW AG / WE, PARTN | Customer number (mandate `customer_number` or account lookup) | Sold-to and ship-to. Today both are the same number. A customer with several delivery addresses needs its ship-to party in the mandate or the account. |
| E1EDK02 QUALF 001 BELNR | Sous order id | Customer PO number (VBKD-BSTKD): the key for idempotency and duplicate checks. |
| E1EDKT1/E1EDKT2 | "Sous order …, mandate …, quote …" | Header text, so Valio's people can trace an order back to its mandate. |
| E1EDP01 POSEX | 000010, 000020, … | Item number. |
| E1EDP01 MENGE / MENEE | Quantity / Valio sales unit as quoted (e.g. `ST`, `KG`) | The units come from the Aimo API. They may need mapping to SAP ISO units (e.g. ST → PCE). |
| E1EDP01 VPREI / PEINH / NETWR | List price ex VAT / 1 / line total | For reference only: the ERP should price the order with the customer's own conditions. |
| E1EDP19 QUALF 002 IDTNR | Valio product id | Material number (we assume the Aimo product id is the SAP material number). |
| E1EDP20 WMENG / EDATU | Quantity / the line's delivery date | Schedule line. |
| E1EDS01 SUMID 002 | Order total ex VAT | |

## How the production version would work

1. **Identity: buyer organisation → customer number.** When a buyer is onboarded, Valio's sales or customer service confirms which customer number(s) the buyer's mandates may use. The buyer then signs mandates that carry `customer_number`. The gateway checks that number against the customer master (KNA1 for general data, KNVV for the sales area, KNVP for partner functions such as ship-to) through a read API or a nightly replica. One Sous `buyer` can map to several customer numbers (one per kitchen or ship-to); each mandate carries one. The buyer's keys and the people who approve (`/buyers/{buyer}/keys`) would come from the customer's Aimo web-shop users, through the Aimo login (OIDC), not from a shared approver code.
2. **Credit and payment blocks.** `CustomerAccount.get` reads the credit segment (SAP Credit Management on S/4: limit, exposure, blocked flag; on ECC: KNKK/S066/S067), plus the blocks on the customer master (KNA1/KNVV order, delivery and billing blocks). The ERP's open orders already count in its exposure, so in production the "Sous orders still to be delivered" term only covers orders not yet confirmed into the ERP. The ERP's own credit check runs again when the sales order is created. If it puts the order on credit hold, the adapter should report that back (see phase 2). Sous's check exists so the agent and the buyer hear about the credit problem at quote time, not after the order is placed.
3. **Prices.** The mock API returns list prices (`catalogPrice`). Real customers buy at contract prices: customer- and group-specific conditions, promotions, and deposits. In production, quotes would be priced with the customer's number: the price API (or SAP pricing simulation, e.g. `BAPI_SALESORDER_SIMULATE` or the S/4 sales order simulation API) is called with the sold-to party, and the signed quote carries that price. The mandate's spend limits then apply to the price the customer will actually pay. Until then, the ORDERS05 VPREI is for reference only, and the ERP's pricing overrides it.
4. **Delivery days, routes and cut-offs.** The account's `delivery_days` and `route` would come from Valio's route planning (in SAP: the customer's route and transportation zone, KNVV-ROUTE / TRAGR, plus a delivery calendar), or from whatever schedules the Aimo trucks. The per-product cut-offs come from the availability API, as they do now. Sous enforces both. Today `approve_by` on a held order is computed from the product cut-offs only; folding the route cut-off into it is a small follow-up.
5. **Order hand-off.** `SOUS_ERP_URL` points at Valio's integration layer. The ORDERS05 JSON can be sent unchanged to a middleware iFlow, or the adapter's `_send` can call a BAPI or OData endpoint directly. Order changes and cancellations (ORDCHG) are not built: a Sous order can't change after `ordered`; the buyer changes it in Aimo as they do today.
6. **Confirmation back.** In SAP the confirmation is an ORDRSP IDoc or an event (sales order created, credit block, item rejected). Today Sous only records a confirmation that comes back synchronously in the HTTP response. Phase 2 adds an inbound endpoint that matches confirmations on the PO number, sets `erp.status` to `confirmed`, `credit_hold` or `rejected`, and notifies the buyer the same way approvals do.

## Phased plan

| Phase | Scope | Needs from Valio |
|---|---|---|
| 0 (now) | Interfaces, mocks, outbox and stub; account rules on mock data | Nothing |
| 1. Shadow | Sous sends ORDERS05 to a test client; the ERP creates orders in a non-productive system; compare with Aimo web-shop orders from the same buyers | A test client or system, an integration user, the partner profile, the sales document type, a material and unit mapping check |
| 2. Read real customer data | `CustomerAccount` backed by the customer master and credit API; prices simulated per customer; ORDRSP / credit-hold events back into Sous | Read APIs or replicas for KNA1/KNVV/credit, a pricing simulation endpoint, an event or ORDRSP channel |
| 3. Pilot | One or two customers (e.g. a hotel with a standing order) order through their own agent under a signed mandate; Valio's customer service sees the orders as `ZAGT` with the mandate reference in the header text | Customer consent, the Aimo login (OIDC) for approvers, an operations runbook for `order.erp_gave_up` |
| 4. Production | `SOUS_REQUIRE_ACCOUNT=1`, gateway HA on MariaDB, monitoring of the outbox age | SLA and ownership |

## What is mocked today

- **Customer accounts.** The numbers, credit limits, balances and routes in `DEMO_ACCOUNTS` are made up.
- **The ERP.** The in-process stub returns made-up `SIM-` numbers and status `simulated`. No real Valio system is called, ever. The adapter has only been tested against the stub and a fake HTTP sender (`tests/test_erp_account.py`).
- **Prices.** Quotes use list prices from the mock API, not contract prices.
- **The mapping.** The ORDERS05 mapping is our reading of the standard layout. Sales area, document type, partner profile and units all need Valio's input.
