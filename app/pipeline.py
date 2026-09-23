from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from threading import Event, Lock
from typing import Callable
from urllib.parse import quote

from . import crawl, freshness, store
from .features import analyze
from .listing_signals import apply_signals
from .dedupe import DEDUPE_VERSION, collapse_duplicates
from .geo import (
    CITIES,
    GEO_VERSION,
    default_city,
    pin_listing_city,
    resolve_city,
    same_place_ids,
)
from .http_client import fetch_json
from .models import Listing
from .places import listed_cities, load_custom_places
from .schedule import (
    COUNTRY_ID,
    is_country_place,
    note_finished,
    note_search,
    note_started,
    queue_public,
    set_listing_counts,
)
from .scoring import SCORE_VERSION, USD_FALLBACK, apply_unit_price, enrich, summarize, to_usd
from .scrapers import argenprop, mercadolibre, properati, zonaprop
from .scrapers.details import enrich_details

Progress = Callable[[str], None]
log = logging.getLogger(__name__)
_PORTAL_RE = re.compile(
    r"zona\s*prop|argenprop|properati|mercado\s*libre|mercadolibre|"
    r"https?://\S*(?:zonaprop|argenprop|properati|mercadolibre)\S*",
    re.I,
)


def public_search_text(message: str, city_label: str = "") -> str:
    raw = (message or "").strip()
    place = f" en {city_label}" if city_label else ""
    if not raw:
        return f"Buscando avisos{place}…"
    low = _PORTAL_RE.sub(" ", raw)
    low = re.sub(r"\s{2,}", " ", low).strip(" ·:-")
    probe = low.lower()
    if _PORTAL_RE.search(raw) or re.search(r"\b(leyendo|ficha|fichas|listado listo|alquiler|renta)\b", probe):
        if "pausad" in probe:
            return f"Pausando búsqueda{place}…"
        if "calculando" in probe:
            return low
        if "listo" in probe and "aviso" in probe:
            return low
        return f"Buscando avisos{place}…"
    return low or f"Buscando avisos{place}…"

_status = {
    "running": False,
    "paused": False,
    "message": "Todavía no se buscó nada.",
    "logs": [],
    "error": "",
    "last_run": "",
    "counts": {},
}
_jobs: dict[str, dict] = {}
_stops: dict[str, Event] = {}
_lock = Lock()
_stop = Event()
_scheduler_started = False
_rest_gen: dict[str, int] = {}
DAILY_SEC = 24 * 3600
CHECK_EVERY_SEC = 8
BETWEEN_CITIES_SEC = 3
MAX_JOBS = 1
MAX_BACKGROUND = 1
PORTAL_WORKERS = 4


def _egress_snapshot() -> dict:
    from .egress import snapshot as egress_snapshot

    return egress_snapshot()


_last_public: dict = {}
_aux: dict = {"queue": [], "daily_city": None, "daily_cities": [], "at": 0.0}
_aux_inflight = False
_aux_lock = Lock()
AUX_TTL_SEC = 15.0
_BUSY_STATUS = {
    "running": False,
    "running_cities": [],
    "mode": "",
    "jobs": {},
    "paused": False,
    "message": "El servidor está ocupado; reintento en un momento.",
    "logs": [],
    "error": "",
    "counts": {},
    "crawl": {"enabled": True, "daily": True},
    "listings_rev": 0,
    "llm": {},
    "details": {},
    "queue": [],
    "city_catalog": [],
    "busy": True,
}


def ping_lock(timeout: float = 0.4) -> bool:
    got = _lock.acquire(timeout=timeout)
    if not got:
        return False
    _lock.release()
    return True


def _home_scrape_id() -> str:
    try:
        from .schedule import home_scrape_id

        return home_scrape_id()
    except Exception:
        return ""


def _bump_rest(city_id: str) -> int:
    _rest_gen[city_id] = _rest_gen.get(city_id, 0) + 1
    return _rest_gen[city_id]


def _job(city_id: str) -> dict:
    if city_id not in _jobs:
        _jobs[city_id] = {
            "city": city_id,
            "running": False,
            "paused": False,
            "mode": "",
            "message": "",
            "logs": [],
            "error": "",
            "last_run": "",
            "counts": {},
        }
    return _jobs[city_id]


def _place_label(city_id: str) -> str:
    if is_country_place(city_id):
        return "todo el país"
    return (CITIES.get(city_id) or {}).get("label") or city_id.replace("-", " ")


def _running_ids() -> set[str]:
    return {cid for cid, job in _jobs.items() if job.get("running")}


def loading_city_ids() -> set[str]:
    return set(_running_ids())


def eta_minutes(
    city_id: str | None = None,
    *,
    running: list[str] | None = None,
    mode: str = "",
    live: bool | None = None,
) -> int:
    if running is None or live is None:
        with _lock:
            running = list(_running_ids())
            job = _jobs.get(city_id or "") or {}
            mode = str(job.get("mode") or mode)
            live = bool(job.get("running")) if live is None else live
    ahead = max(0, len(running) - (1 if live else 0))
    base = 18 if mode == "slow" else 4
    return min(40, base + ahead * 6)


