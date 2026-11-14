# Best Use of Verda

**Ambition.** Sous is Valio Aimo's front door for AI purchasing agents: signed quotes, orders under a mandate signed by the buyer, enforced by plain code. With a Verda GPU we red-teamed it at a scale no API budget allows: **2,000 generated fi/en buyer requests** (injection, slicing, fake approvals, allergens, other buyers' mandates...) through the real agent loop, an open-weight model on one GPU, Valio prices replayed so the shared Valio key saw 0 calls.

**Fit**
- **GPU instance** (1x RTX PRO 6000 96GB, $2.89/h): vLLM serving `Qwen3-30B-A3B-Instruct-2507`, 83M tokens in 69 min (20k tokens/s). Prompts never left a machine we ran.
- **CPU instances** (FIN-01): gateway prod + staging, FastAPI + MCP + MariaDB, ~$0.048/h each.
- **Self-hosted endpoint**: the same Qwen behind HTTPS is provider `verda-selfhosted` for `/agent/ask` and the LLM eval (29a1cd7).
- **Verda Inference**: `kimi-k3-1-pro` (open weights, EU) as the everyday buyer model, same code (`agent/llm.py`).

**Working prototype + process**
1. Gateway live on the CPU instance ([STATUS](../STATUS.md)).
2. CI eval, 48 sentences, live Valio: Kimi 47/48, 34/34 unsafe held, 0 violations.
3. Red team ([REDTEAM.md](../REDTEAM.md)): snapshot from 2 Valio calls -> 4,000 requests (13 categories) -> oracle harness check (2,036/2,036 unsafe held) -> GPU run: **976/997 unsafe held or refused; 0 allergen / limit / other-mandate violations**; 3 gateway-side misses, all order slicing (the demo mandates had the split rule off); fix (`split_window_minutes` on) in progress.
4. Honest log: no H100 was free; two A100s never booted ($1.30); Gemma 4 gave garbage under vLLM 0.33 on this GPU, so Qwen ran. GPU total $8.09 ([COSTS.md](../COSTS.md)).

**Path to scale.** 20k kitchens is ~50-100k orders/day: a few CPU instances + managed MariaDB. The model is the variable cost (~40k tokens/request). Measured: one GPU at $2.89/h did 29 agent requests/min, i.e. ~$0.0017/request, ~30x cheaper than list-price APIs at steady load; bursty load stays on per-token Inference. Swap is one env var (`LLM_BASE_URL`). Gateway measured ([CAPACITY.md](../CAPACITY.md)): one 4-vCPU process ~23 req/s, ~3 orders/s; Valio's API budget binds first. Every model change is re-run through the same red team; safety does not depend on the model.

Code: https://github.com/Zawaer/sous · [VERDA.md](../VERDA.md) · [REDTEAM.md](../REDTEAM.md)
