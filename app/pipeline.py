from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from threading import Event, Lock
from typing import Callable

from . import crawl, freshness, store
from .features import analyze
from .geo import (
    CITIES,
    GEO_VERSION,
    barrio_overlays,
    city_only_address,
    has_street_address,
    in_water,
    pin_listing_city,
    listing_fits_city,
    resolve_city,
)
from .http_client import fetch_json
from .models import Listing
from .places import listed_cities, load_custom_places
from .scoring import SCORE_VERSION, USD_FALLBACK, apply_unit_price, enrich, summarize, to_usd
from .scrapers import argenprop, mercadolibre, properati, zonaprop
from .scrapers.details import enrich_details

Progress = Callable[[str], None]

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
CHECK_EVERY_SEC = 60 * 60


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


def status() -> dict:
    with _lock:
        jobs = {}
        running_cities = []
        logs: list[str] = []
        for city_id, job in _jobs.items():
            jobs[city_id] = {**job, "logs": list(job["logs"][-30:])}
            if job["running"]:
                running_cities.append(city_id)
            logs.extend(job["logs"][-10:])
        message = _status["message"]
        if running_cities:
            message = _job(running_cities[-1])["message"] or message
        return {
            "running": bool(running_cities),
            "running_cities": running_cities,
            "mode": (_job(running_cities[-1]).get("mode") if running_cities else ""),
            "jobs": jobs,
            "paused": any(job.get("paused") for job in _jobs.values()),
            "message": message,
            "logs": logs[-40:] or list(_status["logs"][-40:]),
            "error": _status.get("error") or "",
            "last_run": store.get_meta("last_run", _status["last_run"]),
            "usd_ars": store.get_meta("usd_ars", str(USD_FALLBACK)),
            "counts": {
                key: value
                for job in _jobs.values()
                for key, value in (job.get("counts") or {}).items()
            },
            "crawl": {
                **crawl.snapshot(),
                "enabled": store.get_meta("slow_crawl") == "1",
                "city": store.get_meta("slow_crawl_city"),
            },
        }


def _log(message: str, city_id: str | None = None) -> None:
    with _lock:
        _status["message"] = message
        _status["logs"].append(message)
        if len(_status["logs"]) > 80:
            _status["logs"] = _status["logs"][-80:]
        if city_id:
            job = _job(city_id)
            job["message"] = message
            job["logs"].append(message)
            if len(job["logs"]) > 80:
                job["logs"] = job["logs"][-80:]


def request_pause(city: str | None = None) -> dict:
    city_id = resolve_city(city) if city else None
    crawl.request_abort()
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
        _stop.set()
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


def refresh(city: str | None = None, progress: Progress | None = None, *, fast: bool = True) -> dict:
    from .places import ensure_place

    store.init()
    load_custom_places()
    city_id = ensure_place(query=city, city=city)
    label = (CITIES.get(city_id) or {}).get("label") or city_id
    with _lock:
        job = _job(city_id)
        if job["running"]:
            if fast and job.get("mode") != "fast":
                job["interrupt_for_fast"] = True
                job["mode"] = "fast"
                job["message"] = f"Pasando a búsqueda rápida en {label}…"
                crawl.set_slow(False)
                crawl.request_abort()
                stop = _stops.get(city_id)
                if stop:
                    stop.set()
            return status()
        _bump_rest(city_id)
        job["running"] = True
        job["paused"] = False
        job["mode"] = "fast" if fast else "slow"
        job["error"] = ""
        job["logs"] = []
        job["message"] = (
            f"{'Búsqueda rápida' if fast else 'Motor de fondo'} en {label}…"
        )
        _stops[city_id] = Event()
        _status["running"] = True
        _status["paused"] = False
        _status["error"] = ""
    crawl.clear_abort()
    crawl.set_slow(not fast)
    store.set_meta("scrape_live", "1")
    threading.Thread(
        target=_run_city_job,
        args=(city_id, progress, fast),
        daemon=True,
        name=f"propmap-{'fast' if fast else 'slow'}-{city_id}",
    ).start()
    return status()


