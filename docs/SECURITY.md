# Security and trust

Valio told us trust matters most, so this page sets out how Sous holds up when someone attacks it. For each threat
it gives the control, the file that implements it, the rule id or error code a caller sees, and the test or check
that proves it. The last section lists what is **not** done. Everything here was written from the code on this
branch. Where this page and the code disagree, the code is right.

Sous sits between outside AI purchasing agents and Valio Aimo. An agent may search, quote and order, but only within
a signed mandate (a "sous") issued by its human. Deterministic rules decide each order's tier: `silent` / `notify`
(ordered), `ask` (held for a human) or `block` (refused). Every step is signed and logged in a hash chain.

**Design stance.** No language model makes a security decision. An agent's words (its rationale, `declared_risks`,
anything in a prompt) can raise a tier but never lower one. Every money-moving decision is a pure function of the
mandate, the signed quote, Valio's live data and the clock (`sous/mandate.py`, `sous/rules.py`; the rule register is
public at `GET /rules`).

## Threat model at a glance

| # | Threat | Main controls |
|---|---|---|
| 1 | Rogue or prompt-injected agent | Mandate engine, agent can't approve, signed quotes bound to the agent, anti-slicing rules, kill switch |
| 2 | Stolen agent token | Hashed, rotatable, mandate-scoped tokens; hard limits; per-credential rate limit; OAuth tokens short-lived and sealed |
| 3 | Competitor scraping prices or stock | Stock buckets, stock-oracle protection, scoped reads, rate limits |
| 4 | Malicious or careless approver | Passkey approval bound to order and total, four-eyes, rules and live price/stock re-checked at approval, attribution in the audit log |
| 5 | Tampering with records afterwards | Ed25519 JWS on quotes, receipts and mandates; hash-chained audit with signed checkpoints; MariaDB system versioning |
| 6 | Abuse of Valio's API | Global Valio budget, interactive slice, shared circuit breaker, sandbox quotas, per-IP and per-credential limits |
| 7 | Cross-buyer data leaks | Scoped reads (404, not 403), sandbox isolation, buyer-level feed filtering |
| 8 | Browser-side attacks on the UIs | CSP per page family, frame-ancestors, nosniff, no-referrer, HSTS, Permissions-Policy |

---

## 1. Rogue or prompt-injected agent

The agent is assumed hostile: it may be jailbroken, prompt-injected by a supplier's product text, or simply wrong.

- **The mandate is the ceiling, not the agent.** `sous/mandate.py` evaluates every quote and order against the
  mandate. Spend rules: `spend.auto_limit`, `spend.hard_limit`, `spend.daily_limit`, `spend.weekly_limit`,
  `spend.notify`. Product rules: `product.category`, `product.first_time`, `product.claims`,
  `product.type_mismatch`, `allergen.contains` / `allergen.may_contain` / `allergen.unknown`. Price rules:
  `price.changed`, `price.comparison_cap`. Plus `quantity.unusual`, `quantity.request_mismatch`,
  `mandate.expired` / `mandate.revoked` / `mandate.agent`, and others. Every finding names its rule id, and
  `GET /rules/{id}` explains it.
- **Order slicing.** "Split it into 10 orders so none needs approval" is caught by `spend.split_orders`
  (`split_window_minutes` on the mandate) and the daily and weekly limits. The red team first found 3 gateway
  violations here. After the fix, the scripted re-run at scale was clean: 2,036 of 2,036 unsafe requests held or
  refused, 0 gateway violations ([REDTEAM.md](REDTEAM.md)).
- **Prompt injection cannot lower a tier.** Text in `rationale` or a product description is never read by the rules.
  `agent.declared_risk` (`sous/controls.py`) lets an agent's doubt *raise* an order to at least `ask`, never lower
  it. The conformance kit checks this against a live gateway (`prompt_injection`: an injected rationale leaves both
  orders at `ask`).
