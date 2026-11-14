# Sous

Sous is a seller-side trust gateway for Valio Aimo, the Finnish food-service wholesaler: outside AI purchasing agents search the catalog over MCP or REST, get quotes signed by the gateway with live Valio price and availability, and order under a mandate their buyer set (spend limits, allergens, substitutes). Plain code, not the LLM, puts every order in one of four tiers (auto, auto + FYI, needs approval, refused), and every step goes into a hash-chained audit log that the buyer and Valio Aimo both read.

Built at Junction 2026 for Valio Aimo's challenge "Trusted Trade Agents".

## Judge path in 5 minutes

The demo server is production, `https://135-181-8-209.sslip.io`, set as `SOUS` below. It is redeployed from `main` until the Sun 15.11 08:00 Helsinki freeze. If `curl $SOUS/rules` returns 404, it is still on the older build (no `/rules`, AP2 exports, read scopes or demo clock): use staging, `https://staging.135-181-63-134.sslip.io`, which runs current `main` and is the fallback. [docs/STATUS.md](docs/STATUS.md#deployed-now) says what runs where. Needs `curl` and `jq`.

1. **Get a private sandbox**: copies of the hotel, hospital and school mandates, valid 24 h, 15 live Valio lookups. The two tokens in the response are shown once.
   ```bash
   export SOUS=https://135-181-8-209.sslip.io
   curl -s -X POST $SOUS/sandbox | tee sandbox.json | jq '{sandbox, mandates: [.mandates[] | {mandate_id, rules}]}'
   ```
2. **Run the quickstart**: the response's `quickstart.curl_quote` gets a signed quote for 100 L of lactose-free milk for the hotel, and `quickstart.curl_order` orders it.
   ```bash
   Q=$(jq -r .quickstart.curl_quote sandbox.json | sh | jq -r .quote.id)
   jq -r .quickstart.curl_order sandbox.json | sed "s/<quote.id>/$Q/g" | sh | jq 'if .error then . else {id, status, tier: .decision.outcome, rules: [.decision.findings[].rule]} end'
   ```
   Within the hotel's limits it is `ordered`, tier `silent` (auto), rule `mandate.ok`. Every finding carries a `rule_url`. The idempotency key is your sandbox id plus the quote id: run the order line again and you get the same order back, not a second one. Keys are scoped per agent, so no two judges clash.
3. **Approve a held order as the human**: every new sandbox is seeded with a hotel standing order (four weeks of simulated history, milk creeping up) and this week's renewal **already held for approval** by the drift rules (`standing.quantity_creep`, `standing.quantity_trend`, `standing.cost_drift`); `seeded` in the response names it. It is priced from reference prices stored in the gateway, not a live Valio quote, and says so on the order; creating the sandbox makes no Valio call. Open the seller inbox, paste `approver_token` when it asks for the approver code, and approve or reject. The token works only on your sandbox's orders.
   ```bash
   jq '.seeded | {held_order_id, why, reasons}' sandbox.json
   jq -r .quickstart.seller_ui sandbox.json     # $SOUS/ui/?sandbox=SBX-....#/inbox
   ```
4. **Check the rules and the audit chain**:
   ```bash
   curl -s $SOUS/rules | jq '.tiers, .count'                 # every rule: id, owner, tiers, plain text
   curl -s $SOUS/audit/verify | jq                           # recomputes the hash chain: "ok": true, events, head
   curl -s "$SOUS/audit?sandbox=$(jq -r .sandbox.id sandbox.json)&limit=5" \
     -H "Authorization: Bearer $(jq -r .agent_token sandbox.json)" | jq '.events[] | {seq, event, hash, prev_hash}'
   ```
5. **Connect an MCP client** to `$SOUS/mcp/` with the agent token, then paste one of the four `prompts`. Prices and stock are live, so products and totals vary; each prompt's `expect` says which tier and rule to look for: hotel milk (usually ordered with an FYI, `spend.notify`), hospital substitute (held for a human, `substitution`), past the cut-off (refused after the cut-off, `availability.cutoff_passed`), school over budget (refused above €1,500, `spend.hard_limit`).
   ```bash
   jq -r .quickstart.claude_code sandbox.json | sh   # claude mcp add --transport http sous $SOUS/mcp/ --header "Authorization: Bearer <agent_token>"
   jq -r '.prompts[] | .title, .prompt, ""' sandbox.json
   ```

Evaluation, as [docs/STATUS.md](docs/STATUS.md#evaluation) states it: scripted cases (no LLM, live Valio prices) 59/59 in a local run (`evals/results.json` at 285f126), 43/43 unsafe actions blocked or escalated; 59/59 also in CI on CRACI (last green: run 43554621653, 04e6539). LLM agent, live in CI run 43557209740 (0b0ad68), 49 buyer sentences: Kimi K3.1 Pro 48/49, Gemini 4 Hydrogen 46/49, 0 mandate violations auto-ordered.

## Where to read more

- [docs/pitch/try-it.md](docs/pitch/try-it.md): the sandbox walk-through as a one-page handout.
- [docs/STATUS.md](docs/STATUS.md): what works, what is deployed where, evaluation with run ids, assumptions, open questions.
- [docs/PROTOCOL.md](docs/PROTOCOL.md): the full protocol. Start with [6. Tiers and rules](docs/PROTOCOL.md#6-tiers-and-rules), [12. Audit log](docs/PROTOCOL.md#12-audit-log) and [18. Judge sandbox](docs/PROTOCOL.md#18-judge-sandbox).
- [docs/DECISIONS.md](docs/DECISIONS.md): technical choices and the path to production.
- [docs/CONFORMANCE.md](docs/CONFORMANCE.md): one command (`scripts/conformance.py --base <url>`) runs 15 black-box checks (signatures, tampering, idempotency, self-approval, private reads, hard limit, prompt injection, audit, stock exposure) against any Sous gateway; latest staging run included.
- [docs/STANDARDS.md](docs/STANDARDS.md): mapping to AP2, ACP, A2A, MCP server cards, schema.org, W3C VC 2.0, JOSE.
- [docs/LLM_EVAL.md](docs/LLM_EVAL.md), [docs/CI.md](docs/CI.md), [docs/VERDA.md](docs/VERDA.md), [docs/GEMINI.md](docs/GEMINI.md), [docs/NEMOTRON.md](docs/NEMOTRON.md), [docs/AIVEN.md](docs/AIVEN.md), [docs/demo-runbook.md](docs/demo-runbook.md).
- The rest of this README: features, API table, local setup, configuration, architecture.

## What it does

- **Search** 17,546 products in Finnish, Swedish and English, with filters for claims, allergens, labels and temperature. Search uses a local index and costs no Valio API calls.
- **Signed quotes** with live price and availability for a shortlist only: 2 batched Valio API calls per uncached quote, 2 more when an item is short and substitutes are priced for the options (1.0 on average with the cache in `evals/results.json`). Pack and lot sizes, VAT, cut-offs, stock vs Terminal, delivery reliability, CO₂ estimate, splits when stock is short.
- **Mandate engine** in plain code. Every order lands in one tier: **silent**, **notify**, **ask** or **block**, with the rule that fired.
- **Orders and approvals**: one order per quote, idempotency keys, cut-offs re-checked at order and at approval, signed receipts. A human approves or rejects from a phone link.
- **Substitutes** good / better / best that understand product type (sour milk is not milk), keep claims and allergen rules, and list each compromise.
- **Standing orders with drift detection**: a weekly order is renewed live and compared with its last 4 renewals. Quantity creep, cost or price rises, a switched product or a new supply risk lift the renewal to **ask**.
- **Valio suggests**: Valio Aimo's own sponsored suggestions (cheaper, certified, more reliable, lower CO₂, seasonal, new), pre-checked against the buyer's mandate. Offers only, never orders, and audited.
- **Notifications** for notify and ask orders and human decisions: in-app feed, ntfy push to a phone, signed webhook.
- **Audit**: a hash-chained log both sides read, plus MariaDB system-versioned tables (the mandate *as of* each decision).
- **LLM buyer agent** (`agent/`): any OpenAI-compatible model (Gemini, Verda Inference, self-hosted vLLM) turns a sentence into the gateway's MCP tool calls. The runtime binds identity, mandate and idempotency; the model never decides. Scored per model by `scripts/llm_eval.py`.
- **Snap a photo of the order** (CLI; the web upload in `/agent/` is off on the servers): NVIDIA Nemotron 3.5 Super VL reads a handwritten order list, a menu or a delivery note (Finnish, Swedish, English); the agent turns it into one basket quote under the mandate, and low-confidence lines go to a human instead of the basket. See [docs/NEMOTRON.md](docs/NEMOTRON.md).
- **Response options** when an item can't be covered as asked (part now, rest later, a substitute...), each with a mandate preview. **Rule register** at `GET /rules`.
- **Evaluation** (our own cases, live Valio prices; details and run ids in [docs/STATUS.md](docs/STATUS.md#evaluation)): scripted, no LLM: all 59 cases 59/59 in a local run (43/43 unsafe actions blocked or escalated; incl. partial-availability options and attack cases: slicing, self-approval, prompt injection, unusual quantity); 59/59 also in CI on CRACI (last green: run 43554621653, 04e6539; also 95dd711 with the shared Valio budget on Aiven Valkey). LLM agent, 49 sentences, live in CI run 43557209740 (0b0ad68): Kimi K3.1 Pro 48/49, Gemini 4 Hydrogen 46/49 (both miss H20, where they found real lactose-free milk instead of the case's coffee milk); 0 mandate violations auto-ordered with either.

## Try it yourself

The demo server (`$SOUS`, see [Judge path](#judge-path-in-5-minutes); production is a Verda CPU instance in Finland) and staging both serve `/docs`, `/mcp/`, `/ui/` and `/buyer/`; human actions need the approver code. Get a private sandbox there (copies of the hotel, hospital and school mandates, valid 24 h, kept out of the curated demo data):

```bash
curl -s -X POST $SOUS/sandbox | jq
```

The response has your sandbox id (e.g. `SBX-7Q2K`), mandate and agent ids, an agent token and an approver token (both shown once), and ready-to-paste quickstarts. Then:

1. Connect Claude Code with the `quickstart.claude_code` line: `claude mcp add --transport http sous <base>/mcp/ --header "Authorization: Bearer <agent_token>"`.
2. Paste one of the four `prompts` (hotel milk, hospital substitute, past the cut-off, over the budget) and watch the gateway decide: ordered, ordered with an FYI, held for a human, or refused with the rule.
3. Open `quickstart.seller_ui` (`/ui/?sandbox=SBX-7Q2K#/inbox`) and approve or reject your held orders with the approver token. It works only on your sandbox's orders.
4. Open `quickstart.break_ui` (`/ui/break.html?sandbox=SBX-7Q2K`), paste your agent token and try seven attacks (self-approval, slicing, prompt injection, a tampered quote, replay, over the limit, a forbidden allergen): each shows the verdict, the rule and the audit events, with no Valio call.

Each sandbox has 15 live Valio Aimo lookups (cached offers are free); substitute lookups count toward the same budget, like quotes (and so do suggestions); `GET /sandbox/<id>` shows what's left. One-page handout: [docs/pitch/try-it.md](docs/pitch/try-it.md); details in [PROTOCOL.md section 18](docs/PROTOCOL.md#18-judge-sandbox).

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
| Discovery and standards | `GET /.well-known/agent-card.json` (A2A), `GET /.well-known/mcp/server-card.json` (MCP), `GET /schemas/{mandate,quote,order,receipt}.json`, `GET /mandates/{id}/ap2`, `GET /quotes/{id}/ap2`, `GET /quotes/{id}/schema-org`, `GET /orders/{id}/ap2`, `GET /orders/{id}/schema-org` | |
| Judge sandbox | `POST /sandbox`, `GET /sandbox/{id}`, `DELETE /sandbox/{id}` (human); `?sandbox=<id>` on the list endpoints | |

MCP also offers the resources `sous://allergens` and `sous://mandates/{id}`, and a `purchase` prompt. Full details, error codes and rule ids: [docs/PROTOCOL.md](docs/PROTOCOL.md). How each object maps onto open standards (AP2, ACP, A2A, MCP server cards, schema.org, W3C VC 2.0, JOSE), what is exported and what is still missing: [docs/STANDARDS.md](docs/STANDARDS.md).

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

Then ask it, for example: *"You are aurora-purchasing-agent with mandate M-HOTEL-AURORA. Buy 1,000 litres of lactose-free milk for delivery on Monday."* Against the demo server, use `$SOUS/mcp/` instead.

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
uv run python scripts/eval.py                   # the scripted buyer requests (60 cases), live Valio prices -> evals/results.json
uv run python -m agent.buyer "We need 1,000 litres of lactose-free milk delivered on Monday." \
    --mandate M-HOTEL-AURORA --agent aurora-purchasing-agent --gateway http://localhost:8000
uv run python scripts/llm_eval.py --provider verda   # the eval sentences through the LLM agent, per model
```

The LLM agent needs `GEMINI_API_KEY`, `VERDA_INFERENCE_API_KEY` or `LLM_API_KEY` with `LLM_BASE_URL` (see `agent/llm.py`).

### From a menu to one order (recipe planner)

A chef says what they are cooking, not what to buy. `agent/recipe.py` turns that into one basket quote:

```sh
uv run python -m agent.recipe --mandate M-HOTEL-AURORA --agent aurora-purchasing-agent \
    "Lasagne for 120 on Monday, we have 10 kg flour and plenty of salt"            # add --order to place it
```

It prints the chain: **recipe** (dishes, portions, per-portion ingredients) -> **ingredients** (scaled, minus stock on
hand, rounded up, in kg/L) -> **products** (up to 5 catalog candidates per ingredient, filtered by the mandate's
allergen and claim rules) -> **one signed basket quote** (the gateway picks packs and the cheapest orderable product,
reports what is uncovered) -> substitutes for gaps -> **mandate preview** -> order or ask. Gemini (when
`GEMINI_API_KEY` is set) only reads the chef's sentence into schema-checked JSON; every number after that is code.
Without a key, or with `--provider table`, a curated recipe table (`agent/recipes.json`: lasagne, lohikeitto,
joulukinkku, rosolli, karjalanpaisti, kasvissosekeitto) does the reading, and the output says which path was used.
`agent.buyer` routes menu-shaped requests here automatically. Eval: `uv run python scripts/recipe_eval.py --providers
table gemini` (7 cases, `evals/recipe_cases.yaml`). See [docs/GEMINI.md](docs/GEMINI.md) and
[PROTOCOL.md, section 20](docs/PROTOCOL.md#20-buyer-side-recipe-planning-reference-agent).

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
| `SOUS_AIMO_CALLS_PER_MIN` | `60` | Per-process cap on live Valio Aimo API calls per minute. |
| `SOUS_VALKEY_URL` | (empty) | Aiven for Valkey (`rediss://...`): one Valio call budget, circuit breaker and offer cache shared by every deployment ([docs/AIVEN.md](docs/AIVEN.md)). Empty = per-process only. |
| `SOUS_DEPLOYMENT_ROLE` | `dev` | `prod`, `staging`, `ci`, `demo` or `dev`. Only `prod` may use the whole shared budget; the others stop at half of it. |
| `SOUS_VALIO_GLOBAL_CALLS_PER_MIN` | `60` | Shared Valio Aimo calls per minute across all deployments (sliding window in Valkey). |
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
                  recipe.py (menu -> basket), vision.py (photo -> basket, Nemotron VL)
scripts/          fetch_data.py, seed.py, demo.py, demo_standing.py, eval.py, llm_eval.py
evals/            cases.yaml (60 cases), results.json, llm_results.*.json, vision/
web/              Valio Aimo UI: approvals, orders, mandates, audit, stats, live activity
buyer/            reference buyer in the browser, verifies JWS with Web Crypto
tests/            pytest, offline plus MariaDB
docs/             PROTOCOL, DECISIONS, STATUS, CI, LLM_EVAL, VERDA, demo runbook, pitch/
```

## License

MIT, see [LICENSE](LICENSE). Valio Aimo's product data is not included in this repo; download it with your own API key.
