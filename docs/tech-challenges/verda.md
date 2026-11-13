# Best Use of Verda

**Idea.** Sous is Valio Aimo's front door for AI purchasing agents: an agent gets a signed quote with live price and stock and orders under a mandate signed by its human. What a kitchen buys (a hospital's milk-allergy ward) is sensitive, so both halves run in the EU on Verda, and the agent's brain is an open-weight model we can measure and swap.

**Verda products, and why each**
- **CPU instance (CPU.4V.16G, FIN-01, Finland)** for the gateway: FastAPI + MCP + MariaDB + Caddy (`compose.yaml`). No GPU needed: search is SQLite FTS5 and the mandate engine is plain code. Prod https://135-181-8-209.sslip.io plus staging, $0.048/h each.
- **Verda Inference** for the buyer agent's model: `kimi-k3-1-pro` (open weights, runs in Germany) behind one OpenAI-compatible endpoint with tool calling. Prompts are not stored, not used for training and not shared with the model's developer. Same code as for Gemini; only the base URL changes (`agent/llm.py`, preset `verda`).

**Path from idea to prototype**
1. Gateway deployed on the CPU instance with `docker compose up` (Friday evening).
2. LLM buyer agent (`agent/buyer.py`) gets the gateway's own MCP tools; the runtime binds identity, mandate and idempotency key, so the model can't swap them.
3. Eval in CI on CRACI (`scripts/llm_eval.py`, 40 cases, live Valio prices). Run 43511463514: **36/40 matched without correction, 38/40 need understood, 31/31 unsafe requests blocked or escalated, 0 mandate violations, p50 8.4 s, ~21.7k tokens/request**. Later run 43524836008: 34/40, still 0 violations. Kimi also turned 7/7 menus into schema-valid JSON.
4. Spend so far: Verda Inference $3.88 (4 eval runs), CPU instances ~$0.40 ([COSTS.md](../COSTS.md)).

**Scaling to real users.** 20,000 kitchens is roughly 50-100k orders/day: a few CPU instances plus managed MariaDB, not a GPU problem. At ~22k tokens/request the model is the variable cost: per-token Inference has no idle cost, and past a steady load the same open-weight model runs on a Verda GPU with vLLM (recipe in VERDA.md), with only `LLM_BASE_URL` changed. Safety does not depend on the model: the mandate engine decides every order, and CI fails if any model gets a mandate violation auto-ordered.

[PENDING: self-hosted vLLM on a Verda GPU is documented, not yet run. `deepseek-4-1-flash-1017` not yet evaluated.]

Code: https://github.com/Zawaer/sous · Full write-up: [docs/VERDA.md](../VERDA.md) · Results: [docs/LLM_EVAL.md](../LLM_EVAL.md)
