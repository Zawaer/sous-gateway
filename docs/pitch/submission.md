# Junction submission

## Project name (≤30 chars)
Sous

## Punchline (≤200 chars)
AI purchasing agents carry a signed mandate from their human, and Valio Aimo's gateway enforces it: open MCP/OpenAPI ordering with deterministic guardrails and a verifiable audit trail.

## Description (markdown, ≤5000 chars)

**Sous** is an open front door that lets any AI purchasing agent find products, get a binding quote and place orders with Valio Aimo, inside limits its human set. Live at https://135-181-8-209.sslip.io; a judge path in 5 minutes is at the top of the README.

### The problem
We asked a general shopping agent for "1,000 litres of lactose-free milk by Monday". It researched for 13 minutes and concluded it would have to phone wholesalers on Saturday. Agents will buy from wholesalers anyway. If Valio Aimo doesn't define how they do it safely and openly, someone else's platform will.

### What it does
- **Search** 17,546 products (fi/sv/en) with filters for claims, allergens, labels and temperature. No API calls.
- **Quote** a shortlist with live price and availability: 2 batched Valio API calls, up to 4 when substitutes are priced for the options (1.0 on average with the cache). Pack and lot sizes, VAT, cut-offs, stock vs supplier-shipped, reliability, CO₂ estimate, splits when stock is short. Signed, expiring.
- **Check the mandate** in deterministic code. Every order lands in one of four tiers: **silent**, **notify** (ordered, human gets an FYI), **ask** (held for a human), **block** (refused, with the rule).
- **Approve** from a phone: push notification, approval link, reason and cut-off countdown.
- **Standing orders with drift detection**: a weekly order renews itself, but rising quantities, cost, a switched product or a new supply risk make it ask first. Demo: milk +18 % vs the 4-week median, cost +€135 (+16 %) → ask before renewing.
- **Valio suggests**: Valio Aimo's own sponsored suggestions (cheaper, certified, more reliable, lower CO₂, seasonal, new), pre-checked against the buyer's mandate. Offers only, labelled, audited.
- **Substitutes** good / better / best that understand product type (sour milk is not milk) and keep allergen rules.

In a real run, Claude Code on a laptop connected over MCP and ordered 1,000 × Valio Eila 1 L (€1,400 ex VAT). Above the hotel's notify threshold, it went through and the manager was told. 2 Valio API calls in total.

### Exceptions it handles
Terminal (supplier-shipped) products show an order cut-off that has already passed while still promising Monday delivery; the gateway rejects them and picks a stocked product. About 46 % of products have no allergen declaration; for a mandate with allergen rules, unknown means a human decides. Also: out of stock, later delivery, prices expiring before delivery, lot sizes, short stock, idempotent retries, quote expiry, and Valio rate limits (last-known data is marked stale and always goes to a human).

### Why it's trustworthy
- **The LLM never decides.** Limits, allergens, cut-offs, drift and tiers are plain code with plain-language reasons.
- **Signed artifacts.** Mandates, quotes and receipts are Ed25519 JWS, verifiable against `/.well-known/jwks.json`. Orders use only signed payloads: one order per quote, bound to the agent it was quoted for.
- **Two audit layers.** A hash chain (`/audit/verify`) and MariaDB system versioning: "what did the mandate say when this order was decided?" is one `AS OF` query.

### Any agent, any model
MCP and OpenAPI expose the same operations. A browser reference buyer verifies signatures itself. Our LLM buyer agent runs on OpenAI-compatible models (tested: Gemini, and Kimi on Verda Inference in the EU). The runtime binds identity, mandate and idempotency; the model only fills in the request. We score each model on the same 49 buyer sentences in one CI run (43557209740): Kimi K3.1 Pro on Verda Inference (EU) matched the buyer without correction in 48/49, Gemini 4 Hydrogen in 46/49, 0 mandate violations auto-ordered. Both missed H20 by ordering the real lactose-free milk the buyer asked for; it is now scripted-only. The cases are ours, not a held-out set. The mandate engine, not the model, keeps every unsafe request out.

