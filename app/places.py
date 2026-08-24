from __future__ import annotations

import json
import time
from typing import Any

import httpx

from . import store
from .geo import CITIES, fold, generic_barrios, register_city, slug_place

NOMINATIM = "https://nominatim.openstreetmap.org/search"
OVERPASS = "https://overpass-api.de/api/interpreter"
HEADERS = {
    "User-Agent": "PropMap/1.0 (local real-estate map; no commercial use)",
    "Accept": "application/json",
    "Accept-Language": "es-AR,es;q=0.9",
}
ARG_LAT = -38.4161
ARG_LON = -63.6167
PROVINCE_SLUG = {
    "chubut": "chubut",
    "rio negro": "rio-negro",
    "río negro": "rio-negro",
    "santa cruz": "santa-cruz",
    "neuquen": "neuquen",
    "neuquén": "neuquen",
    "tierra del fuego": "tierra-del-fuego",
    "la pampa": "la-pampa",
    "buenos aires": "buenos-aires",
    "capital federal": "capital-federal",
    "ciudad autonoma de buenos aires": "capital-federal",
    "caba": "capital-federal",
    "cordoba": "cordoba",
    "córdoba": "cordoba",
    "santa fe": "santa-fe",
    "santa fé": "santa-fe",
    "mendoza": "mendoza",
    "tucuman": "tucuman",
    "tucumán": "tucuman",
    "salta": "salta",
    "entre rios": "entre-rios",
    "entre ríos": "entre-rios",
    "corrientes": "corrientes",
    "misiones": "misiones",
    "jujuy": "jujuy",
    "san juan": "san-juan",
    "san luis": "san-luis",
    "la rioja": "la-rioja",
    "catamarca": "catamarca",
    "formosa": "formosa",
    "chaco": "chaco",
    "santiago del estero": "santiago-del-estero",
}


def search_places(query: str, limit: int = 8) -> list[dict]:
    q = (query or "").strip()
    if len(q) < 2:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    folded = fold(q)
    for city_id, cfg in CITIES.items():
        names = [cfg["label"], city_id, *(cfg.get("aliases") or [])]
        if _name_hit(folded, names) and city_id not in seen:
            out.append(_public_city(cfg))
            seen.add(city_id)
    rows = _nominatim({"q": q, "countrycodes": "ar", "format": "json", "addressdetails": 1, "limit": limit})
    for row in rows:
        parsed = _from_nominatim(row)
        if not parsed or parsed["id"] in seen:
            continue
        seen.add(parsed["id"])
        out.append(parsed)
    return out[:limit]


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
    if cfg and cfg.get("builtin"):
        return city_id
    if cfg and cfg.get("lat") is not None and lat is None:
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
            if cfg and cfg.get("builtin"):
                return city_id
    if city_id in CITIES and lat is None:
        return city_id
    label = label or (hit or {}).get("label") or city_id.replace("-", " ").title()
    if lat is None or lon is None:
        lat, lon = ARG_LAT, ARG_LON
        zoom = 5
    else:
        zoom = 14 if abs(float(lat) - ARG_LAT) > 1 else 5
    lat_f, lon_f = float(lat), float(lon)
    barrios = _osm_barrios(lat_f, lon_f, label) or generic_barrios(lat_f, lon_f)
    register_city(
        city_id,
        label=label,
        lat=lat_f,
        lon=lon_f,
        province=province or (hit or {}).get("province") or "chubut",
        barrios=barrios,
        zoom=zoom,
        aliases=[fold(label), fold(raw)] if raw else [fold(label)],
    )
    _persist()
    return city_id


def public_place(city_id: str) -> dict:
    cfg = CITIES.get(city_id) or {"id": city_id, "label": city_id, "lat": ARG_LAT, "lon": ARG_LON, "zoom": 5, "province": "", "barrios": []}
    return _public_city(cfg)


def listed_cities(listings: list | None = None) -> list[dict]:
    load_custom_places()
    seen: dict[str, dict] = {}
    for cfg in CITIES.values():
        seen[cfg["id"]] = _public_city(cfg)
    for item in listings or []:
        cid = getattr(item, "city", None) or "puerto-madryn"
        if cid in {"fuera", "otros"}:
            continue
        if cid not in seen:
            seen[cid] = {
                "id": cid,
                "label": cid.replace("-", " ").title(),
                "lat": getattr(item, "lat", None) or ARG_LAT,
                "lon": getattr(item, "lon", None) or ARG_LON,
                "zoom": 13,
                "province": "chubut",
            }
    preferred = ["puerto-madryn", "playa-union", "trelew", "rawson", "gaiman", "microcentro-caba"]
    ordered = [seen.pop(key) for key in preferred if key in seen]
    ordered.extend(sorted(seen.values(), key=lambda row: fold(row["label"])))
    return ordered


