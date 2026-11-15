# Sous on Verda

Verda's challenge asks for open-weight models run in Europe on infrastructure you control, a documented
process, and a clear reason for each Verda product used. This page is that write-up. The model numbers come
from `docs/LLM_EVAL.md`: Kimi K3.1 Pro ran in CI (job `llm-eval` on CRACI runners) against Verda Inference.

## Why Europe matters here

A purchasing agent sees what a kitchen buys, how much, when, for which diets (a hospital's milk-allergy
ward, a school's gluten-free menu) and at what price. That is commercially sensitive, and for hospitals and
schools it borders on health data. Valio Aimo is a Finnish wholesaler; its customers are Finnish kitchens.
So both halves of the system stay in the EU:

| Part | What it holds | Where it runs |
|---|---|---|
| **Gateway** (mandates, quotes, orders, hash-chained audit log, MariaDB) | every order, every mandate, the signed audit trail | Verda CPU instance, FIN-01 (Finland) |
| **Buyer agent's model** (turns a sentence into tool calls, explains the result) | the buyer's request, the catalog results, the quote | Verda Inference: open-weight model on Verda GPUs in the EU (Kimi K3.1 Pro: Germany); or our own vLLM on a Verda GPU in Finland |
| Valio Aimo API | live prices and stock | Valio's own (Finland) |

The gateway's decisions don't depend on the model at all: the mandate engine is plain code. The model only
chooses search terms, candidates, amount, unit and date, and explains the gateway's answer. That is why a
mid-sized open-weight model is enough, and why we can measure the model separately (below).

## Verda products we use, and why each

1. **CPU instance (CPU.4V.16G, FIN-01) for the gateway.** It needs no GPU: search is SQLite FTS5, the
   mandate engine is deterministic, the heavy part is waiting on Valio's API. 4 vCPU / 16 GB runs FastAPI,
   MCP and MariaDB (compose.yaml) with room to spare. Finland keeps orders and audit next to the
   wholesaler and its customers. Live: production https://sous.saarikko.dev, staging
   https://staging.sous.saarikko.dev (Caddy + Let's Encrypt; deployed by Rene's infra agent from `main`).
2. **Verda Inference (preview) for the buyer agent's model.** One OpenAI-compatible endpoint
   (`https://inference.verda.com/v1`, `Authorization: Bearer $VERDA_INFERENCE_API_KEY`) serving open-weight
   models on Verda's own GPUs in Europe. What made it the right product for buyer data, from Verda's docs:
   prompts and responses are not stored (in-memory cache only, gone within 2 hours), not used for training,
   and **not shared with the model's developer**; each model page names the country it runs in. It speaks
   the same Chat Completions + tool-calling wire format as Gemini's OpenAI endpoint, so the agent's code is
   identical for both (`agent/llm.py`, preset `verda`). Models:
   - `kimi-k3-1-pro` (Moonshot AI, open weights, 1M context, tool calling; runs in Germany). Its default
     reasoning effort is `max`; the agent sends `reasoning_effort=low`, which is plenty for
     "search -> quote -> order" and keeps latency and cost down.
   - `deepseek-4-1-flash-1017` (DeepSeek, open weights): planned as the cheaper/faster comparison point; **not run**.
3. **GPU instance + vLLM, when a buyer wants the model on infrastructure they control** (below). Same
   OpenAI-compatible API, so it plugs in with `--provider local`.

We do not use Verda's serverless containers yet; they are the scale-out path for a self-hosted model (see
Scaling).

## Process (what we did, reproducibly)

1. Gateway: `docker compose up -d --build` on the CPU instance; `.env` on the server only (Valio key,
   approver code, public URL); Caddy terminates TLS. Health: `/health`, MCP at `/mcp`.
2. Reference buyer agent (`agent/buyer.py`): the gateway's own MCP tools are listed over MCP, turned into
   function-calling schemas (agent_id, mandate_id and the idempotency key are bound by the runtime, so the
   model can't present someone else's mandate or double-order), and given to the model with the gateway's
   instructions, the mandate id, and today's date in Helsinki. Up to 12 tool calls per request.
