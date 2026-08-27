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
    note_view,
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
    if _PORTAL_RE.search(raw) or re.search(r"\b(leyendo|ficha|fichas|listado listo)\b", probe):
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
CHECK_EVERY_SEC = 45
BETWEEN_CITIES_SEC = 12
MAX_JOBS = 3
MAX_BACKGROUND = 2


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


def status() -> dict:
    with _lock:
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
            "last_run": store.get_meta("last_run", _status["last_run"]),
            "usd_ars": store.get_meta("usd_ars", str(USD_FALLBACK)),
            "counts": {
                "avisos": sum(sum((job.get("counts") or {}).values()) for job in _jobs.values())
            },
            "crawl": {
                **crawl.snapshot(),
                "enabled": True,
                "daily": True,
            },
        }
        from .listings_cache import current_rev

        payload["listings_rev"] = current_rev()
        from .llm_enrich import queue_stats
        from .detail_fetch import queue_stats as detail_stats

        payload["llm"] = queue_stats()
        payload["details"] = detail_stats()
        running_copy = list(running_cities)
    payload["queue"] = queue_public(set(running_copy))
    payload["crawl"]["city"] = next_daily_city()
    payload["crawl"]["cities"] = daily_city_ids()
    return payload


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
            return status()
        if len(running) >= MAX_JOBS:
            if fast:
                _preempt_one_slow_locked()
            else:
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
    return status()


