from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from .geo import CABA_IDS, CABA_LAT, CABA_LON, CITIES, city_radius_km, default_city, distance_km, in_city_radius

POI_VERSION = "6"
WALK_KM = 0.6
WALK_KM_MIN = 0.6
WALK_KM_MAX = 2.4
WALK_SPACING_MULT = 3.5
CELL_DEG = 0.008
MAX_PER_CELL = 10
CATEGORIES = ("health", "police", "transport", "subway", "train", "beach", "plaza", "shop", "school")
SCORE_GROUPS = {
    "health": ("health",),
    "police": ("police",),
    "transport": ("transport", "subway", "train"),
    "open": ("beach", "plaza"),
    "shop": ("shop",),
    "school": ("school",),
}
CAT_LABEL = {
    "health": "Salud",
    "police": "Policía",
    "transport": "Parada",
    "subway": "Subte",
    "train": "Tren",
    "beach": "Playa",
    "plaza": "Plaza",
    "open": "Plaza o playa",
    "shop": "Súper",
    "school": "Escuela",
}
KIND_LABEL = {
    "hospital": "Hospital",
    "clinic": "Clínica",
    "doctors": "Consultorio",
    "dentist": "Dentista",
    "pharmacy": "Farmacia",
    "police": "Comisaría",
    "bus_station": "Terminal",
    "bus_stop": "Parada",
    "station": "Estación",
    "halt": "Apeadero",
    "tram_stop": "Parada",
    "stop_position": "Parada",
    "platform": "Parada",
    "subway": "Subte",
    "subway_entrance": "Subte",
    "beach": "Playa",
    "beach_resort": "Playa",
    "coastline": "Costa",
    "park": "Parque",
    "square": "Plaza",
    "plaza": "Plaza",
    "supermarket": "Súper",
    "convenience": "Almacén",
    "mall": "Shopping",
    "department_store": "Tienda",
    "grocery": "Almacén",
    "greengrocer": "Verdulería",
    "bakery": "Panadería",
    "marketplace": "Mercado",
    "school": "Escuela",
    "university": "Universidad",
    "college": "Terciario",
}
MAX_PER_CAT = {
    "health": 1800,
    "police": 400,
    "transport": 3500,
    "subway": 400,
    "train": 250,
    "beach": 200,
    "plaza": 1200,
    "shop": 2500,
    "school": 1200,
}
_SHOP_KINDS = {
    "supermarket",
    "convenience",
    "mall",
    "department_store",
    "grocery",
    "greengrocer",
    "bakery",
}
_mem: dict[str, dict[str, Any]] = {}
_walk_km: dict[str, float] = {}
_lock = threading.Lock()
_inflight: set[str] = set()


MAX_POI_MEM = 6


def remember(city_id: str, payload: dict[str, Any]) -> None:
    if not city_id:
        return
    _mem[city_id] = dict(payload)
    _walk_km.pop(city_id, None)
    extra = [cid for cid in _mem if cid != city_id]
    while extra and len(_mem) > MAX_POI_MEM:
        gone = extra.pop(0)
        _mem.pop(gone, None)
        _walk_km.pop(gone, None)


def reset() -> None:
    _mem.clear()
    _walk_km.clear()


def pending(city_id: str | None) -> bool:
    if not city_id:
        return False
    with _lock:
        return city_id in _inflight


def _meta_key(city_id: str) -> str:
    return f"osm_poi:{city_id}:v{POI_VERSION}"


def _looks_caba(lat: float, lon: float) -> bool:
    return abs(lat - CABA_LAT) < 0.08 and abs(lon - CABA_LON) < 0.08


def _is_caba_city(city_id: str) -> bool:
    return city_id in CABA_IDS or city_id == default_city()


def _foreign_caba(city_id: str, lat: float, lon: float) -> bool:
    return (not _is_caba_city(city_id)) and _looks_caba(lat, lon)


