from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from typing import Any

import httpx

from . import store
from .geo import (
    CABA_LAT,
    CABA_LON,
    CITIES,
    DEFAULT_CITY,
    apply_city_extent,
    distance_km,
    fold,
    register_city,
    resolve_city,
    slug_place,
)
from .place_api import lookup_place, province_slug, search_localidades

log = logging.getLogger(__name__)
SEARCH_CACHE_TTL_SEC = 7 * 24 * 3600
_search_mem: dict[str, tuple[float, list[dict]]] = {}
# Consultas a Georef/Nominatim, no un catálogo de coordenadas.
PRIORITY_PLACE_QUERIES = ("Puerto Madryn", "CABA")

NOMINATIM = "https://nominatim.openstreetmap.org/search"
OVERPASS = "https://overpass-api.de/api/interpreter"
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
HEADERS = {
    "User-Agent": "PropMap/1.0 (local real-estate map; no commercial use)",
    "Accept": "application/json",
    "Accept-Language": "es-AR,es;q=0.9",
}
ARG_LAT = -38.4161
ARG_LON = -63.6167


_PLACE_PREFIX = re.compile(
    r"^(puerto|villa|los|las|san|santa|santo|general|rio|río|bahia|bahía|mar|colonia|paso|capilla)(?=[a-záéíóúüñ]{3,})",
    re.I,
)


def _query_variants(query: str) -> list[str]:
    raw = (query or "").strip()
    if not raw:
        return []
    out = [raw]
    spaced = re.sub(r"([a-záéíóúüñ])([A-ZÁÉÍÓÚÜÑ])", r"\1 \2", raw)
    if spaced != raw:
        out.append(spaced)
    dashed = raw.replace("-", " ")
    if dashed != raw:
        out.append(dashed)
    glued = fold(raw).replace(" ", "").replace("-", "")
    prefix = _PLACE_PREFIX.match(glued)
    if prefix:
        out.append(f"{prefix.group(1)} {glued[prefix.end():]}")
    return list(dict.fromkeys(out))


def reset_search_cache() -> None:
    _search_mem.clear()


def priority_place_ids() -> list[str]:
    """IDs ya conocidos en CITIES. No pega a la red."""
    ids: list[str] = []
    seen: set[str] = set()
    for query in PRIORITY_PLACE_QUERIES:
        cid = resolve_city(query)
        if not cid or cid in seen or cid not in CITIES:
            continue
        seen.add(cid)
        ids.append(cid)
    if DEFAULT_CITY in CITIES and DEFAULT_CITY not in seen:
        ids.append(DEFAULT_CITY)
    return ids


def preload_priority_places() -> list[str]:
    """Resuelve Puerto Madryn y CABA por API y deja la búsqueda en cache."""
    load_custom_places()
    ids: list[str] = []
    for query in PRIORITY_PLACE_QUERIES:
        try:
            cid = ensure_place(query=query)
            search_places(query, limit=8)
            if cid:
                ids.append(cid)
        except Exception:
            log.exception("no pude precargar el lugar %s", query)
    return list(dict.fromkeys(ids))


def search_places(query: str, limit: int = 8) -> list[dict]:
    q = (query or "").strip()
    if len(q) < 2:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    folded = fold(q)

    def add(row: dict) -> None:
        cid = row.get("id")
        if not cid or cid in seen:
            return
        seen.add(cid)
        out.append(row)

    for city_id, cfg in CITIES.items():
        names = [cfg["label"], city_id, *(cfg.get("aliases") or [])]
        if _name_hit(folded, names):
            add(_public_city(cfg))
    cached = _read_search_cache(folded)
    if cached:
        for row in cached:
            if isinstance(row, dict):
                add(row)
        if out:
            return out[:limit]
    if out and len(folded) >= 4:
        threading.Thread(
            target=_refresh_place_search,
            args=(q, limit),
            daemon=True,
            name="place-search",
        ).start()
        return out[:limit]
    for row in _search_places_remote(q, limit):
        add(row)
    _write_search_cache(folded, out)
    return out[:limit]