def _drop_empty_listed_place(city_id: str) -> None:
    from .geo import CABA_IDS
    from .places import forget_place, listing_count_for_catalog

    if not city_id or city_id == default_city() or city_id in CABA_IDS:
        return
    wanted = same_place_ids(city_id) | {city_id}
    counts = store.city_listing_counts()
    total = sum(int(counts.get(cid) or 0) for cid in wanted)
    if listing_count_for_catalog(total):
        return
    forget_place(city_id)
    try:
        from .listings_cache import refresh_city_catalog

        refresh_city_catalog()
    except Exception:
        pass


def status() -> dict:
    global _last_public
    got = _lock.acquire(timeout=0.35)
    if not got:
        cached = dict(_last_public or _BUSY_STATUS)
        cached["busy"] = True
        return cached
    try:
        jobs = {}
        running_cities = []
        logs: list[str] = []
        for city_id, job in _jobs.items():
            label = _place_label(city_id)
            jobs[city_id] = {
                **job,
                "message": public_search_text(job.get("message") or "", label),
                "error": public_search_text(job.get("error") or "", label),
                "logs": [public_search_text(row, label) for row in list(job["logs"][-30:])],
                "counts": {"avisos": sum((job.get("counts") or {}).values())},
            }
            if job["running"]:
                running_cities.append(city_id)
            logs.extend(job["logs"][-10:])
        for city_id, row in jobs.items():
            src = _jobs.get(city_id) or {}
            if src.get("running"):
                row["eta_min"] = eta_minutes(
                    city_id,
                    running=running_cities,
                    mode=str(src.get("mode") or ""),
                    live=True,
                )
        message = _status["message"]
        if running_cities:
            last = running_cities[-1]
            last_label = _place_label(last)
            message = public_search_text(_job(last).get("message") or message, last_label)
        else:
            message = public_search_text(message)
        payload = {
            "running": bool(running_cities),
            "running_cities": running_cities,
            "mode": (_job(running_cities[-1]).get("mode") if running_cities else ""),
            "jobs": jobs,
            "paused": any(job.get("paused") for job in _jobs.values()),
            "message": message,
            "logs": [public_search_text(row) for row in (logs[-40:] or list(_status["logs"][-40:]))],
            "error": public_search_text(_status.get("error") or ""),
            "counts": {
                "avisos": sum(sum((job.get("counts") or {}).values()) for job in _jobs.values())
            },
            "crawl": {
                **crawl.snapshot(),
                "egress": _egress_snapshot(),
                "enabled": True,
                "daily": True,
            },
            "eta_min": eta_minutes(
                running_cities[-1] if running_cities else None,
                running=running_cities,
                mode=(_job(running_cities[-1]).get("mode") if running_cities else ""),
                live=bool(running_cities),
            ),
        }
        running_copy = list(running_cities)
        fallback_run = _status["last_run"]
        fallback_usd = USD_FALLBACK
    finally:
        _lock.release()
    from .listings_cache import current_rev, public_meta
    from .llm_enrich import queue_stats
    from .detail_fetch import queue_stats as detail_stats
    from .llm_copy import queue_stats as copy_stats

    meta = public_meta()
    payload["listings_rev"] = current_rev()
    payload["llm"] = queue_stats()
    payload["details"] = detail_stats()
    payload["copy"] = copy_stats()
    payload["last_run"] = meta.get("last_run") or fallback_run
    payload["usd_ars"] = meta.get("usd_ars") if meta.get("usd_ars") is not None else fallback_usd
    payload["queue"] = list(_aux.get("queue") or [])
    payload["crawl"]["city"] = _aux.get("daily_city")
    payload["crawl"]["cities"] = list(_aux.get("daily_cities") or [])
    payload["city_catalog"] = list(meta.get("cities") or [])
    payload["busy"] = False
    _last_public = payload
    _kick_status_aux(running_copy)
    return payload


def _refresh_status_aux(running: set[str] | None = None) -> None:
    global _aux_inflight
    try:
        live = set(running) if running is not None else _running_ids()
        _aux["queue"] = queue_public(live)
        _aux["daily_city"] = next_daily_city()
        _aux["daily_cities"] = daily_city_ids()
        _aux["at"] = time.time()
    except Exception:
        log.exception("no pude armar la cola de estado")
    finally:
        _aux_inflight = False


def _kick_status_aux(running_copy: list[str] | None = None) -> None:
    global _aux_inflight
    if time.time() - float(_aux.get("at") or 0) < AUX_TTL_SEC:
        return
    with _aux_lock:
        if _aux_inflight:
            return
        _aux_inflight = True
    threading.Thread(
        target=_refresh_status_aux,
        args=(set(running_copy or []),),
        daemon=True,
        name="status-aux",
    ).start()


def _log(message: str, city_id: str | None = None) -> None:
    label = _place_label(city_id) if city_id else ""
    text = public_search_text(message, label)
    with _lock:
        _status["message"] = text
        _status["logs"].append(text)
        if len(_status["logs"]) > 80:
            _status["logs"] = _status["logs"][-80:]
        if city_id:
            job = _job(city_id)
            job["message"] = text
            job["logs"].append(text)
            if len(job["logs"]) > 80:
                job["logs"] = job["logs"][-80:]


