# Sous

**AI purchasing agents carry a signed mandate from their human, and Valio Aimo's gateway enforces it.**

Sous is a gateway that lets any buyer's AI agent shop at Valio Aimo, the Finnish food-service wholesaler, over MCP or OpenAPI. The agent searches the catalog, gets a signed quote with live price and availability, and orders. Each agent holds a mandate from its human: spend limits, allergen rules, what may be substituted. A deterministic rules engine checks every order against it and puts it in one of four tiers: **silent** (ordered), **notify** (ordered, human gets an FYI), **ask** (held for a human) or **block** (refused, with the rule that fired). The LLM never decides. Every step goes into a hash-chained audit log that the buyer and Valio Aimo both read.

Built at Junction 2026 for Valio Aimo's challenge "Trusted Trade Agents".

**Live:** production https://135-181-8-209.sslip.io (Verda CPU instance in Finland), staging https://staging.135-181-63-134.sslip.io. Both serve `/docs`, `/mcp/`, `/ui/` and `/buyer/`; human actions need the approver code.

- [docs/PROTOCOL.md](docs/PROTOCOL.md): the trust protocol (mandate, quote, tiers and rules, orders, standing orders, suggestions, notifications, audit log, offline verification).
- [docs/DECISIONS.md](docs/DECISIONS.md): technical choices and the path to production.
- [docs/STATUS.md](docs/STATUS.md): what works now, evaluation, assumptions, open questions.
- [docs/CI.md](docs/CI.md), [docs/LLM_EVAL.md](docs/LLM_EVAL.md), [docs/VERDA.md](docs/VERDA.md), [docs/demo-runbook.md](docs/demo-runbook.md).

## What it does