def _run_city_job(city_id: str, progress: Progress | None = None, fast: bool = True) -> None:
    city = CITIES.get(city_id) or {"label": city_id, "id": city_id}
    stop = _stops[city_id]

    def report(message: str) -> None:
        _log(message, city_id)
        if progress:
            progress(message)

    try:
        store.init()
        rate = usd_rate()
        report(f"{city['label']} · dólar blue ${rate:,.0f}.")
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
            skip_details=fast,
        )
        with _lock:
            jumping = bool(_job(city_id).get("interrupt_for_fast"))
        if jumping:
            report("Cortando para búsqueda rápida…")
            with _lock:
                _job(city_id)["counts"] = counts
        else:
            report(
                f"{city['label']}: calculando precios…"
                if fast
                else f"{city['label']}: calculando precios y oportunidades…"
            )
            listings = store.fetch_all()
            listings = enrich(listings, rate)
            for item in listings:
                store.apply_user_edits(item)
            store.update_scores(listings)
            from .market import ensure_ready, take_snapshots

            ensure_ready(listings)
            take_snapshots(listings)
            stats = summarize([x for x in listings if (x.city or "puerto-madryn") == city_id])
            now = datetime.now(timezone.utc).isoformat()
            store.set_meta("last_run", now)
            if not paused:
                store.set_meta(f"last_complete_run:{city_id}", now)
                store.set_meta("auto_daily", "1")
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
    except Exception as exc:
        with _lock:
            job = _job(city_id)
            job["error"] = str(exc)
            job["message"] = f"Error en {city['label']}: {exc}"
            _status["error"] = str(exc)
            _status["message"] = job["message"]
    finally:
        jumping = False
        with _lock:
            job = _job(city_id)
            jumping = bool(job.pop("interrupt_for_fast", False))
            job["running"] = False
            job["mode"] = ""
            _status["running"] = any(j.get("running") for j in _jobs.values())
        if jumping:
            refresh(city_id, fast=True)
        else:
            store.set_meta("scrape_live", "0")
            if store.get_meta("slow_crawl") == "1":
                _schedule_slow_pass(city_id, soon=fast)


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
    freshness.reset_known(store.listing_ids())
    report(f"Ciudad: {city['label']}")

    def on_chunk(chunk: list[Listing], source: str = "") -> None:
        if not chunk:
            return
        new_n = 0
        for item in chunk:
            item.city = city_id
            pin_listing_city(item)
            if item.price and not item.price_usd:
                item.price_usd = to_usd(item.price, item.currency, rate)
            apply_unit_price(item, rate)
            if not freshness.is_known(item.id):
                new_n += 1
        store.upsert_many(chunk)
        key = f"{source}:{city_id}"
        counts[key] = counts.get(key, 0) + new_n
        old_n = len(chunk) - new_n
        shown = sum(counts.values())
        title = (chunk[-1].address or chunk[-1].title or "").strip()
        bit = f" · {title[:48]}" if title else ""
        if old_n and not new_n:
            report(f"{source} · {city['label']}: sin avisos nuevos, {old_n} ya estaban{bit}")
        elif old_n:
            report(
                f"{source} · {city['label']}: {new_n} nuevos, {old_n} ya bajados · {shown} nuevos en esta pasada{bit}"
            )
        else:
            report(f"{source} · {city['label']}: {new_n} nuevos · {shown} en el mapa{bit}")

    for name, scraper in sources:
        if stop():
            return counts, True
        report(f"Leyendo {name} en {city['label']}…")
        try:
            items = scraper(
                report,
                city=city_id,
                should_stop=stop,
                on_chunk=lambda chunk, src=name: on_chunk(chunk, src),
            )
            for item in items:
                item.city = city_id
                pin_listing_city(item)
            store.upsert_many(items)
            counts.setdefault(f"{name}:{city_id}", 0)
            if skip_details:
                report(f"{name} · {city['label']}: listado listo · {counts[f'{name}:{city_id}']} avisos nuevos.")
            else:
                catalog = [
                    x
                    for x in store.fetch_all()
                    if x.source == name and (x.city or "puerto-madryn") == city_id
                ]
                report(f"{name} · {city['label']}: listado listo. Reviso fichas pendientes…")
                paused = _enrich_details(catalog, report, name, city["label"], should_stop=stop) or paused
        except Exception as exc:
            counts.setdefault(f"{name}:{city_id}", 0)
            if crawl.aborted() or stop() or "pausad" in str(exc).lower():
                paused = True
                report(f"{name} · {city['label']}: pausado.")
            else:
                report(f"{name} · {city['label']} no respondió: {exc}")
        if paused or stop():
            break
    return counts, paused