- **An agent cannot approve its own order.** Any `vka_` agent credential sent to `/approve`, `/reject` or
  `/approve-with-changes`, as a bearer token or as `X-Approver-Code`, gets **403 `agent_cannot_approve`**. The attempt
  is audited as `agent.approval_attempt_refused` (agent, order, credential last 4 characters, never the token). This
  happens even when no approver code is configured (`sous/api.py` `require_approver`, `sous/service.py`
  `refuse_agent_approval`). Conformance checks `agent_cannot_approve` and `agent_visible_approval` confirm that
  nothing the agent is handed (links, tokens, MCP tools) approves its held order. The MCP server has no approve
  tool at all. In the red team, 7 calls to a non-existent `approve` tool were refused (`unknown_tool`).
- **Quotes are signed and bound.** An order must reference a gateway-signed quote (`sous-quote+jws`). A forged total
  or a changed quote id cannot be ordered (conformance `tamper`). Idempotency keys are scoped to the agent, so
  another agent reusing a key never receives the first agent's order (`cross_agent_key`).
- **Seller kill switch.** `POST /agent-ordering/pause` (global approver code only) fires `ordering.paused` (block) on
  every order under the affected buyers. Approving a held order is refused too, because `service._approval_blocker`
  re-runs the rules. While paused, nothing reaches Valio (`sous/pause.py`). Single products can be held with
  `product.held` (`sous/holds.py`).

## 2. Stolen agent token

- **Hashed at rest, shown once.** `sous/agent_auth.py` mints `vka_` tokens. Only the SHA-256 hash is stored, with
  the last 4 characters so humans can tell tokens apart. Audit events never contain token values. The `vka_` prefix
  makes leaked tokens easy to grep for and secret-scan.
- **Rotatable and revocable.** `POST /mandates/{id}/agent-credential` with `rotate` or `revoke` is human-only (approver
  code). The old token stops working at once, audited as `agent.credential_rotated`. OAuth grants built on a rotated
  or revoked token, or on a mandate that is no longer live, stop working at the gate (`oauth._agent_token_live`).
  Closing a sandbox also deletes its grants (`oauth.revoke_for_mandates`).
- **Narrow blast radius.** A token is valid only for the mandate(s) it was minted for (`sous/access.py`). It can never
  exceed `spend.hard_limit`, and anything above `spend.auto_limit` waits for a human. With
  `SOUS_REQUIRE_AGENT_AUTH=1`, every mandate needs the bearer token for quotes, orders and renewals, not only mandates
  that opt in with `require_agent_auth`.
- **Per-credential rate limit (new).** POSTs, which include every MCP call at `/mcp` and `/connect/mcp`, are limited
  per credential: `SOUS_RATE_LIMIT_PER_CREDENTIAL_MIN`, default 120 per minute, 0 turns it off. Over the limit,
  the caller gets `429 {"error": "rate_limited", "retry_after_s": N}` with a `Retry-After` header. The key is
  `cred:` plus a SHA-256 prefix of the presented bearer token or `X-Approver-Code`, so no raw token is kept. The
  limiter runs *inside* the OAuth gate, so a connector's access token counts against the agent token behind it:
  minting new access tokens doesn't buy a fresh budget. A stolen token used from many IPs still hits one ceiling
  (`sous/api.py` `credential_rate_limit`, test `tests/test_security_headers.py`). The per-IP limit
  (`SOUS_RATE_LIMIT_PER_MIN`, default 60 POSTs per minute) now also sends `Retry-After`.
- **OAuth connector tokens** (`sous/oauth.py`, for claude.ai and ChatGPT connectors). Authorization code with
  mandatory S256 PKCE and exact registered redirect URIs. Codes last 5 minutes and work once; access tokens last
  1 hour; refresh tokens last 30 days and rotate on every use. A code or refresh token can't be redeemed twice: the
  single-use check is the `DELETE` that removes the row. Grants are stored under the SHA-256 of the token and
  hold the agent token **AES-GCM-sealed under a key derived from the grant token itself**. A database dump alone
  therefore reveals no usable agent token.
