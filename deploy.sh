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
healthy=0
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16; do
  status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' propmap-prod 2>/dev/null || echo missing)"
  if [ "$status" = "healthy" ]; then
    echo "[deploy] propmap-prod healthy."
    healthy=1
    break
  fi
  sleep 3
done

if [ "$healthy" != "1" ]; then
  echo "[deploy] ERROR: propmap-prod no quedó healthy (estado: $status)."
  docker logs --tail 40 propmap-prod || true
  exit 1
fi

# Nginx Proxy Manager cachea /static/*.js y *.css e ignora el Cache-Control
# del origen. Si no se vacía antes, Cloudflare vuelve a guardar el archivo viejo.
echo "[deploy] Vaciando la caché de assets del proxy..."
docker exec nginx-proxy-manager sh -c 'find /var/lib/nginx/cache/public -type f -delete'
docker exec nginx-proxy-manager nginx -s reload

env_get() {
  local line
  line="$(grep -E "^${1}=" "$REPO_DIR/.env" | tail -n 1 || true)"
  line="${line#*=}"
  line="${line%\"}"
  line="${line#\"}"
  line="${line%\'}"
  line="${line#\'}"
  printf '%s' "$line"
}

CF_TOKEN="$(env_get CLOUDFLARE_API_TOKEN)"
CF_ZONE="$(env_get CLOUDFLARE_ZONE_ID)"
if [ -z "$CF_TOKEN" ] || [ -z "$CF_ZONE" ]; then
  echo "[deploy] ERROR: en .env faltan CLOUDFLARE_API_TOKEN y CLOUDFLARE_ZONE_ID."
  echo "[deploy] El token necesita el permiso Zone.Cache Purge. El zone id está en el resumen de la zona."
  exit 1
fi

echo "[deploy] Purgando Cloudflare para propmap.com.ar..."
CF_BODY="$(mktemp)"
CF_HTTP="$(curl -sS -o "$CF_BODY" -w '%{http_code}' -X POST \
  "https://api.cloudflare.com/client/v4/zones/${CF_ZONE}/purge_cache" \
  -H "Authorization: Bearer ${CF_TOKEN}" \
  -H "Content-Type: application/json" \
  --data '{"hosts":["propmap.com.ar","www.propmap.com.ar"]}')"
if [ "$CF_HTTP" != "200" ] || ! python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); raise SystemExit(0 if d.get("success") else 1)' "$CF_BODY"; then
  echo "[deploy] ERROR: Cloudflare no purgó la caché (HTTP ${CF_HTTP})."
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); errs=d.get("errors") or d; print(errs)' "$CF_BODY" >&2 || true
  rm -f "$CF_BODY"
  exit 1
fi
rm -f "$CF_BODY"
echo "[deploy] Cloudflare purgado."

echo "[deploy] Precargando la home y los estáticos que referencia..."
PAGE="$(mktemp)"
if curl -fsS -m 25 -o "$PAGE" "https://propmap.com.ar/"; then
  grep -oE '/static/(app\.js|styles\.css)\?v=[^" ]+' "$PAGE" | sort -u | while IFS= read -r path; do
    [ -z "$path" ] && continue
    echo "[deploy] Precargando https://propmap.com.ar${path}"
    curl -fsS -m 25 -o /dev/null "https://propmap.com.ar${path}" || echo "[deploy] Aviso: falló la precarga de ${path}"
  done
else
  echo "[deploy] Aviso: no pude precargar https://propmap.com.ar/"
fi
rm -f "$PAGE"
exit 0
