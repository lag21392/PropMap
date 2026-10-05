#!/usr/bin/env bash
set -Eeuo pipefail

readonly SERVICE_NAME="propmap-production.service"
readonly SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}"
readonly LAUNCHER="/usr/local/libexec/propmap-production-startup"

die() {
  printf '[propmap-startup] error: %s\n' "$*" >&2
  exit 1
}

SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")" ||
  die "no se pudo resolver la ruta del script"
REPO_ROOT="$(cd -- "$(dirname -- "$SCRIPT_PATH")/.." && pwd -P)" ||
  die "no se pudo resolver la raíz del repositorio"
COMPOSE_FILE="${REPO_ROOT}/compose.prod.yaml"

start_stack() {
  [[ -f "$COMPOSE_FILE" ]] ||
    die "no existe el Compose de producción: ${COMPOSE_FILE}"
  command -v docker >/dev/null 2>&1 ||
    die "no se encontró Docker CLI en PATH"

  printf '[propmap-startup] iniciando Compose de producción desde %s\n' "$REPO_ROOT"
  cd -- "$REPO_ROOT"
  exec docker compose -f "$COMPOSE_FILE" up -d
}

install_service() {
  [[ "$(id -u)" -eq 0 ]] ||
    die "ejecutá la instalación como root, por ejemplo: sudo $SCRIPT_PATH"
  command -v systemctl >/dev/null 2>&1 ||
    die "systemctl no está disponible; se requiere un host Linux con systemd"
  command -v docker >/dev/null 2>&1 ||
    die "no se encontró Docker CLI en PATH"
  docker compose version >/dev/null 2>&1 ||
    die "Docker Compose v2 no está disponible (falló 'docker compose version')"
  [[ -f "$COMPOSE_FILE" ]] ||
    die "no existe el Compose de producción: ${COMPOSE_FILE}"
  case "$REPO_ROOT" in
    *'"'*|*'\'*|*$'\n'*)
      die "la ruta del repositorio contiene caracteres no admitidos por la unidad systemd"
      ;;
  esac
  SYSTEMD_REPO_ROOT="${REPO_ROOT//%/%%}"
  SYSTEMD_REPO_ROOT="${SYSTEMD_REPO_ROOT// /\\x20}"

  if [[ -e "$(dirname -- "$LAUNCHER")" && ! -d "$(dirname -- "$LAUNCHER")" ]]; then
    die "la ruta del lanzador no es un directorio: $(dirname -- "$LAUNCHER")"
  fi
  if [[ -e "$LAUNCHER" && ! -L "$LAUNCHER" ]]; then
    die "no se sobrescribe el archivo existente: ${LAUNCHER}"
  fi
  if [[ -L "$SERVICE_FILE" ]]; then
    die "no se sobrescribe una unidad systemd que sea un enlace simbólico: ${SERVICE_FILE}"
  fi
  if [[ -e "$SERVICE_FILE" ]] &&
      ! grep -Fq '# Managed by PropMap install-production-startup.sh' "$SERVICE_FILE"; then
    die "no se sobrescribe una unidad existente no administrada: ${SERVICE_FILE}"
  fi

  install -d -m 0755 "$(dirname -- "$LAUNCHER")" ||
    die "no se pudo crear $(dirname -- "$LAUNCHER")"
  ln -sfn -- "$SCRIPT_PATH" "$LAUNCHER" ||
    die "no se pudo instalar el lanzador: ${LAUNCHER}"
  cat >"$SERVICE_FILE" <<UNIT
[Unit]
Description=Start the PropMap production Compose stack
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target
RequiresMountsFor=$SYSTEMD_REPO_ROOT

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$SYSTEMD_REPO_ROOT
ExecStart=/usr/local/libexec/propmap-production-startup --start

[Install]
WantedBy=multi-user.target
# Managed by PropMap install-production-startup.sh
UNIT
  chmod 0644 "$SERVICE_FILE" ||
    die "no se pudo establecer permisos en ${SERVICE_FILE}"

  systemctl daemon-reload ||
    die "systemd no pudo recargar las unidades"
  systemctl enable docker.service ||
    die "no se pudo habilitar docker.service para el arranque"
  systemctl enable --now "$SERVICE_NAME" ||
    die "no se pudo habilitar e iniciar ${SERVICE_NAME}"

  printf '[propmap-startup] listo; Docker y %s quedaron habilitados\n' "$SERVICE_NAME"
}

case "${1:-}" in
  --start)
    [[ $# -eq 1 ]] || die "no se aceptan argumentos adicionales con --start"
    start_stack
    ;;
  "")
    install_service
    ;;
  *)
    die "uso: $0 [--start]"
    ;;
esac