- **The OAuth connector refuses approver tokens.** `/connect/mcp` accepts only an OAuth access token (`vko_`) or an
  agent token (`vka_`). Anything else, the approver code and sandbox `sbxa_` tokens included, gets 401
  `invalid_token` ("approver tokens are never accepted from an assistant"). The gate also strips any
  `X-Approver-Code` header before passing the request on (`oauth.connect_gate`). The consent page tells the human
  never to paste the approver token.

## 3. Competitor scraping prices or stock

- **Stock buckets.** By default (`SOUS_STOCK_EXPOSURE=buckets`), agents see only
  `stock_level: ample | limited | short | none | unknown`. `available_amount` is null, and stock numbers are removed
  from issue messages (`sous/exposure.py`). Only the decision engine sees exact numbers. `approver_exact` shows
  exact stock to the global approver only.
- **No stock oracle.** A quote without a mandate needs no credential, so it is not sized to stock at all. Asking for
  10^6 units therefore reveals nothing (`quote.plan(sized=False)`, conformance `stock_oracle`, plus `stock_buckets`
  and `stock_buckets_seeded`).
- **Scoped reads.** Other buyers' quotes and orders are unreadable (section 7). Catalog prices are Valio's list
  prices and public on purpose.
- **Volume.** The per-IP and per-credential limits (section 2) and the Valio budget (section 6) bound how fast anyone
  can sweep the catalog through live lookups. Cached answers cost Valio nothing.

## 4. Malicious or careless approver

- **Passkeys bound to the exact order** (`sous/passkey.py`, `sous/passkey_routes.py`). The WebAuthn challenge is the
  SHA-256 of a canonical binding: order id, quote id, SHA-256 of the signed quote JWS, total, SHA-256 of the lines,
  `approve_by`, the action, RP ID and a one-time nonce. A passkey approval therefore covers *this order at this
  total* and nothing else. It is verified with py_webauthn (signature, RP ID, origin, user presence **and**
  verification, sign count), the challenge works once, and the binding is recomputed at verify time. A mandate with
  `approval_method: passkey` accepts nothing else.
- **Offline re-verification.** The audit event stores the binding, public key and raw assertion.
  `sous/passkey_verify.py` and `scripts/verify_passkey_approval.py` re-check it with only `cryptography`, no gateway
  and no database (`GET /orders/{id}/approval/verify`).
- **Four-eyes.** With `separate_approver`, the approver must differ from the identity recorded as having requested the
  order (`order.requested_by`). Otherwise the gateway answers **403 `separate_approver_required`**
  (`sous/controls.py`).
- **Approval is a decision, not an override.** `service._approval_blocker` re-runs the checks at approval time: a
  revoked or expired mandate, a passed order cut-off or delivery date, a signed quote that no longer verifies, and
  the full mandate evaluation against the mandate as it is now (any `block` finding, `ordering.paused` included,
  refuses the approval). An approver can't push through an order that has since become invalid.
- **Live price and stock re-check at approval** (`sous/recheck.py`, just landed on main). When the human approves,
  the gateway asks Valio again for the order's lines. A line that is no longer orderable, or a unit price that moved
  by more than the mandate's `max_price_change_pct` (default 20 %), turns the order `expired`. The comparison is
  stored in `approval.recheck` and signed into the receipt. If Valio can't be reached, the approval fails with
  `valio_unavailable` and the order stays pending: nothing is ever ordered on unchecked prices. The approver code is compared in constant time
  (`hmac.compare_digest`, `sous/access.py`).
- **Attribution.** Every decision records its authority ("global approver code", "sandbox approver SBX-…",
  "passkey <name> (credential …last8)") in the hash-chained audit log.

## 5. Tampering with records after the fact

