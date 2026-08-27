from __future__ import annotations

import json
import math
import threading
import time
from datetime import datetime, timezone

COUNTRY_ID = "argentina"
SKIP = {"fuera", "otros", ""}
FAST_STALE_SEC = 4 * 3600
SLOW_STALE_SEC = 20 * 3600
COUNTRY_STALE_SEC = 30 * 3600
STARVE_SEC = 45 * 60
SEARCH_BOOST_SEC = 2 * 3600
HOUR = 3600

_lock = threading.Lock()
_demand: dict[str, dict] = {}
_loaded = False
_persist = True


def is_country_place(city: str | None) -> bool:
    raw = (city or "").strip().lower().replace("_", "-")
    return raw in {COUNTRY_ID, "pais", "el-pais", "todo-el-pais", "todo el pais"}


def reset() -> None:
    global _loaded, _persist
    with _lock:
        _demand.clear()
        _loaded = True
        _persist = False


def load() -> None:
    global _loaded, _persist
    if _loaded:
        return
    from . import store

    store.init()
    raw = store.get_meta("place_demand")
    rows = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                rows = parsed
        except json.JSONDecodeError:
            rows = {}
    with _lock:
        if _loaded:
            return
        for city_id, row in rows.items():
            if isinstance(row, dict) and city_id:
                _demand[city_id] = _normalize(city_id, row)
        _loaded = True
        _persist = True


def note_search(city_id: str | None) -> str:
    load()
    cid = _canon(city_id)
    if not cid:
        return ""
    now = _now()
    with _lock:
        row = _row(cid)
        row["searches"] = int(row.get("searches") or 0) + 1
        row["last_search"] = now
        row["want_fast"] = True
        if not row.get("due_since"):
            row["due_since"] = now
        _save_locked()
    return cid


def note_view(city_id: str | None) -> str:
    load()
    cid = _canon(city_id)
    if not cid:
        return ""
    now = _now()
    with _lock:
        row = _row(cid)
        row["last_view"] = now
        if not row.get("due_since") and _is_due_locked(cid, row, time.time()):
            row["due_since"] = now
        _save_locked()
    return cid


def note_started(city_id: str, *, fast: bool) -> None:
    load()
    cid = _canon(city_id)
    if not cid:
        return
    with _lock:
        row = _row(cid)
        row["due_since"] = ""
        if fast:
            row["want_fast"] = False
        _save_locked()


def note_finished(city_id: str, *, fast: bool, paused: bool = False) -> None:
    load()
    cid = _canon(city_id)
    if not cid:
        return
    now = _now()
    with _lock:
        row = _row(cid)
        if not paused:
            row["last_complete"] = now
            if fast:
                row["last_fast"] = now
            else:
                row["last_slow"] = now
        row["due_since"] = ""
        _save_locked()


def set_listing_counts(counts: dict[str, int]) -> None:
    load()
    with _lock:
        for raw_id, n in counts.items():
            cid = _canon(raw_id)
            if not cid:
                continue
            _row(cid)["listings"] = int(n or 0)
        _save_locked()


def searched_ids() -> list[str]:
    load()
    with _lock:
        return [cid for cid, row in _demand.items() if cid not in SKIP and int(row.get("searches") or 0) > 0]


def pool_ids() -> list[str]:
    load()
    from .geo import CITIES
    from .listings_cache import cached_city_ids
    from .places import listed_cities, load_custom_places

    load_custom_places()
    ids: set[str] = {COUNTRY_ID}
    with _lock:
        ids.update(cid for cid in _demand if cid not in SKIP)
    for cid in cached_city_ids():
        canon = _canon(cid)
        if canon:
            ids.add(canon)
    if len(ids) <= 1:
        for row in listed_cities([]):
            canon = _canon(row.get("id"))
            if canon:
                ids.add(canon)
    for cid in CITIES:
        canon = _canon(cid)
        if canon and (_row_get(canon).get("searches") or _row_get(canon).get("listings")):
            ids.add(canon)
    return sorted(ids)


def score(city_id: str, now: float | None = None) -> float:
    load()
    cid = _canon(city_id)
    if not cid:
        return -1.0
    now = now or time.time()
    with _lock:
        return _score_locked(cid, _row(cid), now)


def next_jobs(running: set[str] | None = None, slots: int = 1) -> list[tuple[str, bool]]:
    """Devuelve (city_id, fast) sin inanición: un cupo va al que más espera."""
    load()
    running = { _canon(cid) for cid in (running or set()) if _canon(cid) }
    slots = max(0, int(slots))
    if not slots:
        return []
    now = time.time()
    ids = _canon_pool(pool_ids())
    candidates: list[str] = []
    with _lock:
        for cid in ids:
            if cid in running:
                continue
            row = _row(cid)
            if not _is_due_locked(cid, row, now):
                continue
            if not row.get("due_since"):
                row["due_since"] = _now()
            candidates.append(cid)
        if not candidates:
            return []
        picks: list[str] = []
        starved = [cid for cid in candidates if _wait_sec_locked(_row(cid), now) >= STARVE_SEC]
        if starved:
            starved.sort(key=lambda cid: -_wait_sec_locked(_row(cid), now))
            picks.append(starved[0])
        rest = [cid for cid in candidates if cid not in picks]
        rest.sort(key=lambda cid: _score_locked(cid, _row(cid), now), reverse=True)
        picks.extend(rest)
        chosen = picks[:slots]
        out = [(cid, _want_fast_locked(cid, _row(cid), now)) for cid in chosen]
        _save_locked()
        return out


