from __future__ import annotations

import logging
import os
import threading
import time
from bisect import bisect_left, bisect_right
from typing import Any

from .geo import (
    has_direccion_fields,
    has_interseccion_fields,
    listing_coords_are_exact,
    listing_location_data,
    location_is_precise,
)
from .models import Listing
from .osm_poi import CAT_LABEL, POI_VERSION, SCORE_GROUPS, WALK_KM, WALK_KM_MAX, around, city_pois, walk_km_for_city

log = logging.getLogger(__name__)
ACCESS_VERSION = "4"

NEAR_N = {
    "health": 5,
    "police": 3,
    "transport": 8,
    "subway": 4,
    "train": 4,
    "beach": 3,
    "plaza": 5,
    "shop": 5,
    "school": 4,
}
MAX_NEARBY = 32
LIST_ORDER = ("subway", "train", "transport", "shop", "health", "school", "plaza", "beach", "police")


def pin_grade(item: Listing) -> str:
    extra = item.extra or {}
    kind = str(extra.get("location_kind") or "").strip().lower()
    if item.lat is None or item.lon is None:
        return "unknown"
    data = listing_location_data(item)
    if location_is_precise(data):
        if has_direccion_fields(data):
            return "exact"
        if has_interseccion_fields(data):
            return "intersection"
        return "intersection" if kind == "intersection" else "exact"
    exact = listing_coords_are_exact(item.source, bool(item.has_exact_location), kind)
    if exact:
        return "intersection" if kind == "intersection" else "exact"
    return "approx"


def pin_is_precise(item: Listing) -> bool:
    return pin_grade(item) in {"exact", "intersection"}


def access_fingerprint(item: Listing, walk_km: float | None = None) -> str:
    try:
        lat = round(float(item.lat), 5)
        lon = round(float(item.lon), 5)
    except (TypeError, ValueError):
        lat = lon = 0.0
    walk = walk_km if walk_km is not None else walk_km_for_city(item.city)
    return f"{POI_VERSION}:{ACCESS_VERSION}:{round(float(walk), 2)}:{lat}:{lon}"


def _empty_access(grade: str, reason: str, walk_km: float | None = None) -> dict[str, Any]:
    walk = WALK_KM if walk_km is None else float(walk_km)
    return {
        "pin_grade": grade,
        "precise": False,
        "method": "pin",
        "categories": {},
        "nearby": [],
        "score": None,
        "reason": reason,
        "walk_km": walk,
        "walk_count": 0,
        "fp": "",
    }


def _row_out(category: str, dist: float, poi: dict[str, Any]) -> dict[str, Any]:
    label = CAT_LABEL.get(category, category)
    return {
        "category": category,
        "label": label,
        "kind": poi.get("kind") or label,
        "name": poi.get("name") or poi.get("kind") or label,
        "km": round(dist, 3),
        "lat": poi.get("lat"),
        "lon": poi.get("lon"),
    }


def _gather_nearby(lat: float, lon: float, walk: float, pois: dict[str, list[dict]]) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    cats: dict[str, Any] = {}
    nearby: list[dict[str, Any]] = []
    for key in NEAR_N:
        hits = around(lat, lon, key, walk, cats=pois, limit=NEAR_N[key])
        if not hits:
            cats[key] = {"present": False, "km": None, "name": "", "kind": "", "score": None}
            continue
        km, poi = hits[0]
        cats[key] = {
            "present": True,
            "km": round(km, 3),
            "name": poi.get("name") or "",
            "kind": poi.get("kind") or CAT_LABEL[key],
            "score": round(100 * (1 - km / walk), 1),
        }
        for dist, row in hits:
            nearby.append(_row_out(key, dist, row))
    nearby.sort(key=lambda row: (row["km"], LIST_ORDER.index(row["category"]) if row["category"] in LIST_ORDER else 9))
    nearby = nearby[:MAX_NEARBY]
    groups_hit = 0
    for keys in SCORE_GROUPS.values():
        if any(cats.get(key, {}).get("present") for key in keys):
            groups_hit += 1
    return cats, nearby, groups_hit


def _raw_access(cats: dict[str, Any], walk: float) -> float:
    """Cercanía media: 100 si cada grupo está en la puerta, 0 si falta o está en el borde."""
    span = max(0.05, float(walk))
    parts: list[float] = []
    for keys in SCORE_GROUPS.values():
        best: float | None = None
        for key in keys:
            row = cats.get(key) or {}
            if not row.get("present") or row.get("km") is None:
                continue
            km = float(row["km"])
            if best is None or km < best:
                best = km
        if best is None:
            parts.append(0.0)
            continue
        ratio = min(1.0, max(0.0, best / span))
        parts.append(100.0 * (1.0 - ratio))
    if not parts:
        return 0.0
    return round(sum(parts) / len(parts), 1)