def load_custom_places() -> None:
    raw = store.get_meta("custom_places")
    if not raw:
        return
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError:
        return
    if not isinstance(rows, list):
        return
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
            province=row.get("province") or "chubut",
            barrios=row.get("barrios") or generic_barrios(float(row.get("lat") or ARG_LAT), float(row.get("lon") or ARG_LON)),
            zoom=int(row.get("zoom") or 13),
            aliases=row.get("aliases") or [],
            builtin=False,
        )


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
                "province": cfg.get("province") or "chubut",
                "aliases": cfg.get("aliases") or [],
                "barrios": cfg.get("barrios") or [],
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


def _public_city(cfg: dict) -> dict:
    return {
        "id": cfg["id"],
        "label": cfg["label"],
        "lat": cfg["lat"],
        "lon": cfg["lon"],
        "zoom": cfg.get("zoom") or 13,
        "province": cfg.get("province") or "",
        "radius_km": cfg.get("radius_km") or 25,
        "barrios": [
            {"name": b["name"], "zona": b.get("zona") or b["name"], "lat": b["lat"], "lon": b["lon"]}
            for b in (cfg.get("barrios") or [])
        ],
    }


def _from_nominatim(row: dict) -> dict | None:
    addr = row.get("address") or {}
    name = (
        addr.get("village")
        or addr.get("town")
        or addr.get("city")
        or addr.get("suburb")
        or addr.get("hamlet")
        or addr.get("municipality")
        or row.get("name")
        or (row.get("display_name") or "").split(",")[0]
    )
    if not name:
        return None
    city_id = slug_place(name)
    state = fold(addr.get("state") or addr.get("state_district") or "")
    province = PROVINCE_SLUG.get(state)
    if not province:
        for key, slug in PROVINCE_SLUG.items():
            if key in state:
                province = slug
                break
    try:
        lat, lon = float(row["lat"]), float(row["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "id": city_id,
        "label": str(name).strip(),
        "lat": lat,
        "lon": lon,
        "zoom": 14,
        "province": province or "chubut",
        "hint": row.get("display_name") or "",
    }


def _osm_barrios(lat: float, lon: float, label: str) -> list[dict]:
    query = f"""
[out:json][timeout:18];
(
  node["place"~"suburb|neighbourhood|quarter|village|hamlet"](around:6000,{lat},{lon});
  way["place"~"suburb|neighbourhood|quarter"](around:6000,{lat},{lon});
);
out center 30;
"""
    data = _overpass(query)
    elements = data.get("elements") if isinstance(data, dict) else None
    barrios = []
    seen: set[str] = set()
    for el in elements or []:
        tags = el.get("tags") or {}
        name = tags.get("name") or tags.get("name:es")
        folded = fold(name or "")
        if not name or folded in seen or folded == fold(label):
            continue
        blat = el.get("lat") or (el.get("center") or {}).get("lat")
        blon = el.get("lon") or (el.get("center") or {}).get("lon")
        try:
            blat, blon = float(blat), float(blon)
        except (TypeError, ValueError):
            continue
        seen.add(folded)
        barrios.append(
            {
                "name": str(name).strip().title(),
                "zona": str(name).strip().title(),
                "lat": blat,
                "lon": blon,
                "aliases": [folded],
            }
        )
        if len(barrios) >= 18:
            break
    if not barrios:
        return []
    barrios.insert(0, {"name": "Centro", "zona": "Centro", "lat": lat, "lon": lon, "aliases": ["centro"]})
    return barrios


def _nominatim(params: dict[str, Any]) -> list[dict]:
    try:
        with httpx.Client(headers=HEADERS, timeout=18.0) as client:
            response = client.get(NOMINATIM, params=params)
            response.raise_for_status()
            data = response.json()
        time.sleep(1.05)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _overpass(query: str) -> dict:
    try:
        with httpx.Client(headers=HEADERS, timeout=22.0) as client:
            response = client.post(OVERPASS, data={"data": query})
            response.raise_for_status()
            data = response.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}
