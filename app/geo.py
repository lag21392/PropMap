from __future__ import annotations

import hashlib
import json
import math
import os
import re
import unicodedata

GEO_VERSION = "36"

# CABA es la ciudad principal. Las coordenadas se pisan con Georef al arrancar.
DEFAULT_CITY = "caba"
CABA_LAT = -34.6037
CABA_LON = -58.3816

# Calles ancla: se llenan desde OSM/avisos, no hay catálogo fijo.
STREETS: list[tuple[str, float, float, str]] = []

CITIES: dict[str, dict] = {}
CABA_IDS = {
    "caba",
    "microcentro-caba",
    "capital-federal",
    "ciudad-autonoma-de-buenos-aires",
}
_STRAY_CITIES = {"fuera", "otros", "argentina"}
# "buenos-aires" is the province bucket, not CABA. Downtown ads often land there.
_LOOSE_CITIES = _STRAY_CITIES | {"buenos-aires"}


def default_city() -> str:
    return DEFAULT_CITY


def generic_barrios(lat: float, lon: float) -> list[dict]:
    step = 0.012
    return [
        {"name": "Centro", "zona": "Centro", "lat": lat, "lon": lon, "aliases": ["centro"]},
        {"name": "Norte", "zona": "Norte", "lat": lat - step, "lon": lon, "aliases": ["zona norte", "norte"]},
        {"name": "Sur", "zona": "Sur", "lat": lat + step, "lon": lon, "aliases": ["zona sur", "sur"]},
        {"name": "Este", "zona": "Este", "lat": lat, "lon": lon + step, "aliases": ["zona este", "este"]},
        {"name": "Oeste", "zona": "Oeste", "lat": lat, "lon": lon - step, "aliases": ["zona oeste", "oeste"]},
    ]


def barrios_for(city: str | None) -> list[dict]:
    """Nombres de barrio: polígonos OSM, lo aprendido de los avisos y lo que Nominatim haya guardado."""
    by: dict[str, dict] = {}
    for row in city_polygons(city or ""):
        _index_barrio(by, row)
    for row in learned_barrios(city or ""):
        _index_barrio(by, row, overwrite=False)
    cfg = CITIES.get(city or "") or {}
    for row in cfg.get("barrios") or []:
        _index_barrio(by, row, overwrite=False)
    return list(by.values())


def _index_barrio(by: dict[str, dict], row: dict, *, overwrite: bool = True) -> None:
    name = (row.get("name") or "").strip()
    if not name:
        return
    key = fold(name)
    if not key or (key in by and not overwrite):
        return
    by[key] = {
        "name": name,
        "zona": row.get("zona") or name,
        "lat": row.get("lat"),
        "lon": row.get("lon"),
        "aliases": list(dict.fromkeys([key, *[fold(a) for a in (row.get("aliases") or []) if a]])),
    }


_POLY_CACHE: dict[str, list[dict]] = {}
_OUTLINE_CACHE: dict[str, list[list[list[float]]]] = {}
_OUTLINE_MISS: set[str] = set()
_POINT_BOXES: list[tuple[str, float, float, float, float, float, float]] | None = None
_POINT_BOXES_N = -1
_SAME_PLACE: dict[str, frozenset[str]] = {}
_WATER_RINGS: dict[str, list[list[list[float]]]] = {}
_WATER_MISS: set[str] = set()
_WATER_POINTS: dict[tuple[float, float], bool] = {}
_LEARNED: dict[str, list[dict]] = {}
_GENERIC_BARRIO = {
    "sin clasificar",
    "sin barrio",
    "sin zona",
    "la ciudad",
    "argentina",
    "centro",
    "norte",
    "sur",
    "este",
    "oeste",
}
_WEAK_CITY_ALIAS = {
    "centro",
    "norte",
    "sur",
    "este",
    "oeste",
    "microcentro",
    "micro centro",
}


def remember_city_polygons(city: str, rows: list[dict]) -> None:
    _POLY_CACHE[city] = rows


def outline_matches_city(city: str | None, rings: list[list[list[float]]]) -> bool:
    """False si el polígono es de otro lugar homónimo (el centro de la ciudad queda afuera)."""
    if not rings:
        return False
    cfg = CITIES.get(city or "") or {}
    if not cfg:
        for alias in (city or "",):
            cfg = CITIES.get(alias) or {}
            if cfg:
                break
    try:
        lat, lon = float(cfg["lat"]), float(cfg["lon"])
    except (KeyError, TypeError, ValueError):
        return True
    return _point_in_rings(lat, lon, rings)


def remember_city_outline(city: str, rings: list[list[list[float]]]) -> None:
    """Contorno administrativo de la ciudad (no la unión de barrios)."""
    cleaned = [ring for ring in rings if isinstance(ring, list) and len(ring) >= 4]
    if city and cleaned and outline_matches_city(city, cleaned):
        _OUTLINE_CACHE[city] = cleaned
        _OUTLINE_MISS.discard(city)
        _invalidate_point_boxes()


def clear_city_polygons(city: str | None = None) -> None:
    if city:
        _POLY_CACHE.pop(city, None)
        _OUTLINE_CACHE.pop(city, None)
        _OUTLINE_MISS.discard(city)
        _invalidate_point_boxes()
        return
    _POLY_CACHE.clear()
    _OUTLINE_CACHE.clear()
    _OUTLINE_MISS.clear()
    _invalidate_point_boxes()


def city_polygons(city: str) -> list[dict]:
    if not city:
        return []
    if city in _POLY_CACHE:
        return _POLY_CACHE[city]
    from . import store

    store.init()
    raw = store.get_meta(f"osm_barrios:{city}")
    rows = []
    if raw:
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, list):
                rows = loaded
        except json.JSONDecodeError:
            rows = []
    if rows:
        _POLY_CACHE[city] = rows
    return rows


def zona_from_bearing(lat: float, lon: float, clat: float | None = None, clon: float | None = None, city: str | None = None) -> str:
    if clat is None or clon is None:
        clat, clon = city_center(city)
    dlat = lat - clat
    dlon = lon - clon
    if abs(dlat) < 0.0028 and abs(dlon) < 0.0032:
        return "Centro"
    ns = "Norte" if dlat > 0.0012 else ("Sur" if dlat < -0.0012 else "")
    ew = "Este" if dlon > 0.002 else ("Oeste" if dlon < -0.002 else "")
    if ns:
        return f"Zona {ns}"
    if ew:
        return f"Zona {ew}"
    return "Centro"


def _point_in_ring(lat: float, lon: float, ring: list[list[float]]) -> bool:
    inside = False
    for i in range(len(ring) - 1):
        y1, x1 = ring[i]
        y2, x2 = ring[i + 1]
        if (x1 > lon) != (x2 > lon):
            y = (y2 - y1) * (lon - x1) / ((x2 - x1) or 1e-18) + y1
            if lat < y:
                inside = not inside
    return inside


def _ring_area(ring: list[list[float]]) -> float:
    area = 0.0
    for i in range(len(ring) - 1):
        area += ring[i][1] * ring[i + 1][0] - ring[i + 1][1] * ring[i][0]
    return abs(area) / 2


def barrio_containing(lat: float, lon: float, city: str = DEFAULT_CITY) -> tuple[str, str, float, float] | None:
    hits = [row for row in city_polygons(city) if row.get("ring") and _point_in_ring(lat, lon, row["ring"])]
    if not hits:
        return None
    best = min(hits, key=lambda row: _ring_area(row["ring"]))
    return best["name"], best["zona"], best["lat"], best["lon"]


def _point_in_rings(lat: float, lon: float, rings: list[list[list[float]]]) -> bool:
    return any(len(ring) >= 4 and _point_in_ring(lat, lon, ring) for ring in rings)


def city_outline_rings(city: str | None) -> list[list[list[float]]]:
    """Polígono de la ciudad (CABA = Capital Federal, no el GBA)."""
    seen: set[str] = set()
    ids = [city or ""]
    resolved = CITY_ALIASES.get(fold(city or ""), city or "")
    if resolved:
        ids.append(resolved)
    if (city or "") in CABA_IDS or resolved in CABA_IDS:
        ids.extend(CABA_IDS)
    for cid in ids:
        if not cid or cid in seen:
            continue
        seen.add(cid)
        cached = _OUTLINE_CACHE.get(cid)
        if cached:
            if outline_matches_city(cid, cached):
                return cached
            _OUTLINE_CACHE.pop(cid, None)
        if cid in _OUTLINE_MISS:
            continue
        if os.environ.get("PROPMAP_TEST") == "1":
            _OUTLINE_MISS.add(cid)
            continue
        from . import store

        store.init()
        raw = store.get_meta(f"osm_outline:{cid}")
        if not raw:
            _OUTLINE_MISS.add(cid)
            continue
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            continue
        rings: list[list[list[float]]] = []
        if isinstance(loaded, dict):
            if isinstance(loaded.get("rings"), list):
                rings = loaded["rings"]
            elif isinstance(loaded.get("ring"), list):
                rings = [loaded["ring"]]
        elif isinstance(loaded, list):
            rings = loaded if loaded and isinstance(loaded[0], list) and loaded and isinstance(loaded[0][0], list) else [loaded]
        cleaned = [ring for ring in rings if isinstance(ring, list) and len(ring) >= 4]
        if cleaned and outline_matches_city(cid, cleaned):
            _OUTLINE_CACHE[cid] = cleaned
            return cleaned
        if cleaned:
            _OUTLINE_CACHE.pop(cid, None)
            try:
                store.set_meta(f"osm_outline:{cid}", "")
            except Exception:
                pass
            try:
                from .listings_cache import invalidate_city_geo

                invalidate_city_geo(cid)
            except Exception:
                pass
    return []


def city_outline_bbox(city: str | None):
    rings = city_outline_rings(city)
    lats: list[float] = []
    lons: list[float] = []
    for ring in rings:
        for point in ring:
            if len(point) >= 2:
                lats.append(float(point[0]))
                lons.append(float(point[1]))
    if len(lats) < 4:
        return None
    return (min(lats), min(lons), max(lats), max(lons))


def barrio_from_pin(lat: float | None, lon: float | None, city: str | None) -> tuple[str, str]:
    """Barrio del pin: polígono OSM, centroide OSM o el más cercano ya geocodificado. Nunca el texto del aviso."""
    if lat is None or lon is None:
        return "Sin clasificar", "Sin clasificar"
    place = city or default_city()
    if place in _LOOSE_CITIES:
        return "Sin clasificar", "Sin clasificar"
    own = own_place_names(place)
    hit = barrio_containing(float(lat), float(lon), place)
    if hit and fold(hit[0]) not in own:
        return hit[0], hit[1]
    osm = [
        row
        for row in city_polygons(place)
        if row.get("lat") is not None
        and row.get("lon") is not None
        and fold(row.get("name") or "") not in own
    ]
    if osm:
        best = min(osm, key=lambda row: (float(row["lat"]) - lat) ** 2 + (float(row["lon"]) - lon) ** 2)
        return best["name"], best.get("zona") or best["name"]
    name, zona, _clat, _clon = nearest_barrio(float(lat), float(lon), place)
    if fold(name) in own or fold(name) in _GENERIC_BARRIO:
        return "Sin clasificar", zona_from_bearing(float(lat), float(lon), city=place)
    return name, zona


def apply_barrio_from_pin(item) -> bool:
    city = getattr(item, "city", None) or default_city()
    if city in _LOOSE_CITIES:
        return False
    lat, lon = getattr(item, "lat", None), getattr(item, "lon", None)
    name, zona = barrio_from_pin(lat, lon, city)
    changed = (item.barrio or "") != name or (item.zona or "") != zona
    item.barrio = name
    item.zona = zona
    return changed