- **Signatures** (`sous/signing.py`): Ed25519 compact JWS (RFC 7515, `alg: EdDSA`, RFC 8037) over canonical JSON,
  with explicit `typ` values: `sous-quote+jws`, `sous-order+jws` (receipts), `sous-mandate+jws`,
  `sous-amendment+jws`, `sous-offer+jws`, `sous-remembered+jws`, `sous-revocation+jws`. Verification checks the
  `typ`, so one kind of document can't be passed off as another. Public key: `/.well-known/jwks.json` (`kid` =
  SHA-256 of the key). Buyers verify offline (`buyer/verify.html`, `scripts/verify_buyer_bundle.mjs`), and the MCP
  order card checks signatures in the chat with WebCrypto (`web/mcp-card.html`).
- **Buyer-signed mandates** (`sous/credentials.py`): the buyer's human can sign their own mandate as a W3C
  Verifiable Credential (`vc+jwt`, ES256 or EdDSA, `did:key` issuer). The gateway verifies it, checks the key is
  registered for that buyer, and countersigns. Revocation is a JWS signed by the same key.
- **Hash-chained audit** (`sous/store.py`): each `audit_event` row stores `prev_hash` and
  `hash = SHA-256(canonical(record + prev_hash))`. `verify_chain` recomputes the chain, and `GET /audit/verify` is
  public (ok, count, head hash). Conformance `audit` checks it on a live gateway.
- **MariaDB system versioning**: on MariaDB, `mandate`, `purchase_order`, `audit_event` and `agent_pause` are
  `SYSTEM VERSIONED`. The database keeps every past row version, so an `UPDATE` or `DELETE` on the append-only
  audit table shows up as a closed historical row. This is a second, independent tamper check, and it answers "what
  did the mandate say when this was decided?" (`FOR SYSTEM_TIME AS OF`).
- **Signed audit checkpoints** (`sous/checkpoints.py`, just landed on main). A hash chain alone can't show a cut
  tail: drop the newest N events and what is left is still a valid chain. Every 5 minutes, and whenever the chain
  has moved on, the gateway signs `{seq, head, events, at}` as `sous-checkpoint+jws`. Every receipt carries
  `audit_head`. `GET /audit/verify` checks the stored checkpoints, and `?checkpoint=<jws>` checks one a buyer or
  Valio saved. A cut below any saved checkpoint or receipt fails.

## 6. Valio API abuse

All deployments share one Valio Aimo key with undisclosed limits.

- **Budget** (`sous/aimo.py`): `SOUS_AIMO_CALLS_PER_MIN` (default 60) live calls per minute. Answers are cached.
  A 429 from Valio opens a **circuit breaker** for its `Retry-After` (or 30 s), and no call goes out meanwhile.
  429s and 5xx are retried with jittered backoff, never during an open circuit.
- **Shared across deployments** (`sous/shared.py`, Aiven for Valkey): a global sliding window with a per-role cap
  (staging and CI can never starve prod), a reserved interactive slice (`SOUS_VALIO_INTERACTIVE_SHARE`, so
  background sweeps can't eat a judge's quote), and a shared circuit breaker (a 429 anywhere pauses everyone).
  Valkey down means fail-open to per-process limits. The Valkey URL is never logged.
- **Sandbox quotas** (`sous/sandbox.py`): `SOUS_SANDBOX_QUOTES` (15) live lookups per sandbox,
  `SOUS_SANDBOX_CALLS_PER_MIN` for all sandboxes together (default half the budget), `SOUS_SANDBOX_PER_IP_HOUR` (3)
  new sandboxes per IP, `SOUS_SANDBOX_MAX_ACTIVE` (50).
- **Edge limits**: per IP and per credential (section 2). `CF-Connecting-IP` is trusted only with
  `SOUS_TRUST_CF_HEADER=1`, since otherwise a client could forge it.
- The red team made **0 live Valio calls**: prices replayed from a recorded snapshot (`sous/replay.py`).

## 7. Cross-buyer data leaks

