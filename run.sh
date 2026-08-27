#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
  echo "Error: Docker no está instalado o no está en el PATH." >&2
  echo "  Instalalo (Linux): https://docs.docker.com/engine/install/" >&2
  echo "  O corré la app sin contenedor:" >&2
  echo "  python3 -m pip install -r requirements.txt" >&2
  echo "  python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000" >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "Error: falta el plugin Docker Compose v2." >&2
  echo "  docker compose up --build" >&2
  exit 1
fi

export HOST_UID="$(id -u)"
export HOST_GID="$(id -g)"

echo "PropMap en http://127.0.0.1:8000"
exec docker compose up --build "$@"