def next_background_city(running: set[str] | None = None) -> str | None:
    jobs = next_jobs(running, slots=1)
    return jobs[0][0] if jobs else None


def queue_public(running: set[str] | None = None, limit: int = 8) -> list[dict]:
    load()
    running = { _canon(cid) for cid in (running or set()) if _canon(cid) }
    now = time.time()
    ids = _canon_pool(pool_ids())
    rows = []
    with _lock:
        for cid in ids:
            row = _row(cid)
            due = _is_due_locked(cid, row, now)
            rows.append(
                {
                    "id": cid,
                    "label": _label(cid),
                    "score": round(_score_locked(cid, row, now), 2),
                    "searches": int(row.get("searches") or 0),
                    "listings": int(row.get("listings") or 0),
                    "due": due,
                    "running": cid in running,
                    "wait_s": int(_wait_sec_locked(row, now)) if due or cid in running else 0,
                    "fast": _want_fast_locked(cid, row, now),
                    "country": cid == COUNTRY_ID,
                }
            )
    rows.sort(key=lambda item: (-item["running"], -item["score"], item["label"]))
    return rows[:limit]


def _canon_pool(ids: list[str] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in ids or []:
        cid = _canon(raw) or raw
        if not cid or cid in SKIP or cid in seen:
            continue
        seen.add(cid)
        out.append(cid)
    return out


def _canon(city_id: str | None) -> str:
    cid = (city_id or "").strip()
    if not cid or cid in SKIP:
        return ""
    if is_country_place(cid):
        return COUNTRY_ID
    try:
        from .places import _canonical_listed_id

        return _canonical_listed_id(cid) or cid
    except Exception:
        return cid


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize(city_id: str, row: dict) -> dict:
    return {
        "id": city_id,
        "searches": int(row.get("searches") or 0),
        "listings": int(row.get("listings") or 0),
        "last_search": row.get("last_search") or "",
        "last_view": row.get("last_view") or "",
        "last_fast": row.get("last_fast") or "",
        "last_slow": row.get("last_slow") or "",
        "last_complete": row.get("last_complete") or "",
        "due_since": row.get("due_since") or "",
        "want_fast": bool(row.get("want_fast")),
    }


def _row(city_id: str) -> dict:
    if city_id not in _demand:
        _demand[city_id] = _normalize(city_id, {})
    return _demand[city_id]


def _row_get(city_id: str) -> dict:
    return _demand.get(city_id) or {}


def _age_sec(raw: str | None, now: float) -> float | None:
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(str(raw))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, now - when.timestamp())
    except ValueError:
        return None


def _wait_sec_locked(row: dict, now: float) -> float:
    return _age_sec(row.get("due_since"), now) or 0.0


def _recency(age: float | None, half_life: float) -> float:
    if age is None:
        return 0.0
    return 0.5 ** (age / half_life)


def _score_locked(city_id: str, row: dict, now: float) -> float:
    searches = int(row.get("searches") or 0)
    listings = int(row.get("listings") or 0)
    search_age = _age_sec(row.get("last_search"), now)
    complete_age = _age_sec(row.get("last_complete"), now)
    wait = _wait_sec_locked(row, now)
    demand = math.log1p(searches) * (1.0 + 3.0 * _recency(search_age, 12 * HOUR))
    if search_age is not None and search_age < SEARCH_BOOST_SEC:
        demand += 8.0
    if listings <= 0 and search_age is not None and search_age < 6 * HOUR:
        demand += 12.0
    important = math.log1p(listings)
    if city_id == COUNTRY_ID:
        important += 3.5
    stale = 10.0 if complete_age is None else complete_age / 86400.0
    aging = (wait / (6 * HOUR)) * 3.0
    return 5.0 * demand + 1.2 * important + 1.5 * stale + aging


def _want_fast_locked(city_id: str, row: dict, now: float) -> bool:
    if city_id == COUNTRY_ID and row.get("last_fast"):
        return False
    if row.get("want_fast"):
        return True
    search_age = _age_sec(row.get("last_search"), now)
    fast_age = _age_sec(row.get("last_fast"), now)
    if search_age is not None and search_age < 24 * HOUR:
        return fast_age is None or fast_age >= FAST_STALE_SEC
    return False


def _is_due_locked(city_id: str, row: dict, now: float) -> bool:
    complete = _age_sec(row.get("last_complete"), now)
    if complete is None:
        return True
    if row.get("want_fast"):
        fast_age = _age_sec(row.get("last_fast"), now)
        return fast_age is None or fast_age >= 15 * 60
    if _want_fast_locked(city_id, row, now):
        return True
    stale = COUNTRY_STALE_SEC if city_id == COUNTRY_ID else SLOW_STALE_SEC
    slow_age = _age_sec(row.get("last_slow") or row.get("last_complete"), now)
    return slow_age is None or slow_age >= stale


def _label(city_id: str) -> str:
    if city_id == COUNTRY_ID:
        return "todo el país"
    from .geo import CITIES

    return (CITIES.get(city_id) or {}).get("label") or city_id.replace("-", " ")


def _save_locked() -> None:
    if not _persist:
        return
    try:
        from . import store

        store.set_meta(
            "place_demand",
            json.dumps({cid: dict(row) for cid, row in _demand.items()}, ensure_ascii=False),
        )
    except Exception:
        pass
