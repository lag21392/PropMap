from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock
from typing import Iterable

from .models import Listing

try:
    from zoneinfo import ZoneInfo

    _AR = ZoneInfo("America/Argentina/Buenos_Aires")
except Exception:
    _AR = timezone.utc

_known: set[str] = set()
_lock = Lock()


def reset_known(ids: Iterable[str] | None = None) -> None:
    global _known
    with _lock:
        _known = set(ids or ())


def known_ids() -> set[str]:
    with _lock:
        return set(_known)


def is_known(listing_id: str) -> bool:
    with _lock:
        return listing_id in _known


def note_ids(ids: Iterable[str]) -> None:
    with _lock:
        _known.update(ids)


def same_local_day(raw: str | None, now: datetime | None = None) -> bool:
    if not raw:
        return False
    try:
        when = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return when.astimezone(_AR).date() == current.astimezone(_AR).date()


def city_lookup_already_ran(item: Listing) -> bool:
    """Hubo una pasada de la LLM para ubicar la ciudad. No cuenta el scrape crudo."""
    extra = item.extra or {}
    if extra.get("skip_details") or extra.get("llm_city_ok") or extra.get("llm_ready"):
        return True
    if extra.get("llm_at"):
        return True
    if extra.get("llm_partial") and extra.get("llm_ver"):
        return True
    return False


def skip_detail_for_unknown_city(item: Listing) -> bool:
    """Sin ciudad después de la primera pasada: no gastar otra bajada de ficha."""
    from .llm_fields import city_is_unassigned

    extra = item.extra or {}
    if extra.get("skip_details"):
        return True
    return city_is_unassigned(item.city) and city_lookup_already_ran(item)


def needs_detail_fetch(item: Listing, now: datetime | None = None) -> bool:
    """Ficha HTTP only if we never got it, or a later day and something actually changed."""
    if not item.url:
        return False
    if skip_detail_for_unknown_city(item):
        return False
    extra = item.extra or {}
    scraped = bool(item.details_scraped) or bool(extra.get("details_at"))
    if not scraped:
        return True
    from .scrapers.details import DETAILS_PARSER

    parser = extra.get("details_parser")
    if parser and parser != DETAILS_PARSER:
        source = (item.source or "").lower()
        if source == "argenprop" and extra.get("portal_lat") is None:
            return True
        return False
    if same_local_day(extra.get("details_at"), now=now):
        return False
    listed_price = item.price
    saved_price = extra.get("detail_price")
    try:
        if listed_price is not None and saved_price is not None:
            if abs(float(listed_price) - float(saved_price)) >= 1:
                return True
    except (TypeError, ValueError):
        pass
    listed_m2 = item.covered_m2 or item.total_m2
    saved_m2 = extra.get("detail_m2")
    try:
        if listed_m2 is not None and saved_m2 is not None:
            if abs(float(listed_m2) - float(saved_m2)) >= 1:
                return True
    except (TypeError, ValueError):
        pass
    return False


def has_usable_listing_text(item: Listing) -> bool:
    """Hay texto de lista o ficha para la LLM; no hace falta esperar otra bajada HTTP."""
    extra = item.extra or {}
    if item.details_scraped or extra.get("details_at"):
        return True
    return len((item.description or "").strip()) >= 160
