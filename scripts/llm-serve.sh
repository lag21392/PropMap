#!/bin/bash
set -euo pipefail

MODEL_DIR="${MODEL_DIR:-/models}"
MODEL_FILE="${MODEL_FILE:-Qwen3.5-9B-UD-IQ2_XXS.gguf}"
MODEL_PATH="${MODEL_DIR}/${MODEL_FILE}"
MODEL_URL="${MODEL_URL:-https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/resolve/main/Qwen3.5-9B-UD-IQ2_XXS.gguf}"
ALIAS="${LLM_MODEL:-qwen35-9b-iq2xxs}"

mkdir -p "$MODEL_DIR"
if [ ! -s "$MODEL_PATH" ]; then
  echo "descargando ${MODEL_FILE} (UD-IQ2_XXS, ~3.2 GB)"
  tmp="${MODEL_PATH}.part"
  curl -L --fail --retry 8 --retry-delay 5 -C - -o "$tmp" "$MODEL_URL"
  mv "$tmp" "$MODEL_PATH"
fi

NP="${LLM_PARALLEL:-1}"
CTX="${LLM_CTX:-4096}"
CACHE_TYPE="${LLM_CACHE_TYPE:-q8_0}"
THREADS="${LLM_THREADS:-1}"

args=(
  -m "$MODEL_PATH"
  --host 0.0.0.0
  --port 8080
  -ngl 99
  -np "$NP"
  -c "$CTX"
  -t "$THREADS"
  -tb "$THREADS"
  --poll 0
  --fit off
  -ctk "$CACHE_TYPE"
  -ctv "$CACHE_TYPE"
  --alias "$ALIAS"
  --jinja
  --reasoning off
  --metrics
)

exec llama-server "${args[@]}"
