# Capacity: how much one gateway process carries

Measured, not estimated: `scripts/loadtest.py`, Sat 14.11.2026. Everything ran on one machine with Valio
replayed; prod, staging and Valio Aimo were not touched.

## Setup

- **Hardware.** The gateway and its MariaDB were pinned with `taskset` to 4 logical CPUs (0-3) of a shared
  32-thread Ryzen 9 7945HX (other sessions were running; load average ~4.5). This stands in for prod's Verda
  CPU.4V.16G (4 vCPU, 16 GB). The load generator ran on 2 other CPUs.
- **Process.** As in the Dockerfile: one uvicorn worker running `sous.api:app`.
- **State.** MariaDB 10.11 (prod runs 11.4) on the same 4 CPUs, durable commits
  (`innodb_flush_log_at_trx_commit=1`), fresh database each run. Docker is not available in this container, so
  MariaDB came from apt, not compose. SQLite (the dev default) was smoke-tested too; see below.
- **Valio.** `--valio replay` serves every price and availability lookup from
  `evals/redteam/valio_snapshot.json` (`sous/replay.py`): 645 recorded products, synthetic offers for the rest.
  The business clock is frozen at Sat 14.11 12:00 so the snapshot's cut-offs hold, and delivery is Mon 16.11.
  `--valio cached` puts the same snapshot behind the real client (`sous/aimo.py`: cache, in-flight
  de-duplication, budget). That mode counts how many Valio calls the traffic would cost.
- **Traffic.** Closed loop. Each concurrent client is one buyer's agent with its own mandate (M-LOAD-NN,
  auto-approve 2,000 EUR). The mix: 50 % search (`GET /products`, 85 fi/en/sv kitchen phrases, sometimes with an
  allergen, claim or temperature filter), 30 % quote (1-3 items from the agent's own recent results, 1-3
  candidates each), 15 % order (the agent's latest quote, with a fresh idempotency key) and 5 % reads (order,
  quote or product by id). Concurrency steps through 1, 2, 4, 8, 16, 32 and 64, 20 s per step.
- **Rate limit.** The per-IP POST limit was raised for the test, because every client comes from 127.0.0.1.

```
uv run python -m scripts.loadtest --data-dir <dir with Valio's products.json> \
    --db-url mysql://load:load@127.0.0.1:33061/sous_load --out results.json
```

## Results (MariaDB, Valio replayed, after the fix below)

| agents | req/s | gateway CPU | search p50 / p95 / p99 ms | quote p50 / p95 / p99 ms | order p50 / p95 / p99 ms | orders/s | errors |
|---:|---:|---:|---|---|---|---:|---:|
| 1 | 18.0 | 0.55 cores | 23 / 113 / 150 | 59 / 179 / 269 | 79 / 151 / 221 | 2.8 | 0 |
| 2 | 18.7 | 0.76 | 34 / 158 / 259 | 121 / 551 / 716 | 123 / 232 / 295 | 2.9 | 0 |
| 4 | **22.7** | 0.86 | 31 / 195 / 262 | 241 / 655 / 1,751 | 276 / 503 / 665 | 3.1 | 0 |
| 8 | 21.2 | 0.81 | 49 / 215 / 364 | 352 / 1,635 / 3,180 | 918 / 1,721 / 1,901 | 3.3 | 0 |
| 16 | 23.5 | 0.93 | 70 / 280 / 557 | 556 / 2,228 / 5,110 | 3,019 / 3,777 / 4,080 | 2.8 | 0 |
| 32 | 23.7 | 0.89 | 104 / 496 / 1,133 | 909 / 3,318 / 4,508 | 7,029 / 8,442 / 8,720 | 3.1 | 0 |
| 64 | 23.4 | 0.90 | 661 / 2,270 / 2,807 | 2,755 / 6,829 / 8,725 | 9,169 / 11,049 / 11,370 | 2.8 | 0 |

Reads (by id) took 1.6-23 ms up to 32 agents. There were no 5xx responses and no transport errors at any step.
About 25 % of orders came back `refused` and ~10 % `pending_approval`. Those are mandate decisions, not errors:
the mandate's 0.9 delivery-reliability floor, unknown allergen data, and stock already reserved by earlier orders.