def _payload_usable(city_id: str, payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    ver = str(payload.get("version") or "")
    if ver and ver != str(POI_VERSION):
        return False
    origin = payload.get("origin") or {}
    try:
        olat, olon = float(origin["lat"]), float(origin["lon"])
    except (KeyError, TypeError, ValueError):
        olat = olon = None
    if olat is not None and _foreign_caba(city_id, olat, olon):
        return False
    cfg = CITIES.get(city_id) or {}
    try:
        clat, clon = float(cfg["lat"]), float(cfg["lon"])
    except (KeyError, TypeError, ValueError):
        clat = clon = None
    if olat is not None and clat is not None and distance_km(olat, olon, clat, clon) > 80:
        return False
    cats = payload.get("categories") or {}
    if not isinstance(cats, dict):
        return False
    for key in CATEGORIES:
        for row in (cats.get(key) or [])[:6]:
            try:
                lat, lon = float(row["lat"]), float(row["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            if _foreign_caba(city_id, lat, lon):
                return False
            if clat is not None and distance_km(lat, lon, clat, clon) > 80:
                return False
            break
    return True


def _centroid_from_listings(city_id: str) -> tuple[float, float] | None:
    from . import store

    store.init()
    with store.connect() as conn:
        row = conn.execute(
            """
            SELECT AVG(lat), AVG(lon), COUNT(*)
            FROM listings
            WHERE city = ? AND lat IS NOT NULL AND lon IS NOT NULL
            """,
            (city_id,),
        ).fetchone()
    if not row or not row[2] or int(row[2]) < 3:
        return None
    try:
        lat, lon = float(row[0]), float(row[1])
    except (TypeError, ValueError):
        return None
    if _foreign_caba(city_id, lat, lon):
        return None
    return lat, lon


def origin_for_city(city_id: str) -> tuple[float, float] | None:
    try:
        from .places import load_custom_places

        load_custom_places()
    except Exception:
        pass
    cfg = CITIES.get(city_id) or {}
    lat, lon = cfg.get("lat"), cfg.get("lon")
    if lat is not None and lon is not None:
        lat_f, lon_f = float(lat), float(lon)
        if not _foreign_caba(city_id, lat_f, lon_f):
            return lat_f, lon_f
    return _centroid_from_listings(city_id)


def _area_clause(city_id: str) -> str | None:
    cfg = CITIES.get(city_id) or {}
    from .geo import city_outline_bbox

    box = city_outline_bbox(city_id) or cfg.get("bbox")
    origin = origin_for_city(city_id)
    if box and len(box) >= 4 and origin:
        south, west, north, east = box
        if not (south <= float(origin[0]) <= north and west <= float(origin[1]) <= east):
            box = None
    if box and len(box) >= 4 and origin and not _foreign_caba(city_id, float(origin[0]), float(origin[1])):
        south, west, north, east = box
        return f"({south},{west},{north},{east})"
    if not origin:
        return None
    lat, lon = origin
    radius = int(min(max(city_radius_km(city_id) * 1000, 8000), 16000))
    if city_id not in CITIES:
        radius = 18000
    return f"(around:{radius},{lat},{lon})"


def _kind(tags: dict, category: str) -> str:
    if category == "subway":
        return "Subte"
    if category == "train":
        return KIND_LABEL.get(str(tags.get("railway") or ""), "Tren")
    if category == "shop":
        val = str(tags.get("shop") or tags.get("amenity") or "")
        return KIND_LABEL.get(val, CAT_LABEL["shop"])
    if category == "school":
        val = str(tags.get("amenity") or "")
        return KIND_LABEL.get(val, CAT_LABEL["school"])
    for key in ("amenity", "healthcare", "highway", "public_transport", "railway", "station", "natural", "leisure", "place"):
        val = str(tags.get(key) or "")
        if val in KIND_LABEL:
            return KIND_LABEL[val]
    return CAT_LABEL.get(category, category)


def _classify(tags: dict) -> list[str]:
    amenity = str(tags.get("amenity") or "")
    healthcare = str(tags.get("healthcare") or "")
    highway = str(tags.get("highway") or "")
    public_transport = str(tags.get("public_transport") or "")
    railway = str(tags.get("railway") or "")
    station = str(tags.get("station") or "")
    natural = str(tags.get("natural") or "")
    leisure = str(tags.get("leisure") or "")
    place = str(tags.get("place") or "")
    shop = str(tags.get("shop") or "")
    subway = str(tags.get("subway") or "") in {"yes", "1"} or station == "subway"
    found: list[str] = []
    if amenity in {"hospital", "clinic", "doctors", "dentist", "pharmacy"} or healthcare:
        found.append("health")
    if amenity == "police" or str(tags.get("government") or "") == "police":
        found.append("police")
    if railway in {"subway", "subway_entrance"} or subway:
        found.append("subway")
    elif railway in {"station", "halt"} or amenity == "train_station":
        found.append("train")
    elif (
        amenity == "bus_station"
        or highway == "bus_stop"
        or public_transport in {"stop_position", "station", "platform"}
        or railway == "tram_stop"
    ):
        found.append("transport")
    if natural == "beach" or leisure == "beach_resort":
        found.append("beach")
    if leisure == "park" or place == "square" or amenity == "plaza":
        found.append("plaza")
    if shop in _SHOP_KINDS or amenity == "marketplace":
        found.append("shop")
    if amenity in {"school", "university", "college"}:
        found.append("school")
    return found


def _coords(el: dict) -> tuple[float, float] | None:
    if el.get("lat") is not None and el.get("lon") is not None:
        try:
            return float(el["lat"]), float(el["lon"])
        except (TypeError, ValueError):
            pass
    center = el.get("center") or {}
    try:
        return float(center["lat"]), float(center["lon"])
    except (KeyError, TypeError, ValueError):
        pass
    geom = el.get("geometry") or []
    if len(geom) >= 2:
        mid = geom[len(geom) // 2]
        try:
            return float(mid["lat"]), float(mid["lon"])
        except (KeyError, TypeError, ValueError):
            return None
    return None


def around(
    lat: float,
    lon: float,
    category: str,
    max_km: float,
    *,
    cats: dict[str, list[dict]],
    limit: int = 4,
) -> list[tuple[float, dict[str, Any]]]:
    """Los más cercanos de una categoría. Un pin, no toda la ciudad."""
    ranked: list[tuple[float, dict[str, Any]]] = []
    for row in cats.get(category) or []:
        try:
            dist = distance_km(lat, lon, float(row["lat"]), float(row["lon"]))
        except (KeyError, TypeError, ValueError):
            continue
        if dist <= max_km:
            ranked.append((dist, row))
    ranked.sort(key=lambda pair: pair[0])
    return ranked[:limit]


def _amenity_points(cats: dict[str, list[dict]] | None) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for key in ("shop", "plaza", "transport", "school"):
        for row in (cats or {}).get(key) or []:
            try:
                pts.append((float(row["lat"]), float(row["lon"])))
            except (KeyError, TypeError, ValueError):
                continue
    return pts


def _median_amenity_spacing_km(cats: dict[str, list[dict]] | None) -> float | None:
    pts = _amenity_points(cats)
    n = len(pts)
    if n < 4:
        return None
    if n > 80:
        step = n / 80.0
        sample = [pts[int(i * step)] for i in range(80)]
    else:
        sample = pts
    nns: list[float] = []
    for i, (lat, lon) in enumerate(sample):
        best = 1e9
        for j, (olat, olon) in enumerate(pts):
            if lat == olat and lon == olon and (pts is sample or i == j):
                continue
            dist = distance_km(lat, lon, olat, olon)
            if dist < 0.04:
                continue
            if dist < best:
                best = dist
        if best < 50:
            nns.append(best)
    if len(nns) < 2:
        return None
    nns.sort()
    return nns[len(nns) // 2]


def walk_km_for_city(city_id: str | None, pois: dict[str, list[dict]] | None = None) -> float:
    """Radio a pie: en ciudad densa ~600 m; si los POIs están más espaciados, se agranda."""
    cid = (city_id or "").strip()
    if cid and cid in _walk_km:
        return _walk_km[cid]
    cats = pois if pois is not None else (city_pois(cid) if cid else {})
    spaced = _median_amenity_spacing_km(cats)
    if spaced is None:
        km = WALK_KM
    else:
        km = max(WALK_KM_MIN, min(WALK_KM_MAX, spaced * WALK_SPACING_MULT))
    km = round(float(km), 2)
    if cid:
        _walk_km[cid] = km
    return km


def _empty() -> dict[str, Any]:
    return {"version": POI_VERSION, "categories": {key: [] for key in CATEGORIES}}


def city_pois(city_id: str | None) -> dict[str, list[dict]]:
    if not city_id:
        return _empty()["categories"]
    cached = _mem.get(city_id)
    if cached:
        if _payload_usable(city_id, cached):
            return cached.get("categories") or _empty()["categories"]
        _mem.pop(city_id, None)
    from . import store

    store.init()
    raw = store.get_meta(_meta_key(city_id))
    if not raw:
        return _empty()["categories"]
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return _empty()["categories"]
    if not _payload_usable(city_id, payload):
        if os.environ.get("PROPMAP_TEST") != "1":
            try:
                store.set_meta(_meta_key(city_id), "")
            except Exception:
                pass
        return _empty()["categories"]
    remember(city_id, payload)
    return payload.get("categories") or _empty()["categories"]


def _keep_point(city_id: str, lat: float, lon: float) -> bool:
    if city_id in CITIES:
        return in_city_radius(lat, lon, city_id)
    origin = origin_for_city(city_id)
    if not origin:
        return True
    return distance_km(lat, lon, origin[0], origin[1]) <= 20


def _ingest(city_id: str, data: dict, cats: dict[str, list[dict]]) -> None:
    seen: set[tuple[str, float, float]] = set()
    cells: dict[tuple[str, int, int], int] = {}
    for el in data.get("elements") or []:
        tags = el.get("tags") or {}
        point = _coords(el)
        if not point:
            continue
        lat, lon = point
        if not _keep_point(city_id, lat, lon):
            continue
        groups = _classify(tags)
        if not groups:
            continue
        name = str(tags.get("name") or tags.get("name:es") or "").strip()
        slim = {k: tags[k] for k in tags if k in {"amenity", "healthcare", "highway", "public_transport", "railway", "station", "subway", "natural", "leisure", "place", "shop"}}
        for cat in groups:
            key = (cat, round(lat, 4), round(lon, 4))
            if key in seen:
                continue
            seen.add(key)
            cell = (cat, int(lat / CELL_DEG), int(lon / CELL_DEG))
            if cells.get(cell, 0) >= MAX_PER_CELL:
                continue
            if len(cats[cat]) >= MAX_PER_CAT[cat]:
                continue
            cells[cell] = cells.get(cell, 0) + 1
            cats[cat].append(
                {
                    "lat": lat,
                    "lon": lon,
                    "name": name,
                    "kind": _kind(slim, cat),
                    "tags": slim,
                }
            )


def fetch_city_pois(city_id: str) -> dict[str, Any] | None:
    if os.environ.get("PROPMAP_TEST") == "1":
        return _mem.get(city_id)
    area = _area_clause(city_id)
    if not area:
        return None
    from .places import _overpass

    query = f"""
[out:json][timeout:80];
(
  nwr["amenity"~"hospital|clinic|doctors|dentist|pharmacy|police|bus_station|school|university|college|marketplace"]{area};
  nwr["healthcare"]{area};
  nwr["highway"="bus_stop"]{area};
  nwr["public_transport"~"stop_position|station|platform"]{area};
  nwr["railway"~"station|halt|tram_stop|subway_entrance|subway"]{area};
  nwr["station"="subway"]{area};
  nwr["natural"="beach"]{area};
  nwr["leisure"="beach_resort"]{area};
  nwr["leisure"="park"]{area};
  nwr["place"="square"]{area};
  nwr["shop"~"supermarket|convenience|mall|department_store|grocery|greengrocer|bakery"]{area};
);
out center;
"""
    data = _overpass(query)
    if not isinstance(data, dict):
        return None
    cats: dict[str, list[dict]] = {key: [] for key in CATEGORIES}
    _ingest(city_id, data, cats)
    useful = sum(len(cats.get(key) or []) for key in CATEGORIES)
    if useful < 3:
        return None
    from . import store

    store.init()
    raw = store.get_meta(_meta_key(city_id))
    if raw:
        try:
            old = json.loads(raw)
            old_n = sum(len((old.get("categories") or {}).get(key) or []) for key in CATEGORIES)
            if old_n > useful:
                remember(city_id, old)
                return old
        except json.JSONDecodeError:
            pass
    origin = origin_for_city(city_id)
    payload = {
        "version": POI_VERSION,
        "origin": {"lat": origin[0], "lon": origin[1]} if origin else None,
        "categories": cats,
    }
    remember(city_id, payload)
    store.set_meta(_meta_key(city_id), json.dumps(payload, ensure_ascii=False))
    try:
        from .access import kick_access_later

        kick_access_later(city_id)
    except Exception:
        pass
    return payload


def ensure_city_pois(city_id: str | None, *, blocking: bool = False) -> dict[str, list[dict]]:
    if not city_id or city_id in {"fuera", "otros"}:
        return _empty()["categories"]
    have = city_pois(city_id)
    if any(have.get(key) for key in CATEGORIES):
        return have
    if os.environ.get("PROPMAP_TEST") == "1":
        return have

    def _job() -> None:
        try:
            fetch_city_pois(city_id)
        finally:
            with _lock:
                _inflight.discard(city_id)

    start = False
    with _lock:
        if city_id in _inflight:
            if not blocking:
                return have
        else:
            _inflight.add(city_id)
            start = True
    if blocking:
        if start:
            _job()
        else:
            while True:
                with _lock:
                    if city_id not in _inflight:
                        break
                time.sleep(0.05)
        return city_pois(city_id)
    if start:
        threading.Thread(target=_job, daemon=True, name=f"osm-poi-{city_id}").start()
    return have
