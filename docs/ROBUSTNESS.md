# Robustness

## Monday rush: many agents, one short product (`scripts/rush.py`)

The question: 60 buyers' agents ask for the same short product at the same moment. Does Sous ever promise more
than Valio has? Before stock reservations it could: each quote is sized to Valio's stock, but N quotes made
before any order each see all of it. Now every order that goes through (or is held for a human) reserves its
quantity per (product, delivery date), and later quotes and orders see Valio's amount minus active reservations
(docs/PROTOCOL.md 5.3, `sous/reservations.py`).

**Setup** (offline, reproducible): one product, Valio butter 500 g, with a fixed Valio stock of 400 ST for one
delivery date (a fake Valio Aimo that never changes during the rush). 60 buyers ("Kitchen 01".."Kitchen 60"),
each with its own mandate and agent, each from its own client address, as with judge sandboxes. Each needs 6 to
40 ST (1,068 ST in total, 2.7x the stock); every 10th has a 5 EUR auto-approve limit, so its order waits for a
human. Each buyer has 0 to 200 ST of orders of the product in the last 4 weeks (only `pro_rata` uses it). All 60
start on one barrier and run the real gateway (FastAPI app through TestClient, `service.place_order`, SQLite on
tmpfs): quote, think 20-200 ms, order; if the order is refused with `availability.allocated` and its options
offer what's left, re-quote that once and order again.

```
uv run python -m scripts.rush            # 60 agents, both policies, with a per-buyer table
uv run pytest -q tests/test_rush.py      # the same with 20 agents: asserts no over-allocation
```

**Result** (32-thread Ryzen host, 14.11):

| | `first_come` (default) | `pro_rata` |
|---|---|---|
| Valio stock | 400 ST | 400 ST |
| Allocated (orders placed or held) | **400** | **397** (3 ST lost to rounding shares down to whole packs) |
| Over-allocated | **0** | **0** |
| Of which tentative (orders waiting for a human) | 48 | 95 |
| Buyers who got all they asked | 21 ordered + 2 held | 13 |
| Buyers who got part | 0 | 47 |
| Buyers who got nothing | 37 | 0 |
| Orders refused with `availability.allocated` | 50 | 29 |
| Agents who re-quoted the remainder from `options` | 13 | 29 |
| Quote latency p50 / p95 | 230 / 333 ms | 191 / 400 ms |
| Order latency p50 / p95 | 230 / 273 ms | 228 / 270 ms |
| Whole agent (quote, think, order, retry) p50 / p95 | 687 / 781 ms | 750 / 893 ms |
| Wall time for all 60 | 0.85 s | 0.99 s |

Who gets what varies a little from run to run with thread timing. A rerun after the ERP hooks landed (af01bc6)
gave first_come 400/400 with 39 buyers getting nothing, and pro_rata 393/400 with nobody left out, order p95
205 / 360 ms. Over-allocation was 0 in every run.

Per buyer, `pro_rata` follows the 4-week volume (excerpt of the printed table):

```
buyer       4w-vol  need   got  status            steps
Kitchen 23     200    20    20  ordered           ordered
Kitchen 37     200    30    25  ordered           refused -> ordered
Kitchen 07     120    24    14  ordered           refused -> ordered
Kitchen 44      80    30     9  ordered           refused -> ordered
Kitchen 56      40    24     5  ordered           refused -> ordered
Kitchen 01      20    20     2  ordered           refused -> ordered
Kitchen 12       0    24     1  ordered           refused -> ordered
```

Under `first_come` the first 21 orders took the stock and 37 buyers got nothing; under `pro_rata` nobody was
left out and each buyer's share tracked their usual volume. A buyer who asked for less than their share (Kitchen
23) got all of it, and the rest went to the others. About a third of the `pro_rata` orders waited for a human:
a quote sized below the need is a partial cover (`need.partial`, ask), which is what the buyer's mandate says.

**What the latency means.** One agent alone: quote 14 ms, order 5 ms. With 60 at once, p50 is about 200 ms
because one process does the work one at a time (orders under `service._order_lock`, the SQLite connection
under its lock, the Python GIL): 60 x ~18 ms is the ~1 s wall time. Correctness doesn't depend on that
serialisation across deployments: with `SOUS_VALKEY_URL` set, check-and-reserve is one Valkey WATCH/MULTI
transaction (tests/test_reservations.py runs two "deployments" with separate databases against one fakeredis).
The same rush with the SQLite file on this host's disk (`--data-dir /tmp`, while a test suite was also using the
disk) still allocated exactly 400 of 400, but at p50 4.7 s per quote: each SQLite commit costs ~27 ms of
fsync here. Production uses MariaDB.

**Pools.** The first version used one global pool. The CI eval (run 43585886793) then failed 7 cases (H03, H04,
P07, P02, O01, O02, O06): earlier cases' orders had reserved the same products' stock, so later cases saw it as
allocated. Now each judge sandbox has its own pool, each eval case runs in its own pool, and only the rush uses one
shared pool (docs/PROTOCOL.md 5.3). For the mock weekend, `SOUS_RESERVATION_RELEASE_UNCHANGED=1` ends a global
reservation once a fresh Valio reading (taken more than 60 s later) is unchanged, so curated demo orders don't hold
stock for 30 minutes.

**Limits.** Reservations know only orders placed through Sous; Valio's other channels show up when Valio's
availability refresh does (a fall in Valio's amount is taken to be our orders arriving first, so a fall caused
by another channel ends our reservations early and the next quote sees the lower amount). Offline nothing
refreshes, so reservations end by their 30-minute TTL. `pro_rata` shares are computed from the demand seen so
far: agents that order before the rest have even asked are not capped yet.
