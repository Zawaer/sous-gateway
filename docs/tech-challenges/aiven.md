# Best Use of Aiven

**Problem.** Sous is Valio Aimo's gateway for AI purchasing agents: every quote asks Valio's API for live price and stock. Prod (used by judges), staging, the CI evals and demo scripts all share **one** Valio API key with undisclosed rate limits. Each process had its own cap and cache, but none saw the others: parallel CI jobs got HTTP 429 from Valio, and prod would too, mid-demo.

**What Aiven for Valkey enables:** one coordinated budget for the key, across every deployment (`sous/shared.py`, used by `sous/aimo.py`).
1. **Global sliding-window budget**: `INCR`/`EXPIRE` per minute in one `MULTI`; over the limit, the agent gets a retryable 503 with `Retry-After`.
2. **Prod first**: `SOUS_DEPLOYMENT_ROLE` = prod/staging/ci/demo/dev. Prod may use 100 % of the shared window, every other role stops at 50 %, so CI and staging can never starve prod during judging.
3. **Shared circuit breaker**: a 429 seen by any deployment sets `circuit_until`; every deployment pauses instead of turning one 429 into a storm.
4. **Shared offer cache (L2)**: prices (600 s) and availability (60 s), kept as stale copies for 15/5 min. When Valio fails, a copy fetched by *any* deployment is served, marked `DATA_STALE`, which always sends the order to a human. A CI eval warms prod's cache.

**Why Valkey, managed by Aiven.** Small, fast shared state with atomic counters and TTLs, reachable from our Verda servers, CRACI runners and laptops over TLS, with no server to run. It is coordination state only (orders and audit live in MariaDB), so it can be lost without harm.

**Fails open.** 1 s timeouts; on any error, one log line, Valkey skipped for 30 s, each process falls back to its own limits. `/health` shows `valio_aimo.shared_budget` (backend, role, usage by role). The URI is never logged.

**Tested.** `tests/test_valkey.py` (fakeredis): ci capped while prod keeps its half; a 429 seen by ci pauses prod; prod reuses staging's cache; a dead Valkey falls back and never leaks the URL. Cost: $0.06 of Aiven trial credits.

**Scales.** Any number of gateway replicas share one budget per supplier key with the same few commands.

[PENDING: not live yet. Prod `/health` (13.11 23:45Z): `backend: local, configured: false, role: dev`; set `SOUS_VALKEY_URL` + role in prod/staging `.env`. CI's TLS to Aiven blocked by a TCP-only CRACI rule; fix not pushed yet.]

Code: https://github.com/Zawaer/sous · [docs/AIVEN.md](../AIVEN.md) · [sous/shared.py](../../sous/shared.py)