def _search_places_remote(q: str, limit: int) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for variant in _query_variants(q):
        for place in search_localidades(variant, limit=limit):
            parsed = _from_georef_place(place)
            if not parsed:
                continue
            parsed["id"] = _adopt_existing_city(parsed)
            if parsed["id"] in seen:
                continue
            seen.add(parsed["id"])
            out.append(parsed)
            if len(out) >= limit:
                return out[:limit]
    rows = _nominatim({"q": q, "countrycodes": "ar", "format": "json", "addressdetails": 1, "limit": limit})
    for row in rows:
        parsed = _from_nominatim(row)
        if not parsed:
            continue
        parsed["id"] = _adopt_existing_city(parsed)
        if parsed["id"] in seen:
            continue
        seen.add(parsed["id"])
        out.append(parsed)
    return out[:limit]


def _refresh_place_search(q: str, limit: int) -> None:
    try:
        rows = _search_places_remote(q, limit)
        _write_search_cache(fold(q), rows)
    except Exception:
        log.exception("no pude refrescar búsqueda de lugar")


def _read_search_cache(key: str) -> list[dict] | None:
    hit = _search_mem.get(key)
    now = time.time()
    if hit and now - hit[0] <= SEARCH_CACHE_TTL_SEC:
        return hit[1]
    if os.environ.get("PROPMAP_TEST") == "1":
        return None
    try:
        raw = store.get_meta(f"place_search:{key}")
        blob = json.loads(raw) if raw else None
    except Exception:
        return None
    if not isinstance(blob, dict):
        return None
    try:
        saved = float(blob.get("at") or 0)
    except (TypeError, ValueError):
        return None
    if saved and now - saved > SEARCH_CACHE_TTL_SEC:
        return None
    rows = blob.get("places")
    if not isinstance(rows, list):
        return None
    _search_mem[key] = (saved or now, rows)
    return rows


def _write_search_cache(key: str, places: list[dict]) -> None:
    now = time.time()
    rows = [row for row in places if isinstance(row, dict)]
    _search_mem[key] = (now, rows)
    if os.environ.get("PROPMAP_TEST") == "1":
        return
    try:
        store.set_meta(
            f"place_search:{key}",
            json.dumps({"at": now, "places": rows}, ensure_ascii=False),
        )
    except Exception:
        pass


def ensure_place(
    city: str | None = None,
    *,
    label: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    province: str | None = None,
    query: str | None = None,
) -> str:
    from .geo import resolve_city

    raw = (query or city or label or "").strip()
    city_id = resolve_city(raw or city)
    cfg = CITIES.get(city_id)
    if cfg and (cfg.get("builtin") or (cfg.get("lat") is not None and lat is None)):
        threading.Thread(
            target=hydrate_place_extent,
            args=(city_id, raw),
            daemon=True,
            name=f"hydrate-{city_id}",
        ).start()
        ensure_osm_barrios(city_id, blocking=False)
        return city_id
    hit = None
    if lat is None or lon is None:
        found = search_places(raw or city_id, limit=5)
        hit = next((row for row in found if row["id"] == city_id), None) or (found[0] if found else None)
        if hit:
            city_id = hit["id"]
            label = label or hit["label"]
            lat = hit["lat"] if lat is None else lat
            lon = hit["lon"] if lon is None else lon
            province = province or hit.get("province")
            cfg = CITIES.get(city_id)
            if cfg:
                apply_city_extent(
                    city_id,
                    bbox=hit.get("bbox"),
                    radius_km=hit.get("radius_km"),
                    zoom=hit.get("zoom"),
                    slug=hit.get("slug"),
                )
                _save_extents()
                ensure_osm_barrios(city_id, blocking=False)
                return city_id
    if city_id in CITIES and lat is None:
        ensure_osm_barrios(city_id, blocking=False)
        return city_id
    label = label or (hit or {}).get("label") or city_id.replace("-", " ").title()
    if lat is None or lon is None:
        lat, lon = ARG_LAT, ARG_LON
        zoom = 5
    else:
        zoom = int((hit or {}).get("zoom") or (14 if abs(float(lat) - ARG_LAT) > 1 else 5))
    lat_f, lon_f = float(lat), float(lon)
    register_city(
        city_id,
        label=label,
        lat=lat_f,
        lon=lon_f,
        province=province or (hit or {}).get("province") or "",
        barrios=[],
        zoom=zoom,
        aliases=[fold(label), fold(raw)] if raw else [fold(label)],
        slug=(hit or {}).get("slug"),
        bbox=(hit or {}).get("bbox"),
        radius_km=(hit or {}).get("radius_km"),
    )
    _persist()
    _save_extents()
    ensure_osm_barrios(city_id, blocking=False)
    return city_id


