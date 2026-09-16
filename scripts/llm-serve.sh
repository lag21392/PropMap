#!/bin/bash
set -euo pipefail

MODEL_DIR="${MODEL_DIR:-/models}"
MODEL_FILE="${MODEL_FILE:-gemma-4-E4B-it-Q4_K_M.gguf}"
MODEL_PATH="${MODEL_DIR}/${MODEL_FILE}"
MODEL_URL="${MODEL_URL:-https://huggingface.co/unsloth/gemma-4-E4B-it-GGUF/resolve/main/gemma-4-E4B-it-Q4_K_M.gguf}"
ALIAS="${LLM_MODEL:-gemma4-e4b-q4}"

mkdir -p "$MODEL_DIR"
if [ ! -s "$MODEL_PATH" ]; then
  echo "descargando ${MODEL_FILE} (~4.6 GiB)"
  tmp="${MODEL_PATH}.part"
  curl -L --fail --retry 8 --retry-delay 5 -C - -o "$tmp" "$MODEL_URL"
  mv "$tmp" "$MODEL_PATH"
fi

NP="${LLM_PARALLEL:-1}"
CTX="${LLM_CTX:-2048}"
CACHE_TYPE="${LLM_CACHE_TYPE:-q8_0}"
THREADS="${LLM_THREADS:-1}"
BATCH="${LLM_BATCH:-512}"
UBATCH="${LLM_UBATCH:-256}"
FIT="${LLM_FIT:-on}"
FIT_TARGET="${LLM_FIT_TARGET:-64}"
CACHE_REUSE="${LLM_CACHE_REUSE:-256}"

args=(
  -m "$MODEL_PATH"
  --host 0.0.0.0
  --port 8080
  -ngl 99
  -np "$NP"
  -c "$CTX"
  -b "$BATCH"
  -ub "$UBATCH"
  -t "$THREADS"
  -tb "$THREADS"
  --poll 0
  --fit "$FIT"
  --fit-target "$FIT_TARGET"
  --fit-ctx "$CTX"
  -ctk "$CACHE_TYPE"
  -ctv "$CACHE_TYPE"
  --cache-prompt
  --cache-reuse "$CACHE_REUSE"
  --slot-prompt-similarity 0
  --alias "$ALIAS"
  --jinja
  --reasoning off
  --metrics
)

echo "llama-server alias=${ALIAS} ctx=${CTX} batch=${BATCH} fit=${FIT} fit-target=${FIT_TARGET}MiB"
exec llama-server "${args[@]}"