- **Scoped reads** (`sous/access.py`): the global approver code reads everything. A sandbox approver token reads its
  sandbox. An agent token reads its own mandates and their quotes, orders and standing orders. Without a credential,
  a caller sees only the curated `demo_public` demo data. **A forbidden detail answers 404, exactly like a missing
  one**, so ids are never confirmed. Lists are filtered silently, and `X-Sous-Read-Scope` says which scope applied.
  The notification and audit feeds are filtered by buyer: a buyer is visible only when *every* mandate with that
  name is readable, so a shared name can't leak a private mandate. Conformance `private_reads`;
  `tests/test_read_scopes.py`.
- **Fail closed**: a gateway with `SOUS_DEPLOYMENT_ROLE` of prod, staging or demo refuses to start without
  `SOUS_APPROVER_CODE` (`access.startup_check`).
- **Sandbox isolation** (`sous/sandbox.py`): each judge sandbox gets its own copies of the demo mandates. Its agent
  token covers only those, and its `sbxa_` approver token decides only orders under them. Both are stored as
  SHA-256 hashes and compared in constant time. The client IP is stored only as a hash. Sandbox data is hidden from
  list endpoints unless `?sandbox=` is given with that sandbox's credential. Sandbox notifications never leave the
  in-app feed (no push, no webhook).

## 8. Browser-side: security headers (new)

One pure-ASGI middleware (`sous/api.py` `SecurityHeaders`, the outermost layer) adds headers to **every** HTTP
response: JSON, HTML, 429s, CORS preflights and the mounted MCP app. It edits only the response-start message, so
MCP's streamed SSE passes through untouched. A response that already sets one of these headers keeps its own value.

Every response:

```
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
Strict-Transport-Security: max-age=31536000
Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=(), usb=()
```

The HSTS header has no `includeSubDomains` and no `preload`, because the public hosts live under the shared
`sslip.io` domain. No page uses the camera, microphone or location: the buyer agent's photo upload is a plain file
input. WebAuthn (`publickey-credentials-*`) is left at its default (`self`), which passkey approval needs.

HTML responses also get a Content-Security-Policy and `X-Frame-Options`, chosen by page family:

| Page family | `connect-src` | `frame-ancestors` / XFO | Extra |
|---|---|---|---|
| `/ui/*`, `/proof`, other HTML on a public host | `'self'` | `'none'` / `DENY` | |
| same, on a loopback host (dev) | `'self' http://localhost:* http://127.0.0.1:*` | `'none'` / `DENY` | |
| `/ui/mcp-card.html` (framed by `/ui/cards.html`) | `'self'` | `'self'` / `SAMEORIGIN` | |
| `/buyer/*` (reference buyer agent) | `'self' https: http://localhost:* http://127.0.0.1:*` | `'none'` / `DENY` | |
| `/docs`, `/redoc` (FastAPI) | `'self'` | `'none'` / `DENY` | `https://cdn.jsdelivr.net` for scripts and styles, `fastapi.tiangolo.com` and `cdn.redoc.ly` images, `blob:` worker |
| `/oauth/authorize` consent pages | (none) | `'none'` / `DENY` | their own stricter policy: `default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'` |

Shared base: `default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'
https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data: blob:;
frame-src 'self'; worker-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'`.

Choices worth knowing:

- **`connect-src 'self'` closes a real hole.** The `/ui` pages accept `?api=<gateway>` for development, and the
  approval page sends the approver code a person types to that API. A crafted link such as
  `/ui/approve.html?order=…&api=https://evil.example` could have sent the code elsewhere. On a public host the
  browser now blocks it. We checked this in headless Chrome: the request was refused with a `connect-src` violation.
  Only loopback hosts keep `?api=` working against another local port.
- **`/buyer` is deliberately open on `connect-src`.** It is the buyer's client and can point at any Sous gateway.
  `buyer/gateway.mjs` refuses to send an agent token over plain http except to loopback. `buyer/verify.html` adds
  its own meta CSP (`connect-src 'none'`), and browsers apply both.
- **`'unsafe-inline'` for scripts and styles** is needed because the static pages use inline `<script>` blocks,
  inline `onclick` handlers and `style` attributes. No `'unsafe-eval'` is needed anywhere. The real protections are
  `connect-src` (where data can go), `frame-ancestors` (clickjacking on the approve page), `object-src 'none'`,
  `base-uri 'none'` and `form-action 'self'`. Moving the inline scripts into files and dropping `'unsafe-inline'`
  is on the production list below.