def public_place(city_id: str) -> dict:
    cfg = CITIES.get(city_id) or {"id": city_id, "label": city_id, "lat": ARG_LAT, "lon": ARG_LON, "zoom": 5, "province": "", "barrios": []}
    return _public_city(cfg)


def listed_cities(listings: list | None = None) -> list[dict]:
    load_custom_places()
    rows = listings
    if rows is None:
        rows = []
    used: set[str] = set()
    fallback: dict[str, dict] = {}
    for item in rows or []:
        cid = _canonical_listed_id(getattr(item, "city", None) or "")
        if not cid or cid in {"fuera", "otros", "argentina"}:
            continue
        used.add(cid)
        if cid not in fallback:
            fallback[cid] = {
                "id": cid,
                "label": cid.replace("-", " ").title(),
                "lat": getattr(item, "lat", None) or ARG_LAT,
                "lon": getattr(item, "lon", None) or ARG_LON,
                "zoom": 13,
                "province": "",
                "place_ids": [cid],
            }
    wanted = set(used)
    wanted.add(DEFAULT_CITY)
    try:
        from .schedule import searched_ids

        for cid in searched_ids():
            token = _canonical_listed_id(cid) or cid
            if token and token not in {"fuera", "otros", "argentina"}:
                wanted.add(token)
    except Exception:
        pass
    seen: dict[str, dict] = {}
    for cfg in CITIES.values():
        cid = cfg["id"]
        if _canonical_listed_id(cid) != cid:
            continue
        if cid in wanted:
            seen[cid] = _public_city(cfg)
    for cid, row in fallback.items():
        seen.setdefault(cid, row)
    return sorted(seen.values(), key=lambda row: fold(row["label"]))


def ensure_default_city() -> str:
    if DEFAULT_CITY in CITIES:
        return DEFAULT_CITY
    cached = None
    try:
        raw = store.get_meta("default_city_json")
        cached = json.loads(raw) if raw else None
    except Exception:
        cached = None
    if isinstance(cached, dict) and cached.get("lat") is not None:
        register_city(
            DEFAULT_CITY,
            label=str(cached.get("label") or "CABA"),
            lat=float(cached["lat"]),
            lon=float(cached["lon"]),
            province=str(cached.get("province") or "capital-federal"),
            zoom=int(cached.get("zoom") or 12),
            aliases=list(cached.get("aliases") or []),
            builtin=True,
            slug=str(cached.get("slug") or "capital-federal"),
            radius_km=float(cached.get("radius_km") or 16),
        )
        return DEFAULT_CITY
    place = lookup_place("Ciudad Autónoma de Buenos Aires") or lookup_place("CABA")
    lat = float(place["lat"]) if place and place.get("lat") is not None else CABA_LAT
    lon = float(place["lon"]) if place and place.get("lon") is not None else CABA_LON
    province = str((place or {}).get("province") or "capital-federal")
    aliases = [
        "caba",
        "capital federal",
        "ciudad autonoma de buenos aires",
        "ciudad de buenos aires",
        "microcentro-caba",
        "microcentro",
    ]
    register_city(
        DEFAULT_CITY,
        label="CABA",
        lat=lat,
        lon=lon,
        province=province,
        zoom=12,
        aliases=aliases,
        builtin=True,
        slug="capital-federal",
        radius_km=16,
    )
    try:
        store.set_meta(
            "default_city_json",
            json.dumps(
                {
                    "label": "CABA",
                    "lat": lat,
                    "lon": lon,
                    "province": province,
                    "slug": "capital-federal",
                    "radius_km": 16,
                    "zoom": 12,
                    "aliases": aliases,
                },
                ensure_ascii=False,
            ),
        )
    except Exception:
        pass
    return DEFAULT_CITY