def city_center(city: str | None) -> tuple[float, float]:
    cfg = CITIES.get(city or "")
    if cfg:
        return cfg["lat"], cfg["lon"]
    cfg = CITIES.get(DEFAULT_CITY)
    if cfg:
        return cfg["lat"], cfg["lon"]
    return CABA_LAT, CABA_LON


def city_radius_km(city: str | None) -> float:
    cfg = CITIES.get(city or "") or {}
    if cfg.get("bbox"):
        return _extent_radius_km(cfg["bbox"], *city_center(city))
    return float(cfg.get("radius_km") or 25)


def _extent_radius_km(bbox, lat: float, lon: float) -> float:
    south, west, north, east = bbox
    return max(
        distance_km(south, west, lat, lon),
        distance_km(south, east, lat, lon),
        distance_km(north, west, lat, lon),
        distance_km(north, east, lat, lon),
        4.0,
    ) * 1.05


def apply_city_extent(
    city_id: str,
    *,
    bbox=None,
    radius_km: float | None = None,
    zoom: int | None = None,
    slug: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
) -> None:
    cfg = CITIES.get(city_id)
    if not cfg:
        return
    if bbox and len(bbox) >= 4:
        new_box = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
        span = float(radius_km or _extent_radius_km(new_box, cfg["lat"], cfg["lon"]))
        if span >= 8:
            cfg["bbox"] = new_box
            radius_km = span
    if radius_km and float(radius_km) >= 8:
        cfg["radius_km"] = float(radius_km)
    good_box = bool(cfg.get("bbox")) and _extent_radius_km(cfg["bbox"], cfg["lat"], cfg["lon"]) >= 8
    if zoom and (good_box or (radius_km and float(radius_km) >= 8)):
        cfg["zoom"] = int(zoom)
    if (cfg.get("province") or "") == "capital-federal":
        cfg["slug"] = "capital-federal"
    elif slug:
        cfg["slug"] = slug
    if lat is not None:
        cfg["lat"] = float(lat)
    if lon is not None:
        cfg["lon"] = float(lon)


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    rlat1, rlon1, rlat2, rlon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    dlat, dlon = rlat2 - rlat1, rlon2 - rlon1
    h = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371 * math.asin(min(1.0, math.sqrt(h)))


def in_city_radius(lat: float | None, lon: float | None, city: str | None) -> bool:
    if lat is None or lon is None:
        return False
    rings = city_outline_rings(city)
    cfg = CITIES.get(city or "") or {}
    if not cfg:
        for alias in same_place_ids(city):
            cfg = CITIES.get(alias) or {}
            if cfg:
                break
    in_rings = bool(rings) and _point_in_rings(float(lat), float(lon), rings)
    in_rad = False
    if cfg:
        clat, clon = cfg["lat"], cfg["lon"]
        radius = float(cfg.get("radius_km") or 25)
        if cfg.get("bbox"):
            radius = _extent_radius_km(cfg["bbox"], clat, clon)
        in_rad = distance_km(lat, lon, clat, clon) <= radius
        box = cfg.get("bbox")
        if box:
            south, west, north, east = box
            in_rad = in_rad or (south <= lat <= north and west <= lon <= east)
    if rings:
        return in_rings
    return in_rad


def _invalidate_point_boxes() -> None:
    global _POINT_BOXES, _POINT_BOXES_N
    _POINT_BOXES = None
    _POINT_BOXES_N = -1
    _SAME_PLACE.clear()


def _rings_bbox(rings: list[list[list[float]]]) -> tuple[float, float, float, float] | None:
    lats: list[float] = []
    lons: list[float] = []
    for ring in rings:
        for point in ring:
            if len(point) >= 2:
                lats.append(float(point[0]))
                lons.append(float(point[1]))
    if len(lats) < 4:
        return None
    return (min(lats), min(lons), max(lats), max(lons))