**Where it saturates.** Throughput tops out at about **23 requests/s with 4 agents** and stays flat to 64;
past that point only latency grows. The gateway process never used more than about one core of the four. In
the same mix that is roughly 11 searches, 7.5 quotes and **~3 orders per second**, or ~10,000 orders an hour.

**Before the fix** (commit ede8518, same run): 20.2-21.3 req/s at the plateau. Search p50 at 1 agent was 32
ms (23 after) and at 16 agents 92 ms (70 after). The order ceiling did not move.

**SQLite** (the dev default, file on this host's disk) is far slower: 6.5 req/s with 1 agent and 9.1 with 8.
Every statement is its own transaction, and an fsync here costs ~19 ms. Use MariaDB for any real load.

## Throughput vs CPUs on a 32-vCPU CRACI runner

The `redteam-scale` CI job (by hand: `gh workflow run ci.yml -f redteam_scale=true`; `scripts/craci_scale.py`) reran
this test on a dedicated CRACI VM (`size: 32`, NVMe), Valio replayed, MariaDB 11.4 as a service container. For
each CPU count the gateway was pinned with `taskset` and MariaDB with `docker update --cpuset-cpus` to the same
CPUs; the load generator had the last 2 vCPUs. Stages of 15 s. Run
43611589764 (commit 84e7826), egress proved by its
`craci-evidence` job (PyPI and Docker Hub only).

| gateway + DB CPUs | 1 agent | 4 agents | 16 agents | 64 agents | peak orders/s | gateway CPU at peak | errors |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 20.1 req/s | 21.9 | **28.4** | 27.4 | 3.9 | 1.25 cores | 0 |
| 8 | 20.6 | 19.6 | 24.8 | **28.9** | 3.3 | 1.45 | 0 |
| 16 | 20.7 | 21.0 | **24.8** | 23.8 | 3.6 | 1.30 | 0 |
| 30 | 19.1 | **23.8** | 23.4 | 21.3 | 3.7 | 1.09 | 0 |

**One process does not get faster with more CPUs**: 24-29 req/s and 3-4 orders/s from 4 to 30 CPUs, at about one
core (the extra 0.1-0.6 cores is uvicorn's I/O and MariaDB driver threads). This confirms the GIL + order-lock
diagnosis below on clean hardware, and the dedicated VM is ~20 % above the shared dev host (23.5 req/s). At 64
agents p95 is 7-8 s for every CPU count. More throughput needs more processes, sharded by buyer (scale-out plan,
step 2). The same job shows what processes do: the scripted red team, one process per CPU, ran **2,433 requests/min
on 4 CPUs, 5,104 on 8, 9,576 on 16 and 15,540 on 32** (all 4,000 requests in 15.4 s; docs/REDTEAM.md). That is
~600 per CPU up to 16 and ~490 per CPU at 32, where nothing is left for the OS.

## The bottleneck

py-spy ran on the server at 8 agents (`--profile`, 25 s). It shows three things:

1. **One core: Python and the GIL.** Search, quote and order handlers run in FastAPI's thread pool in one
   process, so only one of them executes Python at a time. That is why CPU stops at ~1 core while 3 sit idle.
   Profile share: search 47 % (FTS, `json.loads` of up to 2,000 candidates, then `_relevance` and type rerank in
   Python), quote 29 % (options and substitutes, product-type classification, reservations), order 15 %.
2. **The order lock.** `service._order_lock` serialises every order in the process, which is what keeps
   check-and-reserve and idempotency correct. Inside the lock the order competes with searches and quotes for the
   GIL, so one order holds the lock for ~330 ms under load (79 ms alone). The result is a ceiling of ~3 orders/s
   no matter how many agents there are. The queue for that lock is the multi-second order p95 above 8 agents.
3. **The store lock.** `store._lock` puts all SQL on one shared connection. It is 14 % of samples, mostly
   waiting. With MariaDB it costs a round trip per statement and only shows when commits fsync.

## What was fixed (commit "Search and substitutes hot paths")

Both changes produce the same output as before; `tests/test_capacity_perf.py` compares them with the old code.

- `catalog._term_score` built three regexes per (term, name) pair through `re`'s 512-entry cache, which
  thrashed. The patterns are now compiled once per term, and names that don't contain the term are skipped
  before any regex runs. In-process, search on 10 broad queries went from 74 to 51 ms with identical results.
- `substitutes._same_category` ran `json_extract` over all 17.5k product rows on every substitute search (30
  ms). It now uses a category -> rows index built once per catalog file (0.5 ms).

**Next candidate, not done.** `usual.last_comparison_prices` runs inside the order lock. It reads up to 50 past
orders and one stored quote for each (an N+1), so its cost grows with each buyer's history. Reading only the
quote lines it needs, in one query, would shorten the time each order holds the lock.

## Valio's rate limit is the real ceiling

With `--valio cached`, 16 agents for 60 s made 434 quotes and 198 orders, which cost **378 Valio calls** (187
price, 191 availability, 1,249 product ids). The cache answered 9,085 product lookups, an **88 % item hit
rate** (price TTL 600 s, availability 60 s). That works out to **~0.85 Valio calls per quote** (the calls are
counted against quotes only, an upper bound).

The rate is optimistic. The run used about a thousand products named by 85 phrases, while real buyers spread
across more of the 17.5k catalog. The scripted eval saw 1.0 call per request (`evals/results.json`).

Our default budget is `SOUS_AIMO_CALLS_PER_MIN=60`, the self-imposed guard for the shared hackathon key. At
0.85-1.0 calls per quote that allows **~60-70 quotes/min, ~1.1 quotes/s**. One gateway process can serve
about 7 quotes/s, so **Valio's API budget runs out about 6x before our CPU does**. Raising throughput therefore
starts with Valio, not with more gateway processes:

- An agreed call budget per gateway from Valio. At X calls/min, expect about X / 0.9 quotes/min at this hit
  rate.
- Larger batches. One call already covers up to 1,000 ids, so prefetching a buyer's usual products once a
  morning costs about 2 calls.
- A shared cache across deployments (`SOUS_VALKEY_URL` already shares offers and the global budget,
  `sous/shared.py`).

## Scale-out plan, with the honest constraints

1. **Up to ~3 orders/s at peak (about 20k kitchens).** One process on CPU.4V.16G with managed MariaDB carries
   it, and the remaining limit is Valio's budget. 50-100k orders/day concentrated before the evening cut-off is a
   few orders per second at peak, so this is the edge of one process, not comfortably inside it.
2. **Using all 4 vCPUs (more uvicorn workers or more instances).** It is not safe to just add workers today.
   `_order_lock` is per process, so two workers could reserve the same stock or replay one idempotency key.
   Stock already has a cross-process path: with `SOUS_VALKEY_URL`, check-and-reserve is one Valkey WATCH/MULTI
   transaction (tests/test_reservations.py runs two deployments against one store). Before running several
   workers, also make order placement safe across processes, by either:
   - **taking a MariaDB row lock** (`SELECT ... FOR UPDATE` on the mandate row) for spend, split and idempotency
     checks, with idempotency enforced by the existing unique index; or
   - **sharding by Valio customer.** A buyer's orders always go to the same worker or instance (consistent
     hash on customer or mandate at Caddy), so a per-process lock stays correct per buyer.

   Then N processes give about N x 3 orders/s and N x 23 req/s, up to the database and Valio.
3. **Search** is static and read-only. It scales with processes freely; a per-process LRU of parsed product
   rows, or a search replica, would halve its CPU.
4. **The store connection.** One connection behind `store._lock` per process is fine at these rates. A pool
   (and moving the audit hash chain to a single-writer queue) would be needed past ~10 processes.

**What a real Valio deployment needs.** A call budget and per-customer prices from Valio (not the mock's
`customer=123`); managed MariaDB with backups; at least two gateway instances for availability, sharded by
customer as in step 2, with Valkey shared; the ERP adapter talking to SAP instead of the stub
(docs/INTEGRATION.md); and this load test re-run on the actual Verda instance. These numbers come from 4 pinned
CPUs on a shared, faster host, so treat them as an order of magnitude, not a guarantee.