def load_custom_places() -> None:
    _load_extents()
    _load_extra_slugs()
    raw = store.get_meta("custom_places")
    if raw:
        try:
            rows = json.loads(raw)
        except json.JSONDecodeError:
            rows = []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict) or not row.get("id"):
                    continue
                if row["id"] in CITIES and CITIES[row["id"]].get("builtin"):
                    continue
                register_city(
                    row["id"],
                    label=row.get("label") or row["id"],
                    lat=float(row.get("lat") or ARG_LAT),
                    lon=float(row.get("lon") or ARG_LON),
                    province=row.get("province") or "",
                    barrios=row.get("barrios") or [],
                    zoom=int(row.get("zoom") or 13),
                    aliases=row.get("aliases") or [],
                    builtin=False,
                    slug=row.get("slug"),
                    bbox=row.get("bbox"),
                    radius_km=row.get("radius_km"),
                )
    ensure_default_city()


def _persist() -> None:
    rows = []
    for cfg in CITIES.values():
        if cfg.get("builtin"):
            continue
        rows.append(
            {
                "id": cfg["id"],
                "label": cfg["label"],
                "lat": cfg["lat"],
                "lon": cfg["lon"],
                "zoom": cfg.get("zoom") or 13,
                "province": cfg.get("province") or "",
                "aliases": cfg.get("aliases") or [],
                "barrios": cfg.get("barrios") or [],
                "slug": cfg.get("slug") or cfg["id"],
                "radius_km": cfg.get("radius_km"),
                "bbox": list(cfg["bbox"]) if cfg.get("bbox") else None,
            }
        )
    store.set_meta("custom_places", json.dumps(rows, ensure_ascii=False))


def _name_hit(folded: str, names: list[str]) -> bool:
    for name in names:
        n = fold(name)
        if not n:
            continue
        if n == folded or n.replace("-", " ") == folded.replace("-", " "):
            return True
        if len(folded) >= 4 and (folded in n or (len(n) >= 5 and n in folded)):
            return True
    return False


def _canonical_listed_id(cid: str) -> str:
    from .geo import same_place_ids

    if not cid:
        return cid
    if cid in same_place_ids(DEFAULT_CITY):
        return DEFAULT_CITY
    return cid


def _public_city(cfg: dict) -> dict:
    from .geo import same_place_ids

    return {
        "id": cfg["id"],
        "label": cfg["label"],
        "lat": cfg["lat"],
        "lon": cfg["lon"],
        "zoom": cfg.get("zoom") or 13,
        "province": cfg.get("province") or "",
        "slug": cfg.get("slug") or cfg["id"],
        "radius_km": cfg.get("radius_km") or 25,
        "bbox": list(cfg["bbox"]) if cfg.get("bbox") else None,
        "barrios": [],
        "place_ids": sorted(same_place_ids(cfg["id"]) or {cfg["id"]}),
    }


def _from_georef_place(place: dict) -> dict | None:
    name = str(place.get("name") or "").strip()
    if not name or place.get("lat") is None or place.get("lon") is None:
        return None
    city_id = slug_place(name)
    province = province_slug(str(place.get("province") or ""))
    slug = "capital-federal" if province == "capital-federal" else city_id
    return {
        "id": city_id,
        "label": name,
        "lat": float(place["lat"]),
        "lon": float(place["lon"]),
        "zoom": 13,
        "province": province,
        "slug": slug,
        "bbox": None,
        "radius_km": 16 if province == "capital-federal" else 25,
        "addresstype": str(place.get("kind") or "localidad"),
        "hint": f"{name}, {place.get('province') or 'Argentina'}",
    }


_SUBURB_TYPES = {"suburb", "neighbourhood", "neighborhood", "quarter", "hamlet"}