def _point_boxes() -> list[tuple[str, float, float, float, float, float, float]]:
    """Cajas en RAM. No pega a SQLite: el armado de CABA no puede releer 600 contornos."""
    global _POINT_BOXES, _POINT_BOXES_N
    n = len(CITIES)
    if _POINT_BOXES is not None and n == _POINT_BOXES_N:
        return _POINT_BOXES
    boxes: list[tuple[str, float, float, float, float, float, float]] = []
    for city_id, cfg in list(CITIES.items()):
        try:
            clat, clon = float(cfg["lat"]), float(cfg["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        box = cfg.get("bbox")
        rings = _OUTLINE_CACHE.get(city_id)
        if rings:
            ring_box = _rings_bbox(rings)
            if ring_box:
                box = ring_box
        if box and len(box) >= 4:
            south, west, north, east = float(box[0]), float(box[1]), float(box[2]), float(box[3])
        else:
            radius = float(cfg.get("radius_km") or 25)
            dlat = radius / 111.0
            cos_lat = max(0.2, math.cos(math.radians(clat)))
            dlon = radius / (111.0 * cos_lat)
            south, west, north, east = clat - dlat, clon - dlon, clat + dlat, clon + dlon
        boxes.append((city_id, south, west, north, east, clat, clon))
    _POINT_BOXES = boxes
    _POINT_BOXES_N = n
    return boxes


def city_for_point(lat: float, lon: float) -> str | None:
    best_id = None
    best_d = 1e9
    for city_id, south, west, north, east, clat, clon in _point_boxes():
        if not (south <= lat <= north and west <= lon <= east):
            continue
        rings = _OUTLINE_CACHE.get(city_id)
        if rings and not _point_in_rings(lat, lon, rings):
            continue
        dist = distance_km(lat, lon, clat, clon)
        if dist < best_d:
            best_id, best_d = city_id, dist
    return best_id


def _clear_pin(item) -> None:
    item.lat = None
    item.lon = None
    item.has_exact_location = False


def portal_pin_in_city(item, city: str) -> bool:
    extra = getattr(item, "extra", None) or {}
    try:
        lat = extra.get("portal_lat")
        lon = extra.get("portal_lon")
        if lat is None or lon is None or not city:
            return False
        return in_city_radius(float(lat), float(lon), city)
    except (TypeError, ValueError):
        return False


def portal_outside_city(item, city: str) -> bool:
    if not city or city in _LOOSE_CITIES:
        return False
    extra = getattr(item, "extra", None) or {}
    try:
        lat = extra.get("portal_lat")
        lon = extra.get("portal_lon")
        if lat is None or lon is None:
            return False
        cfg = CITIES.get(city) or {}
        if not cfg:
            for alias in same_place_ids(city):
                cfg = CITIES.get(alias) or {}
                if cfg:
                    break
        if not cfg:
            return False
        return not in_city_radius(float(lat), float(lon), city)
    except (TypeError, ValueError):
        return False


def barrio_belongs_to_city(item, city: str) -> bool:
    extra = getattr(item, "extra", None) or {}
    raw = extra.get("portal_barrio") or extra.get("barrio")
    if raw:
        token = fold(raw)
        if not token or token in _GENERIC_BARRIO:
            return False
        return token in own_barrio_names(city) or token in own_place_names(city)
    token = fold(getattr(item, "barrio", None) or "")
    if not token or token in _GENERIC_BARRIO:
        return False
    return token in own_barrio_names(city) or token in own_place_names(city)


def _snap_city_alias_barrio(item, city: str) -> bool:
    token = fold(getattr(item, "barrio", None) or "")
    if not token or token not in own_place_names(city):
        return False
    lat, lon = item.lat, item.lon
    extra = getattr(item, "extra", None) or {}
    if lat is None or lon is None:
        try:
            lat = extra.get("portal_lat")
            lon = extra.get("portal_lon")
            lat = float(lat) if lat is not None else None
            lon = float(lon) if lon is not None else None
        except (TypeError, ValueError):
            lat = lon = None
    if lat is None or lon is None:
        lat, lon = city_center(city)
    name, zona, _, _ = nearest_barrio(float(lat), float(lon), city)
    if not name or fold(name) in own_place_names(city):
        return False
    item.barrio = name
    item.zona = zona
    return True


def pin_listing_city(item, *, remote: bool = True) -> bool:
    changed = _pin_listing_city(item, remote=remote)
    if apply_barrio_from_pin(item):
        changed = True
    from .place_tags import apply_place_tags

    apply_place_tags(item, remote=remote)
    return changed


def _pin_listing_city(item, *, remote: bool = True) -> bool:
    extra = getattr(item, "extra", None) or {}
    search = str(extra.get("search_city") or "").strip()
    tagged = item.city or default_city()
    home = search if search and search not in _LOOSE_CITIES else tagged
    if home not in _LOOSE_CITIES and portal_outside_city(item, home):
        try:
            plat, plon = float(extra["portal_lat"]), float(extra["portal_lon"])
        except (KeyError, TypeError, ValueError):
            plat = plon = None
        if plat is not None:
            item.lat, item.lon = plat, plon
            extra = dict(extra)
            extra["pin_kind"] = "saved"
            if extra.get("portal_exact") and not extra.get("portal_approx"):
                extra["location_kind"] = "exact"
                item.has_exact_location = True
            else:
                extra["location_kind"] = "approx"
                item.has_exact_location = False
            item.extra = extra
            sit = city_for_point(plat, plon)
            if sit and sit not in same_place_ids(home) and sit != home:
                item.city = sit
            else:
                item.city = "fuera"
            if item.city not in _LOOSE_CITIES:
                _snap_city_alias_barrio(item, item.city)
            from .scrapers import attach_location_facts

            attach_location_facts(item)
            return True
    if home not in _LOOSE_CITIES and portal_pin_in_city(item, home):
        changed = item.city != home
        item.city = home
        if item.lat is None or item.lon is None or not in_city_radius(item.lat, item.lon, home):
            item.lat = float(extra["portal_lat"])
            item.lon = float(extra["portal_lon"])
            item.has_exact_location = bool(extra.get("portal_exact")) and not extra.get("portal_approx")
            changed = True
        if _snap_city_alias_barrio(item, home):
            changed = True
        elif item.barrio and item.barrio != "Sin clasificar":
            from .place_api import lookup_place, place_conflicts_city

            place = lookup_place(item.barrio, remote=False)
            if place and place_conflicts_city(place, home):
                item.barrio = "Sin clasificar"
                changed = True
        return changed
    if (
        home not in _LOOSE_CITIES
        and item.lat is not None
        and item.lon is not None
        and in_city_radius(item.lat, item.lon, home)
        and (barrio_belongs_to_city(item, home) or not foreign_locality(item, home, remote=remote))
    ):
        changed = item.city != home
        item.city = home
        if _snap_city_alias_barrio(item, home):
            changed = True
        if changed:
            return True
    changed = False
    if tagged not in _LOOSE_CITIES and tagged not in CITIES:
        return False
    if tagged in _LOOSE_CITIES:
        guessed = city_from_text(item, remote=remote)
        if guessed and guessed not in _LOOSE_CITIES:
            if listing_mentions_city(item, guessed) and not foreign_locality(item, guessed, remote=remote):
                sit = (
                    city_for_point(item.lat, item.lon)
                    if item.lat is not None and item.lon is not None
                    else None
                )
                if sit and sit != guessed and not in_city_radius(item.lat, item.lon, guessed):
                    _clear_pin(item)
                    return True
                item.city = guessed
                if item.lat is not None and item.lon is not None and not in_city_radius(
                    item.lat, item.lon, guessed
                ):
                    _clear_pin(item)
                _snap_city_alias_barrio(item, guessed)
                return True
        if home not in _LOOSE_CITIES and listing_mentions_city(item, home) and not foreign_locality(item, home, remote=remote):
            item.city = home
            if item.lat is not None and item.lon is not None and not in_city_radius(item.lat, item.lon, home):
                _clear_pin(item)
            _snap_city_alias_barrio(item, home)
            return True
        if item.lat is None or item.lon is None:
            return False
        sit = city_for_point(item.lat, item.lon)
        if not sit:
            return False
        if foreign_locality(item, sit, remote=remote) and not barrio_belongs_to_city(item, sit):
            _clear_pin(item)
            return True
        item.city = sit
        _snap_city_alias_barrio(item, sit)
        return True
    if foreign_locality(item, tagged, remote=remote) and not barrio_belongs_to_city(item, tagged):
        guessed = city_from_text(item, remote=remote)
        if guessed == tagged:
            guessed = None
        if not guessed:
            from .place_api import listing_places, place_conflicts_city as _place_conflicts
            from .places import ensure_place

            for place in listing_places(item, remote=remote):
                if str(place.get("kind") or "") not in {"localidad", "municipio"}:
                    continue
                if place.get("lat") is None or place.get("lon") is None:
                    continue
                if not _place_conflicts(place, tagged):
                    continue
                name = str(place.get("name") or "").strip()
                if not name:
                    continue
                guessed = ensure_place(
                    name,
                    label=name,
                    lat=float(place["lat"]),
                    lon=float(place["lon"]),
                    province=str(place.get("province") or "") or None,
                )
                if guessed == tagged:
                    guessed = None
                break
        if not guessed and item.lat is not None and item.lon is not None:
            guessed = city_for_point(item.lat, item.lon)
            if guessed == tagged or (guessed and foreign_locality(item, guessed, remote=remote) and not barrio_belongs_to_city(item, guessed)):
                guessed = None
        item.city = guessed or "fuera"
        if item.lat is not None and item.lon is not None and (
            item.city == "fuera" or not in_city_radius(item.lat, item.lon, item.city)
        ):
            _clear_pin(item)
        if item.city not in _LOOSE_CITIES:
            _snap_city_alias_barrio(item, item.city)
        return True
    if item.lat is not None and item.lon is not None:
        guessed = city_for_point(item.lat, item.lon)
        if guessed and guessed != tagged:
            keep_tagged = (
                listing_mentions_city(item, tagged)
                and not foreign_locality(item, tagged, remote=remote)
                and not listing_mentions_city(item, guessed)
                and not barrio_belongs_to_city(item, guessed)
                and search != guessed
            )
            if keep_tagged:
                _clear_pin(item)
                return True
            item.city = guessed
            tagged = guessed
            changed = True
            _snap_city_alias_barrio(item, guessed)
        elif not guessed and not in_city_radius(item.lat, item.lon, tagged):
            if listing_mentions_city(item, tagged) and not foreign_locality(item, tagged, remote=remote):
                if _dummy_coords(item.lat, item.lon, tagged):
                    _clear_pin(item)
                    return True
                return changed
            item.city = "fuera"
            _clear_pin(item)
            return True
    return changed


def _listing_blob(item) -> str:
    return fold(
        f"{getattr(item, 'title', '')} {getattr(item, 'address', '')} "
        f"{getattr(item, 'description', '') or ''} {getattr(item, 'barrio', '') or ''}"
    )


def own_place_names(city: str) -> set[str]:
    cfg = CITIES.get(city) or {}
    names = [
        cfg.get("label"),
        city,
        str(city or "").replace("-", " "),
        cfg.get("province"),
        cfg.get("slug"),
        *(cfg.get("aliases") or []),
    ]
    return {fold(n) for n in names if n and fold(n)}


def _barrio_name_tokens(rows: list[dict]) -> set[str]:
    names: set[str] = set()
    for barrio in rows:
        token = fold(barrio.get("name") or "")
        if token and token not in _GENERIC_BARRIO:
            names.add(token)
        for alias in barrio.get("aliases") or []:
            token = fold(alias)
            if token and token not in _GENERIC_BARRIO:
                names.add(token)
    return names


def official_barrio_names(city: str) -> set[str]:
    cfg = CITIES.get(city) or {}
    return _barrio_name_tokens(list(city_polygons(city)) + list(cfg.get("barrios") or []))


def own_barrio_names(city: str) -> set[str]:
    return _barrio_name_tokens(barrios_for(city))


def _is_own_phrase(phrase: str, own: set[str]) -> bool:
    token = fold(phrase)
    if not token:
        return False
    if token in own:
        return True
    return any(len(token) >= 5 and len(name) >= 5 and (token in name or name in token) for name in own)


_STREET_USE = (
    r"(?:av(?:da|\.|enida)?|calle|pasaje|pje\.?|ruta|diag(?:onal)?)\s+{name}"
    r"(?:\s+(?:al\s+)?\d{{2,5}})?"
    r"|{name}\s+(?:al\s+)?\d{{2,5}}\b"
)


def _mentions_as_place(blob: str, name: str) -> bool:
    if not name:
        return False
    cleaned = re.sub(_STREET_USE.format(name=re.escape(name)), " ", blob)
    padded = f" {cleaned} "
    if len(name) >= 5 and name in cleaned:
        return True
    return len(name) >= 4 and f" {name} " in padded


def listing_mentions_city(item, city: str) -> bool:
    blob = _listing_blob(item)
    names = {
        name
        for name in own_place_names(city)
        if name and name not in _GENERIC_BARRIO and name not in _WEAK_CITY_ALIAS and len(name) >= 4
    }
    return any(_mentions_as_place(blob, name) for name in names)


def same_place_ids(city: str | None) -> set[str]:
    city = city or ""
    if not city:
        return set()
    hit = _SAME_PLACE.get(city)
    if hit is not None:
        return set(hit)
    resolved = CITY_ALIASES.get(fold(city), city)
    hit = _SAME_PLACE.get(resolved)
    if hit is not None:
        _SAME_PLACE[city] = hit
        return set(hit)
    cfg = CITIES.get(resolved) or CITIES.get(city) or {}
    ids = {city, resolved}
    slug = cfg.get("slug") or city
    province = cfg.get("province") or ""
    for other_id, other in list(CITIES.items()):
        if (other.get("slug") or other_id) == slug:
            ids.add(other_id)
        if province and other.get("province") == province and province == "capital-federal":
            ids.add(other_id)
    if city in CABA_IDS or resolved in CABA_IDS or province == "capital-federal":
        ids.update(CABA_IDS)
        if DEFAULT_CITY in CITIES:
            ids.add(DEFAULT_CITY)
    else:
        try:
            clat, clon = float(cfg["lat"]), float(cfg["lon"])
            radius = float(cfg.get("radius_km") or 25)
        except (KeyError, TypeError, ValueError):
            clat = None
        if clat is not None:
            for other_id, other in list(CITIES.items()):
                if other_id in ids:
                    continue
                try:
                    olat, olon = float(other["lat"]), float(other["lon"])
                except (KeyError, TypeError, ValueError):
                    continue
                if distance_km(clat, clon, olat, olon) <= radius:
                    ids.add(other_id)
    frozen = frozenset(item for item in ids if item)
    _SAME_PLACE[city] = frozen
    if resolved != city:
        _SAME_PLACE[resolved] = frozen
    return set(frozen)


def city_from_text(item, *, remote: bool = True) -> str | None:
    from .place_api import listing_places

    extra = getattr(item, "extra", None) or {}
    search = str(extra.get("search_city") or "").strip()
    tagged = str(getattr(item, "city", None) or "").strip()
    places = listing_places(item, remote=remote)
    guesses: list[str] = []
    seen_guess: set[str] = set()
    for place in places:
        lat, lon = place.get("lat"), place.get("lon")
        if lat is None or lon is None:
            continue
        guessed = city_for_point(float(lat), float(lon))
        if guessed and guessed not in seen_guess:
            seen_guess.add(guessed)
            guesses.append(guessed)
    home = search if search and search not in _LOOSE_CITIES else tagged
    others = [cid for cid in guesses if cid not in same_place_ids(home)]
    if others:
        for cid in others:
            if listing_mentions_city(item, cid):
                return cid
        return others[0]
    if home and home not in _LOOSE_CITIES and home in seen_guess:
        return home
    for guessed in guesses:
        if listing_mentions_city(item, guessed):
            return guessed
    if guesses:
        return guesses[0]
    blob = fold(_listing_blob(item))
    if not blob:
        return None
    best = None
    best_n = 0
    for city_id, cfg in list(CITIES.items()):
        if city_id in _LOOSE_CITIES:
            continue
        label = fold(str(cfg.get("label") or ""))
        if len(label) < 5 or label not in blob:
            continue
        if len(label) > best_n:
            best, best_n = city_id, len(label)
    return best


def foreign_locality(item, city: str, *, remote: bool = True) -> bool:
    from .place_api import listing_places, place_conflicts_city

    guessed = city_from_text(item, remote=remote)
    local = barrio_belongs_to_city(item, city)
    mentioned = listing_mentions_city(item, city)
    if guessed and guessed not in same_place_ids(city) and guessed != city:
        if not (mentioned and local):
            return True
    for place in listing_places(item, remote=remote):
        if not place_conflicts_city(place, city):
            continue
        if mentioned and local:
            continue
        return True
    return False


def listing_fits_city(item, city: str, *, remote: bool = True, require_radius: bool = True) -> bool:
    city = city or default_city()
    wanted = same_place_ids(city)
    item_city = getattr(item, "city", None) or ""
    extra = getattr(item, "extra", None) or {}
    search = str(extra.get("search_city") or "").strip()
    tagged = item_city in wanted or item_city == city or search in wanted or search == city
    lat = getattr(item, "lat", None)
    lon = getattr(item, "lon", None)
    in_radius = lat is not None and lon is not None and in_city_radius(lat, lon, city)
    mentioned = listing_mentions_city(item, city)
    portal_here = portal_pin_in_city(item, city)
    searched_here = bool(search) and (search == city or search in wanted)
    if portal_outside_city(item, city):
        return False
    from .place_tags import is_narrower_place_query, listing_matches_city, listing_place_tags

    place_tags = listing_place_tags(item, remote=False)
    tags_hit = listing_matches_city(item, city)
    if place_tags and not tags_hit and is_narrower_place_query(place_tags, city):
        return False
    if tags_hit:
        tagged = True
    if not tagged and not in_radius and not mentioned:
        return False
    if lat is not None and lon is not None and not in_radius:
        if city_outline_rings(city) and not portal_here:
            return False
        sit = city_for_point(float(lat), float(lon))
        if sit and sit not in wanted and sit != city and not portal_here:
            return False
    # Pin adentro de la ciudad buscada: un topónimo homónimo (Hudson, Córdoba) no lo esconde.
    if foreign_locality(item, city, remote=remote) and not portal_here and not (in_radius and searched_here):
        return False
    if in_radius and mentioned:
        return True
    if in_radius or portal_here:
        return True
    if city_outline_rings(city):
        return False
    if lat is not None and lon is not None and require_radius:
        has_cfg = any(CITIES.get(alias) for alias in (wanted or {city}))
        if has_cfg:
            return False
    return tagged or mentioned


def public_row_fits_city(row: dict, city: str) -> bool:
    class _Row:
        def __init__(self, data: dict):
            self.city = data.get("city") or ""
            self.title = data.get("title") or ""
            self.address = data.get("address") or ""
            self.description = data.get("description") or ""
            self.barrio = data.get("portal_barrio") or data.get("barrio") or ""
            self.lat = data.get("lat")
            self.lon = data.get("lon")
            extra = dict(data.get("extra") or {})
            if data.get("portal_barrio"):
                extra.setdefault("portal_barrio", data.get("portal_barrio"))
                extra.setdefault("barrio", data.get("portal_barrio"))
            if data.get("search_city"):
                extra.setdefault("search_city", data.get("search_city"))
            if data.get("place_tags"):
                extra["place_tags"] = list(data.get("place_tags") or [])
            if data.get("portal_lat") is not None:
                extra["portal_lat"] = data.get("portal_lat")
                extra["portal_lon"] = data.get("portal_lon")
            self.extra = extra

    return listing_fits_city(_Row(row), city, remote=False, require_radius=True)


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text.lower()).strip()


CITY_ALIASES: dict[str, str] = {}


def _seed_city_aliases() -> None:
    CITY_ALIASES.clear()
    for city_id, cfg in list(CITIES.items()):
        CITY_ALIASES[city_id] = city_id
        CITY_ALIASES.setdefault(fold(cfg.get("label") or ""), city_id)
        for alias in cfg.get("aliases") or []:
            CITY_ALIASES.setdefault(fold(alias), city_id)


_seed_city_aliases()


def slug_place(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", fold(value)).strip("-")
    return slug or "lugar"


def city_slug(city: str) -> str:
    resolved = CITY_ALIASES.get(fold(city), city)
    cfg = CITIES.get(city) or CITIES.get(resolved) or {}
    if (cfg.get("province") or "") == "capital-federal" or city in CABA_IDS or resolved in CABA_IDS:
        return "capital-federal"
    return cfg.get("slug") or city


def city_scrape_slugs(city: str) -> list[str]:
    resolved = CITY_ALIASES.get(fold(city), city)
    cfg = CITIES.get(city) or CITIES.get(resolved) or {}
    slugs = [city_slug(city)]
    extra_slug = cfg.get("slug")
    if extra_slug and extra_slug not in slugs:
        slugs.append(extra_slug)
    for extra in cfg.get("extra_slugs") or []:
        token = fold(str(extra))
        if token and token not in slugs:
            slugs.append(token)
    return slugs


def register_city(
    city_id: str,
    *,
    label: str,
    lat: float,
    lon: float,
    province: str = "",
    barrios: list[dict] | None = None,
    zoom: int = 13,
    aliases: list[str] | None = None,
    builtin: bool = False,
    slug: str | None = None,
    bbox=None,
    radius_km: float | None = None,
) -> dict:
    names = [fold(city_id), fold(label), *[fold(a) for a in (aliases or []) if a]]
    CITIES[city_id] = {
        "id": city_id,
        "label": label,
        "lat": float(lat),
        "lon": float(lon),
        "zoom": int(zoom or 13),
        "province": province or "",
        "slug": slug or city_id,
        "builtin": builtin,
        "aliases": [n for n in dict.fromkeys(names) if n],
        "barrios": barrios or [],
        "radius_km": float(radius_km or 25),
    }
    if bbox and len(bbox) >= 4:
        CITIES[city_id]["bbox"] = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
        CITIES[city_id]["radius_km"] = float(radius_km or _extent_radius_km(CITIES[city_id]["bbox"], lat, lon))
    _invalidate_point_boxes()
    def _alias(name: str) -> None:
        token = fold(name)
        if not token:
            return
        owner = CITY_ALIASES.get(token)
        if owner and (CITIES.get(owner) or {}).get("builtin") and not builtin:
            return
        CITY_ALIASES[token] = city_id

    CITY_ALIASES[city_id] = city_id
    _alias(label)
    for name in CITIES[city_id]["aliases"]:
        _alias(name)
    return CITIES[city_id]


def resolve_city(value: str | None) -> str:
    raw = fold(value or "")
    if not raw:
        return DEFAULT_CITY
    slug = slug_place(raw)
    if raw in CITY_ALIASES:
        return CITY_ALIASES[raw]
    if slug in CITY_ALIASES:
        return CITY_ALIASES[slug]
    if slug in CITIES:
        return slug
    if raw in CITIES:
        return raw
    best = ""
    best_len = 0
    for cfg in list(CITIES.values()):
        names = [fold(cfg["id"]), fold(cfg["label"]), *[fold(a) for a in cfg.get("aliases") or []]]
        for name in names:
            if name and (name == raw or name == slug):
                return cfg["id"]
            if name and len(name) >= 5 and name in raw and len(name) > best_len:
                best, best_len = cfg["id"], len(name)
    if best:
        return best
    for alias, city_id in CITY_ALIASES.items():
        if len(alias) >= 5 and alias in raw:
            return city_id
    return slug


def _haystack(*parts: str) -> str:
    return fold(" | ".join(p for p in parts if p))


def _pretty_barrio(raw: str) -> str:
    parts = re.sub(r"\s+", " ", raw).strip(" .,").split()
    return " ".join(p if p.isdigit() else p.capitalize() for p in parts)


def barrio_from_address(address: str | None, city: str) -> str | None:
    parts = [re.sub(r"\s+", " ", p).strip(" .") for p in re.split(r"[,|/•·]+", address or "")]
    parts = [p for p in parts if p]
    if len(parts) < 2:
        return None
    own = own_place_names(city)
    own.update({"argentina", "arg", "provincia", "partido"})
    for i, part in enumerate(parts):
        token = fold(part)
        if not token or len(token) < 4 or token in own or _is_own_phrase(token, own):
            continue
        if re.match(r"^\d+$", token) or re.match(r"^(piso|dto|depto|departamento|casa|ph)\b", token):
            continue
        if token in _GENERIC_BARRIO:
            continue
        if len(part) > 48 or re.search(r"\b(venta|alquiler|dormitorios?|ambientes?)\b", token):
            continue
        if i == 0 and re.search(r"\d", part):
            continue
        return _pretty_barrio(part)
    return None


def match_known_barrio(name: str | None, city: str) -> dict | None:
    text = fold(name or "")
    if not text or len(text) < 4:
        return None
    best = None
    best_len = 0
    exact = None
    for barrio in barrios_for(city):
        aliases = [fold(barrio["name"]), *(barrio.get("aliases") or [])]
        for alias in aliases:
            if not alias or len(alias) < 4:
                continue
            if alias == text:
                exact = barrio
            elif alias in text and len(alias) >= best_len:
                best = barrio
                best_len = len(alias)
    return exact or best


def learned_barrios(city: str) -> list[dict]:
    if not city:
        return []
    if city in _LEARNED:
        return _LEARNED[city]
    from . import store

    rows: list[dict] = []
    try:
        store.init()
        raw = store.get_meta(f"learned_barrios:{city}")
        if raw:
            loaded = json.loads(raw)
            if isinstance(loaded, list):
                rows = [
                    row
                    for row in loaded
                    if isinstance(row, dict) and row.get("name") and not _learned_name_is_foreign_city(city, row.get("name"))
                ]
    except Exception:
        rows = []
    _LEARNED[city] = rows
    return rows


def _learned_name_is_foreign_city(city: str, name: str | None) -> bool:
    if not name:
        return False
    try:
        from .place_api import lookup_place, place_conflicts_city

        place = lookup_place(name, remote=False)
        if not place:
            return False
        if str(place.get("kind") or "") not in {"localidad", "municipio", "asentamiento"}:
            return False
        return place_conflicts_city(place, city)
    except Exception:
        return False


def remember_barrio(city: str | None, name: str | None, lat: float | None = None, lon: float | None = None) -> None:
    if not city or not name:
        return
    pretty = _pretty_barrio(name)
    token = fold(pretty)
    if not token or token in _GENERIC_BARRIO or token in own_place_names(city):
        return
    if token.startswith("zona ") or token in {"centro", "norte", "sur", "este", "oeste"}:
        return
    try:
        from .place_api import lookup_place, place_conflicts_city

        known = lookup_place(pretty, remote=False)
        if (
            known
            and str(known.get("kind") or "") in {"localidad", "municipio", "asentamiento"}
            and place_conflicts_city(known, city)
        ):
            return
    except Exception:
        pass
    rows = list(learned_barrios(city))
    found = next((row for row in rows if fold(row["name"]) == token), None)
    if found:
        if lat is not None and lon is not None:
            found["lat"] = float(lat)
            found["lon"] = float(lon)
            found["zona"] = zona_from_bearing(float(lat), float(lon), city=city)
    else:
        clat, clon = city_center(city)
        rows.append(
            {
                "name": pretty,
                "zona": zona_from_bearing(lat or clat, lon or clon, city=city) if lat and lon else "Centro",
                "lat": float(lat) if lat is not None else clat,
                "lon": float(lon) if lon is not None else clon,
                "aliases": [token],
            }
        )
    _LEARNED[city] = rows
    try:
        from .place_api import forget_barrio_index

        forget_barrio_index(city)
    except Exception:
        pass
    try:
        from . import store

        store.init()
        store.set_meta(f"learned_barrios:{city}", json.dumps(rows, ensure_ascii=False))
    except Exception:
        pass


def _barrio_from_place_hint(hinted: str, city: str) -> tuple[str, str, float, float] | None:
    """Si el aviso dice un apodo (Microcentro), el pin de la API cae en un barrio OSM de la ciudad."""
    cfg = CITIES.get(city) or {}
    try:
        from .place_api import lookup_place

        place = lookup_place(
            hinted,
            province_hint=str(cfg.get("province") or cfg.get("label") or ""),
            remote=True,
        )
    except Exception:
        place = None
    if not place or place.get("lat") is None or place.get("lon") is None:
        return None
    lat, lon = float(place["lat"]), float(place["lon"])
    if not in_city_radius(lat, lon, city):
        return None
    return nearest_barrio(lat, lon, city)


def infer_barrio(*parts: str, city: str = DEFAULT_CITY, barrio_hint: str | None = None) -> tuple[str, str, float, float]:
    text = _haystack(*([barrio_hint] if barrio_hint else []), *parts)
    city_barrios = barrios_for(city)
    clat, clon = city_center(city)
    label = (CITIES.get(city) or {}).get("label") or (city or "").replace("-", " ").title()
    hinted = None
    if barrio_hint and fold(barrio_hint) not in _GENERIC_BARRIO:
        hinted = _pretty_barrio(barrio_hint)
    match = re.search(
        r"(?:barrio|bº|b°|bo\.?)\s+(\d{2,4}\s*viviendas?|[a-z0-9áéíóúüñ][a-z0-9áéíóúüñ\s]{2,40})",
        text,
    )
    if match:
        hinted = _pretty_barrio(match.group(1))
    if not hinted:
        for part in parts:
            if re.search(r"\b(venta|alquiler|dormitorios?|ambientes?)\b", fold(part)):
                continue
            hinted = barrio_from_address(part, city)
            if hinted:
                break
    known = match_known_barrio(hinted, city) if hinted else None
    best = known
    best_len = len(fold(best["name"])) if best else 0
    weak = {"roca", "centro", "comercio", "patagonia", "america", "union", "pioneros"}
    for barrio in city_barrios:
        names = [fold(barrio["name"]), *(barrio.get("aliases") or [])]
        for alias in names:
            if not alias or len(alias) < 5:
                continue
            if alias in weak and f"barrio {alias}" not in text:
                continue
            if alias in text and len(alias) >= best_len:
                best = barrio
                best_len = len(alias)
    if best and (not hinted or fold(best["name"]) in fold(hinted) or fold(hinted) in fold(best["name"])):
        lat = best.get("lat") if best.get("lat") is not None else clat
        lon = best.get("lon") if best.get("lon") is not None else clon
        return best["name"], best["zona"], lat, lon
    resolved = _barrio_from_place_hint(hinted, city) if hinted else None
    if resolved:
        return resolved

    if city == "puerto-madryn":
        street_hit = find_known_street(*parts)
        street_name, number = parse_street(text)
        hit = street_hit
        if not hit and street_name:
            for name, lat, lon, barrio_name in STREETS:
                if street_names_match(name, street_name):
                    hit = (name, lat, lon, barrio_name)
                    break
        if hit:
            _name, lat, lon, barrio_name = hit
            lat2, lon2 = offset_by_number(lat, lon, number, city)
            zona = zona_from_bearing(lat2, lon2, clat, clon)
            name_out = hinted or barrio_name
            if name_out == "Oeste residencial":
                return "Sin clasificar", zona, lat2, lon2
            return name_out, zona, lat2, lon2
    if hinted:
        if fold(hinted) in own_place_names(city):
            return nearest_barrio(clat, clon, city)
        zona = zona_from_bearing(clat, clon, clat, clon)
        if "norte" in text or "287" in fold(hinted):
            zona = "Zona Norte"
        if "sur" in text:
            zona = "Zona Sur"
        return hinted, zona, clat, clon
    if "zona sur" in text:
        return "Zona Sur", "Zona Sur", clat, clon
    if "zona norte" in text:
        return "Zona Norte", "Zona Norte", clat, clon
    if "zona este" in text:
        return "Zona Este", "Zona Este", clat, clon
    if "zona oeste" in text:
        return "Zona Oeste", "Zona Oeste", clat, clon
    return "Sin clasificar", label, clat, clon


_STREET_SKIP = {
    "venta", "dorm", "dormitorio", "dormitorios", "ambiente", "ambientes",
    "ano", "anos", "año", "años", "mts", "cubiertos", "totales", "departamento",
    "depto", "casa", "terreno", "puerto", "madryn", "trelew", "ph",
    "bano", "banos", "baño", "baños", "publicado", "garage", "cochera",
    "destacado", "video", "usd", "ars", "m2", "amb", "jun", "enero", "febrero",
    "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
    "octubre", "noviembre", "diciembre", "olivan", "propiedades",
    "apto", "credito", "espectacular", "moderno", "ubicacion", "excelente",
    "oportunidad", "lindo", "unico", "hermoso",
    "monoambiente", "duplex", "triplex", "piso", "planta", "unidad", "edificio",
    "aprox", "aproximadamente",
    "lote", "lotes", "parcela", "manzana", "fraccion", "loteo", "mz",
}


def parse_street(text: str) -> tuple[str, int | None]:
    folded = fold(text)
    known_hits: list[tuple] = []
    other_hits: list[tuple] = []
    pattern = re.compile(
        r"(?:(?P<kind>avenida|av\.?|calle|pasaje)\s+)?"
        r"(?P<name>[a-z0-9áéíóúüñ\.]{3,}(?:\s+[a-z0-9áéíóúüñ\.]{2,}){0,4})"
        r"\s+(?:al\s+)?(?P<num>\d{2,5})\b"
    )
    pos = 0
    while pos < len(folded):
        match = pattern.search(folded, pos)
        if not match:
            break
        name = fold(match.group("name"))
        kind = fold(match.group("kind") or "").rstrip(".")
        if kind.startswith("avenida"):
            kind = "avenida"
        elif kind.startswith("av"):
            kind = "av"
        if kind:
            name = f"{kind} {name}".strip()
        if not name:
            pos = match.start("name") + 1
            continue
        from .text_quality import is_plot_street_name

        if is_plot_street_name(name) or is_plot_street_name(name.split()[0]):
            pos = match.end()
            continue
        number = int(match.group("num"))
        words = name.split()
        suffixes = [" ".join(words[i:]) for i in range(len(words))]
        picked = False
        for candidate in suffixes:
            cwords = candidate.split()
            if len(candidate) < 3 or any(word in _STREET_SKIP for word in cwords):
                continue
            picked = True
            score = (0 if _street_is_known(candidate) else 1, -len(cwords), -len(candidate))
            if _street_is_known(candidate):
                known_hits.append((score, candidate, number))
            else:
                other_hits.append((score, candidate, number))
        pos = match.end() if picked else match.start("name") + 1
    known_hits.sort()
    other_hits.sort()
    if known_hits:
        return known_hits[0][1], known_hits[0][2]
    if other_hits:
        return other_hits[0][1], other_hits[0][2]
    return "", None


def needs_address_repin(item) -> bool:
    """Hay calle y altura pero el pin todavía no es el de esa dirección."""
    extra = getattr(item, "extra", None) or {}
    kind = str(extra.get("location_kind") or "")
    lat = getattr(item, "lat", None)
    if kind == "exact" and lat is not None:
        return False
    if getattr(item, "has_exact_location", False) and kind not in {"approx", "saved", "unknown"}:
        return False
    street, number = parse_street(
        " ".join(
            p
            for p in (
                getattr(item, "title", "") or "",
                getattr(item, "address", "") or "",
            )
            if p
        )
    )
    return bool(street and number)


def street_names_match(a: str, b: str) -> bool:
    left, right = fold(a), fold(b)
    if not left or not right:
        return False
    if left == right:
        return True
    if min(len(left), len(right)) < 5:
        return False
    return f" {left} " in f" {right} " or f" {right} " in f" {left} "


def _street_is_known(name: str) -> bool:
    if not name:
        return False
    return any(street_names_match(name, street) for street, _lat, _lon, _barrio in STREETS)


def city_only_address(address: str | None) -> bool:
    text = fold(address or "").strip(" ,")
    if not text:
        return True
    weak = {
    "puerto madryn", "puerto madryn chubut", "puerto madryn, chubut",
    "trelew", "trelew chubut", "rawson", "gaiman", "playa union",
    "chubut", "argentina",
    "capital federal", "microcentro", "buenos aires",
}
    compact = re.sub(r"[,/]+", " ", text)
    compact = re.sub(r"\s+", " ", compact).strip()
    return compact in weak or (compact.startswith("puerto madryn") and len(compact) < 32)


def has_street_address(*parts: str) -> bool:
    name, number = parse_street(_haystack(*parts))
    if name and number:
        return True
    text = _haystack(*parts)
    if re.search(r"\b(calle|av\.?|avenida|pasaje)\s+[a-záéíóúüñ]{3,}", text):
        return True
    return find_known_street(*parts) is not None


def location_incomplete(item) -> bool:
    """Falta calle/altura y cruce. Van primero a la LLM; si tampoco hay pin, no salen al mapa."""
    return not has_street_or_corner(item)


def has_street_or_corner(item) -> bool:
    extra = getattr(item, "extra", None) or {}
    if extra.get("intersection") or extra.get("location_kind") == "intersection":
        return True
    return has_street_address(
        getattr(item, "title", "") or "",
        getattr(item, "address", "") or "",
        getattr(item, "description", "") or "",
    )


def can_place_on_map(item) -> bool:
    """Sale al mapa sin esperar la LLM si hay calle/cruce o cualquier pin (también aprox.)."""
    if has_street_or_corner(item):
        return True
    lat = getattr(item, "lat", None)
    lon = getattr(item, "lon", None)
    return lat is not None and lon is not None


def offset_by_number(
    lat: float, lon: float, number: int | None, city: str | None = None
) -> tuple[float, float]:
    if not number:
        return lat, lon
    # ~0.55 m por número de puerta; en Madryn las calles crecen al sur, en CABA eso cae al Riachuelo.
    meters = min(number, 4000) * 0.55
    south = offset_meters(lat, lon, south=meters)
    if not in_water(south[0], south[1], city):
        return south
    north = offset_meters(lat, lon, south=-meters)
    if not in_water(north[0], north[1], city):
        return north
    return lat, lon


def offset_meters(lat: float, lon: float, south: float = 0.0, east: float = 0.0) -> tuple[float, float]:
    dlat = -south / 111_320
    dlon = east / (111_320 * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon


# Portales que publican el punto como zona / manzana, no como la puerta.
APPROX_PORTALS = {"properati"}
APPROX_CELL_M = 180


def listing_coords_are_exact(
    source: str | None,
    has_exact: bool,
    location_kind: str | None = None,
) -> bool:
    """Punto de puerta o esquina geocodificada, no la celda de 180 m del portal."""
    kind = (location_kind or "").strip().lower()
    if kind == "exact":
        return True
    if kind == "intersection":
        return bool(has_exact)
    if kind in {"approx", "saved", "unknown"}:
        return False
    if (source or "").lower() in APPROX_PORTALS:
        return False
    return bool(has_exact)


def has_direccion_fields(data: dict) -> bool:
    """Calle y altura (scrape o LLM), no un barrio suelto ni 'lote 12'."""
    from .text_quality import address_quality, is_plot_label, is_plot_street_name

    street = str(data.get("street") or "").strip()
    number = data.get("street_number")
    if is_plot_street_name(street) or is_plot_label(street, str(number or ""), str(data.get("address") or "")):
        return False
    if street and number not in {None, "", 0, "0"}:
        return True
    return address_quality(str(data.get("address") or "")) >= 5


def has_interseccion_fields(data: dict) -> bool:
    inter = str(data.get("intersection") or "").strip()
    if not inter:
        return False
    if " y " not in inter.lower():
        return True
    left, right = re.split(r"\s+y\s+", inter, maxsplit=1, flags=re.I)
    return not street_names_match(left, right)


def has_scraped_approx_fields(data: dict) -> bool:
    """Pin de manzana que vino del aviso, no una grilla inventada."""
    if data.get("portal_lat") is not None and data.get("portal_lon") is not None:
        return True
    if str(data.get("approx_address") or "").strip():
        return True
    source = str(data.get("source") or "").lower()
    if source in APPROX_PORTALS and data.get("lat") is not None and data.get("lon") is not None:
        return True
    return False


def listing_location_data(item) -> dict:
    extra = getattr(item, "extra", None) or {}
    return {
        "source": getattr(item, "source", None) or "",
        "street": extra.get("street") or "",
        "street_number": extra.get("street_number"),
        "address": getattr(item, "address", None) or "",
        "intersection": extra.get("intersection") or extra.get("between") or "",
        "has_exact_location": getattr(item, "has_exact_location", False),
        "location_kind": extra.get("location_kind") or "",
        "portal_exact": extra.get("portal_exact"),
        "portal_approx": extra.get("portal_approx"),
        "portal_lat": extra.get("portal_lat"),
        "portal_lon": extra.get("portal_lon"),
        "lat": getattr(item, "lat", None),
        "lon": getattr(item, "lon", None),
    }


def location_is_precise(data: dict) -> bool:
    """Calle+altura, esquina real o geo exacta del portal: no es la celda de 180 m."""
    if has_direccion_fields(data) or has_interseccion_fields(data):
        return True
    return bool(data.get("portal_exact")) and not data.get("portal_approx")


def stamp_location_flags(data: dict) -> dict:
    real = bool(data.get("has_exact_location")) and not bool(data.get("location_approx"))
    data["location_real"] = real
    data["location_missing"] = (
        not real
        and not has_direccion_fields(data)
        and not has_interseccion_fields(data)
        and not has_scraped_approx_fields(data)
    )
    return data


def _as_metric_int(value) -> int | None:
    if value in {None, "", 0, "0"}:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0 or abs(number - round(number)) > 0.01:
        return None
    return int(round(number))


def listing_metric_numbers(item) -> set[int]:
    nums: set[int] = set()
    for val in (
        getattr(item, "covered_m2", None),
        getattr(item, "total_m2", None),
        getattr(item, "rooms", None),
        getattr(item, "bedrooms", None),
        getattr(item, "bathrooms", None),
        getattr(item, "parking", None),
        getattr(item, "age_years", None),
    ):
        n = _as_metric_int(val)
        if n is not None:
            nums.add(n)
    extra = getattr(item, "extra", None) or {}
    llm = extra.get("llm") if isinstance(extra.get("llm"), dict) else {}
    for key in ("floor", "expenses", "covered_m2", "total_m2", "rooms", "bedrooms", "bathrooms"):
        n = _as_metric_int(llm.get(key) if llm else extra.get(key))
        if n is not None:
            nums.add(n)
    n = _as_metric_int(extra.get("expenses"))
    if n is not None:
        nums.add(n)
    blob = " ".join(
        str(p)
        for p in (
            getattr(item, "title", ""),
            getattr(item, "address", ""),
            getattr(item, "description", ""),
        )
        if p
    )
    for match in re.finditer(
        r"(\d{1,5})\s*(?:cuotas?|pisos?|amb(?:ientes?)?|dorm(?:itorios?)?|años?|mts?|m²|m2)\b",
        blob,
        re.I,
    ):
        nums.add(int(match.group(1)))
    for match in re.finditer(
        r"\b(?:cuotas?|piso|planta)\s*(?:n(?:ro|umero|úmero)?\.?\s*)?(\d{1,4})\b",
        blob,
        re.I,
    ):
        nums.add(int(match.group(1)))
    return nums


def recovered_location_overlay(item) -> dict:
    """Calle, altura o esquina desde el aviso, el LLM o el geocode. No inventa barrios."""
    from .text_quality import address_quality, is_plot_label, is_plot_street_name

    extra = getattr(item, "extra", None) or {}
    street = str(extra.get("street") or "").strip()
    number = extra.get("street_number")
    address = str(getattr(item, "address", None) or "").strip()
    intersection = str(extra.get("intersection") or "").strip()
    geo_label = str(extra.get("geo_label") or "").strip()
    metrics = listing_metric_numbers(item)
    parsed_addr, parsed_num = parse_street(address)
    if parsed_num and parsed_num in metrics:
        address = _pretty_barrio(parsed_addr) if parsed_addr else ""
        number = None if number in metrics or number == parsed_num else number

    def take_street_number(st: str, num: int | None, raw: str = "") -> None:
        nonlocal street, number, address, geo_label
        from .text_quality import is_plot_label, is_plot_street_name

        if not st or not num or num in metrics or num <= 0:
            return
        if is_plot_street_name(st) or is_plot_label(st, str(num), raw):
            return
        pretty = _pretty_barrio(st)
        if not street:
            street = pretty
        if number in {None, "", 0, "0"} or number in metrics:
            number = num
        shown = raw.strip() if raw.strip() and not raw.isupper() else f"{pretty} {num}"
        if raw.isupper():
            shown = _pretty_barrio(raw.split(",")[0])
        if address_quality(shown) > address_quality(address):
            address = shown
        if raw:
            geo_label = raw.split(",")[0].strip()

    def consider(label: str) -> None:
        nonlocal street, address
        raw = (label or "").split(",")[0].strip()
        if not raw:
            return
        st, num = parse_street(raw)
        if st and num:
            take_street_number(st, num, raw)
        elif st and not street:
            street = _pretty_barrio(st)
            weak = fold(address) in {fold(getattr(item, "barrio", "") or ""), fold(getattr(item, "city", "") or "")}
            if not address or weak:
                address = street

    llm_geo = extra.get("llm_geo") if isinstance(extra.get("llm_geo"), dict) else {}
    plot_addr = is_plot_label(address) or is_plot_street_name(str(extra.get("street") or ""))
    if not plot_addr:
        consider(str(llm_geo.get("label") or ""))
        consider(geo_label)
    llm = extra.get("llm") if isinstance(extra.get("llm"), dict) else {}
    consider(str(llm.get("address_text") or ""))
    llm_street = str(llm.get("street") or "").strip()
    llm_num = _as_metric_int(llm.get("street_number") or llm.get("number"))
    if llm_street and llm_num and llm_num not in metrics:
        take_street_number(llm_street, llm_num)
    elif llm_street and not street and not is_plot_street_name(llm_street):
        street = _pretty_barrio(llm_street)
    ca = str(llm.get("corner_a") or "").strip()
    cb = str(llm.get("corner_b") or "").strip()
    if ca and cb and not intersection:
        intersection = f"{_pretty_barrio(ca)} y {_pretty_barrio(cb)}"
    if not intersection:
        intersection = str(extra.get("between") or "").strip()
    if number in metrics or number in {None, "", 0, "0"}:
        number = None
    if street:
        street = _pretty_barrio(street)
    if is_plot_street_name(street) or is_plot_label(address, street, str(number or "")):
        street = ""
        number = None
        geo_label = ""
    if street and number is not None:
        shown = f"{street} {number}"
        addr_street, _addr_n = parse_street(address)
        conflict = bool(addr_street and not street_names_match(street, addr_street))
        if conflict or address_quality(shown) >= address_quality(address):
            address = shown
    return {
        "street": street,
        "street_number": number,
        "address": address,
        "intersection": intersection,
        "geo_label": geo_label,
    }


def apply_recovered_location(item) -> bool:
    overlay = recovered_location_overlay(item)
    extra = dict(getattr(item, "extra", None) or {})
    changed = False
    if overlay.get("street") and extra.get("street") != overlay["street"]:
        extra["street"] = overlay["street"]
        changed = True
    elif not overlay.get("street") and extra.get("street"):
        from .text_quality import is_plot_street_name, is_plot_label

        if is_plot_street_name(str(extra.get("street") or "")) or is_plot_label(
            str(extra.get("street") or ""), str(extra.get("street_number") or "")
        ):
            extra.pop("street", None)
            extra.pop("street_number", None)
            changed = True
    if overlay.get("street_number") not in {None, ""} and extra.get("street_number") != overlay["street_number"]:
        extra["street_number"] = overlay["street_number"]
        changed = True
    elif overlay.get("street_number") in {None, ""} and extra.get("street_number") not in {None, ""}:
        from .text_quality import is_plot_street_name, is_plot_label

        if is_plot_street_name(str(extra.get("street") or overlay.get("street") or "")) or is_plot_label(
            item.address or "", str(extra.get("street_number") or "")
        ):
            extra.pop("street_number", None)
            changed = True
    if overlay.get("intersection") and not extra.get("intersection"):
        extra["intersection"] = overlay["intersection"]
        changed = True
    if overlay.get("geo_label") and extra.get("geo_label") != overlay["geo_label"]:
        extra["geo_label"] = overlay["geo_label"]
        changed = True
    addr = overlay.get("address") or ""
    if addr and addr != (item.address or ""):
        from .text_quality import address_quality

        old_street, _old_n = parse_street(item.address or "")
        new_street, _new_n = parse_street(addr)
        conflict = bool(old_street and new_street and not street_names_match(old_street, new_street))
        if (
            conflict
            or address_quality(addr) > address_quality(item.address or "")
            or not (item.address or "").strip()
        ):
            item.address = addr
            changed = True
    item.extra = extra
    return changed


def snap_to_approx_cell(lat: float, lon: float, cell_m: float = APPROX_CELL_M) -> tuple[float, float]:
    lat_m = 111_320.0
    step_lat = cell_m / lat_m
    i = round(lat / step_lat)
    slat = i * step_lat
    lon_m = 111_320.0 * math.cos(math.radians(slat))
    step_lon = cell_m / max(abs(lon_m), 1.0)
    j = round(lon / step_lon)
    slon = j * step_lon
    return round(slat, 6), round(slon, 6)


def apply_public_location(data: dict) -> dict:
    """Pin redondo solo con calle+altura, esquina real o geolocalización exacta del portal."""
    lat, lon = data.get("lat"), data.get("lon")
    exact = location_is_precise(data)
    if lat is None or lon is None:
        data["lat"] = None
        data["lon"] = None
        data["has_exact_location"] = False
        data["location_approx"] = True
        data["approx_span_m"] = APPROX_CELL_M
        data["approx_cell"] = ""
        return stamp_location_flags(data)
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        data["lat"] = None
        data["lon"] = None
        data["has_exact_location"] = False
        data["location_approx"] = True
        data["approx_span_m"] = APPROX_CELL_M
        data["approx_cell"] = ""
        return stamp_location_flags(data)
    if exact:
        data["lat"] = lat_f
        data["lon"] = lon_f
        data["has_exact_location"] = True
        data["location_approx"] = False
        data["approx_span_m"] = 0
        data["approx_cell"] = ""
        return stamp_location_flags(data)
    slat, slon = snap_to_approx_cell(lat_f, lon_f)
    data["lat"] = slat
    data["lon"] = slon
    data["has_exact_location"] = False
    data["location_approx"] = True
    data["approx_span_m"] = APPROX_CELL_M
    data["approx_cell"] = f"{slat:.5f}:{slon:.5f}"
    return stamp_location_flags(data)


def jitter(lat: float, lon: float, key: str, city: str = DEFAULT_CITY) -> tuple[float, float]:
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()
    angle = int(digest[:4], 16) / 65535 * 2 * math.pi
    radius = 35 + (int(digest[4:8], 16) / 65535) * 90
    east = math.cos(angle) * radius
    south = math.sin(angle) * radius
    lat2, lon2 = offset_meters(lat, lon, south=south, east=east)
    return _snap_west_if_water(lat2, lon2, city)


def locate(
    listing_id: str,
    lat: float | None,
    lon: float | None,
    *parts: str,
    city: str = DEFAULT_CITY,
    barrio_hint: str | None = None,
    allow_approx: bool = True,
    extracted: dict | None = None,
) -> tuple[str, str, float | None, float | None, bool, str]:
    barrio, zona, blat, blon = infer_barrio(*parts, city=city, barrio_hint=barrio_hint)
    street_name, number = parse_street(_haystack(*parts))
    if lat and lon and _off_land(lat, lon, city):
        lat = lon = None
    if lat and lon and (_dummy_coords(lat, lon, city) or _fake_number_offset(lat, lon, number, city)):
        lat = lon = None
    extra = extracted or {}
    try:
        plat, plon = float(extra["portal_lat"]), float(extra["portal_lon"])
    except (KeyError, TypeError, ValueError):
        plat = plon = None
    if plat is not None and _mapped_water(plat, plon, city):
        plat = plon = None
    if plat is not None and city and not in_city_radius(plat, plon, city):
        portal_exact = bool(extra.get("portal_exact")) and not extra.get("portal_approx")
        contained = barrio_containing(plat, plon, city=city)
        if contained:
            remember_barrio(city, contained[0], plat, plon)
            return (*contained[:2], plat, plon, portal_exact, "saved")
        remember_barrio(city, barrio, plat, plon)
        zona = zona_from_bearing(plat, plon, city=city)
        return barrio, zona, plat, plon, portal_exact, "saved"
    found: dict = {}
    try:
        from .geo_tools import parse_plain_locations, run_location_tools

        blob = " ".join(p for p in parts if p)
        found = parse_plain_locations(blob)
        extra = extracted or {}
        extracted_geo = extra.get("street") or extra.get("corner_a") or extra.get("between")
        parsed_geo = (
            found.get("corners")
            or found.get("between")
            or (found.get("street") and found.get("number"))
        )
        # El pin EXACT del portal no se pisa con números sueltos de la descripción
        # (cuotas, m², precios). Solo se geocodifica si el aviso ya trae calle+altura,
        # esquina o entre-calles extraídos.
        if extracted_geo or (parsed_geo and not extra.get("portal_exact")):
            tools = run_location_tools(blob, city, extra)
        else:
            tools = {}
    except Exception:
        tools = {}
    geo = tools.get("geo") if isinstance(tools, dict) else None
    if geo and geo.get("ok") and geo.get("lat") is not None and geo.get("lon") is not None:
        glat, glon = float(geo["lat"]), float(geo["lon"])
        if _mapped_water(glat, glon, city):
            geo = None
        else:
            portal_exact = bool(extra.get("portal_exact")) and not extra.get("portal_approx")
            if plat is not None and portal_exact and distance_km(glat, glon, plat, plon) > 1.5:
                geo = None
                lat, lon = plat, plon
            else:
                exact = not bool(geo.get("approx"))
                pin_kind = str(geo.get("pin_kind") or ("address" if geo.get("number") else "intersection"))
                contained = barrio_containing(glat, glon, city=city)
                if contained:
                    remember_barrio(city, contained[0], glat, glon)
                    return (*contained[:2], glat, glon, exact, pin_kind)
                remember_barrio(city, barrio, glat, glon)
                zona = zona_from_bearing(glat, glon, city=city)
                return barrio, zona, glat, glon, exact, pin_kind
    had_address = False
    from .geo_tools import _usable_street_pin

    door = _usable_street_pin(
        str((extracted or {}).get("street") or ""),
        (extracted or {}).get("number"),
    )
    parsed_door = _usable_street_pin(street_name or "", number)
    had_address = door or (parsed_door and not (extracted or {}).get("portal_exact"))
    had_corner = bool(found.get("corners")) or bool((extracted or {}).get("corner_a") and (extracted or {}).get("corner_b"))
    had_between = bool(found.get("between")) or bool((extracted or {}).get("between"))
    if lat and lon and abs(lat) > 1 and abs(lon) > 1 and not _dummy_coords(lat, lon, city):
        saved_exact = not (had_address or had_corner or had_between)
        if (extracted or {}).get("portal_exact") and not door:
            saved_exact = True
        contained = barrio_containing(lat, lon, city=city)
        if contained:
            remember_barrio(city, contained[0], lat, lon)
            return (*contained[:2], lat, lon, saved_exact, "saved")
        if barrio == "Sin clasificar":
            barrio, zona, _, _ = nearest_barrio(lat, lon, city=city)
        else:
            zona = zona_from_bearing(lat, lon, city=city)
        remember_barrio(city, barrio, lat, lon)
        return barrio, zona, lat, lon, saved_exact, "saved"
    if not allow_approx:
        return barrio, zona, None, None, False, "none"
    street_hit = find_known_street(*parts)
    if not street_hit and street_name:
        for name, slat, slon, barrio_name in STREETS:
            if street_names_match(street_name, name):
                street_hit = (name, slat, slon, barrio_name)
                break
    if street_hit:
        _name, slat, slon, barrio_name = street_hit
        long_axis = barrio_name in {"Centro", "Zona Sur", "Zona Norte", "Oeste residencial"}
        if number and long_axis:
            lat2, lon2 = offset_by_number(slat, slon, number, city)
            if _mapped_water(lat2, lon2, city):
                lat2, lon2 = slat, slon
            if _mapped_water(lat2, lon2, city):
                street_hit = None
            else:
                zona = zona_from_bearing(lat2, lon2, *city_center(city))
                if barrio == "Sin clasificar" and barrio_name != "Oeste residencial":
                    barrio = barrio_name
                return barrio, zona, lat2, lon2, False, "address"
        if lat and lon and abs(lat) > 1 and abs(lon) > 1:
            exact = bool((extracted or {}).get("portal_exact"))
            contained = barrio_containing(lat, lon, city=city)
            if contained:
                remember_barrio(city, contained[0], lat, lon)
                return (*contained[:2], lat, lon, exact, "saved")
            zona = zona_from_bearing(lat, lon, city=city)
            remember_barrio(city, barrio, lat, lon)
            return barrio, zona, lat, lon, exact, "saved"
        lat2, lon2 = jitter(slat, slon, listing_id, city)
        zona = zona_from_bearing(lat2, lon2, *city_center(city))
        if barrio == "Sin clasificar" and barrio_name != "Oeste residencial":
            barrio = barrio_name
        return barrio, zona, lat2, lon2, False, "approx"
    if street_name and number:
        slot_lat, slot_lon = approx_slot(barrio, zona, listing_id, city=city)
        return barrio, zona, slot_lat, slot_lon, False, "saved"
    if has_street_address(*parts):
        jlat, jlon = jitter(blat, blon, listing_id, city)
        return barrio, zona, jlat, jlon, False, "saved"
    slot_lat, slot_lon = approx_slot(barrio, zona, listing_id, city=city)
    return barrio, zona, slot_lat, slot_lon, False, "saved"


_CATALOG_PINS = (
    (-38.4161, -63.6167),
    (-36.252246, -61.027394),
    (-36.253863, -61.030662),
    (-36.253863, -61.028657),
    (-34.413762, -58.6162),
)


def _dummy_coords(lat: float, lon: float, city: str) -> bool:
    clat, clon = city_center(city)
    if abs(lat - clat) < 0.0007 and abs(lon - clon) < 0.0007:
        return True
    if abs(lon - clon) < 1e-5:
        return True
    if abs(lat + 38.416) < 0.05 and abs(lon + 63.616) < 0.05:
        return True
    for plat, plon in _CATALOG_PINS:
        if abs(lat - plat) < 0.004 and abs(lon - plon) < 0.004:
            return True
    return _off_land(lat, lon, city)


def _fake_number_offset(lat: float, lon: float, number: int | None, city: str) -> bool:
    if lat is None or lon is None or not number:
        return False
    samples = [city_center(city), *[(b["lat"], b["lon"]) for b in barrios_for(city)]]
    for slat, slon in samples:
        elat, elon = offset_by_number(slat, slon, number, city)
        if abs(lat - elat) < 2e-5 and abs(lon - elon) < 2e-5:
            return True
    return False


# Costa este de Madryn (longitud máxima que sigue siendo tierra), de sur a norte.
_MADRYN_SHORE = (
    (-42.830, -65.018),
    (-42.800, -65.010),
    (-42.788, -65.006),
    (-42.780, -65.018),
    (-42.770, -65.031),
    (-42.760, -65.0335),
    (-42.753, -65.0350),
    (-42.745, -65.033),
    (-42.720, -65.040),
    (-42.670, -65.048),
    (-42.640, -65.050),
    (-42.600, -65.050),
)


def _interp_shore(lat: float) -> float:
    pts = _MADRYN_SHORE
    if lat <= pts[0][0]:
        return pts[0][1]
    if lat >= pts[-1][0]:
        return pts[-1][1]
    for (lat_a, lon_a), (lat_b, lon_b) in zip(pts, pts[1:]):
        if lat_a <= lat <= lat_b:
            t = (lat - lat_a) / (lat_b - lat_a) if lat_b != lat_a else 0
            return lon_a + t * (lon_b - lon_a)
    return -65.035


def _mapped_water(lat: float, lon: float, city: str | None) -> bool:
    key = (round(float(lat), 5), round(float(lon), 5))
    cached = _WATER_POINTS.get(key)
    if cached is not None:
        return cached
    return _point_in_rings(float(lat), float(lon), _water_rings(city))


def remember_water(lat: float, lon: float, wet: bool) -> None:
    _WATER_POINTS[(round(float(lat), 5), round(float(lon), 5))] = bool(wet)


def remember_water_rings(city: str, rings: list[list[list[float]]]) -> None:
    cid = (city or "").strip()
    if not cid:
        return
    cleaned = [ring for ring in rings if isinstance(ring, list) and len(ring) >= 4]
    if cleaned:
        _WATER_RINGS[cid] = cleaned
        _WATER_MISS.discard(cid)
    else:
        _WATER_MISS.add(cid)


def _water_rings(city: str | None) -> list[list[list[float]]]:
    seen: set[str] = set()
    for cid in (city or "", *same_place_ids(city)):
        if not cid or cid in seen:
            continue
        seen.add(cid)
        cached = _WATER_RINGS.get(cid)
        if cached:
            return cached
        if cid in _WATER_MISS:
            continue
        if os.environ.get("PROPMAP_TEST") == "1":
            _WATER_MISS.add(cid)
            continue
        from . import store

        store.init()
        raw = store.get_meta(f"osm_water:{cid}")
        if not raw:
            _WATER_MISS.add(cid)
            continue
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            _WATER_MISS.add(cid)
            continue
        rings = loaded if isinstance(loaded, list) else []
        cleaned = [ring for ring in rings if isinstance(ring, list) and len(ring) >= 4]
        if cleaned:
            _WATER_RINGS[cid] = cleaned
            return cleaned
        _WATER_MISS.add(cid)
    return []


def in_water(lat: float | None, lon: float | None, city: str | None = "puerto-madryn") -> bool:
    if lat is None or lon is None:
        return False
    if _off_land(float(lat), float(lon), city or "puerto-madryn"):
        return True
    key = (round(float(lat), 5), round(float(lon), 5))
    cached = _WATER_POINTS.get(key)
    if cached is not None:
        return cached
    rings = _water_rings(city)
    if rings:
        wet = _point_in_rings(float(lat), float(lon), rings)
        _WATER_POINTS[key] = wet
        return wet
    if os.environ.get("PROPMAP_TEST") == "1":
        return False
    try:
        from .places import reverse_is_water

        wet = reverse_is_water(float(lat), float(lon))
    except Exception:
        wet = None
    if wet is None:
        return False
    _WATER_POINTS[key] = bool(wet)
    return bool(wet)


def drop_water_pin(item) -> bool:
    """Si el pin quedó en el río, usa el del portal en tierra o lo saca."""
    extra = dict(getattr(item, "extra", None) or {})
    home = str(getattr(item, "city", None) or extra.get("search_city") or "")
    try:
        lat, lon = float(item.lat), float(item.lon)
    except (TypeError, ValueError):
        return False
    if not _mapped_water(lat, lon, home):
        return False
    try:
        plat, plon = float(extra["portal_lat"]), float(extra["portal_lon"])
    except (KeyError, TypeError, ValueError):
        plat = plon = None
    if plat is not None and not _mapped_water(plat, plon, home):
        item.lat, item.lon = plat, plon
        extra["location_kind"] = "approx"
        extra["pin_kind"] = "saved"
        item.has_exact_location = False
        calc, zona = barrio_from_pin(plat, plon, home)
        if calc and calc != "Sin clasificar":
            item.barrio = calc
            if zona:
                item.zona = zona
        item.extra = extra
        return True
    item.lat = item.lon = None
    extra["location_kind"] = "unknown"
    extra["pin_kind"] = "none"
    item.has_exact_location = False
    item.extra = extra
    return True


def _off_land(lat: float | None, lon: float | None, city: str | None) -> bool:
    if lat is None or lon is None or (city or "puerto-madryn") != "puerto-madryn":
        return False
    if not (-43.05 <= lat <= -42.55 and -65.20 <= lon <= -64.80):
        return False
    if lat <= -42.788 and lon <= -64.85:
        return False
    if -42.790 <= lat <= -42.772 and lon <= -64.990:
        return False
    return lon > _interp_shore(lat)


def _snap_west_if_water(lat: float, lon: float, city: str) -> tuple[float, float]:
    if not _off_land(lat, lon, city):
        return lat, lon
    for _ in range(10):
        lat, lon = offset_meters(lat, lon, east=-80)
        if not _off_land(lat, lon, city):
            break
    return lat, lon


def find_known_street(*parts: str) -> tuple[str, float, float, str] | None:
    text = _haystack(*parts)
    street_name, _number = parse_street(text)
    best = None
    for name, lat, lon, barrio_name in STREETS:
        hit = False
        if len(name) >= 5 and re.search(rf"\b{re.escape(name)}\b", text):
            hit = True
        if street_name and street_names_match(street_name, name):
            hit = True
        if hit and (best is None or len(name) > len(best[0])):
            best = (name, lat, lon, barrio_name)
    return best


def approx_slot(barrio: str, zona: str, listing_id: str, city: str = DEFAULT_CITY) -> tuple[float, float]:
    """Agrupa avisos sin dirección en una grilla al costado del barrio, no mezclados con las casas geolocalizadas."""
    city_barrios = barrios_for(city)
    clat, clon = city_center(city)
    if barrio == "Sin clasificar":
        clat, clon = clat - 0.008, clon - 0.012
    else:
        match = next((b for b in city_barrios if b["name"] == barrio), None)
        if match:
            clat, clon = match["lat"], match["lon"]
        else:
            members = [b for b in city_barrios if b["zona"] == zona]
            if members:
                clat = sum(b["lat"] for b in members) / len(members)
                clon = sum(b["lon"] for b in members) / len(members)
            else:
                clat, clon = clat - 0.008, clon - 0.012
    digest = hashlib.md5(listing_id.encode("utf-8")).hexdigest()
    idx = int(digest[:4], 16)
    col, row = idx % 5, (idx // 5) % 7
    lat, lon = offset_meters(clat, clon, south=140 + row * 32, east=-240 + col * 32)
    return _snap_west_if_water(lat, lon, city)


def barrio_ring(lat: float, lon: float, radius_m: float, n: int = 28, city: str = DEFAULT_CITY) -> list[list[float]]:
    pts = []
    for i in range(n):
        ang = 2 * math.pi * i / n
        east = radius_m * 1.15 * math.cos(ang)
        south = radius_m * math.sin(ang)
        plat, plon = offset_meters(lat, lon, south=south, east=east)
        plat, plon = _snap_west_if_water(plat, plon, city)
        pts.append([plat, plon])
    pts.append(pts[0])
    return pts


def convex_hull(points: list[tuple[float, float]]) -> list[list[float]]:
    pts = sorted({(round(p[0], 6), round(p[1], 6)) for p in points})
    if len(pts) == 1:
        lat, lon = pts[0]
        return barrio_ring(lat, lon, 180)
    if len(pts) == 2:
        return [[pts[0][0], pts[0][1]], [pts[1][0], pts[1][1]], [pts[0][0], pts[0][1]]]

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[tuple[float, float]] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    ring = lower[:-1] + upper[:-1]
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    return [[a, b] for a, b in ring]


def _padded_bbox(pts: list[tuple[float, float]], pad_m: float = 320) -> list[list[float]]:
    lats = [p[0] for p in pts]
    lons = [p[1] for p in pts]
    clat = sum(lats) / len(lats)
    dlat = pad_m / 111_320
    dlon = pad_m / (111_320 * (math.cos(math.radians(clat)) or 1e-6))
    minlat, maxlat = min(lats) - dlat, max(lats) + dlat
    minlon, maxlon = min(lons) - dlon, max(lons) + dlon
    if maxlat - minlat < 2 * dlat:
        mid = (minlat + maxlat) / 2
        minlat, maxlat = mid - dlat, mid + dlat
    if maxlon - minlon < 2 * dlon:
        mid = (minlon + maxlon) / 2
        minlon, maxlon = mid - dlon, mid + dlon
    return [
        [minlat, minlon],
        [minlat, maxlon],
        [maxlat, maxlon],
        [maxlat, minlon],
        [minlat, minlon],
    ]


def _cluster_points(pts: list[tuple[float, float]], radius_km: float = 0.7) -> list[list[tuple[float, float]]]:
    remaining = list(pts)
    clusters: list[list[tuple[float, float]]] = []
    while remaining:
        seed = remaining.pop(0)
        group = [seed]
        grew = True
        while grew:
            grew = False
            rest = []
            for point in remaining:
                if any(distance_km(point[0], point[1], other[0], other[1]) <= radius_km for other in group):
                    group.append(point)
                    grew = True
                else:
                    rest.append(point)
            remaining = rest
        clusters.append(group)
    return clusters


def _clip_halfplane(poly: list[list[float]], site: tuple[float, float], other: tuple[float, float]) -> list[list[float]]:
    if len(poly) < 4:
        return poly
    body = poly[:-1] if poly[0] == poly[-1] else list(poly)
    mx = (site[0] + other[0]) / 2
    my = (site[1] + other[1]) / 2
    nx, ny = site[0] - other[0], site[1] - other[1]

    def inside(p: list[float]) -> bool:
        return (p[0] - mx) * nx + (p[1] - my) * ny >= -1e-18

    def meet(a: list[float], b: list[float]) -> list[float]:
        sa = (a[0] - mx) * nx + (a[1] - my) * ny
        sb = (b[0] - mx) * nx + (b[1] - my) * ny
        den = sa - sb
        t = sa / den if abs(den) > 1e-18 else 0.0
        return [a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])]

    out: list[list[float]] = []
    for i, a in enumerate(body):
        b = body[(i + 1) % len(body)]
        ina, inb = inside(a), inside(b)
        if ina and inb:
            out.append(b)
        elif ina and not inb:
            out.append(meet(a, b))
        elif not ina and inb:
            out.append(meet(a, b))
            out.append(b)
    if len(out) < 3:
        return poly
    if out[0] != out[-1]:
        out.append(out[0])
    return out


def neighbor_polygons(groups: dict[str, list[tuple[float, float]]]) -> list[tuple[str, list[list[float]], float, float]]:
    """Un polígono por agrupación de pines, estirado hasta el barrio vecino (Voronoi)."""
    sites: list[tuple[str, float, float]] = []
    all_pts: list[tuple[float, float]] = []
    for name, pts in groups.items():
        for cluster in _cluster_points(pts):
            lat = sum(p[0] for p in cluster) / len(cluster)
            lon = sum(p[1] for p in cluster) / len(cluster)
            sites.append((name, lat, lon))
            all_pts.extend(cluster)
    if not sites or not all_pts:
        return []
    lat0 = sum(p[0] for p in all_pts) / len(all_pts)
    hull = _padded_bbox(all_pts, 320)
    cos0 = math.cos(math.radians(lat0)) or 1e-6

    def proj(lat: float, lon: float) -> tuple[float, float]:
        return lon * cos0, lat

    def unproj(x: float, y: float) -> list[float]:
        return [y, x / cos0]

    clip = [list(proj(lat, lon)) for lat, lon in hull[:-1]]
    clip.append(clip[0][:])
    projected = [(name, proj(lat, lon)) for name, lat, lon in sites]
    out = []
    for i, (name, site) in enumerate(projected):
        cell = [p[:] for p in clip]
        for j, (_other_name, other) in enumerate(projected):
            if i == j:
                continue
            cell = _clip_halfplane(cell, site, other)
            if len(cell) < 4:
                break
        if len(cell) < 4:
            continue
        ring = [unproj(p[0], p[1]) for p in cell]
        if ring[0] != ring[-1]:
            ring.append(ring[0][:])
        clat = sum(p[0] for p in ring[:-1]) / max(1, len(ring) - 1)
        clon = sum(p[1] for p in ring[:-1]) / max(1, len(ring) - 1)
        out.append((name, ring, clat, clon))
    return out


def _match_osm(name: str, osm: list[dict]) -> dict | None:
    folded = fold(name)
    if not folded:
        return None
    for row in osm:
        key = fold(row.get("name") or "")
        if key == folded:
            return row
    for row in osm:
        key = fold(row.get("name") or "")
        if key and folded and min(len(key), len(folded)) >= 5 and (key in folded or folded in key):
            return row
    return None


def barrio_overlays(listings: list | None = None, city: str | None = None, stats_by_barrio: list[dict] | None = None) -> list[dict]:
    """Polígonos para denominar el barrio: OSM si encierra los pines, si no se estiran hasta el vecino."""
    from collections import defaultdict

    items = [item for item in (listings or []) if getattr(item, "barrio", None) and item.barrio != "Sin clasificar"]
    if city:
        items = [item for item in items if (getattr(item, "city", None) or default_city()) == city]
    groups: dict[str, list[tuple[float, float]]] = defaultdict(list)
    rows_by: dict[str, list] = defaultdict(list)
    for item in items:
        if item.lat and item.lon and getattr(item, "has_exact_location", False):
            groups[item.barrio].append((float(item.lat), float(item.lon)))
            rows_by[item.barrio].append(item)
    stats = {(row.get("city"), row["name"]): row for row in (stats_by_barrio or [])}
    osm = city_polygons(city or "")
    grown = neighbor_polygons(groups)
    overlays = []
    used_osm: set[str] = set()

    def _append(name: str, ring: list, lat: float, lon: float, zona: str, rows: list) -> None:
        st = stats.get((city, name)) or {}
        m2s = [item.price_m2 for item in rows if item.price_m2]
        usds = [item.price_usd for item in rows if item.price_usd]
        overlays.append(
            {
                "name": name,
                "zona": zona,
                "city": city or (getattr(rows[0], "city", "") if rows else "") or default_city(),
                "lat": lat,
                "lon": lon,
                "count": st.get("count") or len(rows),
                "median_m2": st.get("median_m2") or (sorted(m2s)[len(m2s) // 2] if m2s else 0),
                "median_usd": st.get("median_usd") or (sorted(usds)[len(usds) // 2] if usds else 0),
                "ring": ring,
            }
        )

    osm_done: set[str] = set()
    for name, pts in groups.items():
        osm_row = _match_osm(name, osm)
        if osm_row and osm_row.get("ring") and osm_row["name"] not in used_osm:
            inside = sum(1 for lat, lon in pts if _point_in_ring(lat, lon, osm_row["ring"]))
            if inside >= max(1, (len(pts) + 2) // 3):
                used_osm.add(osm_row["name"])
                osm_done.add(name)
                zona = osm_row.get("zona") or (rows_by[name][0].zona if rows_by[name] else name)
                _append(osm_row["name"], osm_row["ring"], osm_row["lat"], osm_row["lon"], zona, rows_by[name])
    for name, ring, lat, lon in grown:
        if name in osm_done:
            continue
        rows = rows_by.get(name) or []
        zona = rows[0].zona if rows else name
        _append(name, ring, lat, lon, zona, rows)
    return overlays


def nearest_barrio(lat: float, lon: float, city: str = DEFAULT_CITY) -> tuple[str, str, float, float]:
    own = own_place_names(city)
    hit = barrio_containing(lat, lon, city=city)
    if hit and fold(hit[0]) not in own and fold(hit[0]) not in _GENERIC_BARRIO:
        return hit
    city_barrios = [
        row
        for row in barrios_for(city)
        if row.get("lat") is not None
        and row.get("lon") is not None
        and fold(row.get("name") or "") not in own
        and fold(row.get("name") or "") not in _GENERIC_BARRIO
    ]
    if not city_barrios:
        zona = zona_from_bearing(lat, lon, city=city)
        return zona, zona, lat, lon
    best = city_barrios[0]
    best_d = 1e9
    for barrio in city_barrios:
        d = (barrio["lat"] - lat) ** 2 + (barrio["lon"] - lon) ** 2
        if d < best_d:
            best, best_d = barrio, d
    return best["name"], best["zona"], best["lat"], best["lon"]


def fingerprint(title: str, address: str, price_usd: float | None, covered_m2: float | None, property_type: str) -> str:
    street, number = parse_street(fold(f"{address} {title}"))
    core = f"{property_type}|{street}|{number}|{int(price_usd or 0)}|{int(covered_m2 or 0)}"
    return hashlib.sha1(core.encode("utf-8")).hexdigest()[:16]