def _needs_details(item: Listing) -> bool:
    return freshness.needs_detail_fetch(item)


def _enrich_details(
    items: list[Listing], report: Progress, source: str, city_label: str, should_stop=None
) -> bool:
    stop = should_stop or _stop.is_set
    pending = [item for item in items if _needs_details(item)]
    skipped = len(items) - len(pending)
    total = len(pending)
    if not total:
        report(
            f"Fichas {source} · {city_label}: {skipped} ya bajadas, no las vuelvo a pedir hasta otro día."
        )
        return False
    if skipped:
        report(
            f"Fichas {source} · {city_label}: {total} pendientes, {skipped} ya bajadas (las salto)."
        )
    for index, item in enumerate(pending, start=1):
        if stop():
            report(f"Pausa pedida. Fichas {source} · {city_label}: {index - 1}/{total}.")
            return True
        try:
            enrich_details(item, should_stop=stop)
        except Exception:
            analyze(item)
        store.upsert_many([item])
        wait = crawl.snapshot().get("wait_s") or 0
        extra = f" · espera {wait:.0f}s" if crawl.is_slow() and wait else ""
        label = (item.address or item.title or "")[:48]
        report(f"Ficha {source} {index}/{total}{extra} · {label}")
    return False


def listings_payload(live: bool = False, city: str | None = None) -> dict:
    store.init()
    load_custom_places()
    items = store.fetch_all()
    rate = float(store.get_meta("usd_ars") or USD_FALLBACK)
    from .scrapers import locate_item

    city_fixed = False
    for item in items:
        apply_unit_price(item, rate)
        if pin_listing_city(item):
            city_fixed = True

    relocated = False
    geo_stale = store.get_meta("geo_version") != GEO_VERSION
    skip_relocate = live
    if not skip_relocate:
        if geo_stale:
            for item in items:
                weak_geo = in_water(item.lat, item.lon, item.city) or (
                    city_only_address(item.address)
                    and not has_street_address(item.address, item.title, item.description)
                )
                if weak_geo:
                    item.lat = item.lon = None
                    item.has_exact_location = False
                locate_item(item)
            relocated = True
        else:
            for item in items:
                if item.has_exact_location:
                    continue
                if has_street_address(item.address, item.title, item.description):
                    locate_item(item)
                    relocated = True
        needs_scores = any(item.score is None and item.price for item in items)
        if items and (
            relocated
            or needs_scores
            or store.get_meta("score_version") != SCORE_VERSION
            or geo_stale
            or city_fixed
        ):
            items = enrich(items, rate)
            for item in items:
                store.apply_user_edits(item)
            store.update_scores(items)
            store.set_meta("score_version", SCORE_VERSION)
            store.set_meta("geo_version", GEO_VERSION)
    if not live:
        from .market import ensure_ready, take_snapshots

        ensure_ready(items)
        take_snapshots(items)
    view_city = resolve_city(city) if city else None
    if view_city:
        items = [item for item in items if listing_fits_city(item, view_city)]
    stats = summarize(items)
    return {
        "listings": [item.to_public_dict() for item in items],
        "stats": stats,
        "barrios": barrio_overlays(stats.get("by_barrio"), city=view_city),
        "cities": listed_cities(items),
        "usd_ars": rate,
        "last_run": store.get_meta("last_run"),
        "facebook": [
            {
                "label": "Marketplace · Madryn",
                "url": "https://www.facebook.com/marketplace/puerto-madryn/search?query=casa%20venta",
            },
            {
                "label": "Marketplace · Trelew",
                "url": "https://www.facebook.com/marketplace/trelew/search?query=casa%20venta",
            },
            {
                "label": "Marketplace · Rawson",
                "url": "https://www.facebook.com/marketplace/rawson/search?query=casa%20venta",
            },
            {
                "label": "Marketplace · Gaiman",
                "url": "https://www.facebook.com/marketplace/gaiman/search?query=casa%20venta",
            },
            {
                "label": "Marketplace · Playa Unión",
                "url": "https://www.facebook.com/marketplace/rawson/search?query=playa%20union%20venta",
            },
            {
                "label": "Marketplace · Microcentro",
                "url": "https://www.facebook.com/marketplace/buenos-aires/search?query=departamento%20microcentro%20venta",
            },
        ],
    }


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
        city=str(payload.get("city") or "puerto-madryn"),
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