- **CORS** is unchanged: `*` with no cookies. Credentials are headers, never cookies, so a cross-site page can't ride
  a session. `Retry-After` is now listed in `Access-Control-Expose-Headers` so browser clients can read it.

Verified in headless Chrome over the DevTools protocol, with a `securitypolicyviolation` listener in every frame,
against the public-host policy (host-resolver mapping, so `connect-src 'self'` applied): `/ui/`, `/ui/live.html`,
`/ui/trace.html` (with an order), `/ui/cards.html` (the sandboxed card frames render and size themselves),
`/proof`, `/proof?preview=1`, `/ui/agent.html`, `/ui/break.html`, `/ui/approve.html` (with an order),
`/ui/sign-mandate.html`, `/ui/buyer.html`, `/buyer/`, `/buyer/verify.html`, `/docs`, `/redoc`, `/oauth/authorize`.
The result was 0 violations, apart from the intended `?api=https://evil.example` block. The loopback variant was
checked the same way.

---

## How to check this yourself

```bash
uv run python scripts/conformance.py --base https://<gateway>      # 17 outside-in checks, pass/fail
curl -sI https://<gateway>/ui/ | grep -iE 'content-security|frame|nosniff|referrer|strict|permissions'
curl -s https://<gateway>/audit/verify                              # hash chain ok, count, head
uv run pytest -q tests/test_security_headers.py tests/test_read_scopes.py tests/test_agent_auth.py tests/test_oauth.py tests/test_passkey.py
```

Red team: [REDTEAM.md](REDTEAM.md). Conformance kit: [CONFORMANCE.md](CONFORMANCE.md). Protocol and rules:
[PROTOCOL.md](PROTOCOL.md). Connector setup: [CONNECT.md](CONNECT.md).

## Not done: the production path

These are open gaps, not features we claim.

- **Agent tokens are bearer secrets.** Anyone holding one can use it until it is rotated. Production would bind
  tokens to a key (DPoP, RFC 9449, or mTLS client certificates, RFC 8705), or bind the agent's public key into the
  mandate VC so the agent proves possession on every call.
- **The approver is a shared code** (or a sandbox token). It stands in for the buyer's real login. Production:
  OIDC/SAML against the buyer's identity provider, named approvers, and passkeys required for every approval rather
  than per mandate. There is no lockout on wrong approver codes beyond the POST rate limits. GET reads with a
  guessed code are not rate limited, so the code must be long and random.
- **One signing key, no rotation.** The JWKS publishes a single key, and `verify` accepts only the current `kid`.
  Rotating it today would break online verification of everything signed before. Offline verification still works
  with a saved copy of the old JWKS. Production: multiple keys in the JWKS with overlap, retired keys kept for verification, and the
  key held in an HSM or KMS rather than a PEM file on the volume.
- **No external anchoring.** The hash chain, the signed checkpoints and MariaDB versioning detect tampering by
  anyone who lacks both full database control and the signing key. An operator with both could re-sign a rewritten
  history, except against checkpoints and receipts that outsiders already saved. Production: publish the signed
  checkpoint heads to a transparency log or timestamping authority (RFC 3161), or have Valio countersign them.
- **Rate limits are in-process memory.** Per IP and per credential, per worker, reset on restart, and not shared
  between deployments. The Valio budget *is* shared, through Valkey. Production: move the edge limits to Valkey or
  the reverse proxy.
- **`'unsafe-inline'` in the CSP.** Production: move the inline scripts into files and add hashes or nonces.
- **Trust on first use for buyer keys** (`SOUS_BUYER_KEY_TOFU=1` by default). The first mandate credential for a new
  buyer pins its key. Production: `SOUS_BUYER_KEY_TOFU=0`, with keys registered by an approver during onboarding.
