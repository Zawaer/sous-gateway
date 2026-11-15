# Best Use of Verda

**Ambition.** Sous is Valio Aimo's front door for AI purchasing agents: signed quotes, orders under a mandate signed by the buyer, enforced by plain code. With a Verda GPU we red-teamed it at a scale no API budget allows: **2,000 generated fi/en buyer requests** through the real agent loop, an open-weight model on one GPU, Valio prices replayed (0 Valio calls).

**Fit**
- **GPU instance** (RTX PRO 6000 96GB, $2.89/h): vLLM, `Qwen3-30B-A3B-Instruct-2507`, 83M tokens in 69 min, prompts on our GPU.
- **CPU instances** (FIN-01): gateway prod + staging, FastAPI + MCP + MariaDB, ~$0.048/h each.
- **Self-hosted endpoint**: the same Qwen behind HTTPS = provider `verda-selfhosted` in /agent and the LLM eval; CI eval: 30/33 unsafe held, 0 violations, 15/47 matched.
- **Verda Inference**: `kimi-k3-1-pro` (open weights, EU), the everyday buyer model (`agent/llm.py`).
- **Nemotron on Verda** (with NVIDIA): reads photos of orders; 0 silent errors, 5.5 s/page ([nemotron.md](nemotron.md)).

**Working prototype + process**
1. Gateway live on the CPU instance ([STATUS](../STATUS.md)).
2. CI eval, 48 sentences, live Valio: Kimi 47/48, 34/34 unsafe held, 0 violations.
3. Red team ([REDTEAM.md](../REDTEAM.md)): snapshot from 2 Valio calls -> 4,000 requests (13 categories) -> oracle harness check (2,036/2,036 unsafe held) -> GPU run: **976/997 unsafe held or refused; 0 allergen / limit / other-mandate violations**; 3 gateway-side misses, all order slicing (split rule was off on the demo mandates; fixed, c71bdde).
4. **Gemma 4 26B-A4B** on a Verda A100: 600 requests, 83.7 % all checks (Qwen 66.5 %), 289/301 unsafe held; after the slicing fix 0 gateway violations ([gemma.md](gemma.md)).
5. Each GPU had a hard stop, deleted after its run; each run's cost is in a ledger. Two A100s never booted; vLLM 0.33 failed ([COSTS.md](../COSTS.md)).

**Path to scale.** 20k kitchens ~ 50-100k orders/day: a few CPU instances + MariaDB. The model is the variable cost (~40k tokens/request). Measured: one GPU at $2.89/h did 29 agent requests/min (~$0.0017/request, ~30x below list-price APIs at steady load); bursty load stays on per-token Inference. Swap is one env var (`LLM_BASE_URL`). Gateway measured ([CAPACITY.md](../CAPACITY.md)): one 4-vCPU process ~23 req/s, ~3 orders/s; Valio's API budget binds first. Model changes re-run the same red team; safety does not depend on the model.

Code: https://github.com/Zawaer/sous · [VERDA.md](../VERDA.md) · [REDTEAM.md](../REDTEAM.md)