def request_pause(city: str | None = None) -> dict:
    city_id = COUNTRY_ID if is_country_place(city) else (resolve_city(city) if city else None)
    if not city_id:
        crawl.request_abort()
        _stop.set()
    with _lock:
        targets = [city_id] if city_id else list(_stops)
        found = False
        for cid in targets:
            ev = _stops.get(cid)
            job = _jobs.get(cid)
            if ev and job and job.get("running"):
                ev.set()
                job["paused"] = True
                job["message"] = "Pausando… lo reunido queda guardado."
                found = True
        if found:
            _status["paused"] = True
            _status["message"] = "Pausando… lo reunido queda guardado."
        else:
            _status["message"] = "No hay una búsqueda en curso en esa ciudad."
    return status()


def usd_rate() -> float:
    try:
        data = fetch_json("https://dolarapi.com/v1/dolares/blue")
        venta = float(data.get("venta") or data.get("promedio") or 0)
        if venta > 100:
            store.set_meta("usd_ars", str(venta))
            return venta
    except Exception:
        pass
    cached = store.get_meta("usd_ars")
    try:
        return float(cached) if cached else USD_FALLBACK
    except ValueError:
        return USD_FALLBACK


def refresh(
    city: str | None = None,
    progress: Progress | None = None,
    *,
    fast: bool = True,
    interactive: bool | None = None,
) -> dict:
    from .places import ensure_place

    store.init()
    load_custom_places()
    if is_country_place(city):
        city_id = COUNTRY_ID
    else:
        city_id = ensure_place(query=city, city=city)
    if interactive is None:
        interactive = fast
    if interactive:
        note_search(city_id)
    try:
        from .listings_cache import refresh_city_catalog

        refresh_city_catalog()
    except Exception:
        pass
    label = _place_label(city_id)
    with _lock:
        job = _job(city_id)
        if job["running"]:
            if fast and job.get("mode") != "fast":
                job["interrupt_for_fast"] = True
                job["mode"] = "fast"
                job["message"] = f"Pasando a búsqueda rápida en {label}…"
                stop = _stops.get(city_id)
                if stop:
                    stop.set()
            return status()
        running = _running_ids()
        background_n = sum(1 for cid in running if _jobs[cid].get("mode") != "fast")
        if not fast and background_n >= MAX_BACKGROUND:
            home = _home_scrape_id()
            if city_id != home:
                return status()
        if len(running) >= MAX_JOBS:
            home = _home_scrape_id()
            if fast or city_id == home:
                _preempt_one_slow_locked()
                running = _running_ids()
                if interactive and len(running) >= MAX_JOBS:
                    _preempt_one_locked(except_city=city_id)
            else:
                return status()
            if not fast and len(running) >= MAX_JOBS:
                return status()
        _bump_rest(city_id)
        job["running"] = True
        job["paused"] = False
        job["mode"] = "fast" if fast else "slow"
        job["error"] = ""
        job["logs"] = []
        job["message"] = (
            f"{'Búsqueda rápida' if fast else 'Actualización'} en {label}…"
        )
        _stops[city_id] = Event()
        _status["running"] = True
        _status["paused"] = False
        _status["error"] = ""
    if fast:
        crawl.clear_abort()
    note_started(city_id, fast=fast)
    store.set_meta("scrape_live", "1")
    threading.Thread(
        target=_run_city_job,
        args=(city_id, progress, fast),
        daemon=True,
        name=f"propmap-{'fast' if fast else 'slow'}-{city_id}",
    ).start()
    _kick_status_aux([city_id, *(_running_ids())])
    _kick_llm_enrich(city_id)
    return status()


def _preempt_one_slow_locked() -> None:
    _preempt_one_locked(prefer_slow=True)


def _preempt_one_locked(*, prefer_slow: bool = False, except_city: str | None = None) -> None:
    victims = [cid for cid, job in _jobs.items() if job.get("running") and cid != except_city]
    if prefer_slow:
        slow = [cid for cid in victims if _jobs[cid].get("mode") != "fast"]
        victims = slow or []
    if not victims:
        return
    victim = victims[0]
    ev = _stops.get(victim)
    if ev:
        ev.set()
    _jobs[victim]["paused"] = True
    _jobs[victim]["message"] = "Cedo el cupo a una búsqueda con más prioridad…"


