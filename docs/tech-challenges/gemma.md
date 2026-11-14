# Best Use of Gemma: Sous

_Status: **Gemma did not run.** We tried; this file says exactly what happened. Nothing below claims a Gemma result._

**What Gemma was for.** The open-weight buyer agent in our large-scale red team ([REDTEAM.md](../REDTEAM.md)): thousands of Finnish and English kitchen buyers, normal and adversarial, against the real gateway, on a Verda GPU, with Valio prices replayed from a snapshot.

**What we did (Sat 14.11, Verda `sous-redteam-gpu3`, 1x RTX PRO 6000 Blackwell 96GB, confidential-compute variant)**
- Model: `google/gemma-4-26B-A4B-it` (bf16, 52 GB). Only vLLM 0.33 supports Gemma 4 (tool parser `gemma4`, reasoning parser `gemma4`), so we used it.
- Every prompt came back as garbage tokens ("What is 2+2?" -> random multilingual tokens), with each MoE backend (FlashInfer CUTLASS, Triton) and attention backend (Triton, FlexAttention, FlashInfer), eager or compiled. A 40-request smoke run: 0 quotes, 0 orders, nothing unsafe (the gateway was never asked for anything).
- Control: `Qwen3-30B-A3B` on the same vLLM 0.33 was garbage too; plain PyTorch on the GPU was correct; Qwen on vLLM 0.11 was correct. So the fault is vLLM 0.33 on this Blackwell CC GPU, not Gemma. vLLM 0.11 predates Gemma 4.
- With the GPU's hard stop and Rene's $10 cap, the 2,000-request pass ran on Qwen instead (976/997 unsafe held; REDTEAM.md). GPU time spent on the Gemma attempts: ~40 min (~$1.90).

**What a Gemma pass needs.** An H100/A100 (where vLLM 0.33 is well tested), then one command: `bash scripts/redteam_gpu.sh` (Gemma 4 is its default) and `scripts/redteam_run.py --model google/gemma-4-26B-A4B-it --sample 2000`. The harness, scoring and report are model-agnostic and already ran end to end.

Code: https://github.com/Zawaer/sous · [REDTEAM.md](../REDTEAM.md)