- **Search** 17,546 products in Finnish, Swedish and English, with filters for claims, allergens, labels and temperature. Search uses a local index and costs no Valio API calls.
- **Signed quotes** with live price and availability for a shortlist only: at most 2 Valio API calls per uncached request (0.95 on average with the cache). Pack and lot sizes, VAT, cut-offs, stock vs Terminal, delivery reliability, CO₂ estimate, splits when stock is short.
- **Mandate engine** in plain code. Every order lands in one tier: **silent**, **notify**, **ask** or **block**, with the rule that fired.
- **Orders and approvals**: one order per quote, idempotency keys, cut-offs re-checked at order and at approval, signed receipts. A human approves or rejects from a phone link.
- **Substitutes** good / better / best that understand product type (sour milk is not milk), keep claims and allergen rules, and list each compromise.
- **Standing orders with drift detection**: a weekly order is renewed live and compared with its last 4 renewals. Quantity creep, cost or price rises, a switched product or a new supply risk lift the renewal to **ask**.
- **Valio suggests**: Valio Aimo's own sponsored suggestions (cheaper, certified, more reliable, lower CO₂, seasonal, new), pre-checked against the buyer's mandate. Offers only, never orders, and audited.
- **Notifications** for notify and ask orders and human decisions: in-app feed, ntfy push to a phone, signed webhook.
- **Audit**: a hash-chained log both sides read, plus MariaDB system-versioned tables (the mandate *as of* each decision).
- **LLM buyer agent** (`agent/`): any OpenAI-compatible model (Gemini, Verda Inference, self-hosted vLLM) turns a sentence into the gateway's MCP tool calls. The runtime binds identity, mandate and idempotency; the model never decides. Scored per model by `scripts/llm_eval.py`.
- **Evaluation**: 45 scripted buyer requests pass 45/45; 36/36 unsafe actions are blocked or escalated. See [docs/STATUS.md](docs/STATUS.md#evaluation).

## API and MCP at a glance

| Area | REST | MCP tool |
|---|---|---|
| Catalog | `GET /products`, `GET /products/{id}`, `GET /allergens` | `search_products`, `get_product` |
| Substitutes | `GET /products/{id}/substitutes` | `suggest_substitutes` |
| Quotes | `POST /quotes`, `GET /quotes/{id}` | `request_quote` |
| Orders | `POST /orders`, `GET /orders`, `GET /orders/{id}`, `GET /orders/{id}/history` | `place_order`, `get_order` |
| Approvals (human) | `POST /orders/{id}/approve`, `POST /orders/{id}/reject` | none, on purpose |
| Mandates | `POST /mandates` (human), `GET /mandates[/{id}[/history]]`, `POST /mandates/{id}/revoke` (human) | `get_mandate` |
| Suggestions | `GET /mandates/{id}/suggestions` | `get_suggestions` |
| Standing orders | `POST /standing-orders`, `GET /standing-orders[/{id}]`, `POST /standing-orders/{id}/renew`, `.../pause`, `.../resume` (human) | `create_standing_order`, `renew_standing_order`, `get_standing_order` |
| Notifications | `GET /notifications`, `POST /notifications/test` (human) | |
| Audit and trust | `GET /audit`, `GET /audit/verify`, `GET /.well-known/jwks.json`, `GET /health` | |

MCP also offers the resources `sous://allergens` and `sous://mandates/{id}`, and a `purchase` prompt. Full details, error codes and rule ids: [docs/PROTOCOL.md](docs/PROTOCOL.md).

## Quick start

Needs Python 3.11+, [uv](https://docs.astral.sh/uv/) and a Valio Aimo API key (from the Junction challenge portal).

```sh
uv sync
cp .env.example .env                    # set VALIO_AIMO_API_KEY
uv run python -m scripts.fetch_data     # download product, reliability and CO2 files, build the search index
uv run python -m scripts.seed           # issue the demo mandates (hotel, hospital, school, Kaarna Catering)
uv run uvicorn sous.api:app --port 8000
```

Then open:

- http://localhost:8000/docs for the REST API (OpenAPI schema at `/openapi.json`)
- http://localhost:8000/ui/ for Valio Aimo's view: approval inbox, orders, mandates, audit log
- http://localhost:8000/buyer/ for the reference buyer (see [buyer/README.md](buyer/README.md))

Connect Claude Code as the buyer agent over MCP:

```sh
claude mcp add --transport http sous http://localhost:8000/mcp/
```

Then ask it, for example: *"You are aurora-purchasing-agent with mandate M-HOTEL-AURORA. Buy 1,000 litres of lactose-free milk for delivery on Monday."* Against the deployed gateway, use `https://135-181-8-209.sslip.io/mcp/` instead.

Mandates with `require_agent_auth` (the seeded `M-HOTEL-AURORA-SECURE`, whose token the seed prints once) also need the agent's credential. Pass it as a header:

```sh
claude mcp add --transport http sous http://localhost:8000/mcp/ --header "Authorization: Bearer <token>"
```

Without it, quotes and orders on that mandate get `agent_auth_required`. See [PROTOCOL.md, section 16](docs/PROTOCOL.md#16-agent-authentication).

The MCP server also runs over stdio: `uv run python -m sous.mcp_server`.

Demos and evaluation (each takes `--base <gateway URL>`; see [docs/demo-runbook.md](docs/demo-runbook.md)):

```sh
uv run python scripts/demo.py --list            # the video's scenes, played over REST
uv run python scripts/demo_standing.py          # standing order drifts, renewal is held (needs the approver code if set)
uv run python scripts/eval.py                   # 45 scripted buyer requests, live Valio prices -> evals/results.json
uv run python -m agent.buyer "We need 1,000 litres of lactose-free milk delivered on Monday." \
    --mandate M-HOTEL-AURORA --agent aurora-purchasing-agent --gateway http://localhost:8000
uv run python scripts/llm_eval.py --provider verda   # the eval sentences through the LLM agent, per model
```

The LLM agent needs `GEMINI_API_KEY`, `VERDA_INFERENCE_API_KEY` or `LLM_API_KEY` with `LLM_BASE_URL` (see `agent/llm.py`).

Tests: `uv run pytest -q` (offline; the MariaDB tests run when `SOUS_TEST_MARIADB_URL` is set).

## Configuration

Read from the environment, or from `.env` in development. Never commit `.env`. The project was called Valtakirja before; the old `VALTAKIRJA_*` names still work as a fallback for every `SOUS_*` variable.

| Variable | Default | Purpose |
|---|---|---|
| `VALIO_AIMO_API_KEY` | (empty) | Key for Valio Aimo's price, availability and download APIs. Required. |
| `VALIO_AIMO_API_BASE` | `https://junction26.valioaimo.fi/api` | Valio Aimo API base URL. |
| `VALIO_AIMO_CUSTOMER` | `123` | Customer number sent to Valio Aimo (the mock ignores it). |
| `SOUS_DATA_DIR` | `./data` | Data files, catalog index, SQLite state and the Ed25519 signing key. |
| `SOUS_STATE_DB` | `$DATA_DIR/state.db` | SQLite state file, used when no MariaDB URL is set. |
| `SOUS_DB_URL` | (empty) | `mysql://user:pass@host:3306/db` to use MariaDB with system-versioned tables. |
| `SOUS_QUOTE_TTL` | `900` | Quote lifetime in seconds (shortened further by order cut-offs). |
| `SOUS_APPROVER_CODE` | (empty) | If set, human actions (approve, reject, issue or revoke a mandate) need it in the `X-Approver-Code` header. |
| `SOUS_REQUIRE_AGENT_AUTH` | (empty) | `1` = every mandate needs the agent's bearer credential for quotes, orders and renewals, not only those with `require_agent_auth`. |
| `SOUS_PUBLIC_BASE_URL` | (empty) | Public URL, used to build `order_url` and `approval_url` links. |
| `SOUS_RATE_LIMIT_PER_MIN` | `60` | POST requests per client IP per minute. |
| `SOUS_AIMO_CALLS_PER_MIN` | `60` | Global cap on live Valio Aimo API calls per minute. |
| `MARIADB_PASSWORD` | (empty) | Used by `compose.yaml` for the MariaDB service and to build `SOUS_DB_URL`. |
| `SOUS_TRUST_CF_HEADER` | (empty) | `1` to take the client IP from `CF-Connecting-IP` (only behind Cloudflare). |
| `SOUS_NTFY_TOPIC` | (empty) | ntfy topic for push notifications to the buyer's phone. Optional `SOUS_NTFY_SERVER` (default `https://ntfy.sh`) and `SOUS_NTFY_TOKEN`. |
| `SOUS_NOTIFY_WEBHOOK_URL` | (empty) | Webhook that receives notifications as JSON. With `SOUS_NOTIFY_WEBHOOK_SECRET` the body is HMAC-SHA256 signed. |
| `GEMINI_API_KEY`, `VERDA_INFERENCE_API_KEY` | (empty) | Model keys for the LLM buyer agent (`agent/`) and `scripts/llm_eval.py`. Not used by the gateway. |
| `LLM_PROVIDER`, `LLM_MODEL`, `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_REASONING_EFFORT` | (presets) | Override the agent's model: `gemini`, `verda` or `local` (any OpenAI-compatible server). |

## Docker

```sh
cp .env.example .env   # set VALIO_AIMO_API_KEY, MARIADB_PASSWORD (any random string), SOUS_APPROVER_CODE
docker compose up -d --build
```

This starts the gateway on port 8000 and MariaDB 11.4. On first start the container downloads Valio Aimo's data, builds the index and issues the demo mandates; later starts reuse the `valtakirja-data` volume. State lives in the `mariadb-data` volume.

## Architecture

```mermaid
flowchart LR
    subgraph Buyer side
        H[Buyer human]
        A[Buyer agent<br/>Claude Code, agent/ LLM agent, scripts]
        B[Reference buyer<br/>/buyer]
    end
    subgraph Sous gateway
        MCP[MCP server<br/>/mcp]
        REST[REST + OpenAPI<br/>/docs]
        SVC[service.py<br/>search, quote, order, approve]
        CAT[(Catalog index<br/>SQLite FTS5 trigram)]
        Q[quote.py<br/>packs, lots, cut-offs, issues]
        M[mandate.py<br/>rules engine, 4 tiers]
        SO[standing.py<br/>standing orders, drift]
        SG[suggest.py<br/>Valio suggests]
        N[notify.py<br/>feed, ntfy, webhook]
        SIG[signing.py<br/>Ed25519 JWS, JWKS]
        ST[(store.py<br/>MariaDB system-versioned<br/>or SQLite)]
        UI[Valio Aimo UI<br/>/ui]
    end
    V[Valio Aimo APIs<br/>price, availability]

    A --> MCP --> SVC
    B --> REST
    A --> REST --> SVC
    H -- approve / reject --> UI --> REST
    SVC --> CAT
    SVC --> Q -- shortlist only --> V
    SVC --> M
    SO --> SVC
    SG --> M
    SVC --> N -- notify / ask --> H
    SVC --> SIG
    SVC --> ST
```

## Repo layout

```
sous/             the gateway
  api.py          REST API, mounts MCP, UI and buyer
  mcp_server.py   MCP tools, resources and prompt (same operations as REST)
  service.py      search, quote, order, approve, substitutes, history
  catalog.py      static catalog and FTS5 trigram search, allergen checks
  quote.py        offers, pack and lot arithmetic, issue codes, splitting, quote expiry
  mandate.py      mandate model and the deterministic rules engine
  substitutes.py  good / better / best substitutes, product-type aware
  standing.py     standing orders and drift detection (standing_store.py: storage)
  suggest.py      Valio suggests: sponsored, mandate-checked suggestions
  notify.py       notifications: feed, ntfy push, signed webhook (notifications_store.py)
  signing.py      Ed25519 compact JWS and JWKS
  store.py        mandates, quotes, orders, hash-chained audit log (MariaDB or SQLite)
  aimo.py         Valio Aimo API client: batching, caching, call budget
  config.py       settings
agent/            LLM buyer agent: buyer.py (MCP tool loop), llm.py (OpenAI-compatible model presets)
scripts/          fetch_data.py, seed.py, demo.py, demo_standing.py, eval.py, llm_eval.py
evals/            cases.yaml (45 cases), results.json, llm_results.*.json
web/              Valio Aimo UI: approvals, orders, mandates, audit, stats, live activity
buyer/            reference buyer in the browser, verifies JWS with Web Crypto
tests/            pytest, offline plus MariaDB
docs/             PROTOCOL, DECISIONS, STATUS, CI, LLM_EVAL, VERDA, demo runbook, pitch/
```

## License

MIT, see [LICENSE](LICENSE). Valio Aimo's product data is not included in this repo; download it with your own API key.
