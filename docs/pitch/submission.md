# Junction submission

## Project name (≤30 chars)
Valtakirja

## Punchline (≤200 chars)
AI purchasing agents carry a signed mandate from their human, and Valio Aimo's gateway enforces it: open MCP/OpenAPI ordering with deterministic guardrails and a verifiable audit trail.

## Description (markdown, ≤5000 chars)

**Valtakirja** (Finnish for *mandate*, a power of attorney) is an open front door that lets any AI purchasing agent find products, get a binding quote and place orders with Valio Aimo, inside limits its human set.

### The problem
We asked a general shopping agent for "1,000 litres of lactose-free milk by Monday". It researched for 13 minutes and concluded it would have to phone wholesalers on Saturday. Wholesalers don't publish prices or stock to agents. Agents will buy from wholesalers anyway. If Valio Aimo doesn't define how they do it safely and openly, someone else's platform will.

### What it does
- **Search** 17,546 products (fi/sv/en) with filters for claims, allergens, labels and temperature. Search uses static data only, so it costs no API calls.
- **Quote** a shortlist with live price and availability from Valio Aimo's API (2 batched calls), with pack and lot sizes, VAT, cut-off checked against now, stock vs supplier-shipped, delivery reliability, CO₂ estimate, and splits when stock is short. Quotes are signed and expire.
- **Check the mandate** in deterministic code. Every order lands in one of four tiers: **silent** (ordered), **notify** (ordered, human gets an FYI), **ask** (held for a human), **block** (refused, with the rule that fired).
- **Approve** from a phone: the agent gets an approval link, the human sees the reason and a cut-off countdown. Late approvals expire.
- **Substitute** good / better / best, keeping the category, claims and allergen rules, with each compromise listed.
- **Audit** everything in a hash-chained log both sides can read, stored in MariaDB system-versioned tables.

In a real run, Claude Code on a laptop connected over MCP and ordered 1,000 × Valio Eila 1 L at €1.40/L (€1,400 ex VAT). That was above the hotel's notify threshold, so it went through and the manager was told. It took 2 Valio API calls in total.

### Exceptions it handles
Testing the live API showed that **Terminal (supplier-shipped) products show an order cut-off that has already passed while still promising Monday delivery**. The gateway rejects those and picks a stocked alternative. **About 46% of products have no allergen declaration**. For a mandate with allergen rules, unknown means unsafe, so a human decides. It also handles out-of-stock items, later delivery dates, prices expiring before delivery, lot sizes, short stock, idempotent retries and quote expiry.

### Why it's trustworthy
- **The LLM never decides.** It only turns language into a structured request and explains the result. Limits, allergens, cut-offs and tiers are plain code with plain-language reasons.
- **Signed artifacts.** Mandates, quotes and receipts are Ed25519 JWS, verifiable by anyone against `/.well-known/jwks.json`.
- **Two independent audit layers.** A hash chain (`/audit/verify`) and MariaDB system versioning. `/orders/{id}/history` answers "what did the mandate say when this order was decided?" with `FOR SYSTEM_TIME AS OF`, and any edit or delete of an audit row shows up.
- **Controls.** An approver code for human actions, per-IP rate limits and a global cap on Valio API calls.

### Open by design
The same operations are exposed over **MCP** (for agents like Claude) and **OpenAPI** (for everything else). We include an open reference buyer that runs in the browser and checks signatures itself. Agents never touch Valio Aimo's internal systems: they see the public catalog and the quotes they ask for, and the gateway caps how often it calls the backend. MIT-licensed.

### Results
`[EVAL: % correct]` of `[EVAL: N]` scripted requests handled correctly with no manual fixes; `[EVAL: % unsafe blocked]` of unsafe orders stopped; `[EVAL: API calls per request]` Valio API calls per request.

### What's next
OIDC identity for buyers and agents with mandates bound to the agent's key; Valio Aimo's real order API with per-customer prices; mandates as W3C Verifiable Credentials / AP2; push and SMS notifications; signing-key rotation through JWKS.

### Tech
Python, FastAPI, MCP (streamable HTTP), OpenAPI, SQLite FTS5 search, MariaDB system-versioned tables, Ed25519 / JWS / JWKS, plain JS web UI, Docker Compose, GitHub Actions CI.

## Technologies used
- Python, FastAPI, Pydantic, uv
- Model Context Protocol (MCP)
- OpenAPI
- MariaDB (system-versioned tables), SQLite FTS5
- Ed25519, JWS, JWKS
- JavaScript (web UI and reference buyer)
- Docker, Docker Compose
- GitHub Actions (CI on CRACI runners)
- Claude Code, OpenAI Codex (development and as buyer agents)

## Tech challenges to tick
- **Valio Aimo: Trusted Trade Agents** (main): end-to-end request → lookup → order/approval, with guardrails, signatures and an audit trail.
- **MariaDB**: mandates, orders and the audit log live in system-versioned tables, so we can show exactly which mandate version an order was decided under (`AS OF`), and tampering with history is detectable.
- **CRACI**: our pytest suite (offline tests + a MariaDB service) runs on every push on CRACI runners. `[CONFIRM: CI_RUNNER=craci set and a green run exists]`
- **OpenAI Codex**: the open reference buyer (`buyer/`) was built with Codex and acts as an independent agent against the gateway.
