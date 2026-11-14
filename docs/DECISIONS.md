# Technical decisions

Short ADR-style notes: what we chose, why, and what we'd do in production. The protocol itself is in [PROTOCOL.md](PROTOCOL.md).

## 1. MCP and OpenAPI over one service layer
All operations live in `sous/service.py`. `api.py` exposes them as REST with an OpenAPI schema; `mcp_server.py` exposes the same functions as MCP tools.
MCP is how today's agents (Claude, and others via adapters) discover tools. OpenAPI is what every other client and integration team already uses.
One service layer means the rules, signing and audit log are identical whichever door an agent uses. Neither transport has logic of its own.
Approve and reject are REST-only on purpose: an agent should not have a tool that approves its own order.

## 2. Compact JWS with Ed25519, not a bespoke signature format
Quotes, mandates and receipts are RFC 7515 compact JWS, `alg: EdDSA` (RFC 8037), with the key published as a JWKS at `/.well-known/jwks.json`.
Any language has a JOSE library, so a buyer can verify offline without our code. Ed25519 keys are small, signing is fast and deterministic.
Each object has its own `typ` so a quote can't be passed off as a receipt.
Production path: express the mandate as a W3C Verifiable Credential signed by the buyer, and align quote and order with the agent payment protocols now emerging (Google's AP2 mandates, OpenAI/Stripe's ACP). The JWS payloads map onto both without changing the rules engine. Done as views: AP2 v0.2 / schema.org exports, A2A and MCP server cards and JSON Schemas; mapping and gaps in [STANDARDS.md](STANDARDS.md).

## 3. Deterministic rules engine; no model approves or relaxes anything
`mandate.evaluate` is plain Python: every finding has a stable rule id, a tier and a human-readable sentence. The same input always gives the same tier.
The buyer's LLM turns "1,000 l lactose-free milk by Monday" into a structured request and explains results. It never sets a price, quantity, allergen verdict or approval.
The gateway uses one model, and only to add caution: for a product that declares no allergens, the Gemini Decisions API (`gemini-decision-1`) gives the probability that it contains each avoided allergen, and code refuses at 0.8 or more (`allergen.likely`). Below that nothing changes: the product stays unknown and a human decides. The answer is cached, so the same product gets the same verdict at quote, order and approval, and the probability is in the finding. No model output can approve an order, lower a tier or make a product safe.
This makes the controls testable (`tests/test_core.py` covers all four tiers) and explainable to the human approving.
A prompt-injected or confused agent can at worst ask for the wrong thing; it can't talk its way past the mandate.

## 4. Unknown allergen data is unsafe
30 % of Valio Aimo's food products (46 % of all products) have no allergen declaration. If a mandate avoids allergens, a product without a declaration becomes `allergen.unknown` with tier `ask` (or `block`, per mandate), never `silent`; if the Gemini Decisions API puts an avoided allergen at 0.8 or more, it is refused (`allergen.likely`, section 3).
"May contain" counts as contains unless the mandate allows it. `AM`, `AN` and `AW` expand to their whole family (milk incl. lactose, all tree nuts, all gluten cereals).
For a hospital or school kitchen a false "safe" is the one error that can't be undone. A human confirming is cheap by comparison.

## 5. Shortlist first, then live calls
Valio Aimo asked teams not to poll the whole catalog. Search runs against a local index of the static product file and costs zero live calls.
Only the products the agent asks to quote are priced: one batched `/price` and one `/availability` call per quote, covering every item (up to 1000 ids each), price cached 10 min, availability 60 s.
A typical request costs 2 live calls. Substitute suggestions price the top 25 candidates, also in 2 calls; a quote that can't be met as asked also prices substitutes for its options, so it costs up to 4 (avg 1.0 per request in `evals/results.json`, with the cache). Every quote reports its `valio_api_calls`.
A global per-minute cap (`SOUS_AIMO_CALLS_PER_MIN`) protects Valio Aimo's systems from a runaway agent.

## 6. SQLite FTS5 trigram index for the catalog
The catalog has names in Finnish, Swedish and English. Finnish compounds ("laktoositon", "kevytmaitojuoma") defeat word tokenisers and stemmers.
A trigram tokenizer matches any 3+ letter substring, so "maito" finds "kevytmaitojuoma" without language-specific rules. A small re-rank prefers whole words and compound endings, so "voi" (butter) ranks "kasvivoi" over "Voima".
SQLite ships with Python, needs no server, and builds the 17.5k-product index once, at first start.
Production: a proper search service (OpenSearch with Finnish analyzers, plus embeddings) fed from Valio Aimo's product master.

