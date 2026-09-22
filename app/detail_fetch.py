from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Any

from .freshness import needs_detail_fetch
from .geo import location_incomplete
from .models import Listing

_urgent: deque[str] = deque()
_queue: deque[str] = deque()
_seen: set[str] = set()
_skip_until: dict[str, float] = {}
_lock = threading.Lock()
_workers = 0
MAX_WORKERS = 3
SKIP_FAIL_SEC = 300.0
SKIP_MAX_SEC = 6 * 3600.0
COLD_TRIES = 4


def enabled() -> bool:
    return os.environ.get("PROPMAP_TEST") != "1"


def _ops_note(metric: str, **labels: Any) -> None:
    try:
        from .ops import note

        note(metric, **labels)
    except Exception:
        return


def workers() -> int:
    from .egress import lane_count

    raw = os.environ.get("DETAIL_WORKERS")
    if raw:
        try:
            return max(1, min(16, int(raw)))
        except ValueError:
            pass
    n = lane_count()
    target = n * 3 if n > 1 else MAX_WORKERS
    # Tor extra no multiplica workers: si el portal bloquea, saturaban la IP local.
    return max(1, min(6, max(MAX_WORKERS, target)))


def queue_stats() -> dict[str, Any]:
    now = time.time()
    with _lock:
        return {
            "pending": len(_urgent) + len(_queue),
            "urgent": len(_urgent),
            "rest": len(_queue),
            "cooling": sum(1 for until in _skip_until.values() if until > now),
            "downloading": int(_workers),
            "workers": workers(),
            "enabled": enabled(),
        }


def _wait_sec(tries: int) -> float:
    """Si el portal contesta 403, insistir al toque solo quema CPU y SQLite."""
    step = max(1, min(tries, 6))
    return min(SKIP_MAX_SEC, SKIP_FAIL_SEC * (2 ** (step - 1)))


def _cool_locked(listing_id: str, tries: int) -> None:
    _skip_until[listing_id] = time.time() + _wait_sec(tries)
    if len(_skip_until) > 4000:
        now = time.time()
        for lid in [lid for lid, until in _skip_until.items() if until <= now]:
            _skip_until.pop(lid, None)


def _cooling_locked(listing_id: str) -> bool:
    until = _skip_until.get(listing_id) or 0.0
    if until > time.time():
        return True
    if until:
        _skip_until.pop(listing_id, None)
    return False


def needs_llm(item: Listing) -> bool:
    from .llm_enrich import needs_improve

    return needs_improve(item)


def enqueue(listings: list[Listing] | None, *, ignore_cooling: bool = False) -> None:
    if not enabled() or not listings:
        return
    from .llm_enrich import enqueue as enqueue_llm

    ready_for_llm: list[Listing] = []
    with _lock:
        for item in listings:
            if not item or not item.id:
                continue
            if needs_detail_fetch(item) and item.url:
                extra = item.extra or {}
                tries = int(extra.get("detail_tries") or 0)
                if tries >= COLD_TRIES and item.id not in _skip_until:
                    # Tras un reinicio, los que ya venían fallando no arrancan de cero.
                    _cool_locked(item.id, tries)
                if not ignore_cooling and _cooling_locked(item.id):
                    continue
                loc_first = location_incomplete(item) or extra.get("await_llm")
                if item.id in _seen:
                    if loc_first and item.id in _queue:
                        _queue.remove(item.id)
                        _urgent.appendleft(item.id)
                    continue
                _seen.add(item.id)
                if loc_first:
                    _urgent.appendleft(item.id)
                else:
                    _queue.append(item.id)
            elif needs_llm(item):
                ready_for_llm.append(item)
        _ensure_workers_locked()
    if ready_for_llm:
        enqueue_llm(ready_for_llm)


def refill(prefer_city: str = "") -> int:
    """Baja fichas de toda la base, no solo de la ciudad en scrape."""
    if not enabled():
        return 0
    now = time.time()
    with _lock:
        pending = len(_urgent) + len(_queue)
        room = max(0, workers() * 3 - pending)
        skip = set(_seen)
        # El backfill es un reintento deliberado: no filtrar por cooling (_skip_until)
        # para que los items con detail_tries agotados puedan reintentarse.
    if room <= 0:
        return 0
    from . import store

    items = store.fetch_detail_backlog(min(24, room + 8), prefer_city=prefer_city)
    take = [item for item in items if item.id not in skip][:room]
    if take:
        # Limpiar cooling para estos items ya que el backfill los está reintentando
        with _lock:
            for item in take:
                _skip_until.pop(item.id, None)
        enqueue(take, ignore_cooling=True)
    return len(take)


def _has_work() -> bool:
    return bool(_urgent or _queue)


def _ensure_workers_locked() -> None:
    global _workers
    while _has_work() and _workers < workers():
        _workers += 1
        threading.Thread(target=_drain, daemon=True, name=f"detail-fetch-{_workers}").start()


def _way_id(listing_id: str) -> str:
    from .http_client import portal_way

    return portal_way(listing_id=listing_id)


def _extract_match(match) -> str | None:
    for bucket in (_urgent, _queue):
        for _ in range(len(bucket)):
            lid = bucket.popleft()
            if match(lid):
                return lid
            bucket.append(lid)
    return None


def _pop_work() -> str | None:
    """Saca un aviso cuyo carril esté libre. Varias fichas locales en paralelo (~20 s cada una)."""
    from .egress import local_has_room, local_ready
    from .http_client import portal_host

    def local_ok(lid: str) -> bool:
        return _way_id(lid) == "local" and local_ready(portal_host(listing_id=lid))

    if local_has_room():
        local_id = _extract_match(local_ok)
        if local_id:
            return local_id
    other = _extract_match(lambda lid: _way_id(lid) != "local")
    if other:
        return other
    return _extract_match(local_ok)


def _drain() -> None:
    global _workers
    try:
        while True:
            with _lock:
                listing_id = _pop_work()
                stalled = not listing_id and _has_work()
            if listing_id:
                _fetch_id(listing_id)
                continue
            if stalled:
                time.sleep(1.0)
                continue
            return
    finally:
        with _lock:
            _workers = max(0, _workers - 1)
            _ensure_workers_locked()


def _fetch_id(listing_id: str) -> None:
    from . import store
    from .features import analyze
    from .llm_enrich import enqueue as enqueue_llm
    from .scrapers.details import enrich_details

    item = store.get_listing(listing_id)
    if not item:
        return
    if needs_detail_fetch(item) and item.url:
        try:
            enrich_details(item)
            ok = bool(item.details_scraped)
        except Exception:
            analyze(item)
            ok = False
        _ops_note("details", outcome="ok" if ok else "fail")
        extra = dict(item.extra or {})
        if ok:
            extra.pop("detail_tries", None)
            with _lock:
                _skip_until.pop(listing_id, None)
        else:
            tries = int(extra.get("detail_tries") or 0) + 1
            extra["detail_tries"] = tries
            with _lock:
                _cool_locked(listing_id, tries)
        item.extra = extra
        store.upsert_many([item])
        item = store.get_listing(listing_id) or item
    if needs_llm(item):
        extra = item.extra or {}
        enqueue_llm(
            [item],
            urgent=location_incomplete(item) or bool(extra.get("await_llm")),
        )
    else:
        from .llm_copy import enqueue as enqueue_copy

        enqueue_copy([item])
    with _lock:
        if listing_id not in _urgent and listing_id not in _queue:
            _seen.discard(listing_id)
