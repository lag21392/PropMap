#!/bin/bash
set -eu
HTML=/var/www/html
MISC="$HTML/misc"
CONSOLE="$HTML/console"
CONFIG="$HTML/config/config.ini.php"
WWW="www-data"
INSTALL=/init/install.php

fix_perms() {
  if id "$WWW" >/dev/null 2>&1; then
    chown -R "$WWW:$WWW" "$HTML"
    chmod -R u+rwX "$HTML/tmp" "$HTML/config" "$HTML/misc" 2>/dev/null || true
  fi
}

as_www() {
  runuser -u "$WWW" -- "$@"
}

echo "[matomo-init] esperando archivos de Matomo"
for _ in $(seq 1 90); do
  if [ -f "$CONSOLE" ] && [ -d "$MISC" ]; then
    break
  fi
  sleep 3
done
fix_perms

echo "[matomo-init] esperando MariaDB"
for _ in $(seq 1 60); do
  if as_www php -r '
    $h=getenv("MATOMO_DATABASE_HOST") ?: getenv("MATOMO_DB_HOST") ?: "matomo-db";
    $u=getenv("MATOMO_DATABASE_USERNAME") ?: getenv("MATOMO_DB_USER") ?: "matomo";
    $p=getenv("MATOMO_DATABASE_PASSWORD") ?: getenv("MATOMO_DB_PASSWORD") ?: "";
    $d=getenv("MATOMO_DATABASE_DBNAME") ?: getenv("MATOMO_DB_NAME") ?: "matomo";
    try { new PDO("mysql:host=$h;dbname=$d", $u, $p); exit(0); }
    catch (Throwable $e) { exit(1); }
  '; then
    break
  fi
  sleep 3
done

download_city() {
  local ym="$1"
  local url="https://download.db-ip.com/free/dbip-city-lite-${ym}.mmdb.gz"
  echo "[matomo-init] bajando GeoIP ciudad ${ym}"
  as_www php -r "file_put_contents('/tmp/dbip-city.mmdb.gz', file_get_contents('${url}'));" || return 1
  as_www bash -c "gzip -dc /tmp/dbip-city.mmdb.gz > '$MISC/DBIP-City.mmdb.tmp'"
  as_www mv "$MISC/DBIP-City.mmdb.tmp" "$MISC/DBIP-City.mmdb"
  chmod 644 "$MISC/DBIP-City.mmdb"
  fix_perms
  rm -f /tmp/dbip-city.mmdb.gz
}

if [ ! -s "$MISC/DBIP-City.mmdb" ]; then
  YM="$(date +%Y-%m)"
  if ! download_city "$YM"; then
    PREV="$(as_www php -r 'echo date("Y-m", strtotime("-1 month"));')"
    download_city "$PREV" || echo "[matomo-init] no se pudo bajar DB-IP City"
  fi
else
  echo "[matomo-init] GeoIP ciudad ya esta en misc/DBIP-City.mmdb"
fi

if [ -f "$INSTALL" ]; then
  echo "[matomo-init] instalando Matomo por CLI"
  as_www php "$INSTALL" config
  as_www php "$INSTALL" tables
  as_www php "$CONSOLE" core:update --yes || true
  as_www php "$INSTALL" finalize
  as_www php "$CONSOLE" plugin:activate GeoIp2 || true
  as_www php "$CONSOLE" plugin:activate UserCountry || true
  as_www php "$CONSOLE" plugin:activate UserCountryMap || true
  as_www php "$CONSOLE" cache:clear || true
  fix_perms
fi

echo "[matomo-init] listo"
while true; do
  sleep 604800
  YM="$(date +%Y-%m)"
  download_city "$YM" || true
done