def _run_city_job(city_id: str, progress: Progress | None = None, fast: bool = True) -> None:
    country = is_country_place(city_id)
    city = CITIES.get(city_id) or {"label": _place_label(city_id), "id": city_id}
    stop = _stops[city_id]

    def report(message: str) -> None:
        _log(message, city_id)
        if progress:
            progress(message)

    with crawl.job_context(slow=not fast, should_stop=stop.is_set):
        try:
            store.init()
            rate = usd_rate()
            report(f"{city['label']} · dólar blue ${rate:,.0f}.")
            if not country and not stop.is_set():
                try:
                    from .scrapers.rentals import scrape_rentals
                    from .yields import apply_yields

                    n = scrape_rentals(city_id, report, should_stop=stop.is_set, usd_ars=rate)
                    if n:
                        wanted = same_place_ids(city_id)
                        listings = store.fetch_by_cities(wanted)
                        apply_yields(listings)
                        store.update_scores(listings)
                except Exception:
                    report(f"{city['label']}: no se pudieron bajar alquileres ahora; sigo con la venta.")
            sources = [
                ("zonaprop", zonaprop.scrape),
                ("argenprop", argenprop.scrape),
                ("properati", properati.scrape),
                ("mercadolibre", mercadolibre.scrape),
            ]
            counts, paused = _scrape_one_city(
                city_id,
                city,
                sources,
                report,
                should_stop=stop.is_set,
                skip_details=fast or country,
            )
            with _lock:
                jumping = bool(_job(city_id).get("interrupt_for_fast"))
            if jumping:
                report("Cortando para búsqueda rápida…")
                with _lock:
                    _job(city_id)["counts"] = counts
            elif paused or stop.is_set():
                shown = sum(counts.values())
                report(f"{city['label']}: dejo el cupo. {shown} avisos nuevos quedan para el próximo turno.")
                with _lock:
                    job = _job(city_id)
                    job["counts"] = counts
                    job["paused"] = True
                    if "cupo" not in (job.get("message") or "").lower():
                        job["message"] = (
                            f"{city['label']}: pausado · {shown} avisos nuevos. Sigue en el próximo turno."
                        )
                    _status["counts"].update(counts)
                    _status["paused"] = True
                    _status["message"] = job["message"]
            elif country:
                now = datetime.now(timezone.utc).isoformat()
                store.set_meta("last_run", now)
                if not paused:
                    store.set_meta(f"last_complete_run:{city_id}", now)
                    store.set_meta("auto_daily", "1")
                    if not fast:
                        store.set_meta(f"last_daily_run:{city_id}", now)
                shown = sum(counts.values())
                with _lock:
                    job = _job(city_id)
                    job["counts"] = counts
                    job["last_run"] = now
                    job["paused"] = paused
                    job["message"] = (
                        f"{'Pausado' if paused else 'Listo'} · {city['label']}: "
                        f"{shown} avisos nuevos en esta pasada."
                    )
                    _status["counts"].update(counts)
                    _status["last_run"] = now
                    _status["paused"] = paused
                    _status["message"] = job["message"]
            else:
                report(
                    f"{city['label']}: calculando precios…"
                    if fast
                    else f"{city['label']}: calculando precios y oportunidades…"
                )
                wanted = same_place_ids(city_id)
                listings = store.fetch_by_cities(wanted)
                listings = enrich(listings, rate)
                for item in listings:
                    store.apply_user_edits(item)
                from .yields import apply_yields

                apply_yields(listings)
                store.update_scores(listings)
                from .market import ensure_ready, take_snapshots

                ensure_ready(listings)
                take_snapshots(listings)
                stats = summarize(listings)
                now = datetime.now(timezone.utc).isoformat()
                store.set_meta("last_run", now)
                if not paused:
                    store.set_meta(f"last_complete_run:{city_id}", now)
                    store.set_meta("auto_daily", "1")
                    if not fast:
                        store.set_meta(f"last_daily_run:{city_id}", now)
                with _lock:
                    job = _job(city_id)
                    job["counts"] = counts
                    job["last_run"] = now
                    job["paused"] = paused
                    job["message"] = (
                        f"{'Pausado' if paused else 'Listo'} · {city['label']}: "
                        f"{stats['total']} avisos, {stats['deals']} oportunidades."
                    )
                    _status["counts"].update(counts)
                    _status["last_run"] = now
                    _status["paused"] = paused
                    _status["message"] = job["message"]
        except Exception:
            with _lock:
                job = _job(city_id)
                job["error"] = f"Error en {city['label']}"
                job["message"] = f"Error en {city['label']}. Se puede reintentar."
                _status["error"] = job["error"]
                _status["message"] = job["message"]
        finally:
            jumping = False
            paused = False
            with _lock:
                job = _job(city_id)
                jumping = bool(job.pop("interrupt_for_fast", False))
                paused = bool(job.get("paused"))
                job["running"] = False
                job["mode"] = ""
                _status["running"] = any(j.get("running") for j in _jobs.values())
            note_finished(city_id, fast=fast, paused=paused or jumping)
            if jumping:
                refresh(city_id, fast=True)
            else:
                if not paused:
                    _drop_empty_listed_place(city_id)
                if not _running_ids():
                    store.set_meta("scrape_live", "0")
                _schedule_daily_next()


