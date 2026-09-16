from __future__ import annotations

import os
import threading
from collections import deque
from typing import Any

from .freshness import needs_detail_fetch
from .geo import location_incomplete
from .models import Listing

_urgent: deque[str] = deque()
_queue: deque[str] = deque()
_seen: set[str] = set()
_lock = threading.Lock()
_workers = 0
MAX_WORKERS = 3


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
    return max(1, min(16, max(MAX_WORKERS, target)))


def queue_stats() -> dict[str, Any]:
    with _lock:
        return {
            "pending": len(_urgent) + len(_queue),
            "urgent": len(_urgent),
            "rest": len(_queue),
            "downloading": int(_workers),
            "workers": workers(),
            "enabled": enabled(),
        }


def needs_llm(item: Listing) -> bool:
    from .llm_enrich import needs_improve

    return needs_improve(item)


def enqueue(listings: list[Listing] | None) -> None:
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
    with _lock:
        pending = len(_urgent) + len(_queue)
        room = max(0, workers() * 3 - pending)
        skip = set(_seen)
    if room <= 0:
        return 0
    from . import store

    items = store.fetch_detail_backlog(min(24, room + 8), prefer_city=prefer_city)
    take = [item for item in items if item.id not in skip][:room]
    if take:
        enqueue(take)
    return len(take)


def _has_work() -> bool:
    return bool(_urgent or _queue)


def _ensure_workers_locked() -> None:
    global _workers
    while _has_work() and _workers < workers():
        _workers += 1
        threading.Thread(target=_drain, daemon=True, name=f"detail-fetch-{_workers}").start()


def _drain() -> None:
    global _workers
    try:
        while True:
            with _lock:
                if _urgent:
                    listing_id = _urgent.popleft()
                elif _queue:
                    listing_id = _queue.popleft()
                else:
                    return
            _fetch_id(listing_id)
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
            _ops_note("details", outcome="ok" if item.details_scraped else "fail")
        except Exception:
            analyze(item)
            _ops_note("details", outcome="fail")
        store.upsert_many([item])
        item = store.get_listing(listing_id) or item
    if needs_llm(item):
        extra = item.extra or {}
        enqueue_llm(
            [item],
            urgent=location_incomplete(item) or bool(extra.get("await_llm")),
        )
    with _lock:
        if listing_id not in _urgent and listing_id not in _queue:
            _seen.discard(listing_id)
