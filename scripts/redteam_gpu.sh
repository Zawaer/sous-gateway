#!/usr/bin/env bash
# Serve an open-weight instruct model with vLLM on a rented GPU (Verda), for scripts/redteam_run.py.
#
# On the GPU box (Ubuntu + CUDA + Docker):  bash redteam_gpu.sh            # Gemma 4 26B-A4B-it, bf16
#   Qwen instead:  MODEL=Qwen/Qwen3-30B-A3B-Instruct-2507 TOOL_PARSER=hermes REASONING= bash redteam_gpu.sh
# From the runner (this repo):
#   ssh -i <key> -N -L 8000:127.0.0.1:8000 root@<gpu-ip> &
#   uv run python scripts/redteam_run.py --base-url http://127.0.0.1:8000/v1 --model "$MODEL" \
#       --concurrency 48 --gpu "1x RTX PRO 6000 96GB (Verda)" --gpu-price 2.89
#
# vLLM listens on 127.0.0.1 only: nothing is exposed beyond SSH. No Valio key, no repo data and no buyer data go to
# the box; the gateway and the replayed Valio snapshot stay with the runner. Stop/delete the instance after.
set -euo pipefail
MODEL="${MODEL:-google/gemma-4-26B-A4B-it}"
TOOL_PARSER="${TOOL_PARSER:-gemma4}"
REASONING="${REASONING---reasoning-parser gemma4}"  # Gemma 4 emits an (empty) thought channel; keep it out of content
MAX_LEN="${MAX_LEN:-32768}"
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv
mkdir -p /root/hf
docker rm -f vllm >/dev/null 2>&1 || true
date -u +"vLLM start %FT%TZ"
# shellcheck disable=SC2086
docker run -d --name vllm --gpus all --ipc=host --network host \
  -v /root/hf:/root/.cache/huggingface \
  vllm/vllm-openai:${VLLM_TAG:-latest} \
  --model "$MODEL" --host 127.0.0.1 --port 8000 --max-model-len "$MAX_LEN" \
  --gpu-memory-utilization 0.92 --max-num-seqs "${MAX_SEQS:-96}" \
  --enable-auto-tool-choice --tool-call-parser "$TOOL_PARSER" $REASONING --enable-prefix-caching ${VLLM_EXTRA:-}
until curl -sf http://127.0.0.1:8000/v1/models >/dev/null; do
  docker ps -q -f name=vllm | grep -q . || { docker logs --tail 50 vllm; exit 1; }
  sleep 10
done
date -u +"vLLM ready %FT%TZ"
curl -s http://127.0.0.1:8000/v1/models