def _preempt_one_slow_locked() -> None:
    slow = [cid for cid, job in _jobs.items() if job.get("running") and job.get("mode") != "fast"]
    if not slow:
        return
    victim = slow[0]
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
                        listings = [x for x in store.fetch_all() if (x.city or "") in wanted]
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
                listings = [x for x in store.fetch_all() if (x.city or "") in wanted]
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
    stop = should_stop or _stop.is_set
    counts: dict[str, int] = {}
    paused = False
    rate = usd_rate()
    country = is_country_place(city_id)
    slow_flag = crawl.is_slow()
    count_lock = Lock()
    freshness.reset_known(store.listing_ids())
    report(f"{'País' if country else 'Lugar'}: {city['label']}")

    def on_chunk(chunk: list[Listing], source: str = "") -> None:
        if not chunk:
            return
        new_n = 0
        for item in chunk:
            if not country:
                item.city = city_id
                extra = dict(item.extra or {})
                extra["search_city"] = city_id
                item.extra = extra
            pin_listing_city(item)
            if item.price and not item.price_usd:
                item.price_usd = to_usd(item.price, item.currency, rate)
            apply_unit_price(item, rate)
            if not freshness.is_known(item.id):
                new_n += 1
                if not country:
                    from .llm_enrich import mark_await_llm

                    mark_await_llm(item)
        store.upsert_many(chunk)
        from .detail_fetch import enqueue as enqueue_details

        enqueue_details(chunk)
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
        with crawl.job_context(slow=slow_flag, should_stop=stop):
            try:
                items = scraper(
                    report,
                    city=city_id,
                    should_stop=stop,
                    on_chunk=lambda chunk, src=name: on_chunk(chunk, src),
                )
                if not country:
                    for item in items:
                        extra = dict(item.extra or {})
                        extra["search_city"] = city_id
                        item.extra = extra
                        if item.city not in {"fuera"} and not item.city:
                            item.city = city_id
                        if not freshness.is_known(item.id):
                            from .llm_enrich import mark_await_llm

                            mark_await_llm(item)
                        pin_listing_city(item)
                    store.upsert_many(items)
                with count_lock:
                    counts.setdefault(f"{name}:{city_id}", 0)
                if skip_details:
                    from .detail_fetch import enqueue as enqueue_details

                    enqueue_details(items)
                    report(
                        f"{city['label']}: {counts.get(f'{name}:{city_id}', 0)} avisos nuevos. "
                        "Siguen cargando en segundo plano."
                    )
                    return name, False
                catalog = [
                    x
                    for x in store.fetch_all()
                    if x.source == name and (x.city or default_city()) == city_id
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
    with ThreadPoolExecutor(max_workers=min(4, len(sources) or 1)) as pool:
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

    workers = 3 if not crawl.is_slow() else 1

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
_llm_lock = Lock()
_llm_kicked: set[str] = set()
_dedupe_lock = Lock()
_dedupe_inflight: set[str] = set()


def _kick_score_refresh(city_id: str | None) -> None:
    global _score_inflight
    if store.get_meta("score_version") == SCORE_VERSION:
        return
    with _score_lock:
        if _score_inflight:
            return
        _score_inflight = True

    def _job() -> None:
        global _score_inflight
        try:
            store.init()
            items = store.fetch_all()
            rate = float(store.get_meta("usd_ars") or USD_FALLBACK)
            if city_id:
                wanted = same_place_ids(city_id)
                scoped = [item for item in items if (item.city or "") in wanted]
            else:
                scoped = items
            scoped = enrich(scoped, rate)
            for item in scoped:
                store.apply_user_edits(item)
            store.update_scores(scoped)
            store.set_meta("score_version", SCORE_VERSION)
        except Exception:
            pass
        finally:
            with _score_lock:
                _score_inflight = False

    threading.Thread(target=_job, daemon=True, name="score-refresh").start()


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
            from .scrapers import locate_item
            from .places import hydrate_place_extent

            hydrate_place_extent(city_id)
            from .geo import barrio_belongs_to_city, foreign_locality, in_city_radius, listing_mentions_city, needs_address_repin, pin_listing_city

            items = store.fetch_all()
            rate = float(store.get_meta("usd_ars") or USD_FALLBACK)
            wanted = same_place_ids(city_id)
            strays = []
            for item in items:
                if (item.city or "") in wanted:
                    continue
                if (item.city or "") not in {"fuera", "otros", "argentina", "buenos-aires"}:
                    continue
                in_here = item.lat is not None and item.lon is not None and in_city_radius(item.lat, item.lon, city_id)
                mentions = listing_mentions_city(item, city_id)
                barrio_here = barrio_belongs_to_city(item, city_id)
                if not mentions and not in_here:
                    continue
                if foreign_locality(item, city_id, remote=False) and not in_here and not (mentions and barrio_here):
                    continue
                item.city = city_id
                extra = dict(item.extra or {})
                extra.setdefault("search_city", city_id)
                item.extra = extra
                strays.append(item)
            scoped = [item for item in items if (item.city or "") in wanted] + strays
            to_pin = []
            seen: set[str] = set()
            for item in strays:
                seen.add(item.id)
                here = item.lat is not None and item.lon is not None and in_city_radius(item.lat, item.lon, city_id)
                if item.lat is None or needs_address_repin(item) or not here:
                    to_pin.append(item)
                else:
                    pin_listing_city(item)
            for item in scoped:
                if item.id in seen:
                    continue
                if item.lat is None or needs_address_repin(item):
                    to_pin.append(item)
            for item in to_pin:
                locate_item(item)
            scoped = enrich(scoped, rate)
            for item in scoped:
                store.apply_user_edits(item)
            store.update_scores(scoped)
            store.set_meta(f"geo_ver:{city_id}", GEO_VERSION)
            store.set_meta("score_version", SCORE_VERSION)
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
            items = [item for item in store.fetch_all() if (item.city or "") in wanted]
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


def _kick_llm_enrich(city_id: str) -> None:
    from .detail_fetch import enqueue as enqueue_details
    from .freshness import needs_detail_fetch
    from .llm_enrich import LLM_SCHEMA, enabled, enqueue as enqueue_llm, needs_improve

    if not enabled() or not city_id or city_id in {"fuera", "otros", "argentina"}:
        return
    with _llm_lock:
        if city_id in _llm_kicked:
            return
        _llm_kicked.add(city_id)

    def _job() -> None:
        try:
            store.init()
            pending_details = []
            pending_llm = []
            changed = []

            for item in store.fetch_by_cities(same_place_ids(city_id)):
                extra = dict(item.extra or {})
                ready = extra.get("llm_ver") == LLM_SCHEMA and extra.get("llm_ready") and not extra.get("llm_partial")
                if ready:
                    continue
                if not extra.get("search_city"):
                    extra["search_city"] = city_id
                    item.extra = extra
                    changed.append(item)
                if not needs_improve(item):
                    continue
                if needs_detail_fetch(item):
                    pending_details.append(item)
                else:
                    pending_llm.append(item)
            if changed:
                store.upsert_many(changed)
            enqueue_details(pending_details)
            enqueue_llm(pending_llm)
            log.info(
                "llm kick %s: %s fichas, %s a limpiar",
                city_id,
                len(pending_details),
                len(pending_llm),
            )
        except Exception:
            log.exception("llm enrich kick failed for %s", city_id)
            with _llm_lock:
                _llm_kicked.discard(city_id)

    threading.Thread(target=_job, daemon=True, name=f"llm-enrich-{city_id}").start()


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
    from .listings_cache import cache_ready, payload as cache_payload, start_warmup

    start_warmup()
    view_city = resolve_city(city)
    data = cache_payload(view_city, since=since)
    if view_city and not live:
        threading.Thread(target=note_view, args=(view_city,), daemon=True, name="note-view").start()
    if view_city and not data.get("warming"):
        if cache_ready():
            _kick_geo_refresh(view_city)
            _kick_score_refresh(view_city)
            _kick_dedupe(view_city)
        _kick_llm_enrich(view_city)
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
    listings = enrich(store.fetch_all(), usd_rate())
    for row in listings:
        store.apply_user_edits(row)
    store.update_scores(listings)
    return item


def edit_listing(payload: dict) -> Listing:
    store.init()
    item = store.update_listing(payload)
    listings = enrich(store.fetch_all(), usd_rate())
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


def maybe_daily_refresh() -> None:
    store.init()
    load_custom_places()
    if crawl.aborted() and not _running_ids():
        return
    from .listings_cache import city_counts
    from .schedule import next_jobs

    try:
        set_listing_counts(city_counts())
    except Exception:
        pass
    running = _running_ids()
    background_n = sum(1 for cid in running if (_jobs.get(cid) or {}).get("mode") != "fast")
    slots = min(MAX_JOBS - len(running), MAX_BACKGROUND - background_n)
    if slots <= 0:
        return
    for city_id, fast in next_jobs(running, slots):
        label = _place_label(city_id)
        _log(f"Motor · toca {label}{' (rápido)' if fast else ''}.", city_id)
        refresh(city_id, fast=fast, interactive=False)
        running.add(city_id)


def start_background_scraper() -> None:
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True

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