## 7. MariaDB system-versioned tables for state
`mandate`, `purchase_order` and `audit_event` are `WITH SYSTEM VERSIONING`. The database keeps every past version of every row without extra code.
That answers the auditor's question directly: "what did the mandate say when this order was decided?" is one `FOR SYSTEM_TIME AS OF` query (`/orders/{id}/history`).
It is also a second tamper check on the audit log, independent of the hash chain: the log is append-only, so any closed historical row means someone edited or deleted an event.
SQLite stays as a zero-setup fallback for development and tests, without history.

## 8. Hash-chained audit log shared by both sides
Each event's hash covers its content and the previous hash, so editing or removing any event breaks every later link. `/audit/verify` recomputes it.
Buyer and Valio Aimo read the same log (`/audit?buyer=`), so there's one version of events in a dispute.
Production: periodically anchor the chain head somewhere neither party controls (signed and sent to the buyer, or a transparency log).

## 9. What we'd change for production
- **Identity.** Replace the shared `X-Approver-Code` with the buyer's own login (OIDC from their identity provider). Reads are already scoped (approver code, sandbox token or the agent's own token; PROTOCOL.md section 22), but the approver code is still one shared secret. Today agents can be required to send a per-mandate bearer token (stored hashed, rotatable; PROTOCOL.md section 16); in production they would prove possession of their own key (mTLS or DPoP), bound to the mandate's `agent_id`.
- **Mandate signed by the human.** Built opt-in (section 10); production makes it the default, with passkeys and organisation keys from onboarding instead of trust on first use.
- **Per-customer prices.** Use the real customer number so quotes carry the buyer's contract prices. The mock ignores it.
- **Valio Aimo's real order API.** `ordered` would submit to the order system, with its confirmation id in the receipt and the audit log.
- **Notifications.** Built for the demo: in-app feed, ntfy push and an HMAC-signed webhook, with the approval deadline set by the earliest cut-off. Production adds email/Teams and per-buyer channel settings (today the channels come from server env vars).
- **Rate limits per mandate and agent**, not only per IP and globally.
- **Key rotation.** Several keys in the JWKS, KMS/HSM-held private keys and `iat`/`exp` in the JWS header. (Receipts are already re-signed after a human decision.)

## 10. Buyer-signed mandates as W3C Verifiable Credentials (did:key, ES256), trust on first use
The gateway signing a mandate "on the buyer's behalf" asks everyone to trust the gateway about who authorised what. Opt-in, the buyer's human now signs the mandate with a key only they hold, and the gateway verifies and countersigns it (PROTOCOL.md section 17).
- **Format: VC Data Model 2.0 secured with JOSE (`vc+jwt`)**, not a custom JWS: it is the open standard the EU wallet (EUDI/EBSI) world uses, any JOSE library verifies it, and it reuses our JWS code path. Not JSON-LD Data Integrity proofs: those need RDF canonicalisation, a heavy dependency for no gain here.
- **ES256 (P-256) first, EdDSA too.** P-256 is what WebCrypto and passkeys support in every browser; Ed25519 is accepted because our own key is Ed25519 and it's free to support.
- **`did:key`, resolved locally**, not `did:web`/`did:jwk`: the identifier is the key, so verification needs no network and no hosting, and it's the usual issuer id in wallets. About 30 lines of base58 + multicodec; no new dependency.
- **Countersign, don't replace.** The gateway still signs the mandate (`sous-mandate+jws`), now also covering `issuer` and the VC's SHA-256. Agents and the buyer client keep verifying one gateway signature as before; auditors can additionally verify the buyer's. Every load re-verifies both, so editing the stored VC or mandate is caught.
- **Trust on first use for buyer keys**, with an approver-managed registry (`POST /buyers/{id}/keys`) as the override, and no automatic pinning for buyers that already have gateway-issued mandates. Chosen because the demo has no onboarding and a judge must be able to sign from a browser alone. Production: keys are bound at onboarding through an organisation credential (EUDI/EBSI legal person, eIDAS seal), and signing uses passkeys.
- **Revocation as a signed statement** bound to the credential's hash, so it proves the buyer (not the gateway) revoked it. Production: W3C Bitstring Status List so revocation is checkable without asking the gateway.
