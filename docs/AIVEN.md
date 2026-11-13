# One Valio Aimo budget for every deployment (Aiven for Valkey)

## The problem

Sous is a purchasing gateway: every quote asks Valio Aimo's price and availability APIs for live
data. We have **one** Valio Aimo API key, and it is shared by everything we run: the prod gateway
the judges use, staging, the CI evals (scripted and LLM buyer agents) and demo scripts. Valio's
rate limits are undisclosed. Each process already had a per-minute call cap, a circuit breaker and
a cache ([sous/aimo.py](../sous/aimo.py)), but each one only saw its own calls. One night two CI jobs
running in parallel got HTTP 429 from Valio, and so would prod, right in the middle of a demo.

The limit belongs to the key, so the budget has to be shared by everything that uses the key.

## Why Valkey

We needed a small, fast piece of shared state that every deployment can reach (GitHub/CRACI
runners, our servers, a laptop running a demo) with atomic counters and expiring keys. That is
exactly what Redis-protocol stores do well: `INCR` + `EXPIRE` for a rate window, `SET ... PX`
for a pause, `MGET`/`SET ... EX` for a cache with TTLs. **Aiven for Valkey** gives us this
managed, with TLS (`rediss://`) and no server of our own to run; plain `redis-py` talks to it.
It doesn't replace our database (MariaDB holds mandates, quotes, orders and the audit log): it only
holds short-lived coordination state that can be lost at any time without harm.

## Design

[sous/shared.py](../sous/shared.py) is used by the Valio client [sous/aimo.py](../sous/aimo.py).
All keys start with `sous:valio:`.

1. **Global call budget, sliding window.** Every live Valio call first does, in one `MULTI`:
   `INCR budget:<minute>`, `EXPIRE`, `GET budget:<minute-1>`. Usage is the sliding-window estimate
   `prev × (1 − elapsed fraction of this minute) + cur`. Over the limit: `DECR` and refuse with a
   `Retry-After`; the agent gets the usual retryable `valio_unavailable` (503). A per-role counter
   `budget:<minute>:<role>` is kept for visibility.
2. **Prod first.** `SOUS_DEPLOYMENT_ROLE` = `prod`, `staging`, `ci`, `demo` or `dev`. The cap compares
   *everybody's* usage with the role's share: prod may use 100 % of
   `SOUS_VALIO_GLOBAL_CALLS_PER_MIN` (default 50), every other role stops at 50 %. So however busy CI
   and staging are, at least half of every minute is left for prod during judging.
3. **Shared circuit breaker.** When any deployment gets a 429 it sets `circuit_until` (epoch, `PX` =
   Retry-After or 10 s, never shortening an existing pause). Every deployment checks it before calling
   Valio and pauses too, so one rate-limit response doesn't turn into a 429 storm from the others.
4. **Shared offer cache (L2).** Each fetched item is stored as JSON under
   `offer:<price|availability>:<date>:<product id>` with its fetch time. The in-process cache stays as
   L1. Freshness is judged from the fetch time (prices 600 s, availability 60 s), but keys live for
   their **stale-copy horizon** (15 min / 5 min): when Valio fails, the last-known copy from *any*
   deployment can still be served, marked `DATA_STALE`, which the mandate engine always sends to a
   human. A CI eval warms prod's cache and vice versa.
5. **The per-process protections stay**: the local per-minute cap (`SOUS_AIMO_CALLS_PER_MIN`),
   request coalescing, Retry-After backoff and stale-while-error all still apply on top.

## When Valkey is down

Valkey is an optimisation for a shared limit, never a dependency of a quote:

- Every Valkey operation has a 1 s socket timeout (2 s to connect). Any error is logged **once**
  (`Valkey unreachable (ConnectionError): using per-process Valio budget and cache`), and Valkey is
  skipped for 30 s before the next attempt, so a dead Valkey costs at most one timeout per 30 s.
- Meanwhile each process falls back to exactly today's behaviour: its own budget, circuit and cache.
- `GET /health` shows it: `valio_aimo.shared_budget.backend` is `local` and `ok` is `false`.
- The URL contains the password: it is never logged or returned; `/health` shows only the error type.
- With `SOUS_VALKEY_URL` unset nothing changes at all (`backend: local`, `ok: true`).

## Configure

| Variable | Default | |
|---|---|---|
| `SOUS_VALKEY_URL` | (empty) | The Aiven service URI, `rediss://default:<password>@<service>.aivencloud.com:<port>` |
| `SOUS_DEPLOYMENT_ROLE` | `dev` | `prod` on the prod server, `staging`, `ci` in CI, `demo` for demo scripts |
| `SOUS_VALIO_GLOBAL_CALLS_PER_MIN` | `50` | Shared budget per minute, all deployments together |
| `SOUS_VALIO_ROLE_SHARE` | (empty) | Override the 0.5 share of non-prod roles |

1. Create an Aiven for Valkey service (any plan; ours is in Europe, near Valio Aimo's API) and copy
   its Service URI.
2. Set `SOUS_VALKEY_URL` and `SOUS_DEPLOYMENT_ROLE` in each server's `.env`, and the GitHub secret
   `VALKEY_URL` (`gh secret set VALKEY_URL -R Zawaer/sous`); CI's `eval` and `llm-eval` jobs map it to
   `SOUS_VALKEY_URL` with role `ci`. On CRACI those jobs' deny-all egress policy allows TCP to
   `*.aivencloud.com` (Aiven ports differ per service) in addition to Valio and the model APIs.
3. Check: `curl -s https://<gateway>/health | jq .valio_aimo.shared_budget`, or
   `uv run python -m scripts.valio_budget`:

```json
{"backend": "valkey", "ok": true, "role": "prod", "used_this_minute": 12.4, "limit": 50,
 "role_limit": 50, "by_role": {"ci": 7, "prod": 5}, "denied": 0, "shared_cache_hits": 31, "configured": true}
```

## Tests

[tests/test_valkey.py](../tests/test_valkey.py), offline with `fakeredis`: two simulated deployments
share one budget; ci is capped while prod keeps its half; a 429 seen by ci pauses prod without prod
calling Valio; prod gets staging's cached offers (only the miss goes live); a stale copy written by
another deployment is served when Valio fails; a broken Valkey (and a real client on a closed port)
falls back to local limits, logs once and never leaks the URL. Also checked by hand against a local
`redis-server`.

## Submission summary (Aiven: Best Use of Aiven)

Sous lets AI buyer agents order from Valio Aimo under a mandate signed by their human. All our
deployments (prod, staging, CI evals, demos) share one Valio API key with undisclosed rate limits, and
parallel jobs got HTTP 429. **Aiven for Valkey** turns the per-process rate limiting into one
coordinated budget for the key: a sliding-window counter with prod-first priority (CI and staging can
use at most half, so they can never starve prod during judging), a circuit breaker that pauses every
deployment when any one of them is rate-limited, and a shared offer cache with stale copies that lets
staging warm prod. It fails open: if Valkey is unreachable each process falls back to its own limits,
and `/health` says so.