def _scrape_one_city(
    city_id: str,
    city: dict,
    sources: list,
    report: Progress,
    should_stop=None,
    skip_details: bool = False,
) -> tuple[dict, bool]:
    from .egress import can_fetch

    stop = should_stop or _stop.is_set
    counts: dict[str, int] = {}
    paused = False
    rate = usd_rate()
    country = is_country_place(city_id)
    slow_flag = crawl.is_slow()
    count_lock = Lock()
    work_lock = Lock()
    freshness.reset_known(store.listing_ids())
    report(f"{'País' if country else 'Lugar'}: {city['label']}")
    seen_ids: dict[str, set[str]] = {}

    def on_chunk(chunk: list[Listing], source: str = "") -> None:
        if not chunk:
            return
        new_n = 0
        with work_lock:
            if source:
                seen_ids.setdefault(source, set()).update(item.id for item in chunk)
            for item in chunk:
                if not country:
                    item.city = city_id
                    extra = dict(item.extra or {})
                    extra["search_city"] = city_id
                    item.extra = extra
                pin_listing_city(item, remote=False)
                if item.price and not item.price_usd:
                    item.price_usd = to_usd(item.price, item.currency, rate)
                apply_unit_price(item, rate)
                apply_signals(item)
                if not freshness.is_known(item.id):
                    new_n += 1
                    if not country:
                        from .llm_enrich import mark_await_llm

                        mark_await_llm(item)
            store.upsert_many(chunk)
            from .detail_fetch import enqueue as enqueue_details
            from .freshness import has_usable_listing_text
            from .llm_enrich import enqueue as enqueue_llm

            enqueue_details(chunk)
            if not country:
                ready = [item for item in chunk if has_usable_listing_text(item)]
                if ready:
                    enqueue_llm(ready, urgent=True)
        if source:
            try:
                from .ops import note as ops_note

                ops_note("ingest", n=len(chunk), source=source)
                if new_n:
                    ops_note("new", n=new_n, source=source)
            except Exception:
                pass
        key = f"{source}:{city_id}"
        with count_lock:
            counts[key] = counts.get(key, 0) + new_n
            shown = sum(counts.values())
        old_n = len(chunk) - new_n
        title = (chunk[-1].address or chunk[-1].title or "").strip()
        bit = f" · {title[:48]}" if title else ""
        if old_n and not new_n:
            report(f"{city['label']}: sin avisos nuevos, {old_n} ya estaban{bit}")
        elif old_n:
            report(
                f"{city['label']}: {new_n} nuevos, {old_n} ya bajados · {shown} nuevos en esta pasada{bit}"
            )
        else:
            report(f"{city['label']}: {new_n} nuevos · {shown} en esta pasada{bit}")

    def run_source(name, scraper) -> tuple[str, bool]:
        if stop():
            return name, True
        host = {
            "zonaprop": "www.zonaprop.com.ar",
            "argenprop": "www.argenprop.com",
            "properati": "www.properati.com.ar",
            "mercadolibre": "inmuebles.mercadolibre.com.ar",
        }.get(name, "")
        if host and not can_fetch(host):
            report(f"{city['label']}: {name} en pausa por bloqueo, sigo con otros portales.")
            with count_lock:
                counts.setdefault(f"{name}:{city_id}", 0)
            return name, False
        with crawl.job_context(slow=slow_flag, should_stop=stop):
            try:
                items = scraper(
                    report,
                    city=city_id,
                    should_stop=stop,
                    on_chunk=lambda chunk, src=name: on_chunk(chunk, src),
                )
                if not country:
                    with work_lock:
                        for item in items:
                            seen_ids.setdefault(name, set()).add(item.id)
                            extra = dict(item.extra or {})
                            extra["search_city"] = city_id
                            item.extra = extra
                            if item.city not in {"fuera"} and not item.city:
                                item.city = city_id
                            if not freshness.is_known(item.id):
                                from .llm_enrich import mark_await_llm

                                mark_await_llm(item)
                            pin_listing_city(item, remote=False)
                        store.upsert_many(items)
                    from .freshness import has_usable_listing_text
                    from .llm_enrich import enqueue as enqueue_llm

                    ready = [item for item in items if has_usable_listing_text(item)]
                    if ready:
                        enqueue_llm(ready, urgent=True)
                    if not stop():
                        dropped = _retire_unseen(city_id, name, seen_ids.get(name) or set())
                        if dropped:
                            report(
                                f"{city['label']}: saqué {dropped} avisos de {name} que ya no están en el listado."
                            )
                with count_lock:
                    counts.setdefault(f"{name}:{city_id}", 0)
                if skip_details:
                    from .detail_fetch import enqueue as enqueue_details

                    enqueue_details(items)
                    report(
                        f"{city['label']}: {counts.get(f'{name}:{city_id}', 0)} avisos nuevos. "
                        "Las fichas se siguen completando."
                    )
                    return name, False
                catalog = [
                    x
                    for x in store.fetch_by_cities({city_id})
                    if x.source == name
                ]
                report(f"{city['label']}: listado listo. Reviso fichas pendientes…")
                return name, _enrich_details(catalog, report, name, city["label"], should_stop=stop)
            except Exception as exc:
                with count_lock:
                    counts.setdefault(f"{name}:{city_id}", 0)
                if crawl.aborted() or stop() or "pausad" in str(exc).lower():
                    report(f"{city['label']}: pausado.")
                    return name, True
                report(f"{city['label']}: un origen no respondió, sigo con el resto.")
                return name, False

    report(f"Buscando avisos en {city['label']}…")
    workers = min(PORTAL_WORKERS, max(1, len(sources)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run_source, name, scraper) for name, scraper in sources]
        for fut in as_completed(futures):
            _name, source_paused = fut.result()
            paused = paused or source_paused
            if stop():
                paused = True
                break
    return counts, paused


def _needs_details(item: Listing) -> bool:
    return freshness.needs_detail_fetch(item)


def _retire_unseen(city_id: str, source: str, seen: set[str]) -> int:
    """Si el listado de un portal salió completo, borra los avisos que ya no aparecen."""
    if not city_id or not source or not seen:
        return 0
    wanted = same_place_ids(city_id) or {city_id}
    existing = [item for item in store.fetch_by_cities(wanted) if (item.source or "") == source]
    if len(seen) < max(20, int(0.5 * max(1, len(existing)))):
        return 0
    gone = [item.id for item in existing if item.id not in seen]
    if not gone:
        return 0
    return store.drop_listings(gone)