### Results
Scripted buyer requests (no LLM, live Valio prices, cases we wrote): 59/59 in a local run, incl. partial-availability options and attack cases (order slicing, an agent approving itself, prompt injection, unusual quantity); 43/43 unsafe actions blocked or escalated. 59/59 also in CI on CRACI (last green: run 43554621653, 04e6539). Search first-result accuracy 56 % → 100 % on 39 fi/sv/en queries we wrote while tuning.

### Built like production
CI on CRACI: tests with MariaDB, Docker build, CycloneDX SBOM, grype gate, and a smoke test under deny-by-default egress (a gateway holding a supplier key must not talk to anything else). Deployed on a Verda CPU instance in Finland, redeployed from `main` until the Sunday freeze; docs/STATUS.md lists the live commit.

### What's next
Agent keys bound to the mandate (mTLS, DPoP or VC-bound keys) and buyer login via OIDC; Valio Aimo's real order API with per-customer prices; buyer-signed VC mandates by default (opt-in today) and signed AP2 mandates (today: unsigned AP2 v0.2 views); key rotation through JWKS.

## Technologies used
- Python, FastAPI, Pydantic, uv
- Model Context Protocol (MCP), OpenAPI
- MariaDB (system-versioned tables), SQLite FTS5
- Ed25519, JWS, JWKS
- JavaScript (web UI and reference buyer)
- Docker, Docker Compose, Caddy
- GitHub Actions on CRACI runners; syft (CycloneDX SBOM), grype
- Verda (CPU instance in Finland; Verda Inference for open-weight models)
- OpenAI-compatible LLM APIs: Gemini, Verda Inference, vLLM
- ntfy (push notifications)
- Claude Code, OpenAI Codex (development and as buyer agents)

## Tech challenges to tick
- **Valio Aimo: Trusted Trade Agents** (main): end-to-end request → lookup → order/approval, with guardrails, signatures and an audit trail.
- **Best Use of Verda**: the gateway (with MariaDB) runs on a Verda CPU instance in Finland; the buyer agent runs open-weight models on Verda Inference in the EU, scored per model. Write-up: [docs/VERDA.md](../VERDA.md).
- **Best use of CRACI**: CI runs on CRACI runners: tests with MariaDB, Docker build with SBOM and grype gate, compose smoke under deny-by-default egress, eval jobs whose egress is limited to PyPI, Valio and the model APIs. Write-up: [docs/CI.md](../CI.md).
- **Best Use of Codex**: Rene's reference buyer (`buyer/`) was built with Codex and acts as an independent agent against the gateway.
- **Best Use of Gemini API**: Gemini reads menus into a structured plan and drives the buyer agent; scored in CI. Write-up: [docs/GEMINI.md](../GEMINI.md).
- **Best Use of Nemotron**: Nemotron VL reads photos of order lists, menus and delivery notes; eval loop over 5 CI runs. Write-up: [docs/NEMOTRON.md](../NEMOTRON.md).
- **Best Use of Aiven**: Aiven for Valkey shares one Valio budget across deployments (live in CI; prod not yet). Write-up: [docs/AIVEN.md](../AIVEN.md).

(MariaDB has no tech-challenge prize at this event. System versioning still matters for Valio's auditability criterion, so it stays in the description.)

## Before submitting
- [x] Fill `[LLM EVAL]` in this description, the deck and `docs/STATUS.md` from `docs/LLM_EVAL.md`; if a model didn't run, delete the placeholder sentence instead.
- [x] Resolve `[CONFIRM]` on Gemini: tick it only if the Gemini run exists.
- [ ] Verda: the process is documented in `docs/VERDA.md`; **post a short summary in Verda's Discord challenge channel** (what runs where, which Verda products and why, link to the repo and the doc).
- [ ] CRACI: link a green CRACI run, with the SBOM and the network trace, from `docs/CI.md`.
- [ ] Video ≤ 2:00 uploaded; demo URL and repo link work in a private browser window.
- [ ] Description ≤ 5,000 characters; no secrets (approver code, API keys) anywhere in the repo, video or deck.
- [ ] Repo public, README quick start checked once on a clean clone.
