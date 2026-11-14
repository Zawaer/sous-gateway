# Sous reference buyer

The reference buyer runs in a browser and speaks to a Sous gateway over its documented REST API. It needs a running gateway with catalog data and an issued mandate. The browser needs HTTPS or localhost and Web Crypto support for Ed25519. The buyer needs no model API key, build step, browser extensions, or seller credentials.

If the gateway requires agent authentication, enter your buyer agent token under **Product filters and gateway** after choosing the gateway and mandate. The token stays in the current page. For a private order link, enter the token and choose **Open linked order**.

Open `/buyer/` on a gateway that serves this directory. To run the buyer separately from the gateway, start a static server from the repo root and pass the gateway URL as a query parameter or enter it under **Product filters and gateway**.

```sh
python3 -m http.server 8432 --bind 127.0.0.1 --directory buyer
```

With the gateway on port 8431, open [the local reference buyer](http://localhost:8432/?gateway=http%3A%2F%2Flocalhost%3A8431). For a freshly seeded gateway use `M-HOTEL-AURORA`, fill the request from `1,000 litres of lactose-free milk by Monday`, and choose up to six plain milk products. Enter category `10400` to narrow this demo to milk and milk drinks. Verify product fit from the cards before requesting the live quote. To compare substitutes, enter the original product's SKU and click **Find substitutes**. A signed quote records which SKU would be replaced. The actual outcome depends on live availability, signed mandate rules, and the delivery date.

The optional English input fills fields for a single need in the form `quantity unit product by date`. It supports weekdays, today/tomorrow and ISO dates. The editable form also accepts other catalog languages and explicit product constraints. General-purpose agents can connect directly to the gateway's MCP endpoint for more flexible natural-language input.

The browser verifies mandate and quote JWS payloads against the gateway's JWKS. Use a trusted gateway URL to establish whose public keys you are using. The browser shows the signed quote and submits only after the buyer clicks the order or approval button. The gateway computes the authoritative order outcome. Submissions use one idempotency key per quote and can be retried after an uncertain response while the page is open. Current gateways issue a new signed receipt when a seller approves or rejects an order. The buyer checks that the signed status and seller decision match what the gateway reports. If an older gateway still returns the original pending receipt, the buyer labels it as the original receipt and identifies the current status as reported by the gateway.

After a submission, the buyer URL keeps the order ID in the `order` query parameter. Reopen that URL to check the status and verify the receipt against the original signed quote. **Refresh status** checks the same order again.

When the current receipt verifies, **Download verification bundle** exports a JSON file with the original signed quote, current signed receipt, signed amendments and gateway public keys. Each JWS must verify with the expected document type. The receipt binds amendment hashes and the original quote ID and total. Establish the gateway key's trust independently when checking a shared bundle. The bundle excludes the agent token.

To verify a bundle offline with Node 22 or newer, supply public keys obtained independently from the trusted gateway:

```sh
node scripts/verify_buyer_bundle.mjs order-proof.json trusted-jwks.json
```

The verifier checks signatures, document types, quote binding and amendment hashes using the supplied keys. It prints the signed status and total on success and exits with a nonzero status on failure. It makes no network requests and ignores the keys embedded in the bundle for trust.

**Download signed receipt** saves its compact JWS. For offline verification, keep the public key set from the trusted gateway's `/.well-known/jwks.json` alongside the receipt and use the key matching its `kid` to verify the Ed25519 signature. The receipt includes order details, so share it only with its intended recipients.

For a multi-item request, select candidates for each product and click **Add to basket**. The basket shares one mandate and delivery date. **Get signed basket quote** shows requested and quoted quantities per item. Use **Find alternatives** on a shortage row to replace that basket item and request a fresh quote. On an existing order, the same action looks up alternatives for the missing quantity.

Pending orders refresh automatically while the tab is visible. **View audit trail** opens the seller audit log filtered to the order ID.

Run the isolated tests with `node --test buyer/buyer.test.mjs` on Node 22 or newer.