def _last_complete_age_sec() -> float | None:
    raw = store.get_meta("last_complete_run") or store.get_meta("last_run")
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(raw)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - when).total_seconds()
    except ValueError:
        return None


def set_slow_crawl(enabled: bool, city: str | None = None) -> dict:
    from .places import ensure_place

    store.init()
    load_custom_places()
    if enabled:
        city_id = ensure_place(query=city, city=city)
        store.set_meta("slow_crawl", "1")
        store.set_meta("slow_crawl_city", city_id)
        with _lock:
            already = bool(_job(city_id).get("running"))
        if already:
            return status()
        refresh(city_id, fast=False)
    else:
        city_id = resolve_city(city) if city else None
        city_id = city_id or store.get_meta("slow_crawl_city")
        store.set_meta("slow_crawl", "0")
        crawl.set_slow(False)
        if city_id:
            _bump_rest(city_id)
            with _lock:
                job = _jobs.get(city_id)
                pause_slow = bool(job and job.get("running") and job.get("mode") == "slow")
            if pause_slow:
                request_pause(city_id)
    return status()


def _schedule_slow_pass(city_id: str, soon: bool = False) -> None:
    if store.get_meta("slow_crawl") != "1":
        return
    target = store.get_meta("slow_crawl_city") or city_id
    if target != city_id:
        return
    rest = crawl.rest_seconds(soon=soon)
    label = (CITIES.get(city_id) or {}).get("label") or city_id
    gen = _bump_rest(city_id)
    if soon:
        _log(f"Motor de fondo · {label}: en {max(1, int(rest))} s sigue juntando avisos.", city_id)
    else:
        minutes = max(1, int(rest / 60))
        _log(f"Motor de fondo · {label}: descanso {minutes} min y sigue juntando avisos.", city_id)

    def later() -> None:
        left = rest
        while left > 0:
            if store.get_meta("slow_crawl") != "1":
                return
            if _rest_gen.get(city_id) != gen:
                return
            time.sleep(min(2.0, left))
            left -= 2.0
        if store.get_meta("slow_crawl") != "1":
            return
        if _rest_gen.get(city_id) != gen:
            return
        with _lock:
            if _job(city_id).get("running"):
                return
        refresh(city_id, fast=False)

    threading.Thread(target=later, daemon=True, name="propmap-slow-rest").start()


def _resume_slow_if_needed() -> None:
    if store.get_meta("slow_crawl") != "1":
        return
    city = store.get_meta("slow_crawl_city") or "puerto-madryn"
    refresh(city, fast=False)


def maybe_daily_refresh() -> None:
    if store.get_meta("slow_crawl") == "1":
        return
    if store.get_meta("auto_daily") != "1":
        return
    with _lock:
        if any(job.get("running") for job in _jobs.values()):
            return
    age = _last_complete_age_sec()
    if age is None or age > DAILY_SEC:
        for city_id, cfg in CITIES.items():
            if not cfg.get("builtin"):
                continue
            with _lock:
                if _job(city_id)["running"]:
                    continue
            refresh(city_id, fast=False)
            time.sleep(1)


def start_background_scraper() -> None:
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True

    def loop() -> None:
        time.sleep(8)
        try:
            _resume_slow_if_needed()
        except Exception:
            pass
        while True:
            try:
                maybe_daily_refresh()
            except Exception:
                pass
            time.sleep(CHECK_EVERY_SEC)

    threading.Thread(target=loop, daemon=True, name="propmap-daily").start()