def _enrich_details(
    items: list[Listing], report: Progress, source: str, city_label: str, should_stop=None
) -> bool:
    stop = should_stop or _stop.is_set
    from .llm_enrich import enqueue

    pending = [item for item in items if _needs_details(item)]
    already = [item for item in items if not _needs_details(item)]
    if already:
        enqueue(already)
    skipped = len(already)
    total = len(pending)
    if not total:
        report(
            f"{city_label}: las fichas de este lote ya estaban bajadas."
        )
        return False
    if skipped:
        report(
            f"{city_label}: {total} fichas pendientes, {skipped} ya bajadas."
        )

    from .detail_fetch import workers as detail_workers

    workers = detail_workers()

    def _one(item: Listing) -> Listing:
        try:
            enrich_details(item, should_stop=stop)
        except Exception:
            analyze(item)
        return item

    done = 0
    with ThreadPoolExecutor(max_workers=min(workers, total)) as pool:
        futs = [pool.submit(_one, item) for item in pending]
        for fut in as_completed(futs):
            if stop():
                report(f"Pausa pedida. Fichas {city_label}: {done}/{total}.")
                return True
            try:
                item = fut.result()
            except Exception:
                continue
            if (item.extra or {}).get("gone"):
                done += 1
                continue
            store.upsert_many([item])
            enqueue([item])
            done += 1
            wait = crawl.snapshot().get("wait_s") or 0
            extra = f" · espera {wait:.0f}s" if crawl.is_slow() and wait else ""
            label = (item.address or item.title or "")[:48]
            report(f"{city_label}: ficha {done}/{total}{extra} · {label}")
    return False


_geo_lock = Lock()
_geo_inflight: set[str] = set()
_score_lock = Lock()
_score_inflight = False
_dedupe_lock = Lock()
_dedupe_inflight: set[str] = set()


def _kick_score_refresh(city_id: str | None) -> None:
    global _score_inflight

    skip = {"fuera", "otros", "argentina", ""}
    counts = store.city_listing_counts() or {}
    ids = [
        cid
        for cid, n in sorted(counts.items(), key=lambda row: -int(row[1] or 0))
        if cid not in skip and 0 < int(n or 0) < 400
    ]
    if city_id and city_id not in skip:
        ids = [city_id, *[cid for cid in ids if cid != city_id]]
    pending = [cid for cid in ids if store.get_meta(f"score_ver:{cid}") != SCORE_VERSION]
    if not pending:
        if store.get_meta("score_version") != SCORE_VERSION:
            store.set_meta("score_version", SCORE_VERSION)
        return
    with _score_lock:
        if _score_inflight:
            return
        _score_inflight = True
    target = pending[0]

    def _job() -> None:
        global _score_inflight
        try:
            store.init()
            rate = float(store.get_meta("usd_ars") or USD_FALLBACK)
            log.warning("score refresh start %s", target)
            rows = store.fetch_by_cities([target])
            scored = enrich(rows, rate, profiles=len(rows) < 350)
            for item in scored:
                store.apply_user_edits(item)
            for i in range(0, len(scored), 300):
                store.update_scores(scored[i : i + 300])
                time.sleep(0.15)
            store.set_meta(f"score_ver:{target}", SCORE_VERSION)
            left = [cid for cid in pending[1:] if store.get_meta(f"score_ver:{cid}") != SCORE_VERSION]
            if not left:
                store.set_meta("score_version", SCORE_VERSION)
            log.warning("score refresh done %s n=%s", target, len(scored))
        except Exception:
            log.exception("score refresh failed %s", target)
        finally:
            with _score_lock:
                _score_inflight = False
        time.sleep(0.25)
        _kick_score_refresh(None)

    threading.Thread(target=_job, daemon=True, name=f"score-refresh-{target}").start()


