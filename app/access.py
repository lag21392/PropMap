from __future__ import annotations

import logging
import os
import threading
import time
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
ACCESS_VERSION = "3"

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
    return f"{POI_VERSION}:{round(float(walk), 2)}:{lat}:{lon}"


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
    n_groups = max(1, len(SCORE_GROUPS))
    if not have_pois:
        score = None
        reason = "todavía no bajamos del mapa lo que hay a pie en esta ciudad"
    elif not groups_hit:
        score = 0.0
        reason = f"nada de esto a menos de {meters} m"
    else:
        score = round(100.0 * groups_hit / n_groups, 1)
        reason = f"{len(nearby)} lugares a menos de {meters} m"
    return {
        "pin_grade": grade,
        "precise": True,
        "method": "pin",
        "categories": cats,
        "nearby": nearby,
        "score": score,
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


def _save_access(item: Listing, access: dict[str, Any]) -> None:
    from . import store
    from .profile import _servicios_axis

    extra = dict(item.extra or {})
    stored = {key: value for key, value in access.items() if key not in {"pending", "city"}}
    extra["access"] = stored
    extra["pin_grade"] = stored.get("pin_grade") or pin_grade(item)
    from .profile import stamp_profile

    profile = dict(extra.get("profile") or {})
    axes = dict(profile.get("axes") or {})
    axes["servicios"] = _servicios_axis(item, stored)
    profile["axes"] = axes
    profile["access"] = stored
    extra["profile"] = stamp_profile(profile)
    item.extra = extra
    store.update_extras([item])
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
    """Lee cercanías ya guardadas. Si faltan y hay POIs, calcula este pin y dispara el resto."""
    from . import store
    from .osm_poi import city_pois, ensure_city_pois, pending

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
    if cid:
        kick_access_later(cid)
    cats = city_pois(cid)
    if any(cats.get(key) for key in cats):
        live = compute_access(item, cats)
        live["pending"] = False
        live["city"] = cid
        if lid and row is not None and live.get("precise"):
            _save_access(row, live)
        return live
    ensure_city_pois(cid, blocking=False)
    empty = compute_access(item, cats)
    empty["pending"] = pending(cid)
    empty["city"] = cid
    return empty