def _nominatim_bbox(row: dict):
    box = row.get("boundingbox")
    if not box or len(box) < 4:
        return None
    try:
        south, north, west, east = float(box[0]), float(box[1]), float(box[2]), float(box[3])
    except (TypeError, ValueError):
        return None
    if south >= north or west >= east:
        return None
    return (south, west, north, east)


def _zoom_from_bbox(bbox) -> int:
    south, west, north, east = bbox
    mid = (south + north) / 2
    span = max(north - south, (east - west) * max(0.2, abs(math.cos(math.radians(mid)))))
    if span > 0.45:
        return 10
    if span > 0.22:
        return 11
    if span > 0.11:
        return 12
    if span > 0.05:
        return 13
    return 14


def _from_nominatim(row: dict) -> dict | None:
    addr = row.get("address") or {}
    addresstype = fold(str(row.get("addresstype") or row.get("type") or ""))
    name = (
        addr.get("village")
        or addr.get("town")
        or addr.get("city")
        or addr.get("municipality")
        or addr.get("suburb")
        or addr.get("hamlet")
        or row.get("name")
        or (row.get("display_name") or "").split(",")[0]
    )
    if not name:
        return None
    city_id = slug_place(name)
    state = fold(addr.get("state") or addr.get("state_district") or "")
    province = province_slug(state) if state else ""
    if province == "capital-federal" and addresstype not in _SUBURB_TYPES:
        city_id = "capital-federal"
    try:
        lat, lon = float(row["lat"]), float(row["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    bbox = _nominatim_bbox(row)
    radius_km = None
    zoom = 14
    if bbox:
        from .geo import _extent_radius_km

        radius_km = _extent_radius_km(bbox, lat, lon)
        zoom = _zoom_from_bbox(bbox)
    slug = "capital-federal" if province == "capital-federal" and addresstype not in _SUBURB_TYPES else city_id
    return {
        "id": city_id,
        "label": str(name).strip(),
        "lat": lat,
        "lon": lon,
        "zoom": zoom,
        "province": province or "",
        "slug": slug,
        "bbox": bbox,
        "radius_km": radius_km,
        "addresstype": addresstype,
        "hint": row.get("display_name") or "",
    }


def _adopt_existing_city(parsed: dict) -> str:
    addresstype = parsed.get("addresstype") or ""
    if addresstype in _SUBURB_TYPES:
        return parsed["id"]
    best = None
    best_d = 1e9
    for city_id, cfg in CITIES.items():
        dist = distance_km(parsed["lat"], parsed["lon"], cfg["lat"], cfg["lon"])
        limit = min(float(cfg.get("radius_km") or 15), 14)
        if dist <= limit and dist < best_d:
            best, best_d = city_id, dist
    return best or parsed["id"]


def hydrate_place_extent(city_id: str, query: str = "") -> None:
    cfg = CITIES.get(city_id) or {}
    box = cfg.get("bbox")
    if box:
        from .geo import _extent_radius_km

        if _extent_radius_km(box, cfg["lat"], cfg["lon"]) >= 8:
            return
    candidates = [query, *(cfg.get("aliases") or []), cfg.get("label"), city_id]
    q = next((str(item) for item in candidates if item and len(fold(str(item))) >= 8), cfg.get("label") or city_id)
    rows = _nominatim({"q": q, "countrycodes": "ar", "format": "json", "addressdetails": 1, "limit": 5})
    best = None
    best_span = 0.0
    for row in rows:
        parsed = _from_nominatim(row)
        if not parsed or not parsed.get("bbox"):
            continue
        if distance_km(parsed["lat"], parsed["lon"], cfg["lat"], cfg["lon"]) > 45:
            continue
        span = float(parsed.get("radius_km") or 0)
        if span > best_span:
            best, best_span = parsed, span
    if not best or best_span < 8:
        return
    slug = best.get("slug") or cfg.get("slug")
    if (cfg.get("province") or "") == "capital-federal":
        slug = "capital-federal"
    apply_city_extent(
        city_id,
        bbox=best["bbox"],
        radius_km=best.get("radius_km"),
        zoom=best.get("zoom"),
        slug=slug,
    )
    _save_extents()


def _save_extents() -> None:
    data = {}
    for city_id, cfg in CITIES.items():
        if not cfg.get("bbox") and not cfg.get("extent_from_api"):
            continue
        data[city_id] = {
            "bbox": list(cfg["bbox"]) if cfg.get("bbox") else None,
            "radius_km": cfg.get("radius_km"),
            "zoom": cfg.get("zoom"),
            "slug": cfg.get("slug"),
        }
    store.set_meta("place_extents", json.dumps(data, ensure_ascii=False))


def _load_extra_slugs() -> None:
    for city_id, cfg in CITIES.items():
        raw = store.get_meta(f"extra_slugs:{city_id}")
        if not raw:
            continue
        try:
            slugs = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(slugs, list):
            cfg["extra_slugs"] = [str(s) for s in slugs if s]


def _remember_locality_slugs(city_id: str, lat: float, lon: float) -> None:
    slugs = fetch_osm_locality_slugs(lat, lon, city_id)
    if slugs is None:
        return
    cfg = CITIES.get(city_id)
    if cfg is not None:
        cfg["extra_slugs"] = slugs
    store.set_meta(f"extra_slugs:{city_id}", json.dumps(slugs, ensure_ascii=False))


def fetch_osm_locality_slugs(lat: float, lon: float, city_id: str) -> list[str] | None:
    cfg = CITIES.get(city_id) or {}
    box = cfg.get("bbox")
    if box:
        south, west, north, east = box
        area = f"({south},{west},{north},{east})"
    else:
        radius = int(min(max(float(cfg.get("radius_km") or 16) * 1000, 8000), 24000))
        area = f"(around:{radius},{lat},{lon})"
    data = _overpass(
        f"""
[out:json][timeout:25];
node["place"~"village|hamlet|isolated_dwelling"]{area};
out tags;
"""
    )
    if data is None:
        return None
    skip = {fold(cfg.get("label") or ""), fold(city_id), fold(cfg.get("slug") or "")}
    out: list[str] = []
    from .geo import in_city_radius, slug_place

    for el in data.get("elements") or []:
        tags = el.get("tags") or {}
        name = (tags.get("name") or tags.get("name:es") or "").strip()
        token = slug_place(name)
        if not name or token in skip or token in out:
            continue
        try:
            elat, elon = float(el.get("lat")), float(el.get("lon"))
        except (TypeError, ValueError):
            continue
        if city_id and not in_city_radius(elat, elon, city_id):
            continue
        out.append(token)
        if len(out) >= 8:
            break
    return out


def _load_extents() -> None:
    raw = store.get_meta("place_extents")
    if not raw:
        return
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return
    if not isinstance(data, dict):
        return
    for city_id, ext in data.items():
        if not isinstance(ext, dict):
            continue
        apply_city_extent(
            city_id,
            bbox=ext.get("bbox"),
            radius_km=ext.get("radius_km") if float(ext.get("radius_km") or 0) >= 8 else None,
            zoom=ext.get("zoom"),
            slug=ext.get("slug"),
        )


def _nominatim(params: dict[str, Any]) -> list[dict]:
    if os.environ.get("PROPMAP_TEST") == "1":
        return []
    try:
        with httpx.Client(headers=HEADERS, timeout=18.0) as client:
            response = client.get(NOMINATIM, params=params)
            response.raise_for_status()
            data = response.json()
        time.sleep(1.05)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _overpass(query: str) -> dict | None:
    try:
        with httpx.Client(headers=HEADERS, timeout=55.0) as client:
            last = None
            for url in OVERPASS_URLS:
                try:
                    response = client.post(url, data={"data": query})
                    response.raise_for_status()
                    data = response.json()
                    if isinstance(data, dict) and "elements" in data:
                        return data
                    last = data
                except Exception:
                    continue
            return last if isinstance(last, dict) else None
    except Exception:
        return None


_osm_lock = threading.Lock()
_osm_inflight: set[str] = set()
_osm_tried: set[str] = set()


def osm_pending(city_id: str | None) -> bool:
    if not city_id:
        return False
    with _osm_lock:
        return city_id in _osm_inflight


def ensure_osm_barrios(city_id: str | None, *, blocking: bool = False) -> list[dict]:
    """Carga polígonos de barrios de OSM para cualquier ciudad y los cachea."""
    from .geo import CITIES, city_center, city_polygons, remember_city_polygons

    if not city_id or city_id in {"fuera", "otros"}:
        return []
    cached = city_polygons(city_id)
    have_slugs = (CITIES.get(city_id) or {}).get("extra_slugs") is not None
    if len(cached) >= 6 and have_slugs:
        return cached
    cfg = CITIES.get(city_id) or {}
    lat = cfg.get("lat")
    lon = cfg.get("lon")
    if lat is None or lon is None:
        lat, lon = city_center(city_id)

    def _job() -> None:
        try:
            rows = fetch_osm_barrio_polygons(
                float(lat),
                float(lon),
                city_label=str(cfg.get("label") or ""),
                city_id=city_id,
            )
            if rows is None:
                return
            store.set_meta(f"osm_barrios:{city_id}", json.dumps(rows, ensure_ascii=False))
            if rows:
                remember_city_polygons(city_id, rows)
                _extent_from_rings(city_id, rows)
            _remember_locality_slugs(city_id, float(lat), float(lon))
        finally:
            with _osm_lock:
                _osm_tried.add(city_id)
                _osm_inflight.discard(city_id)

    start = False
    with _osm_lock:
        if city_id in _osm_inflight:
            if not blocking:
                return []
        elif city_id in _osm_tried and not blocking:
            return []
        else:
            _osm_inflight.add(city_id)
            start = True
    if blocking:
        if start:
            _job()
        return city_polygons(city_id)
    if start:
        threading.Thread(target=_job, daemon=True, name=f"osm-barrios-{city_id}").start()
    return []


def _extent_from_rings(city_id: str, rows: list[dict]) -> None:
    if (CITIES.get(city_id) or {}).get("bbox"):
        return
    lats = []
    lons = []
    for row in rows:
        for point in row.get("ring") or []:
            if len(point) >= 2:
                lats.append(float(point[0]))
                lons.append(float(point[1]))
    if len(lats) < 8:
        return
    pad = 0.004
    apply_city_extent(
        city_id,
        bbox=(min(lats) - pad, min(lons) - pad, max(lats) + pad, max(lons) + pad),
    )
    _save_extents()


def fetch_osm_barrio_polygons(
    lat: float, lon: float, city_label: str = "", city_id: str | None = None
) -> list[dict] | None:
    cfg = CITIES.get(city_id or "") or {}
    box = cfg.get("bbox")
    if box:
        south, west, north, east = box
        area = f"({south},{west},{north},{east})"
    else:
        radius = int(min(max(float(cfg.get("radius_km") or 16) * 1000, 8000), 28000))
        area = f"(around:{radius},{lat},{lon})"
    query = f"""
[out:json][timeout:45];
(
  rel["place"~"suburb|neighbourhood|quarter"]{area};
  rel["boundary"="administrative"]["admin_level"~"9|10"]{area};
  way["place"~"suburb|neighbourhood|quarter"]{area};
);
out body geom;
"""
    data = _overpass(query)
    if data is None:
        return None
    out = []
    seen: set[str] = set()
    skip = fold(city_label)
    for el in data.get("elements") or []:
        tags = el.get("tags") or {}
        name = (tags.get("name") or tags.get("name:es") or "").strip()
        folded = fold(name)
        if not name or folded in seen or (skip and folded == skip):
            continue
        outers = []
        geom = el.get("geometry") or []
        if geom:
            pts = [[float(p["lat"]), float(p["lon"])] for p in geom if "lat" in p and "lon" in p]
            if len(pts) >= 4:
                outers.append(pts)
        for member in el.get("members") or []:
            if member.get("type") != "way" or member.get("role") not in {"outer", ""}:
                continue
            mgeom = member.get("geometry") or []
            pts = [[float(p["lat"]), float(p["lon"])] for p in mgeom if "lat" in p and "lon" in p]
            if len(pts) >= 2:
                outers.append(pts)
        if not outers:
            continue
        ring = _simplify_ring(_stitch_ways(outers)[0])
        if len(ring) < 4:
            continue
        clat = sum(p[0] for p in ring[:-1]) / max(1, len(ring) - 1)
        clon = sum(p[1] for p in ring[:-1]) / max(1, len(ring) - 1)
        from .geo import in_city_radius, zona_from_bearing

        if city_id and not in_city_radius(clat, clon, city_id):
            continue
        seen.add(folded)
        out.append(
            {
                "name": name,
                "zona": zona_from_bearing(clat, clon, lat, lon),
                "lat": round(clat, 5),
                "lon": round(clon, 5),
                "ring": [[round(a, 5), round(b, 5)] for a, b in ring],
            }
        )
    return out


def _almost(a: list[float], b: list[float]) -> bool:
    return abs(a[0] - b[0]) < 1e-6 and abs(a[1] - b[1]) < 1e-6


def _stitch_ways(ways: list[list[list[float]]]) -> list[list[list[float]]]:
    unused = [list(w) for w in ways if w]
    rings: list[list[list[float]]] = []
    while unused:
        ring = unused.pop(0)
        changed = True
        while changed:
            changed = False
            for i, way in enumerate(unused):
                if _almost(ring[-1], way[0]):
                    ring += way[1:]
                    unused.pop(i)
                    changed = True
                    break
                if _almost(ring[-1], way[-1]):
                    ring += list(reversed(way[:-1]))
                    unused.pop(i)
                    changed = True
                    break
                if _almost(ring[0], way[-1]):
                    ring = way + ring[1:]
                    unused.pop(i)
                    changed = True
                    break
                if _almost(ring[0], way[0]):
                    ring = list(reversed(way)) + ring[1:]
                    unused.pop(i)
                    changed = True
                    break
        rings.append(ring)
    rings.sort(key=len, reverse=True)
    return rings or [[]]


def _simplify_ring(pts: list[list[float]], max_pts: int = 72) -> list[list[float]]:
    if not pts:
        return []
    if pts[0] != pts[-1]:
        pts = pts + [pts[0]]
    if len(pts) <= max_pts:
        return pts
    step = max(1, (len(pts) - 1) // (max_pts - 1))
    out = pts[::step]
    if out[-1] != pts[-1]:
        out.append(pts[-1])
    if out[0] != out[-1]:
        out.append(out[0])
    return out


_geocode_mem: dict[str, list[dict]] = {}


def geocode_local(query: str, city_id: str = "", limit: int = 3) -> list[dict]:
    q = (query or "").strip()
    if len(q) < 4:
        return []
    cache_key = fold(f"{q}|{city_id}|{limit}")
    if cache_key in _geocode_mem:
        return _geocode_mem[cache_key]
    if os.environ.get("PROPMAP_TEST") == "1":
        _geocode_mem[cache_key] = []
        return []
    try:
        raw = store.get_meta(f"geocode:{cache_key}")
        cached = json.loads(raw) if raw else None
        if isinstance(cached, list):
            _geocode_mem[cache_key] = cached
            return cached
    except Exception:
        pass
    cfg = CITIES.get(city_id) or {}
    label = cfg.get("label") or city_id.replace("-", " ")
    full = f"{q}, {label}, Argentina" if label else f"{q}, Argentina"
    rows = _nominatim(
        {
            "q": full,
            "countrycodes": "ar",
            "format": "json",
            "addressdetails": 1,
            "limit": limit,
        }
    )
    from .geo import in_city_radius

    out = []
    for row in rows:
        try:
            lat, lon = float(row["lat"]), float(row["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        inside = bool(city_id) and in_city_radius(lat, lon, city_id)
        out.append(
            {
                "ok": inside or not city_id,
                "in_city": inside,
                "lat": lat,
                "lon": lon,
                "label": (row.get("display_name") or q).split(",")[0],
                "query": full,
            }
        )
    _geocode_mem[cache_key] = out
    try:
        store.set_meta(f"geocode:{cache_key}", json.dumps(out, ensure_ascii=False))
    except Exception:
        pass
    return out
