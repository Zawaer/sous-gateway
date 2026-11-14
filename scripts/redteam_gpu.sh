#!/usr/bin/env bash
# Serve an open-weight instruct model with vLLM on a rented GPU (Verda), for scripts/redteam_run.py.
#
# On the GPU box (Ubuntu + CUDA):   MODEL=Qwen/Qwen3-30B-A3B-Instruct-2507 bash redteam_gpu.sh
# From the runner (this repo):      ssh -i ~/.ssh/sous_redteam -N -L 8000:127.0.0.1:8000 root@<gpu-ip> &
#                                   uv run python scripts/redteam_run.py --base-url http://127.0.0.1:8000/v1 \
#                                       --model "$MODEL" --concurrency 64 --gpu "1x <GPU> (Verda)" --gpu-price <$/h>
#
# The server listens on 127.0.0.1 only: nothing is exposed beyond SSH. No Valio key and no repo data go to the box;
# the gateway and the replayed Valio snapshot stay with the runner. Stop/delete the instance when the run ends.
set -euo pipefail
MODEL="${MODEL:-Qwen/Qwen3-30B-A3B-Instruct-2507}"
MAX_LEN="${MAX_LEN:-32768}"
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv
if ! command -v vllm >/dev/null; then
  command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  uv venv -p 3.12 /opt/vllm && . /opt/vllm/bin/activate
  uv pip install vllm
else
  true
fi
[ -f /opt/vllm/bin/activate ] && . /opt/vllm/bin/activate
export HF_HUB_ENABLE_HF_TRANSFER="${HF_HUB_ENABLE_HF_TRANSFER:-0}"
date -u +"vLLM start %FT%TZ"
exec vllm serve "$MODEL" --host 127.0.0.1 --port 8000 --max-model-len "$MAX_LEN" \
  --gpu-memory-utilization 0.92 --max-num-seqs 128 \
  --enable-auto-tool-choice --tool-call-parser hermes ${VLLM_EXTRA:-}
