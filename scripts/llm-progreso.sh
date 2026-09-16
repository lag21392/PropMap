#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: falta python3 en el host." >&2
  echo "  ./scripts/llm-progreso.sh --city puerto-madryn" >&2
  exit 1
fi

export DATA_DIR="${DATA_DIR:-$ROOT/data}"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  python3 -m app.llm_progress --help
  exit 0
fi

exec python3 -m app.llm_progress "$@"