def _sample_points(pois: dict[str, list[dict]]) -> list[tuple[float, float]]:
    """Un centro de manzana por celda, para armar la referencia de esa ciudad."""
    cells: dict[tuple[int, int], None] = {}
    for key in ("shop", "plaza", "school", "health", "transport", "subway"):
        for row in pois.get(key) or []:
            try:
                lat = float(row["lat"])
                lon = float(row["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            cells[(int(lat / 0.007), int(lon / 0.007))] = None
    pts = [(round(lat * 0.007 + 0.0035, 5), round(lon * 0.007 + 0.0035, 5)) for lat, lon in cells]
    if len(pts) <= 140:
        return pts
    step = len(pts) / 140.0
    return [pts[int(i * step)] for i in range(140)]


_curves: dict[tuple, list[float]] = {}


def _city_curve(pois: dict[str, list[dict]], walk: float) -> list[float]:
    samples: list[float] = []
    for lat, lon in _sample_points(pois):
        cats, _nearby, groups_hit = _gather_nearby(lat, lon, walk, pois)
        if not groups_hit:
            continue
        samples.append(_raw_access(cats, walk))
    samples.sort()
    return samples


def _curve_for(city_id: str | None, pois: dict[str, list[dict]], walk: float) -> list[float]:
    if os.environ.get("PROPMAP_TEST") == "1":
        return _city_curve(pois, walk)
    n = 0
    for key in ("shop", "plaza", "school", "health", "transport", "subway"):
        n += len(pois.get(key) or [])
    key = ((city_id or "").strip(), round(float(walk), 2), n)
    hit = _curves.get(key)
    if hit is not None:
        return hit
    curve = _city_curve(pois, walk)
    if key[0] and len(curve) >= 12:
        _curves[key] = curve
    return curve


def _city_percentile(raw: float, curve: list[float]) -> float | None:
    """50 es un punto típico de esta ciudad. No llega a 100: el techo era el problema."""
    n = len(curve)
    if n < 12:
        return None
    below = bisect_left(curve, raw)
    ties = bisect_right(curve, raw) - below
    rank = below + ties / 2.0
    pct = 100.0 * (rank + 0.5) / (n + 1)
    return round(min(96.0, max(4.0, pct)), 1)


def _nearest_amenity_km(lat: float, lon: float, pois: dict[str, list[dict]] | None) -> float | None:
    from .geo import distance_km

    best = None
    for key in ("shop", "plaza", "transport", "school", "health"):
        for row in (pois or {}).get(key) or []:
            try:
                dist = distance_km(lat, lon, float(row["lat"]), float(row["lon"]))
            except (KeyError, TypeError, ValueError):
                continue
            if best is None or dist < best:
                best = dist
    return best


def compute_access(item: Listing, pois: dict[str, list[dict]] | None = None) -> dict[str, Any]:
    grade = pin_grade(item)
    walk = walk_km_for_city(item.city, pois)
    if not pin_is_precise(item) or item.lat is None or item.lon is None:
        if grade == "approx":
            return _empty_access(grade, "pin aproximado: no se miden distancias a la puerta", walk)
        if grade == "unknown":
            return _empty_access(grade, "sin coordenadas", walk)
        return _empty_access(grade, "hace falta ubicación exacta", walk)
    pois = pois if pois is not None else city_pois(item.city)
    lat, lon = float(item.lat), float(item.lon)
    cats, nearby, groups_hit = _gather_nearby(lat, lon, walk, pois)
    have_pois = any(pois.get(key) for key in pois)
    if have_pois and not groups_hit:
        nearest = _nearest_amenity_km(lat, lon, pois)
        if nearest is not None:
            expanded = min(WALK_KM_MAX, max(walk, round(nearest * 1.2, 2)))
            if expanded > walk + 0.04:
                walk = expanded
                cats, nearby, groups_hit = _gather_nearby(lat, lon, walk, pois)
    meters = int(round(walk * 1000))
    raw = _raw_access(cats, walk) if groups_hit else 0.0
    if not have_pois:
        score = None
        reason = "todavía no bajamos del mapa lo que hay a pie en esta ciudad"
    elif not groups_hit:
        score = 0.0
        reason = f"nada de esto a menos de {meters} m"
    else:
        relative = _city_percentile(raw, _curve_for(item.city, pois, walk))
        if relative is None:
            score = raw
            reason = f"{len(nearby)} lugares a menos de {meters} m"
        else:
            score = relative
            reason = f"respecto de esta ciudad · {len(nearby)} lugares a menos de {meters} m"
    return {
        "pin_grade": grade,
        "precise": True,
        "method": "pin",
        "categories": cats,
        "nearby": nearby,
        "score": score,
        "score_raw": raw if have_pois else None,
        "reason": reason,
        "walk_km": walk,
        "walk_count": groups_hit,
        "fp": access_fingerprint(item, walk),
    }


def _stored_access(item: Listing) -> dict[str, Any]:
    extra = item.extra or {}
    stored = extra.get("access")
    return stored if isinstance(stored, dict) else {}


def stored_access_ok(item: Listing, stored: dict[str, Any] | None = None) -> bool:
    """True si el aviso ya tiene cercanías (o la cuenta de que no hay nada a pie)."""
    blob = stored if stored is not None else _stored_access(item)
    walk = blob.get("walk_km") if isinstance(blob, dict) else None
    try:
        walk_f = float(walk) if walk is not None else None
    except (TypeError, ValueError):
        walk_f = None
    if not blob or blob.get("fp") != access_fingerprint(item, walk_f):
        return False
    if blob.get("precise") is False:
        return True
    if blob.get("nearby"):
        return True
    return blob.get("score") is not None


def access_needs_refresh(item: Listing) -> bool:
    if not pin_is_precise(item):
        return False
    return not stored_access_ok(item)


_access_lock = threading.Lock()
_access_inflight: set[str] = set()
_access_at: dict[str, float] = {}
_access_run = threading.Semaphore(1)
ACCESS_COOLDOWN_SEC = 180.0
ACCESS_TURN_SEC = 30.0


def _publish_poi_axes(items: list[Listing]) -> None:
    axes: dict[str, dict[str, Any]] = {}
    cities: set[str] = set()
    for item in items:
        axis = ((item.extra or {}).get("profile") or {}).get("axes", {}).get("servicios")
        if not isinstance(axis, dict) or axis.get("score") is None:
            continue
        axes[item.id] = {
            "score": axis.get("score"),
            "confidence": axis.get("confidence") or "high",
            "note": axis.get("note") or "",
        }
        if item.city:
            cities.add(item.city)
    if not axes:
        return
    try:
        from .listings_cache import remember_poi_axes, schedule_pin_flush

        remember_poi_axes(axes)
        for cid in cities:
            schedule_pin_flush(cid)
    except Exception:
        return


def refresh_city_access(city_id: str) -> int:
    """Calcula cercanías y el eje de POIs a pie para avisos con pin preciso."""
    if not city_id or city_id in {"fuera", "otros"}:
        return 0
    from . import store
    from .geo import same_place_ids
    from .osm_poi import ensure_city_pois
    from .profile import compute_profile

    store.init()
    pois = ensure_city_pois(city_id, blocking=True)
    if not any(pois.get(key) for key in pois):
        return 0
    wanted = [cid for cid in same_place_ids(city_id) if cid]
    items = store.fetch_by_cities(wanted or [city_id])
    dirty: list[Listing] = []
    updated = 0
    for item in items:
        if os.environ.get("PROPMAP_TEST") == "1":
            break
        if not access_needs_refresh(item):
            continue
        extra = dict(item.extra or {})
        profile = compute_profile(item, pois)
        extra["profile"] = profile
        extra["access"] = profile.get("access") or extra.get("access")
        extra["pin_grade"] = profile.get("pin_grade")
        item.extra = extra
        dirty.append(item)
        if len(dirty) >= 80:
            store.update_extras(dirty)
            _publish_poi_axes(dirty)
            try:
                from .listings_cache import ingest

                ingest(dirty)
            except Exception:
                pass
            updated += len(dirty)
            dirty = []
            # Sin esta pausa el ingest se queda con el candado del cache y el
            # watchdog cree que la API está trabada.
            time.sleep(0.05)
    if dirty:
        store.update_extras(dirty)
        _publish_poi_axes(dirty)
        try:
            from .listings_cache import ingest

            ingest(dirty)
        except Exception:
            pass
        updated += len(dirty)
    store.set_meta(f"access_ver:{city_id}", f"{ACCESS_VERSION}:{POI_VERSION}")
    return updated


def kick_access_later(city_id: str | None, *, now: bool = False) -> None:
    """Un refresh recorre toda la ciudad: sin enfriamiento, cada aviso que entra
    dispara otra pasada completa y el cache queda sin candado libre."""
    cid = (city_id or "").strip()
    if not cid or cid in {"fuera", "otros"} or os.environ.get("PROPMAP_TEST") == "1":
        return
    last = _access_at.get(cid) or 0.0
    with _access_lock:
        if cid in _access_inflight:
            return
        if not now and time.monotonic() - last < ACCESS_COOLDOWN_SEC:
            return
        _access_inflight.add(cid)

    def _job() -> None:
        # De a una ciudad: varias pasadas juntas leen la base entera en paralelo
        # y no dejan respirar al cache ni a la API.
        turn = _access_run.acquire(timeout=ACCESS_TURN_SEC)
        try:
            if not turn:
                return
            n = refresh_city_access(cid)
            if n:
                log.info("access refresh %s n=%s", cid, n)
        except Exception:
            log.exception("access refresh %s", cid)
        finally:
            if turn:
                _access_run.release()
            with _access_lock:
                if turn:
                    _access_at[cid] = time.monotonic()
                _access_inflight.discard(cid)

    threading.Thread(target=_job, daemon=True, name=f"access-{cid}").start()


def _stamp_access(item: Listing, access: dict[str, Any]) -> dict[str, Any]:
    from .profile import _servicios_axis, stamp_profile

    extra = dict(item.extra or {})
    stored = {key: value for key, value in access.items() if key not in {"pending", "city"}}
    extra["access"] = stored
    extra["pin_grade"] = stored.get("pin_grade") or pin_grade(item)
    profile = dict(extra.get("profile") or {})
    axes = dict(profile.get("axes") or {})
    axis = _servicios_axis(item, stored)
    axes["servicios"] = axis
    profile["axes"] = axes
    profile["access"] = stored
    extra["profile"] = stamp_profile(profile)
    item.extra = extra
    return axis if isinstance(axis, dict) else {}


def _save_access(item: Listing, access: dict[str, Any]) -> None:
    from . import store

    axis = _stamp_access(item, access)
    store.update_extras([item])
    if axis.get("score") is not None:
        try:
            from .listings_cache import remember_poi_axes, schedule_pin_flush

            remember_poi_axes({item.id: axis})
            schedule_pin_flush(item.city or "")
        except Exception:
            pass
    try:
        from .listings_cache import ingest

        ingest([item])
    except Exception:
        pass


def near_payload(
    *,
    listing_id: str = "",
    city: str = "",
    lat: float | None = None,
    lon: float | None = None,
) -> dict[str, Any]:
    """Lee cercanías ya guardadas en la ficha. Si faltan, encola el proceso que las escribe."""
    from . import store

    store.init()
    lid = (listing_id or "").strip()
    cid = (city or "").strip()
    row = store.get_listing(lid) if lid else None
    if row is not None:
        item = row
        cid = item.city or cid
        if item.lat is not None and item.lon is not None:
            lat, lon = float(item.lat), float(item.lon)
    else:
        item = Listing(
            source="pin",
            source_id="near",
            url="",
            title="",
            property_type="",
            lat=lat,
            lon=lon,
            city=cid,
            has_exact_location=True,
            extra={"location_kind": "exact"},
        )
    stored = _stored_access(item)
    if stored_access_ok(item, stored):
        out = dict(stored)
        out["pending"] = False
        out["city"] = cid
        return out
    # No calcular acá: un score armado en la lectura no está en la ficha y
    # el pentágono muestra un número que desaparece al recargar.
    out = dict(stored) if stored else {}
    out["pending"] = True
    out["score"] = None
    out["city"] = cid
    out["nearby"] = list(stored.get("nearby") or [])
    out["reason"] = "el proceso de la ficha todavía no guardó los POIs"
    return out


_poi_lock = threading.Lock()
_poi_queue: list[str] = []
_poi_seen: set[str] = set()
_poi_started = False
_synced_cities: set[str] = set()
_sync_lock = threading.Lock()
_poi_publish_lock = threading.Lock()
POI_BATCH = 8


def score_listings(items: list[Listing]) -> int:
    """Calcula el eje de POIs de estos avisos y lo deja guardado."""
    if not items:
        return 0
    from .osm_poi import city_pois, ensure_city_pois

    grouped: dict[str, list[Listing]] = {}
    for item in items:
        if not access_needs_refresh(item):
            continue
        grouped.setdefault(item.city or "", []).append(item)
    dirty: list[Listing] = []
    overlay: dict[str, dict[str, Any]] = {}
    cities: set[str] = set()
    for cid, group in grouped.items():
        if not cid or cid in {"fuera", "otros"}:
            continue
        pois = city_pois(cid)
        if not any(pois.get(key) for key in pois):
            if os.environ.get("PROPMAP_TEST") == "1":
                continue
            ensure_city_pois(cid, blocking=False)
            continue
        for item in group:
            live = compute_access(item, pois)
            if live.get("precise") and live.get("score") is None:
                continue
            axis = _stamp_access(item, live)
            dirty.append(item)
            cities.add(cid)
            if axis.get("score") is not None:
                overlay[item.id] = axis
    if not dirty:
        return 0
    from . import store

    store.update_extras(dirty)
    try:
        from .listings_cache import ingest, remember_poi_axes, schedule_pin_flush

        if overlay:
            remember_poi_axes(overlay)
        for cid in cities:
            schedule_pin_flush(cid)
        ingest(dirty)
    except Exception:
        log.exception("no pude publicar el score de POIs")
    return len(dirty)


def enqueue_poi_score(items: Listing | list[Listing] | None) -> None:
    """Encola el score de POIs sin esperar a que alguien abra el aviso."""
    if items is None or os.environ.get("PROPMAP_TEST") == "1":
        return
    batch = items if isinstance(items, list) else [items]
    ids: list[str] = []
    for item in batch:
        if item is None or not pin_is_precise(item) or not access_needs_refresh(item):
            continue
        ids.append(item.id)
    if not ids:
        return
    with _poi_lock:
        for lid in ids:
            if lid in _poi_seen:
                continue
            _poi_seen.add(lid)
            _poi_queue.append(lid)
        _ensure_poi_worker()


def publish_known_poi_scores(prefer_city: str = "") -> int:
    """Trae a la lista los scores que ya están en la base y el snap no muestra.

    Una ciudad por llamada: el planificador y el backfill no se quedan
    escaneando todas las fichas antes de arrancar el scraping.
    """
    if os.environ.get("PROPMAP_TEST") == "1":
        return 0
    if not _poi_publish_lock.acquire(blocking=False):
        return 0
    try:
        return _publish_one_city_poi_scores(prefer_city)
    finally:
        _poi_publish_lock.release()


def _publish_one_city_poi_scores(prefer_city: str) -> int:
    from . import store
    from .geo import DEFAULT_CITY, same_place_ids
    from .listings_cache import cached_city_ids, remember_poi_axes, schedule_pin_flush

    cities: list[str] = []
    for cid in (prefer_city, DEFAULT_CITY, *cached_city_ids()):
        token = (cid or "").strip()
        if not token or token in cities or token in {"fuera", "otros", "argentina"}:
            continue
        cities.append(token)
    for cid in cities:
        with _sync_lock:
            if cid in _synced_cities:
                continue
        try:
            wanted = [token for token in (same_place_ids(cid) or [cid]) if token]
            axes = store.poi_axes_for_cities(wanted or [cid])
        except Exception:
            log.exception("no pude leer scores de POIs de %s", cid)
            with _sync_lock:
                _synced_cities.add(cid)
            return 0
        with _sync_lock:
            _synced_cities.add(cid)
        if axes:
            remember_poi_axes(axes)
        schedule_pin_flush(cid)
        return len(axes)
    return 0


def refill_poi(prefer_city: str = "") -> int:
    if os.environ.get("PROPMAP_TEST") == "1":
        return 0
    published = publish_known_poi_scores(prefer_city)
    try:
        from .listings_cache import flush_pin_scores

        flush_pin_scores()
    except Exception:
        log.exception("flush pines POI")
    _ensure_poi_worker()
    with _poi_lock:
        room = max(0, 40 - len(_poi_queue))
    if room <= 0:
        return published
    from . import store

    try:
        items = store.fetch_poi_backlog(room, prefer_city=prefer_city)
    except Exception:
        log.exception("backlog de POIs")
        return published
    enqueue_poi_score(items)
    return published


def _ensure_poi_worker() -> None:
    global _poi_started
    if os.environ.get("PROPMAP_TEST") == "1":
        return
    with _poi_lock:
        if _poi_started:
            return
        _poi_started = True
    threading.Thread(target=_poi_loop, daemon=True, name="propmap-poi").start()


def _poi_loop() -> None:
    while True:
        batch: list[str] = []
        with _poi_lock:
            while _poi_queue and len(batch) < POI_BATCH:
                batch.append(_poi_queue.pop(0))
        if not batch:
            time.sleep(1.0)
            continue
        try:
            from . import store

            items = []
            for lid in batch:
                item = store.get_listing(lid)
                if item is not None:
                    items.append(item)
            if items:
                score_listings(items)
        except Exception:
            log.exception("score de POIs")
        finally:
            with _poi_lock:
                for lid in batch:
                    _poi_seen.discard(lid)
        time.sleep(0.05)
