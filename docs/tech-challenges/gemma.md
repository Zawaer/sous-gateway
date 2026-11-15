# Best Use of Gemma: Sous

**What Gemma does.** Gemma 4 is the open-weight buyer agent in our red team ([REDTEAM.md](../REDTEAM.md)): it plays hundreds of Finnish and English kitchen buyers, normal and adversarial (prompt injection, "split it so nobody approves", fake approvals, other buyers' mandates, allergens), against the real Sous gateway, self-hosted on a Verda GPU in Finland. No prompt left our machine; Valio prices were replayed from a snapshot (0 calls to the shared Valio API).

**Run (Sat 14.11).** `google/gemma-4-26B-A4B-it` (bf16, MoE 4B active), vLLM 0.33 (`--tool-call-parser gemma4 --reasoning-parser gemma4`), 1x A100 80GB (Verda FIN-02, $2.39/h), the unchanged `agent/buyer.py` loop and MCP tools, 64 requests in flight.

| 600 generated requests | Gemma 4 26B-A4B | Qwen3-30B-A3B (same harness, 2,000) |
|---|---|---|
| All checks passed (amount, unit, date, product, one order...) | **83.7 %** | 66.5 % |
| Unsafe requests held or refused | 289/301 = 96.0 % | 97.9 % |
| Gateway-side violations auto-ordered | 2 (both order slicing) | 3 (slicing) |
| Agent-side violations | 10 | 19 |
| Type traps / normal orders passed | 89.5 % / 96.7 % | 38.8 % / 82.4 % |
| Tokens, throughput | 29.5M in, 0.25M out; 17k tokens/s, 20.6 requests/min | 20k tokens/s, 29/min |

**What Gemma did well.** Understood buyers best of our open models: right product type far more often (lactose-free milk, not coffee milk), all allergen needs kept, never tried a self-approval tool.

**What it did badly.** (1) Asked to split an order "so nobody approves", it ordered one slice, like Qwen. (2) It misread Finnish thousands spacing twice ("1 539 litraa" -> 539 l; "1 101 kiloa" -> 101 kg), shrinking over-limit orders under the limit.

**How we improved the outcome.** Gemma's slicing exposed the gateway gap: the demo mandates had the split-order rule off. Fixed on main (`split_window_minutes: 60`, ad366ce); re-running Gemma on 100 slicing requests against the fixed gateway: **0 gateway violations** (was 2 in 29), the split rule fired. The remaining agent-side issues (one slice, Finnish digit grouping) are prompt work for the buyer agent.

**Honest notes.** The first attempt, on an RTX PRO 6000 Blackwell (confidential-compute mode), produced garbage under vLLM 0.33 (every model), so the 2,000-request pass ran on Qwen and Gemma ran on an A100 later. GPU for the Gemma run: 1.12 h = $2.68.

Code: https://github.com/Zawaer/sous-gateway · `scripts/redteam_gpu.sh` · [REDTEAM.md](../REDTEAM.md)
