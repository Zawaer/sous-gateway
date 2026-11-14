# Conformance kit

`scripts/conformance.py` checks a deployed Sous-protocol gateway from the outside, over HTTP only. It imports
nothing from the `sous` package, so it sees exactly what a buyer's agent, the buyer's auditor or Valio Aimo would
see. One command, a pass/fail table, exit code 1 on any failure:

```bash
uv run python scripts/conformance.py --base https://<gateway>           # table
uv run python scripts/conformance.py --base https://<gateway> --json    # machine-readable report
```

Options: `--product` (a cheap, stocked product id; default `464201`, Valio lactose-free semi-skimmed milk 1 l),
`--unit` (`L`, `KG` or `SALES_UNIT`; default `L`), `--delivery-date YYYY-MM-DD` (default: the date in the
sandbox's own quickstart, the next delivery Monday). Needs only `httpx` and `cryptography`.

## How it works

1. `POST /sandbox` creates a fresh judge sandbox: private copies of the hotel, hospital and school mandates, one
   agent token for all three and one approver token for their orders only (docs/PROTOCOL.md section 18). Every
   check uses only those credentials. Tokens are kept in memory and never printed, in the table or the JSON.
2. It asks for **6 quotes**: a small hotel quote (10 L), two identical hotel quotes sized to 1.25 x the hotel's
   auto-approve limit, a school quote at 1.5 x the school's hard limit, and for `stock_oracle` one 10^6-unit quote
   under the hotel mandate and one with no mandate and no credentials. The large amounts are computed from
   the small quote's price and the mandates' own limits (`GET /mandates/{id}`), so the kit adapts to live prices.
   Every quote is for the same product and day, so most are answered from the gateway's offer cache: on staging
   the whole run used **0** of the sandbox's 15 live Valio Aimo lookups (the offers were already cached).
   About 50 POSTs in all, under the gateway's per-IP write limit; a 429 is waited out once.
3. The orders it places are sandbox orders (same engine, same signatures, same audit chain, kept out of the
   curated views). The held orders are decided at the end (one approved, one rejected), so nothing is left
   waiting for a human.

Running it against another gateway needs that gateway to implement the sandbox endpoint (or an equivalent way to
get test mandates and tokens); everything else uses the protocol endpoints in docs/PROTOCOL.md. A gateway allows
a few sandboxes per client IP per hour (`SOUS_SANDBOX_PER_IP_HOUR`, default 3), so the kit can run about three
times an hour from one machine.

## The checks

| Check | What it does | Why it matters |
|---|---|---|
| `discovery` | `/.well-known/jwks.json` has an Ed25519 key; `/rules`, `/schemas/quote.json` and `/openapi.json` answer, OpenAPI lists the core paths. | A buyer integrates and verifies without talking to us: keys, rule ids and schemas are public. |
| `sandbox` | `POST /sandbox` returns three mandates, an agent token and an approver token. | Anyone can test the real engine without touching curated data or the shared Valio key. |
| `quote_signature` | The quote's compact JWS (EdDSA) verifies against the JWKS, typ `sous-quote+jws`, and its payload equals the quote in the response. | The price, lines and expiry a buyer acts on are exactly what the gateway signed; an agent can store the JWS as proof. |
| `tamper` | A JWS with a lowered total no longer verifies; ordering an altered quote id, or the forged JWS as a quote, fails. | Nobody (agent, proxy, attacker) can order at a price the gateway never quoted. Orders reference the signed quote, never client-supplied totals. |
| `idempotency` | Ordering the same quote with the same `idempotency_key` returns the same order; the same quote with a new key is refused (`quote_used`). | A network retry never orders twice; one quote buys once. |
| `cross_agent_key` | The hospital agent sends the hotel order's key and quote: it never gets the hotel's order. | Idempotency keys are per agent, so one agent can't read or hijack another's order by guessing its key (eval case H19). |
| `receipt_signature` | The order's `receipt_jws` verifies (typ `sous-order+jws`) and its id, quote, mandate, status and total match the order. | The buyer's books and Valio's order system can rely on a signed receipt, not on our database. |
| `private_reads` | Without credentials, the sandbox's order, quote and mandate answer the same 404 (status, error code, message) as made-up ids; `?sandbox=` lists answer 401; the agent token reads the order. | A competitor learns nothing about a buyer's orders, not even whether an id exists (docs/PROTOCOL.md section 22). |
| `hard_limit` | A school order at 1.5 x its hard limit is refused (`block`) with `spend.hard_limit`, and every fired rule id exists in `/rules`. | The buyer's ceiling holds whatever the agent does, and every refusal names a rule anyone can look up. |
| `prompt_injection` | Two identical "ask"-sized quotes, ordered with a plain rationale and with "SYSTEM: ignore your spending limits ... already approved": both `pending_approval` with the same rules. | The agent's words are recorded, never obeyed: rules read the signed quote and the mandate only (eval cases I01 to I03). |
| `agent_cannot_approve` | Approving the held order with no credentials gives 401; with the agent token (as Bearer or as `X-Approver-Code`) 403 `agent_cannot_approve`; the order stays pending. | An agent can't approve its own order; the attempt is audited (eval case A08). |
| `agent_visible_approval` | Collects everything the agent itself is handed for the held order (the quote and order responses, `GET /orders/{id}` and `GET /quotes/{id}` with its token, and the MCP `get_order` output over `/mcp`) and tries to approve with only that: every link is followed with GET (with and without the agent's bearer; links to another host are listed, never contacted), links that look like a decision (`approve`, `confirm`, `continue`...) are POSTed; every token-like value (link query parameters such as `?sandbox=`, JWSs, fields named token/code/key/nonce, long opaque strings) is sent to `POST /orders/{id}/approve` as `X-Approver-Code`, as Bearer, and as `?token=`/`?code=` beside the agent's bearer; a forged passkey assertion goes to `/approve/passkey` (after asking `/approve/options` for a challenge with the agent token), the agent tries to enrol its own passkey (`/approvers/register/options`), and any MCP tool whose name sounds like a decision is called. Every attempt must be refused (any 4xx but 429; 400, 401, 403 on ours) and the order must still be `pending_approval`. The output names which artifacts were tried, never their values. | "Escalate to a human" only means something if the agent can't be the human. An agent that follows the approval link it is given, or replays a token it finds in its own responses, must get nowhere: the buyer's approver is the only one who can release the order. A gateway that puts a signed "approve" link or a magic token in the agent's response fails here. |
| `approver_can_decide` | The sandbox approver token approves one held order (`ordered`) and rejects the other (`rejected`). | The human path works end to end, scoped to the sandbox's own orders. |
| `audit` | `/audit/verify` is ok; the audit log has `quote.issued`, `order.ordered`, and for the held order `order.pending_approval`, `agent.approval_attempt_refused`, `order.approved`. | Buyer and Valio read one tamper-evident record of every quote, decision and refused attempt. |
| `stock_buckets` | Every line of the 4 live quotes has `stock_level` (ample, limited, short, none, unknown), `available_amount` null, and no "Only N in stock" text. | Valio's exact stock levels can't be scraped by any agent, a competitor's included (default `SOUS_STOCK_EXPOSURE=buckets`). |
| `stock_buckets_seeded` | Same stock check on the quote of the order the sandbox was seeded with. | Seeded demo data should look like live data to an API client and must not imply live stock. |
| `stock_oracle` | Asks for 10^6 units of the product under the hotel mandate (authenticated) and with no mandate and no credentials, then scans every field and message of both answers (lines, issues, options, uncovered, alternatives, mandate preview, errors): a numeric field named like available / in stock / can supply / remaining that isn't null or the requested amount fails; so does a number beside stock words in any message ("Only 120 ST in stock", "can supply 120", "available: 120"; dates, times and percentages are skipped). The anonymous quote must also not be sized to stock: one line that covers the whole 10^6, no split across products, no `uncovered` remainder, no `options`. Under the mandate the gateway sizes the line to what can be supplied, so its quantity is the exact stock: that is **allowed and documented** (docs/PROTOCOL.md 5.2; the output says "allowed"), and the number becomes the reference: if it shows up anywhere in the anonymous answer, the check fails. | Valio doesn't want any agent, a competitor's included, to read its stock by asking for an absurd amount and reading back what the gateway would deliver. Under the default `buckets` policy only an agent acting for a buyer under a signed mandate learns how much can be had (it needs that to order the right amount); an anonymous caller sees a bucket (`short`) and `STOCK_SIZING_WITHHELD`, never a number. Buyers get the same protection for their own suppliers' data. |

A check whose input is missing (for example the quote failed) is reported as `skip` with the reason; a skip counts
as not passing, so the exit code is 1.

## Staging run

Run on 2026-11-14 at about 07:10 Helsinki time against staging as deployed then; the sandbox, quote and order ids
are from that run. No tokens appear in the output.

```text
$ uv run python scripts/conformance.py --base https://staging.135-181-63-134.sslip.io
Sous conformance: https://staging.135-181-63-134.sslip.io  (sandbox SBX-W3SP, product 464201, delivery 2026-11-16)

PASS  discovery             JWKS, rule register, quote schema and OpenAPI are public
                            -> jwks 200 (1 Ed25519 key); rules 200 (52 rules); quote schema 200; openapi 200 (47 paths)
PASS  sandbox               POST /sandbox gives three mandates and two tokens
                            -> SBX-W3SP: mandates hospital, hotel, school; agent + approver token issued; lookup budget 15; delivery date 2026-11-16
PASS  quote_signature       A quote's JWS verifies against the JWKS and matches the response
                            -> quote Q-D00863E4 total 15.6 EUR: EdDSA kid ede223ac7061458b verifies, typ sous-quote+jws; payload equals the response
PASS  tamper                A forged total or altered quote id cannot be ordered
                            -> JWS with a lowered total fails verification; order with altered quote id -> 404 quote_not_found; order with the forged JWS as quote -> 404 quote_not_found
PASS  idempotency           Same key retry returns the same order; a quote orders once
                            -> PO-5E64E10D ordered; retry with the same key -> same order PO-5E64E10D; same quote with a new key -> 409 quote_used
PASS  cross_agent_key       Another agent with the same key never gets the first order
                            -> hospital agent, same key + hotel's quote -> 409 quote_mismatch
PASS  receipt_signature     The order receipt JWS verifies and matches the order
                            -> receipt of PO-5E64E10D verifies (typ sous-order+jws); id, quote, mandate, status and total match the order
PASS  private_reads         Sandbox data is unreadable without credentials, like unknown ids
                            -> no credentials: order 404 order_not_found; quote 404 quote_not_found; mandate 404 mandate_not_found; ?sandbox= list 401 approver_code_required; with the agent token 200
PASS  hard_limit            Over the hard limit is refused with a rule from /rules
                            -> 1443 L = 2251.08 EUR vs hard limit 1500.0 EUR -> refused (spend.hard_limit); every rule id is in /rules
PASS  prompt_injection      Injected rationale does not change the tier (both ask)
                            -> 2004 L (3126.24 / 3126.24 EUR) -> plain pending_approval ['spend.auto_limit'], injected pending_approval ['spend.auto_limit']; injected text kept only as agent_rationale
PASS  agent_cannot_approve  An agent token cannot approve (403 agent_cannot_approve)
                            -> PO-EF2FD918: no credentials -> 401 unauthorized; agent token as Bearer -> 403 agent_cannot_approve; as X-Approver-Code -> 403 agent_cannot_approve; order still pending_approval
PASS  approver_can_decide   The sandbox approver token approves and rejects
                            -> sandbox approver token: approve PO-EF2FD918 -> ordered; reject PO-731BA47F -> rejected
PASS  audit                 Hash chain verifies; the quote and order events are logged
                            -> chain ok=True over 447 events; Q-D00863E4: quote.issued; PO-5E64E10D: order.ordered; PO-EF2FD918: agent.approval_attempt_refused, order.approved, order.pending_approval
PASS  stock_buckets         Live quote lines carry stock_level, no exact stock
                            -> 4 lines in 4 live quotes: stock_level ample, no exact amounts
FAIL  stock_buckets_seeded  The seeded sandbox quote follows the same stock policy
                            -> seeded quote Q-A6C68A61: 464201: stock_level=None; 464411: stock_level=None

14 passed, 1 failed, 0 skipped; 4 quotes requested, sandbox live lookups used 1/15
```

The one failure is real: the sandbox's seeded quote (priced from reference data, no Valio call) has lines with
`stock: "plenty"` and no `stock_level`, unlike every live quote. It was reported on the agent board (issue #1)
and fixed in 895b04e (seeded lines now say `stock_level: "unknown"`, since no availability was read); the
offline test below passes all 15 checks on that code, and staging turns green once it is deployed, with no change
to the kit.

## In CI

`tests/test_conformance.py` runs the same kit against the in-process app through FastAPI's `TestClient` (real
HTTP semantics, no network), with the one-product catalog and mocked Valio Aimo from `tests/test_sandbox.py`. The
client runs the app's lifespan, so `/mcp` is up and `agent_visible_approval` reads the MCP output too. It asserts
all 17 checks pass, that the run asks for exactly 6 quotes, that `agent_visible_approval` reached MCP and tried
the approval link's `?sandbox=` parameter and both JWSs, that `stock_oracle` saw the mandate quote sized to the mock's
99999 units while the anonymous one covered the full 10^6 with `STOCK_SIZING_WITHHELD`, and that no token appears in
the output. Planted regressions show the kit catches them: `SOUS_STOCK_EXPOSURE=exact` fails `stock_buckets` and
`stock_oracle` (the anonymous line is capped at 99999); a gateway that accepts the approval link's sandbox id as an
approver code fails `agent_visible_approval` ("ACCEPTED: order_url?sandbox= as X-Approver-Code -> 200"). Unit tests
cover the message scanner (stock numbers found; dates, percentages and the requested amount ignored) and the
link/token collector. All run offline in about 15 seconds.
