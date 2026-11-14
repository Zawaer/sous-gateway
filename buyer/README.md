# Sous reference buyer

The reference buyer runs in a browser and speaks to a Sous gateway over its documented REST API. It needs a running gateway with catalog data and an issued mandate. The browser needs HTTPS or localhost and Web Crypto support for Ed25519. The buyer needs no model API key, build step, browser extensions, or seller credentials.

The reference page supports public demo reads and quote submission on gateways that allow it without agent authentication. If your gateway requires a buyer agent bearer token, use the authenticated REST API for quotes and orders. The reference page currently has no token field.

Open `/buyer/` on a gateway that serves this directory. To run the buyer separately from the gateway, start a static server from the repo root and pass the gateway URL as a query parameter or enter it under **Product filters and gateway**.

```sh
python3 -m http.server 8432 --bind 127.0.0.1 --directory buyer
```

With the gateway on port 8431, open [the local reference buyer](http://localhost:8432/?gateway=http%3A%2F%2Flocalhost%3A8431). For a freshly seeded gateway use `M-HOTEL-AURORA`, fill the request from `1,000 litres of lactose-free milk by Monday`, and choose up to six plain milk products. Enter category `10400` to narrow this demo to milk and milk drinks. Verify product fit from the cards before requesting the live quote. To compare substitutes, enter the original product's SKU and click **Find substitutes**. A signed quote records which SKU would be replaced. The actual outcome depends on live availability, signed mandate rules, and the delivery date.

The optional English input fills fields for a single need in the form `quantity unit product by date`. It supports weekdays, today/tomorrow and ISO dates. The editable form also accepts other catalog languages and explicit product constraints. General-purpose agents can connect directly to the gateway's MCP endpoint for more flexible natural-language input.

The browser verifies mandate and quote JWS payloads against the gateway's JWKS. Use a trusted gateway URL to establish whose public keys you are using. The browser shows the signed quote and submits only after the buyer clicks the order or approval button. The gateway computes the authoritative order outcome. Submissions use one idempotency key per quote and can be retried after an uncertain response while the page is open. When approval changes the current status, the buyer identifies a receipt that signs the original decision.

After a submission, the buyer URL keeps the order ID in the `order` query parameter. Reopen that URL to check the status and verify the receipt against the original signed quote. **Refresh status** checks the same order again.

For a multi-item request, select candidates for each product and click **Add to basket**. The basket shares one mandate and delivery date. **Get signed basket quote** shows requested and quoted quantities per item. Use **Find alternatives** on a shortage row to replace that basket item and request a fresh quote. On an existing order, the same action looks up alternatives for the missing quantity.

Pending orders refresh automatically while the tab is visible. **View audit trail** opens the seller audit log filtered to the order ID.

Run the isolated tests with `node --test buyer/buyer.test.mjs` on Node 22 or newer.