def _kick_geo_refresh(city_id: str) -> None:
    if not city_id or city_id in {"fuera", "otros"}:
        return
    from .places import ensure_osm_barrios

    ensure_osm_barrios(city_id, blocking=False)
    if store.get_meta(f"geo_ver:{city_id}") == GEO_VERSION:
        return
    with _geo_lock:
        if city_id in _geo_inflight:
            return
        _geo_inflight.add(city_id)

    def _job() -> None:
        try:
            store.init()
            log.info("geo refresh start %s", city_id)
            from .places import ensure_city_outline, hydrate_place_extent
            from .geo import apply_recovered_location, pin_listing_city

            ensure_city_outline(city_id, blocking=True)

            wanted = same_place_ids(city_id)
            wanted_list = [cid for cid in wanted if cid]
            items = store.fetch_by_cities(wanted_list) if wanted_list else []
            log.warning("geo refresh start %s n=%s", city_id, len(items))
            moved_n = 0
            chunk_size = 60
            for i in range(0, len(items), chunk_size):
                batch = []
                for item in items[i : i + chunk_size]:
                    if not item:
                        continue
                    before = (
                        item.city,
                        item.barrio,
                        item.zona,
                        item.address,
                        item.lat,
                        item.lon,
                        (item.extra or {}).get("street"),
                        (item.extra or {}).get("street_number"),
                        (item.extra or {}).get("intersection"),
                        (item.extra or {}).get("location_kind"),
                    )
                    extra = item.extra or {}
                    from .scrapers import _keep_portal_map_pin, locate_item
                    from .text_quality import is_plot_label, is_plot_street_name

                    streets = extra.get("intersection") or extra.get("between")
                    street_only = extra.get("pin_kind") == "address" and not extra.get("street_number")
                    if streets and (
                        is_plot_label(item.address or "")
                        or is_plot_street_name(str(extra.get("street") or ""))
                    ):
                        locate_item(item)
                    elif street_only:
                        locate_item(item)
                    else:
                        pin_listing_city(item, remote=False)
                        apply_recovered_location(item)
                    _keep_portal_map_pin(item)
                    after = (
                        item.city,
                        item.barrio,
                        item.zona,
                        item.address,
                        item.lat,
                        item.lon,
                        (item.extra or {}).get("street"),
                        (item.extra or {}).get("street_number"),
                        (item.extra or {}).get("intersection"),
                        (item.extra or {}).get("location_kind"),
                    )
                    if (item.city or "") not in wanted or before != after:
                        batch.append(item)
                if batch:
                    store.update_scores(batch)
                    moved_n += len(batch)
                time.sleep(0.05)
            log.warning("geo refresh %s moved %s listings out", city_id, moved_n)
            hydrate_place_extent(city_id)
            store.set_meta(f"geo_ver:{city_id}", GEO_VERSION)
            store.set_meta("geo_version", GEO_VERSION)
        except Exception:
            log.exception("geo refresh failed for %s", city_id)
        finally:
            with _geo_lock:
                _geo_inflight.discard(city_id)

    threading.Thread(target=_job, daemon=True, name=f"geo-refresh-{city_id}").start()


def _kick_dedupe(city_id: str) -> None:
    if not city_id or city_id in {"fuera", "otros", "argentina"}:
        return
    if store.get_meta(f"dedupe_ver:{city_id}") == DEDUPE_VERSION:
        return
    with _dedupe_lock:
        if city_id in _dedupe_inflight:
            return
        _dedupe_inflight.add(city_id)

    def _job() -> None:
        try:
            store.init()
            wanted = same_place_ids(city_id)
            items = store.fetch_by_cities(wanted)
            changed = collapse_duplicates(items)
            if changed:
                store.update_scores(changed)
            store.set_meta(f"dedupe_ver:{city_id}", DEDUPE_VERSION)
        except Exception:
            log.exception("dedupe failed for %s", city_id)
        finally:
            with _dedupe_lock:
                _dedupe_inflight.discard(city_id)

    threading.Thread(target=_job, daemon=True, name=f"dedupe-{city_id}").start()


def kick_llm_later(city_id: str) -> None:
    """Arranca el scan en un hilo: el GET de avisos no debería llamarlo."""
    cid = resolve_city(city_id) if city_id else ""
    if not cid:
        return
    threading.Thread(target=_kick_llm_enrich, args=(cid,), daemon=True, name=f"llm-kick-{cid}").start()


def _kick_llm_enrich(city_id: str) -> None:
    from .backfill import pump

    prefer = [city_id] if city_id and city_id not in {"fuera", "otros", "argentina"} else []
    pump(prefer_cities=prefer)


def _marketplace_links(city_id: str | None) -> list[dict]:
    if not city_id or city_id in {"fuera", "otros"}:
        return []
    cfg = CITIES.get(city_id) or {}
    label = cfg.get("label") or city_id.replace("-", " ").title()
    query = quote(f"departamento venta {label}")
    return [
        {
            "label": f"Marketplace · {label}",
            "url": f"https://www.facebook.com/marketplace/search/?query={query}",
        }
    ]


def listings_payload(live: bool = False, city: str | None = None, since: int | None = None) -> dict:
    from .listings_cache import payload as cache_payload, start_warmup

    start_warmup()
    view_city = resolve_city(city)
    data = cache_payload(view_city, since=since)
    data["live"] = bool(live)
    return data


def add_manual(payload: dict) -> Listing:
    store.init()
    source_id = str(payload.get("source_id") or payload.get("url") or datetime.now().timestamp())
    item = Listing(
        source="manual",
        source_id=source_id[-24:],
        url=str(payload.get("url") or ""),
        title=str(payload.get("title") or "Aviso cargado a mano"),
        property_type=str(payload.get("property_type") or "casa"),
        price=float(payload["price"]) if payload.get("price") else None,
        currency=str(payload.get("currency") or "USD"),
        address=str(payload.get("address") or ""),
        covered_m2=float(payload["covered_m2"]) if payload.get("covered_m2") else None,
        total_m2=float(payload["total_m2"]) if payload.get("total_m2") else None,
        bedrooms=int(payload["bedrooms"]) if payload.get("bedrooms") else None,
        bathrooms=float(payload["bathrooms"]) if payload.get("bathrooms") else None,
        description=str(payload.get("notes") or ""),
        publisher="Carga manual / Facebook",
        city=str(payload.get("city") or default_city()),
    )
    from .scrapers import locate_item

    item = locate_item(item)
    analyze(item)
    store.upsert_many([item])
    listings = enrich(store.fetch_by_cities(same_place_ids(item.city)), usd_rate())
    for row in listings:
        store.apply_user_edits(row)
    store.update_scores(listings)
    return item


