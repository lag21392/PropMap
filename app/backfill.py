"""Rellena fichas y LLM con el backlog de toda la base, no solo la ciudad en curso."""
from __future__ import annotations

import logging
import os
import threading
import time

log = logging.getLogger(__name__)
PUMP_SEC = 2.0
_started = False
_lock = threading.Lock()


def pump(prefer_cities: list[str] | None = None) -> dict[str, int]:
    """Llena huecos en las colas. Barato si ya están llenas."""
    if os.environ.get("PROPMAP_TEST") == "1":
        return {"details": 0, "llm": 0, "copy": 0}
    prefer = next((cid for cid in (prefer_cities or []) if cid), "")
    details_n = 0
    llm_n = 0
    copy_n = 0
    try:
        from .detail_fetch import refill as refill_details

        details_n = refill_details(prefer)
    except Exception:
        log.exception("backfill fichas")
    try:
        from .llm_enrich import refill as refill_llm

        llm_n = refill_llm(prefer)
    except Exception:
        log.exception("backfill llm")
    try:
        from .llm_copy import refill as refill_copy

        copy_n = refill_copy(prefer)
    except Exception:
        log.exception("backfill copy")
    return {"details": details_n, "llm": llm_n, "copy": copy_n}


def start() -> None:
    global _started
    with _lock:
        if _started or os.environ.get("PROPMAP_TEST") == "1":
            return
        _started = True

    def loop() -> None:
        while True:
            try:
                from .llm_copy import ensure_running as ensure_copy
                from .llm_enrich import ensure_running as ensure_llm
                from .pipeline import _running_ids

                ensure_llm()
                ensure_copy()
                pump(prefer_cities=list(_running_ids() or []))
            except Exception:
                log.exception("backfill loop")
            time.sleep(PUMP_SEC)

    threading.Thread(target=loop, daemon=True, name="propmap-backfill").start()