3. Evaluation (`scripts/llm_eval.py`): the 59 cases of `evals/cases.yaml` (48 apply to an LLM; the rest test
   protocol mechanics like expired quotes, or, like H20, only make sense with the scripted plan's candidates), each in a fresh state database, live Valio prices, the
   gateway's 60-calls/min Valio cap respected. Scored against the same expectations as the scripted eval
   plus "did the agent understand the buyer". Run per model, in CI (`llm-eval` job, network egress limited
   to PyPI, Valio and the model API) or locally:

   ```bash
   VERDA_INFERENCE_API_KEY=... uv run python scripts/llm_eval.py --provider verda --model kimi-k3-1-pro
   VERDA_INFERENCE_API_KEY=... uv run python scripts/llm_eval.py --provider verda --model deepseek-4-1-flash-1017
   GEMINI_API_KEY=...          uv run python scripts/llm_eval.py --provider gemini   # comparison, non-EU
   ```

   One request against the live staging gateway:

   ```bash
   VERDA_INFERENCE_API_KEY=... uv run python -m agent.buyer --provider verda \
     --mandate M-HOTEL-AURORA --agent aurora-purchasing-agent \
     "We need 1,000 litres of lactose-free milk delivered on Monday."
   ```

## Results

From `docs/LLM_EVAL.md` (numbers as [STATUS.md](STATUS.md#evaluation) states them): all three models live in the same CI run on CRACI runners, [43587725624](https://github.com/Zawaer/sous/actions/runs/43587725624) on commit 11d8448 (Sat 14.11), 60 cases of which 48 are one buyer sentence written by the team, live Valio prices, real model calls, fresh state per case. (Nemotron 3.5 Super VL, the third, scored 30/48 with 0 violations; see STATUS.)

| | `verda:kimi-k3-1-pro` | `gemini:gemini-4-hydrogen` |
|---|---|---|
| Run | CI 43587725624, 11d8448, Sat 14.11 11:24Z | same run, 11:16Z |
| Matched the buyer's need without manual correction | 47/48 = 97.9 % | 46/48 = 95.8 % |
| Need understood (amount, unit, date, product) | 48/48 | 47/48 |
| Unsafe requests blocked or escalated | 34/34 | 34/34 |
| Mandate violations auto-ordered | 0 | 0 |
| Tool calls / Valio API calls per request | 2.92 / 1.04 | 2.96 / 1.71 |
| Tokens per request | ~27.0k | ~29.4k |
| Latency per request (p50) | 9.9 s | 14.8 s |
| Cost per request | ~$0.058 at list price (~$2.80 the run) | $0.046 ($2.21 the run) |
| Buyer data stays in the EU | yes (Germany) | no guarantee |

Same code, same run, so this is a like-for-like comparison. Kimi's one miss is **S04**, which all three models miss and which is not a real miss: the case expects a coconut cream with no allergen data to be refused, but a live agent finds one with declared allergens (468967), and the gateway held Kimi's order for a human (`product.type_mismatch`). Gemini also missed H02 (a milk drink instead of milk). Earlier runs: [43557209740](https://github.com/Zawaer/sous/actions/runs/43557209740) (0b0ad68, 49 sentences) Kimi 48/49, Gemini 46/49, both missing only H20 (now scripted-only for the same reason); [43533142451](https://github.com/Zawaer/sous/actions/runs/43533142451) (768efee, 43 sentences) 43/43 with both. Verda Inference had an outage on Fri-Sat night (empty HTTP 200s in run 43530717095, then a registrar suspension page); it has worked again since run 43533142451. No mandate violation was auto-ordered with any model: the gateway decides. `deepseek-4-1-flash-1017` has not been run.

The harness itself is validated without a model: replaying the scripted plan through the same agent runtime passes 40/40 (`evals/llm_results.oracle.json`, the 40-case set of Fri 13.11; not re-run).

## Red team on a Verda GPU (run, Sat 14.11)

The self-hosting path below was run for real for the red team ([REDTEAM.md](REDTEAM.md)): 2,000 generated Finnish and
English buyer requests through the real agent loop, `Qwen/Qwen3-30B-A3B-Instruct-2507` served by vLLM 0.11 on
1x RTX PRO 6000 Blackwell 96GB ($2.89/h; no H100 was free, two A100s never booted), Valio prices replayed from a
snapshot (0 live Valio calls). 83M tokens in 69 min (20.3k tokens/s, 29 requests/min); **976/997 unsafe requests held
or refused**, 0 allergen / limit / other-mandate violations, 3 gateway-side misses that are all order slicing (the
demo mandates had the split rule off; fixed in c71bdde: `split_window_minutes: 60` on the demo mandates and sandbox
copies, re-run with 0 gateway violations). Since 29a1cd7 the same model, behind HTTPS on that GPU, is provider `verda-selfhosted` for `/agent/ask` and
`scripts/llm_eval.py` (`SOUS_SELFHOSTED_URL` / `_KEY` / `_MODEL`); CI LLM eval with it in run 43612368619 (below). 2.35 GPU hours = $6.79. **Gemma 4 26B-A4B-it** was the intended model but
returned garbage under vLLM 0.33 on that GPU (so did Qwen on 0.33), so it **ran later on a Verda A100 80GB**: 600
requests, 83.7 % of all checks passed (Qwen 66.5 %), 289/301 unsafe held, 2 slicing violations, 17k tokens/s, $2.68
([tech-challenges/gemma.md](tech-challenges/gemma.md)). Re-run after the slicing fix: 0 gateway violations.
Recipe: `scripts/redteam_gpu.sh`, `scripts/redteam_run.py`.

**Self-hosted model as a buyer-agent provider.** The same GPU box then became our own OpenAI-compatible endpoint
(vLLM 0.11 + Qwen3-30B-A3B behind Caddy/HTTPS with a bearer key, systemd): provider `verda-selfhosted` in
`agent/llm.py` (`SOUS_SELFHOSTED_URL` / `_KEY` / `_MODEL`), selectable in `/agent/ask` and `scripts/llm_eval.py`, and
an optional model in CI's llm-eval. CI llm-eval with it (run 43612368619): 15/47 matched without correction, 30/33 unsafe held, **0 mandate violations**, ~54k tokens/request, p50 4.5 s (it often stopped at a 'block' preview instead of placing the order the gateway then refuses, and asked the buyer instead of quoting).

## Self-hosting the model on a Verda GPU (recipe)

The red team above ran this pattern (vLLM, SSH tunnel). For a buyer (say, a hospital district) whose policy is "our purchasing data never leaves a machine we
run", the same agent talks to a vLLM server on a Verda GPU instance in Finland instead of the shared
Inference API:

```bash
# On a Verda GPU instance (e.g. 1x H100 80GB, FIN-02; Ubuntu 24.04 + CUDA 12 + Docker image).
# An open-weight model with good tool calling; Qwen3-32B fits one H100 in BF16.
export LLM_API_KEY=$(openssl rand -hex 24)        # shared secret between the agent and vLLM
docker run -d --gpus all --ipc=host -p 8000:8000 \
  -v /root/hf:/root/.cache/huggingface \
  vllm/vllm-openai:latest \
  --model Qwen/Qwen3-32B --served-model-name qwen3-32b \
  --max-model-len 32768 --enable-auto-tool-choice --tool-call-parser hermes \
  --api-key "$LLM_API_KEY"
```

Expose port 8000 only on a Verda private network shared with the gateway's instance (or through an SSH
tunnel), never publicly. Then:

```bash
LLM_BASE_URL=http://<gpu-private-ip>:8000/v1 LLM_API_KEY=... \
  uv run python scripts/llm_eval.py --provider local --model qwen3-32b
```

Trade-off: a dedicated H100 costs ~$5.19/h on demand (~$2.60/h spot) whether or not anyone is buying, so it
pays off only at steady volume or under a strict data policy; for bursty ordering the per-token Inference API
is cheaper and already keeps data in the EU.

## Scaling from a demo to 20,000 kitchens

- **Gateway.** Stateless FastAPI/MCP processes behind Caddy, state in MariaDB (system-versioned audit).
  A kitchen orders a few times a day, mostly before the evening cut-off, so 20k kitchens is on the order of
  50-100k orders a day with a peak of a few orders per second. **Measured** ([CAPACITY.md](CAPACITY.md),
  `scripts/loadtest.py`, Valio replayed, 4 pinned CPUs + MariaDB): one uvicorn process saturates at ~23 req/s
  (~3 orders/s, ~7.5 quotes/s) on about one core (GIL, plus the per-process order lock). More workers need
  cross-process order locking first (MariaDB row locks or sharding by Valio customer); then two or three CPU
  instances plus a managed MariaDB, not a GPU problem. The binding limit is Valio's API: a quote costs 2 live calls (batched shortlist, cached), 4 when
  substitutes are priced for options; 1.0 per request on average in the scripted eval.
- **Model.** Tokens per request x requests per day decides the product: about 26-28k tokens per
  request in the eval (~27k for Kimi K3.1 Pro, CI 43587725624). At 75k requests/day that is about 2 billion tokens/day; per-token
  Inference API pricing scales linearly with no idle cost and per-model rate limits (requests and tokens per
  minute, raised on request). Past a steady load, the same model on Verda serverless containers (autoscaling
  GPU containers, scale to zero at night) or reserved GPU instances with vLLM becomes cheaper per token,
  with the code unchanged: only `LLM_BASE_URL` moves.
- **Safety doesn't scale with the model.** Whatever model or size runs the agent, the mandate engine
  decides every order, so a cheaper model can lower the share of requests handled without correction but
  can't make an unsafe order go through (`mandate_violations_auto_ordered` must stay 0, and the CI job fails
  if it doesn't).
