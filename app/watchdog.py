"""Último recurso si un candado o el event loop se traban de verdad.

Los candados cubren deadlocks de cache/pipeline. El GET a /api/alive cubre
el caso en que la portada no responde aunque los locks estén libres.
Si no ceden, deja de latir y Docker levanta el proceso.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

INTERVAL_SEC = 2.0
LOCK_WAIT_SEC = 1.2
HTTP_WAIT_SEC = 1.5
STALE_SEC = 12.0
SUICIDE_FAILS = 8
BOOT_GRACE_SEC = 40.0

_started = 0.0
_fails = 0
_ok_once = False
_stop = threading.Event()
_thread: threading.Thread | None = None


def heartbeat_path() -> Path:
    from .store import DATA_DIR

    return Path(os.environ.get("DATA_DIR") or DATA_DIR) / "heartbeat"


def snapshot() -> dict:
    path = heartbeat_path()
    try:
        age = time.time() - path.stat().st_mtime
        fresh = age <= STALE_SEC
    except OSError:
        age = None
        fresh = False
    return {"ok": fresh, "age_sec": None if age is None else round(age, 2)}


def alive() -> bool:
    return snapshot()["ok"]


def start() -> None:
    global _thread, _started
    if os.environ.get("PROPMAP_TEST") == "1":
        return
    if _thread and _thread.is_alive():
        return
    _started = time.time()
    _stop.clear()
    _write(ok=True)
    _thread = threading.Thread(target=_loop, daemon=True, name="watchdog")
    _thread.start()


def stop() -> None:
    _stop.set()


def _write(*, ok: bool) -> None:
    path = heartbeat_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{int(time.time())} {'ok' if ok else 'stuck'}\n", encoding="ascii")
    except OSError:
        log.exception("watchdog no pudo escribir heartbeat")


def _ping_locks() -> bool:
    from . import listings_cache, pipeline

    return pipeline.ping_lock(LOCK_WAIT_SEC) and listings_cache.ping_lock(LOCK_WAIT_SEC)


def _http_ok() -> bool:
    port = os.environ.get("PORT") or "8000"
    url = f"http://127.0.0.1:{port}/api/alive"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=HTTP_WAIT_SEC) as resp:
            return 200 <= int(resp.status) < 300
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


def _healthy() -> bool:
    from . import listings_cache

    # cities_loading() no debe esperar el candado: si está tomado, ping_lock decide.
    if listings_cache.busy_building() or listings_cache.cities_loading():
        return True
    ok = _ping_locks()
    if not ok:
        return False
    if time.time() - _started >= BOOT_GRACE_SEC:
        return _http_ok()
    return True


def _loop() -> None:
    global _fails, _ok_once
    while not _stop.wait(INTERVAL_SEC):
        try:
            ok = _healthy()
        except Exception:
            log.exception("watchdog ping")
            ok = False
        if ok:
            _fails = 0
            _ok_once = True
            _write(ok=True)
            continue
        _fails += 1
        log.warning("watchdog: API trabada (%s)", _fails)
        if (
            _ok_once
            and _fails >= SUICIDE_FAILS
            and time.time() - _started > BOOT_GRACE_SEC
        ):
            log.error("watchdog: la API sigue trabada; salgo para que Docker me levante")
            os._exit(1)
