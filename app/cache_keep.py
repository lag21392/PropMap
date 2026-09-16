"""Mantiene el gzip de cada ciudad del catálogo listo.

El GET no arma SQLite: este hilo recorre el desplegable, precarga lo que falta
y refresca lo que creció. El visitante lee disco (aunque el TTL haya vencido).
"""
from __future__ import annotations

import logging
import os
import threading
import time

log = logging.getLogger(__name__)

KEEP_SEC = 90
BOOT_DELAY_SEC = 12

_thread: threading.Thread | None = None
_stop = threading.Event()


def start() -> None:
    global _thread
    if os.environ.get("PROPMAP_TEST") == "1":
        return
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="cache-keep")
    _thread.start()


def stop() -> None:
    _stop.set()


def _loop() -> None:
    if _stop.wait(BOOT_DELAY_SEC):
        return
    while not _stop.is_set():
        try:
            from .listings_cache import keep_catalog_cached

            keep_catalog_cached()
        except Exception:
            log.exception("cache-keep")
        if _stop.wait(KEEP_SEC):
            return
