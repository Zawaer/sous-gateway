# Best Use of Aiven

**Problem.** Sous is Valio Aimo's gateway for AI purchasing agents: every quote asks Valio's API for live price and stock. Prod, staging, the CI evals and demo scripts share **one** Valio API key with undisclosed rate limits. Each process had its own cap and cache, but none saw the others: parallel CI jobs got HTTP 429 from Valio.

**Meaningful use: Aiven for Valkey holds one budget for the key, across every deployment** (`sous/shared.py`, used by `sous/aimo.py`).
1. **Global sliding-window budget**: `INCR`/`EXPIRE` per minute in one `MULTI`; over the limit, the agent gets a retryable 503 with `Retry-After`.
2. **Prod first**: `SOUS_DEPLOYMENT_ROLE` = prod/staging/ci/demo/dev. Prod may use 100 % of the window, every other role 50 %.
3. **Shared circuit breaker**: a 429 seen by any deployment pauses all of them.
4. **Shared offer cache (L2)**: prices 600 s, availability 60 s, stale copies kept 15/5 min. When Valio fails, a copy fetched by *any* deployment is served marked `DATA_STALE`, which always sends the order to a human.

Valkey holds coordination state only (orders and audit are in MariaDB), so losing it is harmless.

**Implementation.** Fails open: 1 s timeouts; on error one log line, Valkey skipped for 30 s, each process falls back to its own limits. `/health` shows `valio_aimo.shared_budget`; the URI is never logged. `tests/test_valkey.py` (fakeredis): ci capped while prod keeps its half; a 429 seen by ci pauses prod; prod reuses staging's cache; a dead Valkey falls back without leaking the URL. CRACI's deny-by-default egress allows only TLS to `*.aivencloud.com:23770`.

**Where it is live.** CI's `eval` and `llm-eval` jobs use it: run 43530484114 logs `backend: valkey, role: ci, role_limit: 25`. **Prod and staging do not yet** (`/health`: `backend: local, configured: false`); setting `SOUS_VALKEY_URL` there is the next step. So "prod-first" is tested, not yet exercised in production.

**Impact.** Not measured as a before/after 429 count. What it changes: CI evals can no longer starve the judges' prod gateway once prod is connected, and one 429 no longer becomes a storm from several deployments. Cost: about $0.08 of Aiven trial credits so far.

**Creativity.** A managed key-value store used as a shared rate-limit, circuit breaker and cache for a third-party API key, with priority by deployment role.

Code: https://github.com/Zawaer/sous · [docs/AIVEN.md](../AIVEN.md) · [sous/shared.py](../../sous/shared.py)
