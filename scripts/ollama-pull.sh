#!/bin/sh
set -eu
HOST="${OLLAMA_HOST:-http://propmap-llm:11434}"
MODEL="${OLLAMA_MODEL:-gemma4:e2b}"
export OLLAMA_HOST="$HOST"
i=1
while [ "$i" -le 40 ]; do
  if ollama pull "$MODEL"; then
    exit 0
  fi
  i=$((i + 1))
  sleep 6
done
exit 1
