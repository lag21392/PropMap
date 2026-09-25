#!/usr/bin/env bash
# Actualiza el worktree de producción (rama main) y reinicia propmap-prod.
# En desarrollo no se corre: ahí se commitea y se mergea a main.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
echo "[deploy] Directorio: $REPO_DIR"
echo "[deploy] Rama: $BRANCH"

if [ "$BRANCH" != "main" ]; then
  echo "[deploy] ERROR: este script solo corre en el worktree de producción, en main."
  echo "[deploy] En desarrollo se mergea a main y recién ahí se despliega."
  exit 1
fi

if [ ! -f "$REPO_DIR/compose.prod.yaml" ]; then
  echo "[deploy] ERROR: falta compose.prod.yaml"
  exit 1
fi

echo "[deploy] Fetching origin/main..."
git fetch origin main

LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse origin/main)"

if [ "$LOCAL" = "$REMOTE" ]; then
  echo "[deploy] Ya estás en origin/main ($(git rev-parse --short HEAD))."
else
  echo "[deploy] Actualizando a origin/main..."
  # Sin git clean: data/ es la base de producción.
  git reset --hard origin/main
  echo "[deploy] Actualizado a $(git rev-parse --short HEAD)."
fi

# El compose.yaml del repo es el de desarrollo. En esta carpeta queda el de producción.
cp "$REPO_DIR/compose.prod.yaml" "$REPO_DIR/compose.yaml"
rm -f "$REPO_DIR/compose.override.yaml"
echo "[deploy] compose.yaml de producción listo."

BUILD=()
if [ "${1:-}" = "--build" ]; then
  BUILD=(--build)
  echo "[deploy] Reconstruyendo la imagen (cambiaron requirements o el Dockerfile)."
else
  echo "[deploy] Sin build: app/ y static/ entran montados desde el repo."
fi

export COMPOSE_FILE="$REPO_DIR/compose.prod.yaml"
echo "[deploy] Levantando propmap-prod..."
docker compose up -d --no-deps "${BUILD[@]}" propmap

echo "[deploy] Esperando que quede healthy..."
status="unknown"
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16; do
  status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' propmap-prod 2>/dev/null || echo missing)"
  if [ "$status" = "healthy" ]; then
    echo "[deploy] propmap-prod healthy."
    exit 0
  fi
  sleep 3
done

echo "[deploy] ERROR: propmap-prod no quedó healthy (estado: $status)."
docker logs --tail 40 propmap-prod || true
exit 1
