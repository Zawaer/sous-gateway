# Valtakirja

**AI purchasing agents carry a signed mandate from their human, and Valio Aimo's gateway enforces it.**

Valtakirja (Finnish for "power of attorney") is a gateway that lets any buyer's AI agent shop at Valio Aimo, the Finnish food-service wholesaler, over MCP or OpenAPI. The agent searches the catalog, gets a signed quote with live price and availability, and orders. Each agent holds a mandate from its human: spend limits, allergen rules, what may be substituted. A deterministic rules engine checks every order against it and puts it in one of four tiers: **silent** (ordered), **notify** (ordered, human gets an FYI), **ask** (held for a human) or **block** (refused, with the rule that fired). The LLM never decides. Every step goes into a hash-chained audit log that the buyer and Valio Aimo both read.

Built at Junction 2026 for Valio Aimo's challenge "Trusted Trade Agents".

- [docs/PROTOCOL.md](docs/PROTOCOL.md): the trust protocol (mandate, quote, tiers and rules, orders, audit log, offline verification).
- [docs/DECISIONS.md](docs/DECISIONS.md): technical choices and the path to production.
- [docs/STATUS.md](docs/STATUS.md): what works now, assumptions, open questions.

## Quick start

Needs Python 3.11+, [uv](https://docs.astral.sh/uv/) and a Valio Aimo API key (from the Junction challenge portal).

```sh
uv sync
cp .env.example .env                    # set VALIO_AIMO_API_KEY
uv run python -m scripts.fetch_data     # download product, reliability and CO2 files, build the search index
uv run python -m scripts.seed           # issue the demo mandates (hotel, hospital, school)
uv run uvicorn valtakirja.api:app --port 8000
```

Then open:

- http://localhost:8000/docs for the REST API (OpenAPI schema at `/openapi.json`)
- http://localhost:8000/ui/ for Valio Aimo's view: approval inbox, orders, mandates, audit log
- http://localhost:8000/buyer/ for the reference buyer (see [buyer/README.md](buyer/README.md))

Connect Claude Code as the buyer agent over MCP:

```sh
claude mcp add --transport http valtakirja http://localhost:8000/mcp/
```

Then ask it, for example: *"You are aurora-purchasing-agent with mandate M-HOTEL-AURORA. Buy 1,000 litres of lactose-free milk for delivery on Monday."*

The MCP server also runs over stdio: `uv run python -m valtakirja.mcp_server`.

Tests: `uv run pytest -q` (offline; the MariaDB tests run when `VALTAKIRJA_TEST_MARIADB_URL` is set).

## Configuration

Read from the environment, or from `.env` in development. Never commit `.env`.

| Variable | Default | Purpose |
|---|---|---|
| `VALIO_AIMO_API_KEY` | (empty) | Key for Valio Aimo's price, availability and download APIs. Required. |
| `VALIO_AIMO_API_BASE` | `https://junction26.valioaimo.fi/api` | Valio Aimo API base URL. |
| `VALIO_AIMO_CUSTOMER` | `123` | Customer number sent to Valio Aimo (the mock ignores it). |
| `VALTAKIRJA_DATA_DIR` | `./data` | Data files, catalog index, SQLite state and the Ed25519 signing key. |
| `VALTAKIRJA_STATE_DB` | `$DATA_DIR/state.db` | SQLite state file, used when no MariaDB URL is set. |
| `VALTAKIRJA_DB_URL` | (empty) | `mysql://user:pass@host:3306/db` to use MariaDB with system-versioned tables. |
| `VALTAKIRJA_QUOTE_TTL` | `900` | Quote lifetime in seconds (shortened further by order cut-offs). |
| `VALTAKIRJA_APPROVER_CODE` | (empty) | If set, human actions (approve, reject, issue or revoke a mandate) need it in the `X-Approver-Code` header. |
| `VALTAKIRJA_PUBLIC_BASE_URL` | (empty) | Public URL, used to build `order_url` and `approval_url` links. |
| `VALTAKIRJA_RATE_LIMIT_PER_MIN` | `60` | POST requests per client IP per minute. |
| `VALTAKIRJA_AIMO_CALLS_PER_MIN` | `60` | Global cap on live Valio Aimo API calls per minute. |
| `MARIADB_PASSWORD` | (empty) | Used by `compose.yaml` for the MariaDB service and to build `VALTAKIRJA_DB_URL`. |

## Docker

```sh
cp .env.example .env   # set VALIO_AIMO_API_KEY, MARIADB_PASSWORD (any random string), VALTAKIRJA_APPROVER_CODE
docker compose up -d --build
```

This starts the gateway on port 8000 and MariaDB 11.4. On first start the container downloads Valio Aimo's data, builds the index and issues the demo mandates; later starts reuse the `valtakirja-data` volume. State lives in the `mariadb-data` volume.

## Architecture

```mermaid
flowchart LR
    subgraph Buyer side
        H[Buyer human]
        A[Buyer agent<br/>Claude, Gemini, scripts]
        B[Reference buyer<br/>/buyer]
    end
    subgraph Valtakirja gateway
        MCP[MCP server<br/>/mcp]
        REST[REST + OpenAPI<br/>/docs]
        SVC[service.py<br/>search, quote, order, approve]
        CAT[(Catalog index<br/>SQLite FTS5 trigram)]
        Q[quote.py<br/>packs, lots, cut-offs, issues]
        M[mandate.py<br/>rules engine, 4 tiers]
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
    SVC --> SIG
    SVC --> ST
```

## Repo layout

```
valtakirja/       the gateway
  api.py          REST API, mounts MCP, UI and buyer
  mcp_server.py   MCP tools (same operations as REST)
  service.py      search, quote, order, approve, substitutes, history
  catalog.py      static catalog and FTS5 trigram search, allergen checks
  quote.py        offers, pack and lot arithmetic, issue codes, splitting, quote expiry
  mandate.py      mandate model and the deterministic rules engine
  substitutes.py  good / better / best substitutes
  signing.py      Ed25519 compact JWS and JWKS
  store.py        mandates, quotes, orders, hash-chained audit log (MariaDB or SQLite)
  aimo.py         Valio Aimo API client: batching, caching, call budget
  config.py       settings
scripts/          fetch_data.py (download data, build index), seed.py (demo mandates)
web/              Valio Aimo UI: approvals, orders, mandates, audit
buyer/            reference buyer in the browser, verifies JWS with Web Crypto
tests/            pytest, offline plus MariaDB
docs/             PROTOCOL, DECISIONS, STATUS, challenge brief
```

## License

MIT, see [LICENSE](LICENSE). Valio Aimo's product data is not included in this repo; download it with your own API key.