def edit_listing(payload: dict) -> Listing:
    store.init()
    item = store.update_listing(payload)
    listings = enrich(store.fetch_by_cities(same_place_ids(item.city)), usd_rate())
    for row in listings:
        store.apply_user_edits(row)
    store.update_scores(listings)
    return next((row for row in listings if row.id == item.id), item)


def _parse_age_sec(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(raw)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - when).total_seconds()
    except ValueError:
        return None


def daily_city_ids() -> list[str]:
    skip = {"fuera", "otros"}
    from .listings_cache import cached_city_ids
    from .schedule import pool_ids

    ids = [cid for cid in cached_city_ids() if cid not in skip]
    extra = [cid for cid in pool_ids() if cid not in skip]
    seen = []
    for cid in [*ids, *extra]:
        if cid not in seen:
            seen.append(cid)
    return seen


def _city_daily_age_sec(city_id: str) -> float | None:
    return _parse_age_sec(
        store.get_meta(f"last_daily_run:{city_id}") or store.get_meta(f"last_complete_run:{city_id}")
    )


def next_daily_city() -> str | None:
    from .schedule import next_background_city

    return next_background_city(_running_ids())


def set_slow_crawl(_enabled: bool, _city: str | None = None) -> dict:
    return {
        **status(),
        "message": "El motor recorre el país en paralelo: primero lo que se busca en Lugar, después el resto, sin dejar a nadie sin turno.",
    }


def _schedule_daily_next() -> None:
    gen = _bump_rest("_daily")

    def later() -> None:
        time.sleep(BETWEEN_CITIES_SEC)
        if _rest_gen.get("_daily") != gen:
            return
        maybe_daily_refresh()

    threading.Thread(target=later, daemon=True, name="propmap-daily-next").start()


def _tick_background_upkeep(running: set[str] | None = None) -> None:
    """Mantenimiento de lugares ya cargados: no depende de quién esté mirando la página."""
    from .geo import DEFAULT_CITY
    from .schedule import pool_ids

    skip = {"fuera", "otros", "argentina", ""}
    ids = [cid for cid in pool_ids() if cid not in skip]
    if DEFAULT_CITY in ids:
        ids = [DEFAULT_CITY, *[cid for cid in ids if cid != DEFAULT_CITY]]
    if not ids:
        return
    busy = {cid for cid in (running or set()) if cid}
    for cid in ids:
        if cid in busy:
            continue
        if store.get_meta(f"geo_ver:{cid}") != GEO_VERSION:
            try:
                from .listings_cache import cities_loading, city_loaded_from_db

                if cities_loading() or not city_loaded_from_db(cid):
                    continue
            except Exception:
                pass
            _kick_geo_refresh(cid)
            break
    for cid in ids:
        if cid in busy:
            continue
        if store.get_meta(f"dedupe_ver:{cid}") != DEDUPE_VERSION:
            _kick_dedupe(cid)
            break
    if store.get_meta("score_version") != SCORE_VERSION:
        _kick_score_refresh(None)
    from .access import ACCESS_VERSION, kick_access_later
    from .osm_poi import POI_VERSION, ensure_city_pois

    n_access = 0
    for cid in ids:
        if n_access >= 3:
            break
        if store.get_meta(f"access_ver:{cid}") != f"{ACCESS_VERSION}:{POI_VERSION}":
            ensure_city_pois(cid, blocking=False)
            kick_access_later(cid, now=True)
            n_access += 1
    from .backfill import pump

    pump(prefer_cities=[cid for cid in (running or set()) if cid])


def maybe_daily_refresh() -> None:
    store.init()
    load_custom_places()
    running = _running_ids()
    _tick_background_upkeep(running)
    if crawl.aborted() and not running:
        return
    from .schedule import home_scrape_id, next_jobs

    try:
        set_listing_counts(store.city_listing_counts())
    except Exception:
        pass
    running = _running_ids()
    background_n = sum(1 for cid in running if (_jobs.get(cid) or {}).get("mode") != "fast")
    slots = min(MAX_JOBS - len(running), MAX_BACKGROUND - background_n)
    home = home_scrape_id()
    if slots <= 0 and home and home not in running:
        peek = next_jobs(running, slots=1)
        if peek and peek[0][0] == home:
            with _lock:
                _preempt_one_slow_locked()
    elif slots > 0:
        for city_id, fast in next_jobs(running, slots):
            label = _place_label(city_id)
            _log(f"Motor · toca {label}{' (rápido)' if fast else ''}.", city_id)
            refresh(city_id, fast=fast, interactive=False)
            running.add(city_id)
    _tick_background_upkeep(running)
    _kick_status_aux(list(running))


def start_background_scraper() -> None:
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    from .backfill import start as start_backfill

    start_backfill()

    def loop() -> None:
        time.sleep(8)
        try:
            store.init()
            store.set_meta("slow_crawl", "0")
            maybe_daily_refresh()
        except Exception:
            pass
        while True:
            try:
                maybe_daily_refresh()
            except Exception:
                pass
            time.sleep(CHECK_EVERY_SEC)

    threading.Thread(target=loop, daemon=True, name="propmap-daily").start()
