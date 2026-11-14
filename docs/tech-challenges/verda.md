# Best Use of Verda

**Ambition.** Sous is Valio Aimo's front door for AI purchasing agents: an agent gets a signed quote with live price and stock and orders under a mandate signed by its human; plain code enforces it. What a kitchen buys (a hospital's milk-allergy ward) is sensitive, so the gateway and the agent's model both run in the EU on Verda, and the model is open-weight, measured and swappable.

**Fit: Verda products, and why each**
- **CPU instance (CPU.4V.16G, FIN-01, Finland)** for the gateway: FastAPI + MCP + MariaDB + Caddy (`compose.yaml`). No GPU needed: search is SQLite FTS5, the mandate engine is plain code. Prod https://135-181-8-209.sslip.io and staging, ~$0.048/h each.
- **Verda Inference** for the buyer agent's model: `kimi-k3-1-pro` (open weights, runs in Germany), OpenAI-compatible with tool calling; prompts not stored or used for training. Same code as Gemini; only the base URL changes (`agent/llm.py`, preset `verda`).

**Working prototype**
1. Gateway deployed with `docker compose up` on the CPU instance (prod runs an older commit than `main`; [STATUS](../STATUS.md#deployed-now)).
2. LLM buyer agent (`agent/buyer.py`) on the gateway's own MCP tools; the runtime binds identity, mandate and idempotency key.
3. Eval in CI on CRACI, 40 buyer sentences, live Valio prices. Kimi, run 43511463514: **36/40 matched without correction, 31/31 unsafe requests blocked or escalated, 0 mandate violations, p50 8.4 s, ~21.7k tokens/request**. Later run 43524836008 (older code): 34/40, 0 violations. Not re-measured on current code: Verda Inference returned empty responses, then its API went offline (Sat ~00:15Z). Kimi also turned 7/7 menus into schema-valid plans.
4. Spend: Verda Inference $3.88, CPU instances ~$0.46 ([COSTS.md](../COSTS.md)).

**Path to scale (estimates, no load test run).** 20,000 kitchens is roughly 50-100k orders/day: a few CPU instances plus managed MariaDB. The model is the variable cost at ~22k tokens/request; per-token Inference has no idle cost, and past a steady load the same open-weight model can run on a Verda GPU with vLLM, changing only `LLM_BASE_URL`. The vLLM recipe is in VERDA.md; **we have not run it**. Safety does not depend on the model: CI fails if any model gets a mandate violation auto-ordered.

Not done: self-hosted vLLM on a Verda GPU, `deepseek-4-1-flash-1017`.

Code: https://github.com/Zawaer/sous · [docs/VERDA.md](../VERDA.md) · [docs/LLM_EVAL.md](../LLM_EVAL.md)
