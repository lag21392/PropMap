from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from . import store
from .geo import (
    CABA_IDS,
    CABA_LAT,
    CABA_LON,
    CITIES,
    DEFAULT_CITY,
    apply_city_extent,
    city_outline_rings,
    distance_km,
    fold,
    remember_city_outline,
    register_city,
    resolve_city,
    slug_place,
    _extent_radius_km,
    _point_in_rings,
)
from .place_api import lookup_place, province_slug, provinces, search_city_places

log = logging.getLogger(__name__)
SEARCH_CACHE_TTL_SEC = 7 * 24 * 3600
_search_mem: dict[str, tuple[float, list[dict]]] = {}
# Consultas a Georef/Nominatim, no un catálogo de coordenadas.
PRIORITY_PLACE_QUERIES = ("Puerto Madryn", "CABA")

NOMINATIM = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE = "https://nominatim.openstreetmap.org/reverse"
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
_LISTED_SKIP = {"fuera", "otros", "argentina", "buenos-aires", ""}
MIN_CATALOG_LISTINGS = 8
MIN_SCRAPE_LISTINGS = 100
_SEARCHABLE_KINDS = {
    "localidad",
    "localidade",
    "city",
    "town",
}
_BLOCKED_KINDS = {
    "suburb",
    "neighbourhood",
    "neighborhood",
    "quarter",
    "hamlet",
    "village",
    "isolated_dwelling",
    "asentamiento",
    "departamento",
    "provincia",
    "state",
    "county",
    "region",
    "administrative",
    "peak",
    "river",
    "natural",
    "road",
}
_NOT_CITY_CATEGORIAS = {"paraje", "pje"}
_PLACE_META_KEYS = ("kind", "addresstype", "municipio", "localidad_censal", "categoria")
_purged_unofficial = False
_restored_loaded = False


def is_cache_artifact_id(city_id: str | None) -> bool:
    """Nombres que salieron de archivos de cache (caba-pins, foo.pins), no de un lugar."""
    token = fold(city_id or "")
    if not token:
        return True
    compact = token.replace(" ", "-")
    return compact.endswith("-pins") or compact.endswith(".pins") or "-pins-" in compact or ".pins" in compact


def _norm_place_name(value: str) -> str:
    return fold(value).replace("-", " ")


def _same_place_token(left: str, right: str) -> bool:
    return bool(left) and bool(right) and _norm_place_name(left) == _norm_place_name(right)


def _label_key(city_id: str, cfg: dict | None = None) -> str:
    row = cfg or CITIES.get(city_id) or {}
    name = _norm_place_name(str(row.get("label") or city_id.replace("-", " ")))
    if city_id in CABA_IDS or city_id == DEFAULT_CITY:
        return name
    prov = _norm_place_name(province_slug(str(row.get("province") or row.get("province_label") or "")))
    if prov in {"capital federal", "caba", "ciudad autonoma de buenos aires"}:
        return name
    return f"{name}|{prov}" if prov else name


def _prefer_suggest_row(new: dict, old: dict) -> bool:
    """Ante homónimos, quedate con el id ciudad-provincia, no el slug pelado."""
    new_id = str(new.get("id") or "")
    old_id = str(old.get("id") or "")
    label = str(new.get("label") or old.get("label") or "")
    province = str(new.get("province") or old.get("province") or "")
    want = _place_id(label, province)
    if new_id == want and old_id != want:
        return True
    if old_id == want and new_id != want:
        return False
    new_prov = bool(str(new.get("province") or new.get("province_label") or "").strip())
    old_prov = bool(str(old.get("province") or old.get("province_label") or "").strip())
    if new_prov != old_prov:
        return new_prov
    return len(new_id) > len(old_id)


def _merge_search_hit(
    out: list[dict],
    seen_ids: set[str],
    seen_keys: dict[str, int],
    row: dict,
    *,
    limit: int | None = None,
) -> bool:
    cid = str(row.get("id") or "")
    if not cid or cid in seen_ids or not _search_hit_ok(row):
        return False
    if not str(row.get("province_label") or "").strip():
        pretty = _province_display(str(row.get("province") or ""))
        if pretty and not _is_caba_province(pretty):
            row["province_label"] = pretty
    key = _label_key(cid, row)
    if key in seen_keys:
        i = seen_keys[key]
        prev = out[i]
        if not _prefer_suggest_row(row, prev):
            return False
        seen_ids.discard(str(prev.get("id") or ""))
        seen_ids.add(cid)
        out[i] = row
        return True
    if limit is not None and len(out) >= limit:
        return False
    seen_ids.add(cid)
    seen_keys[key] = len(out)
    out.append(row)
    return True


def _is_caba_province(province: str) -> bool:
    token = fold(province_slug(province or "")).replace(" ", "-")
    return token in {"capital-federal", "caba", "ciudad-autonoma-de-buenos-aires"}


def _place_id(name: str, province: str = "") -> str:
    base = slug_place(name)
    if not base or base in CABA_IDS or _is_caba_province(province):
        return base
    prov = province_slug(province or "")
    if not prov:
        return base
    return f"{base}-{prov}"


def _province_display(value: str) -> str:
    token = province_slug(value or "")
    if not token:
        return ""
    for row in provinces():
        slug = str(row.get("slug") or "")
        nombre = str(row.get("nombre") or "")
        if slug == token or fold(nombre) == fold(value) or fold(nombre) == fold(token):
            return nombre
    return str(value).replace("-", " ").title()


def _place_hint(cfg: dict) -> str:
    label = str(cfg.get("label") or cfg.get("id") or "").strip()
    pretty = _province_display(str(cfg.get("province") or ""))
    if pretty and not _is_caba_province(pretty):
        return f"{label}, {pretty}"
    return label


def apply_city_province(city_id: str, province: str) -> None:
    """Completa la provincia de un lugar ya cargado con lo que dio Georef. No pisa un valor."""
    cid = (city_id or "").strip()
    raw = (province or "").strip()
    if not cid or not raw:
        return
    cfg = CITIES.get(cid)
    if not cfg or cid in CABA_IDS:
        return
    if _is_caba_province(raw):
        return
    current = str(cfg.get("province") or "").strip()
    if current and not _is_caba_province(current):
        return
    cfg["province"] = province_slug(raw)
    if os.environ.get("PROPMAP_TEST") != "1":
        try:
            _persist()
        except Exception:
            pass


def _prefer_listed_id(left: str, right: str) -> bool:
    if left == DEFAULT_CITY or left in CABA_IDS:
        return True
    if right == DEFAULT_CITY or right in CABA_IDS:
        return False
    left_cfg, right_cfg = CITIES.get(left) or {}, CITIES.get(right) or {}
    if left_cfg.get("builtin") and not right_cfg.get("builtin"):
        return True
    if right_cfg.get("builtin") and not left_cfg.get("builtin"):
        return False
    return len(left) < len(right)


def _search_hit_ok(row: dict | None) -> bool:
    if not isinstance(row, dict) or row.get("lat") is None or row.get("lon") is None:
        return False
    cid = str(row.get("id") or "")
    if not cid or cid in _LISTED_SKIP or is_cache_artifact_id(cid):
        return False
    kind = fold(str(row.get("addresstype") or row.get("kind") or "localidad"))
    if kind in _BLOCKED_KINDS:
        return False
    label = fold(str(row.get("label") or ""))
    if label.startswith("barrio ") or label.startswith("departamento "):
        return False
    return _is_city_place(row)


def _is_city_place(row: dict) -> bool:
    """Ciudad de provincia: city/town/municipio, o localidad censal (no barrio ni paraje).

    Florida (Vicente López), Palermo (CABA) o Canning (Ezeiza) quedan afuera: son
    barrios o entidades dentro de otra ciudad, no el lugar que se busca.
    """
    cid = str(row.get("id") or "")
    if cid in CABA_IDS or cid == DEFAULT_CITY:
        return True
    kind = fold(str(row.get("addresstype") or row.get("kind") or "localidad"))
    if kind in _BLOCKED_KINDS:
        return False
    cat = fold(str(row.get("categoria") or ""))
    if cat in _NOT_CITY_CATEGORIAS or cat.startswith("paraje") or cat.startswith("pje"):
        return False
    if kind in {"city", "town"}:
        return True
    name = _norm_place_name(str(row.get("label") or row.get("name") or cid.replace("-", " ")))
    censal = _norm_place_name(str(row.get("localidad_censal") or ""))
    mun = _norm_place_name(str(row.get("municipio") or ""))
    prov = _norm_place_name(str(row.get("province") or ""))
    if censal and ("ciudad autonoma" in censal or censal in {"caba", "capital federal"}):
        return name in {"caba", "capital federal", "ciudad autonoma de buenos aires"}
    if "ciudad autonoma" in prov or prov in {"caba", "capital federal"}:
        return name in {"caba", "capital federal", "ciudad autonoma de buenos aires"}
    if censal or mun:
        return bool(name) and (name == censal or name == mun)
    if not prov:
        return False
    return kind in _SEARCHABLE_KINDS


def _place_has_city_scope(place: dict | None) -> bool:
    if not isinstance(place, dict):
        return False
    kind = fold(str(place.get("kind") or place.get("addresstype") or ""))
    if kind in {"city", "town", "municipio"}:
        return True
    return bool(place.get("localidad_censal") or place.get("municipio"))


def official_place(query: str | None) -> dict | None:
    """El nombre existe como ciudad (localidad / city / town) en Georef o Nominatim."""
    raw = (query or "").strip()
    if len(raw) < 2:
        return None
    if is_cache_artifact_id(slug_place(raw)):
        return None
    q = raw.replace("-", " ")
    remote = os.environ.get("PROPMAP_TEST") != "1"
    place = lookup_place(q, remote=remote)
    parsed = _from_georef_place(place) if place else None
    if parsed and _search_hit_ok(parsed) and _official_name_matches(raw, parsed):
        if os.environ.get("PROPMAP_TEST") == "1" or _place_has_city_scope(place):
            return parsed
    if not remote:
        return None
    for row in _search_places_remote(q, limit=8):
        if _search_hit_ok(row) and _official_name_matches(raw, row):
            return row
    return None


def _official_name_matches(query: str, hit: dict) -> bool:
    names = [str(hit.get("id") or ""), str(hit.get("label") or "")]
    return any(_same_place_token(query, name) or slug_place(query) == slug_place(name) for name in names if name)


def can_list_place(city_id: str | None, *, label: str | None = None, query: str | None = None) -> bool:
    raw = (query or label or city_id or "").strip()
    cid = slug_place((city_id or raw).strip()) if (city_id or raw).strip() else ""
    if not cid or cid in _LISTED_SKIP or is_cache_artifact_id(cid):
        return False
    if cid in CABA_IDS or cid == DEFAULT_CITY:
        return True
    cfg = CITIES.get(cid) or CITIES.get(resolve_city(raw) if raw else "")
    if cfg and cfg.get("builtin") and _has_map_coords(cfg):
        return True
    return official_place(raw or cid) is not None


def _listed_row_is_city(cid: str, cfg: dict) -> bool:
    """Filtro local: no pega a la red. La limpieza con API corre en purge_unofficial_places."""
    if not cid or cid in CABA_IDS or cid == DEFAULT_CITY or cfg.get("builtin"):
        return True
    if _junk_place_row(cid, cfg):
        return False
    row = {
        "id": cid,
        "label": cfg.get("label") or cid,
        "lat": cfg.get("lat"),
        "lon": cfg.get("lon"),
        "kind": cfg.get("kind") or "localidad",
        "addresstype": cfg.get("kind") or "localidad",
        "municipio": cfg.get("municipio") or "",
        "localidad_censal": cfg.get("localidad_censal") or "",
        "categoria": cfg.get("categoria") or "",
        "province": cfg.get("province") or "",
    }
    if cfg.get("province") and _has_map_coords(cfg):
        return _is_city_place(row)
    cached = lookup_place(str(cfg.get("label") or cid), remote=False)
    parsed = _from_georef_place(cached) if cached else None
    if parsed:
        return _search_hit_ok(parsed)
    return _is_city_place(row)


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


def _row_matches_query(query: str, row: dict) -> bool:
    names = [str(row.get("label") or ""), str(row.get("id") or ""), str(row.get("slug") or "")]
    return _name_hit(fold(query), [name for name in names if name])


def search_places(query: str, limit: int = 8) -> list[dict]:
    q = (query or "").strip()
    if len(q) < 2:
        return []
    out: list[dict] = []
    seen_ids: set[str] = set()
    seen_keys: dict[str, int] = {}
    folded = fold(q)

    def add(row: dict) -> None:
        _merge_search_hit(out, seen_ids, seen_keys, row)

    for city_id, cfg in list(CITIES.items()):
        if is_cache_artifact_id(city_id) or not _has_map_coords(cfg):
            continue
        names = [cfg["label"], city_id, *(cfg.get("aliases") or [])]
        if _name_hit(folded, names) and _listed_row_is_city(city_id, cfg):
            add(_public_city(cfg))
    cached = _read_search_cache(folded)
    if cached:
        for row in cached:
            if isinstance(row, dict) and _row_matches_query(q, row):
                add(row)
        if out:
            return out[:limit]
    if out and len(folded) < 4:
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
    seen_ids: set[str] = set()
    seen_keys: dict[str, int] = {}
    for variant in _query_variants(q):
        for place in search_city_places(variant, limit=limit):
            parsed = _from_georef_place(place)
            if not parsed or not _row_matches_query(q, parsed):
                continue
            parsed["id"] = _adopt_existing_city(parsed)
            _merge_search_hit(out, seen_ids, seen_keys, parsed, limit=limit)
            if len(out) >= limit:
                return out[:limit]
    if out:
        return out[:limit]
    rows = _nominatim({"q": q, "countrycodes": "ar", "format": "json", "addressdetails": 1, "limit": limit})
    for row in rows:
        parsed = _from_nominatim(row)
        if not parsed or not _row_matches_query(q, parsed):
            continue
        parsed["id"] = _adopt_existing_city(parsed)
        _merge_search_hit(out, seen_ids, seen_keys, parsed, limit=limit)
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
        raw = store.get_meta(f"place_search:v2:{key}")
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
            f"place_search:v2:{key}",
            json.dumps({"at": now, "places": rows}, ensure_ascii=False),
        )
    except Exception:
        pass


def _commit_listed_place(city_id: str, **kwargs) -> str:
    if not can_list_place(city_id, label=kwargs.get("label"), query=kwargs.get("query")):
        raise ValueError("elegí una ciudad de las sugerencias")
    cid = ensure_place(city_id, **kwargs)
    remember_listed_place(cid)
    return cid


def place_from_suggestion(
    city: str | None = None,
    *,
    query: str | None = None,
    label: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    province: str | None = None,
) -> str:
    """Solo un lugar de las sugerencias (Georef/Nominatim o ya cargado). No inventa texto libre."""
    from .geo import resolve_city

    city_id = (city or "").strip()
    raw = (query or label or city_id).strip()
    if is_cache_artifact_id(slug_place(city_id or raw)):
        raise ValueError("elegí una ciudad de las sugerencias")
    if lat is None and lon is None and not city_id and not raw:
        raise ValueError("elegí una ciudad de las sugerencias")
    if lat is not None and lon is not None and (city_id or raw):
        if not can_list_place(city_id or raw, label=label, query=raw):
            raise ValueError("elegí una ciudad de las sugerencias")
        return _commit_listed_place(
            city_id or raw,
            query=raw,
            label=label or raw,
            lat=lat,
            lon=lon,
            province=province,
        )
    resolved = resolve_city(city_id or raw) if (city_id or raw) else ""
    if resolved and resolved in CITIES:
        cfg = CITIES.get(resolved) or {}
        if cfg.get("builtin") or resolved in catalog_place_ids():
            return _commit_listed_place(resolved, query=raw or resolved)
    if len(raw) < 2:
        raise ValueError("elegí una ciudad de las sugerencias")
    found = search_places(raw, limit=8)
    folded = fold(raw)
    hit = next(
        (
            row
            for row in found
            if fold(row.get("id") or "") == folded
            or fold(row.get("label") or "") == folded
            or (city_id and row.get("id") == city_id)
        ),
        None,
    )
    if hit is None and len(found) == 1:
        hit = found[0]
    if not hit or hit.get("lat") is None or hit.get("lon") is None:
        raise ValueError("elegí una ciudad de las sugerencias")
    return _commit_listed_place(
        hit["id"],
        query=hit.get("label") or raw,
        label=hit.get("label"),
        lat=hit.get("lat"),
        lon=hit.get("lon"),
        province=hit.get("province"),
    )


def _caba_coords(lat: float, lon: float) -> bool:
    return abs(float(lat) - CABA_LAT) < 0.08 and abs(float(lon) - CABA_LON) < 0.08


def _apply_place_meta(city_id: str, src: dict | None) -> None:
    cfg = CITIES.get(city_id)
    if not cfg or not isinstance(src, dict):
        return
    for key in _PLACE_META_KEYS:
        val = src.get(key)
        if val:
            cfg[key] = val
    if cfg.get("kind") and not cfg.get("addresstype"):
        cfg["addresstype"] = cfg["kind"]
    if cfg.get("addresstype") and not cfg.get("kind"):
        cfg["kind"] = cfg["addresstype"]


def _register_view_city(
    city_id: str,
    *,
    label: str,
    lat: float,
    lon: float,
    province: str = "",
    zoom: int = 13,
    aliases: list[str] | None = None,
    slug: str | None = None,
    bbox=None,
    radius_km: float | None = None,
    meta: dict | None = None,
) -> str:
    if not city_id or city_id in _LISTED_SKIP or is_cache_artifact_id(city_id):
        return city_id
    if city_id not in CABA_IDS and _caba_coords(lat, lon):
        return city_id
    if abs(float(lat) - ARG_LAT) < 0.2 and abs(float(lon) - ARG_LON) < 0.2:
        return city_id
    register_city(
        city_id,
        label=label,
        lat=float(lat),
        lon=float(lon),
        province=province or "",
        barrios=[],
        zoom=int(zoom or 13),
        aliases=aliases or [fold(label), fold(city_id)],
        slug=slug,
        bbox=bbox,
        radius_km=radius_km,
    )
    _apply_place_meta(city_id, meta)
    if os.environ.get("PROPMAP_TEST") != "1":
        _persist()
        _save_extents()
    return city_id


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
    requested = slug_place((city or "").strip()) if (city or "").strip() else ""
    if requested in _LISTED_SKIP or is_cache_artifact_id(requested):
        requested = ""
    city_id = resolve_city(raw or city)
    cfg = CITIES.get(city_id) or (CITIES.get(requested) if requested else None)

    def _keep_requested(official_id: str, official_label: str) -> bool:
        return bool(
            requested
            and requested not in CITIES
            and not is_cache_artifact_id(requested)
            and (
                _same_place_token(requested, official_id)
                or _same_place_token(requested, official_label)
            )
        )

    if cfg and _has_map_coords(cfg) and (cfg.get("builtin") or (cfg.get("lat") is not None and lat is None)):
        if _keep_requested(city_id, str(cfg.get("label") or "")):
            _register_view_city(
                requested,
                label=str(cfg.get("label") or label or requested),
                lat=float(cfg["lat"]),
                lon=float(cfg["lon"]),
                province=str(cfg.get("province") or ""),
                zoom=int(cfg.get("zoom") or 13),
                aliases=[fold(requested), fold(cfg.get("label") or "")],
                slug=cfg.get("slug"),
                bbox=cfg.get("bbox"),
                radius_km=cfg.get("radius_km"),
                meta=cfg,
            )
            city_id = requested
        threading.Thread(
            target=hydrate_place_extent,
            args=(city_id, raw),
            daemon=True,
            name=f"hydrate-{city_id}",
        ).start()
        ensure_osm_barrios(city_id, blocking=False)
        return requested if requested in CITIES else city_id
    hit = None
    if lat is None or lon is None:
        found = search_places(raw or city_id, limit=5)
        hit = next((row for row in found if row["id"] in {city_id, requested}), None) or (
            found[0] if found else None
        )
        if hit:
            city_id = hit["id"]
            label = label or hit["label"]
            lat = hit["lat"] if lat is None else lat
            lon = hit["lon"] if lon is None else lon
            province = province or hit.get("province")
            cfg = CITIES.get(city_id)
            if cfg and _has_map_coords(cfg):
                apply_city_extent(
                    city_id,
                    bbox=hit.get("bbox"),
                    radius_km=hit.get("radius_km"),
                    zoom=hit.get("zoom"),
                    slug=hit.get("slug"),
                )
                _save_extents()
                if _keep_requested(city_id, str(label or "")):
                    _register_view_city(
                        requested,
                        label=label or requested,
                        lat=float(lat),
                        lon=float(lon),
                        province=str(province or cfg.get("province") or ""),
                        zoom=int(hit.get("zoom") or cfg.get("zoom") or 13),
                        aliases=[fold(requested), fold(label or "")],
                        slug=hit.get("slug"),
                        bbox=hit.get("bbox") or cfg.get("bbox"),
                        radius_km=hit.get("radius_km") or cfg.get("radius_km"),
                        meta=hit,
                    )
                    ensure_osm_barrios(requested, blocking=False)
                    return requested
                ensure_osm_barrios(city_id, blocking=False)
                return city_id
    if requested:
        city_id = requested
    if city_id in CITIES and lat is None and _has_map_coords(CITIES.get(city_id)):
        ensure_osm_barrios(city_id, blocking=False)
        return city_id
    label = label or (hit or {}).get("label") or city_id.replace("-", " ").title()
    if lat is None or lon is None:
        lat, lon = ARG_LAT, ARG_LON
        zoom = 5
    else:
        zoom = int((hit or {}).get("zoom") or (14 if abs(float(lat) - ARG_LAT) > 1 else 5))
    _register_view_city(
        city_id,
        label=label,
        lat=float(lat),
        lon=float(lon),
        province=province or (hit or {}).get("province") or "",
        zoom=zoom,
        aliases=[fold(label), fold(raw)] if raw else [fold(label)],
        slug=(hit or {}).get("slug"),
        bbox=(hit or {}).get("bbox"),
        radius_km=(hit or {}).get("radius_km"),
        meta=hit,
    )
    ensure_osm_barrios(city_id, blocking=False)
    return city_id


def ensure_view_city(city_id: str | None) -> str:
    """Deja el lugar de la vista en CITIES solo si existe en Georef/Nominatim.

    Ver un aviso no lo agrega al pool de scrape: eso es place_from_suggestion.
    """
    cid = slug_place((city_id or "").strip()) if (city_id or "").strip() else ""
    if not cid or cid in _LISTED_SKIP or is_cache_artifact_id(cid):
        return cid or ""
    load_custom_places()
    cfg = CITIES.get(cid) or {}
    if _has_map_coords(cfg) and (cfg.get("builtin") or can_list_place(cid, label=str(cfg.get("label") or cid))):
        city_outline_rings(cid)
        return cid
    if os.environ.get("PROPMAP_TEST") == "1":
        return cid
    query = str(cfg.get("label") or cid).replace("-", " ")
    hit = official_place(query) or official_place(cid)
    if not hit:
        return cid
    got = ensure_place(
        city=hit["id"],
        query=hit.get("label") or query,
        label=hit.get("label"),
        lat=hit.get("lat"),
        lon=hit.get("lon"),
        province=hit.get("province"),
    )
    if _has_map_coords(CITIES.get(got) or {}):
        city_outline_rings(got)
        return got
    return got or cid


def public_place(city_id: str) -> dict:
    cfg = CITIES.get(city_id) or {"id": city_id, "label": city_id, "lat": ARG_LAT, "lon": ARG_LON, "zoom": 5, "province": "", "barrios": []}
    return _public_city(cfg)


def listing_count_for_catalog(n: int | None) -> bool:
    try:
        return int(n or 0) >= MIN_CATALOG_LISTINGS
    except (TypeError, ValueError):
        return False


def empty_view_ids() -> set[str]:
    """Lugares ya abiertos cuyo mapa quedó sin avisos."""
    try:
        raw = store.get_meta("empty_view_places", "[]")
        data = json.loads(raw or "[]")
    except Exception:
        return set()
    if not isinstance(data, list):
        return set()
    return {str(item) for item in data if item}


def mark_empty_view(city_id: str) -> None:
    token = (city_id or "").strip()
    if not token or token == DEFAULT_CITY or token in CABA_IDS:
        return
    try:
        current = empty_view_ids()
        if token in current:
            return
        current.add(token)
        store.set_meta("empty_view_places", json.dumps(sorted(current)))
    except Exception:
        log.exception("no pude deslistar %s", token)


def clear_empty_view(city_id: str) -> bool:
    token = (city_id or "").strip()
    try:
        current = empty_view_ids()
        if token not in current:
            return False
        current.discard(token)
        store.set_meta("empty_view_places", json.dumps(sorted(current)))
    except Exception:
        log.exception("no pude volver a listar %s", token)
        return False
    return True


def listing_count_for_scrape(n: int | None) -> bool:
    try:
        return int(n or 0) >= MIN_SCRAPE_LISTINGS
    except (TypeError, ValueError):
        return False


def _ids_with_saved_listings() -> set[str]:
    if os.environ.get("PROPMAP_TEST") == "1":
        return set()
    try:
        store.init()
        bundled: dict[str, int] = {}
        for cid, n in store.city_listing_counts().items():
            token = _canonical_listed_id(cid) or cid
            if not token or token in _LISTED_SKIP:
                continue
            bundled[token] = bundled.get(token, 0) + int(n or 0)
        return {cid for cid, n in bundled.items() if listing_count_for_catalog(n)}
    except Exception:
        return set()


def _ids_with_scrape_listings() -> set[str]:
    if os.environ.get("PROPMAP_TEST") == "1":
        return set()
    try:
        store.init()
        bundled: dict[str, int] = {}
        for cid, n in store.city_listing_counts().items():
            token = _canonical_listed_id(cid) or cid
            if not token or token in _LISTED_SKIP:
                continue
            bundled[token] = bundled.get(token, 0) + int(n or 0)
        return {cid for cid, n in bundled.items() if listing_count_for_scrape(n)}
    except Exception:
        return set()


_listed_lock = threading.Lock()
_listed_mem: list[str] | None = None


def reset_listed_places() -> None:
    global _listed_mem
    with _listed_lock:
        _listed_mem = [] if os.environ.get("PROPMAP_TEST") == "1" else None


def listed_place_ids() -> list[str]:
    with _listed_lock:
        return [cid for cid in _listed_ids_locked() if cid not in _LISTED_SKIP and not is_cache_artifact_id(cid)]


def _listed_ids_locked() -> list[str]:
    global _listed_mem
    if _listed_mem is not None:
        return list(_listed_mem)
    ids: list[str] = []
    if os.environ.get("PROPMAP_TEST") != "1":
        try:
            store.init()
            raw = store.get_meta("listed_places")
            parsed = json.loads(raw) if raw else []
            if isinstance(parsed, list):
                ids = [str(cid) for cid in parsed if cid and not is_cache_artifact_id(str(cid))]
            if ids != [str(cid) for cid in (parsed if isinstance(parsed, list) else []) if cid]:
                store.set_meta("listed_places", json.dumps(ids, ensure_ascii=False))
        except Exception:
            ids = []
    _listed_mem = list(dict.fromkeys(ids))
    return list(_listed_mem)


def remember_listed_place(city_id: str | None) -> str:
    global _listed_mem
    cid = _canonical_listed_id(city_id or "") or (city_id or "").strip()
    if not cid or cid in _LISTED_SKIP or is_cache_artifact_id(cid):
        return ""
    cfg = CITIES.get(cid) or {}
    if not can_list_place(cid, label=str(cfg.get("label") or cid)):
        return ""
    token = _label_key(cid, cfg)
    with _listed_lock:
        ids = _listed_ids_locked()
        for other in ids:
            if other != cid and _label_key(other) == token:
                return other
        if cid not in ids:
            ids.append(cid)
            _listed_mem = list(ids)
            if os.environ.get("PROPMAP_TEST") != "1":
                try:
                    store.init()
                    store.set_meta("listed_places", json.dumps(_listed_mem, ensure_ascii=False))
                except Exception:
                    pass
        return cid


def unlist_place(city_id: str | None) -> str:
    """Saca del pool de scrape sin borrar el lugar ni los avisos."""
    global _listed_mem
    cid = (city_id or "").strip()
    if not cid or cid == DEFAULT_CITY or cid in CABA_IDS:
        return ""
    cfg = CITIES.get(cid) or {}
    if cfg.get("builtin"):
        return ""
    with _listed_lock:
        ids = [item for item in _listed_ids_locked() if item != cid]
        _listed_mem = list(ids)
        if os.environ.get("PROPMAP_TEST") != "1":
            try:
                store.init()
                store.set_meta("listed_places", json.dumps(_listed_mem, ensure_ascii=False))
            except Exception:
                pass
    return cid


def scrape_place_ok(city_id: str | None) -> bool:
    """Solo ciudades de provincia con búsqueda del usuario, catálogo o prioridad."""
    cid = (city_id or "").strip()
    if not cid or cid in _LISTED_SKIP or is_cache_artifact_id(cid):
        return False
    if cid in CABA_IDS or cid == DEFAULT_CITY:
        return True
    cfg = CITIES.get(cid) or {}
    if cfg.get("builtin"):
        return True
    if _junk_place_row(cid, cfg) or not _listed_row_is_city(cid, cfg):
        return False
    if cid in priority_place_ids():
        return True
    try:
        from . import schedule

        schedule.load()
        if cid in schedule.searched_ids() or cid in schedule.viewed_ids():
            return True
    except Exception:
        pass
    return cid in _ids_with_scrape_listings()


def _ghost_listed_place(cid: str, cfg: dict) -> bool:
    if cid in CABA_IDS or cid == DEFAULT_CITY or cfg.get("builtin"):
        return False
    if cid in priority_place_ids():
        return False
    try:
        from . import schedule

        schedule.load()
        if cid in schedule.searched_ids() or cid in schedule.viewed_ids():
            return False
    except Exception:
        return False
    return cid not in _ids_with_scrape_listings()


def forget_place(city_id: str | None) -> str:
    """Saca del catálogo un id que no es un lugar real."""
    from .geo import CITY_ALIASES

    global _listed_mem
    cid = (city_id or "").strip()
    if not cid or cid == DEFAULT_CITY or cid in CABA_IDS:
        return ""
    cfg = CITIES.get(cid) or {}
    if cfg.get("builtin"):
        return ""
    with _listed_lock:
        ids = [item for item in _listed_ids_locked() if item != cid]
        _listed_mem = list(ids)
        if os.environ.get("PROPMAP_TEST") != "1":
            try:
                store.init()
                store.set_meta("listed_places", json.dumps(_listed_mem, ensure_ascii=False))
            except Exception:
                pass
    CITIES.pop(cid, None)
    for token, owner in list(CITY_ALIASES.items()):
        if owner == cid or token == cid:
            CITY_ALIASES.pop(token, None)
    if os.environ.get("PROPMAP_TEST") != "1":
        try:
            _persist()
        except Exception:
            pass
        try:
            from .listings_cache import drop_city_snap

            drop_city_snap(cid)
        except Exception:
            pass
        try:
            from . import schedule

            schedule.forget(cid)
        except Exception:
            pass
    return cid


def _junk_place_row(city_id: str, cfg: dict | None = None) -> bool:
    if is_cache_artifact_id(city_id):
        return True
    row = cfg or CITIES.get(city_id) or {}
    label = fold(row.get("label") or city_id.replace("-", " "))
    if label.startswith("barrio ") or label.startswith("departamento "):
        return True
    kind = fold(str(row.get("kind") or row.get("addresstype") or ""))
    if kind in _BLOCKED_KINDS:
        return True
    cat = fold(str(row.get("categoria") or ""))
    return cat in _NOT_CITY_CATEGORIAS or cat.startswith("paraje") or cat.startswith("pje")


def _drop_artifact_cache_files() -> None:
    folder = Path(store.DATA_DIR) / "city_cache"
    if not folder.is_dir():
        return
    for path in folder.iterdir():
        name = path.name
        if name.endswith(".pins.json.gz") or name.endswith(".pins.json"):
            stem = name.replace(".pins.json.gz", "").replace(".pins.json", "")
        elif name.endswith(".meta.json"):
            stem = name[: -len(".meta.json")]
        elif name.endswith(".json.gz"):
            stem = name[: -len(".json.gz")]
        elif name.endswith(".json"):
            stem = name[: -len(".json")]
        else:
            continue
        if is_cache_artifact_id(stem):
            try:
                path.unlink()
            except OSError:
                pass


def _purge_duplicate_labels() -> list[str]:
    dropped: list[str] = []
    kept: dict[str, str] = {}
    for cid in list(listed_place_ids()):
        key = _label_key(cid)
        if not key:
            continue
        prev = kept.get(key)
        if prev is None:
            kept[key] = cid
            continue
        drop, keep = (cid, prev) if _prefer_listed_id(prev, cid) else (prev, cid)
        kept[key] = keep
        if drop != keep and forget_place(drop):
            dropped.append(drop)
    return dropped


def _catalog_count(cid: str, counts: dict[str, int]) -> int:
    token = _canonical_listed_id(cid) or cid
    total = 0
    for key, val in counts.items():
        got = _canonical_listed_id(key) or key
        if got == token:
            total += int(val or 0)
    return total


def _georef_rejects_city(query: str) -> bool:
    """True solo si Georef contestó y el nombre es un barrio, no una ciudad.

    Si la API no responde, no se borra nada: un corte no puede vaciar el desplegable.
    """
    try:
        rows = list(search_city_places(query, limit=6))
    except Exception:
        return False
    matched: list[dict] = []
    for place in rows:
        parsed = _from_georef_place(place)
        if parsed and _official_name_matches(query, parsed):
            matched.append(parsed)
    if not matched:
        return False
    return not any(_search_hit_ok(row) for row in matched)


def _should_forget_unofficial(cid: str, cfg: dict, counts: dict[str, int]) -> bool:
    """Una ciudad con avisos cargados no se borra si Georef no contesta."""
    if not cid or cid == DEFAULT_CITY or cid in CABA_IDS or cfg.get("builtin"):
        return False
    if _junk_place_row(cid, cfg):
        return True
    if os.environ.get("PROPMAP_TEST") == "1":
        return False
    label = str(cfg.get("label") or cid)
    if _georef_rejects_city(label):
        return True
    if listing_count_for_catalog(_catalog_count(cid, counts)):
        return False
    return official_place(label) is None


def _unlist_ghost_places() -> list[str]:
    """Saca del catálogo pueblos que nadie buscó y no tienen masa de avisos. Sin pegarle a la red."""
    if os.environ.get("PROPMAP_TEST") == "1":
        return []
    dropped: list[str] = []
    for cid in list(listed_place_ids()):
        if cid == DEFAULT_CITY or cid in CABA_IDS:
            continue
        cfg = CITIES.get(cid) or {}
        if cfg.get("builtin") or _junk_place_row(cid, cfg):
            continue
        if _ghost_listed_place(cid, cfg) and unlist_place(cid):
            dropped.append(cid)
    return dropped


def purge_unofficial_places() -> list[str]:
    """Tira ids inventados, barrios/parajes, y pueblos que nadie buscó ni tienen catálogo."""
    if os.environ.get("PROPMAP_TEST") != "1":
        _drop_artifact_cache_files()
    dropped = _unlist_ghost_places()
    seen: set[str] = set()
    candidates = list(listed_place_ids())
    for cfg in list(CITIES.values()):
        cid = str(cfg.get("id") or "")
        if cid and not cfg.get("builtin"):
            candidates.append(cid)
    counts: dict[str, int] = {}
    if os.environ.get("PROPMAP_TEST") != "1":
        try:
            counts = store.city_listing_counts()
        except Exception:
            counts = {}
    for cid in candidates:
        if cid in seen or cid == DEFAULT_CITY or cid in CABA_IDS:
            continue
        seen.add(cid)
        cfg = CITIES.get(cid) or {}
        if cfg.get("builtin"):
            continue
        if _should_forget_unofficial(cid, cfg, counts) and forget_place(cid):
            dropped.append(cid)
    dropped.extend(_purge_duplicate_labels())
    if os.environ.get("PROPMAP_TEST") != "1":
        try:
            from .listings_cache import rewrite_snap_cities

            rewrite_snap_cities()
        except Exception:
            log.exception("no pude actualizar el listado de ciudades en cache")
    return dropped


def _kick_unofficial_purge() -> None:
    global _purged_unofficial
    if _purged_unofficial or os.environ.get("PROPMAP_TEST") == "1":
        return
    _purged_unofficial = True

    def _job() -> None:
        try:
            purge_unofficial_places()
        except Exception:
            log.exception("no pude limpiar lugares que no existen")

    threading.Thread(target=_job, daemon=True, name="purge-places").start()


def catalog_place_ids(*, extra_have: set[str] | None = None) -> set[str]:
    """CABA y lugares con avisos de verdad. Uno o dos pines no entran al desplegable."""
    have = _ids_with_saved_listings()
    if extra_have:
        have |= {cid for cid in extra_have if cid}
    wanted = {DEFAULT_CITY}
    pool = set(listed_place_ids()) | set(have)
    for cid in pool:
        token = _canonical_listed_id(cid) or cid
        if not token or token in _LISTED_SKIP:
            continue
        cfg = CITIES.get(token) or {}
        if token != DEFAULT_CITY and token not in CABA_IDS and not _listed_row_is_city(token, cfg):
            continue
        if token == DEFAULT_CITY or token in have:
            wanted.add(token)
    return wanted


def _has_map_coords(cfg: dict | None) -> bool:
    if not cfg:
        return False
    try:
        lat = float(cfg.get("lat"))
        lon = float(cfg.get("lon"))
    except (TypeError, ValueError):
        return False
    if abs(lat - ARG_LAT) < 0.05 and abs(lon - ARG_LON) < 0.05:
        return False
    return True


def listed_cities(listings: list | None = None) -> list[dict]:
    load_custom_places()
    extra_counts: dict[str, int] = {}
    for item in listings or []:
        cid = _canonical_listed_id(getattr(item, "city", None) or "")
        if cid and cid not in _LISTED_SKIP:
            extra_counts[cid] = extra_counts.get(cid, 0) + 1
    extra_have = {cid for cid, n in extra_counts.items() if listing_count_for_catalog(n)}
    wanted = catalog_place_ids(extra_have=extra_have or None)
    counts: dict[str, int] = dict(extra_counts)
    if os.environ.get("PROPMAP_TEST") != "1":
        try:
            store.init()
            for cid, n in store.city_listing_counts().items():
                token = _canonical_listed_id(cid) or cid
                if token:
                    counts[token] = counts.get(token, 0) + int(n or 0)
        except Exception:
            pass
    seen: dict[str, dict] = {}
    empty = empty_view_ids()

    def take(cid: str, cfg: dict) -> None:
        if not cid or cid in seen or cid in empty or cid in _LISTED_SKIP or is_cache_artifact_id(cid):
            return
        if _canonical_listed_id(cid) != cid:
            return
        label = fold(cfg.get("label") or "")
        if label.startswith("barrio ") or label.startswith("departamento "):
            return
        if not _has_map_coords(cfg):
            return
        if cid != DEFAULT_CITY and cid not in CABA_IDS and not _listed_row_is_city(cid, cfg):
            return
        n = int(counts.get(cid) or 0)
        if os.environ.get("PROPMAP_TEST") != "1" and not listing_count_for_catalog(n):
            return
        row = _public_city(cfg)
        row["n"] = n
        seen[cid] = row

    for cfg in list(CITIES.values()):
        if cfg.get("id") in wanted:
            take(str(cfg.get("id") or ""), cfg)
    for cid in wanted:
        cfg = CITIES.get(cid)
        if cfg:
            take(cid, cfg)
    unique: dict[str, dict] = {}
    for row in seen.values():
        key = _label_key(str(row.get("id") or ""), row)
        if not key:
            continue
        prev = unique.get(key)
        if prev is None or _prefer_listed_id(str(row["id"]), str(prev["id"])):
            unique[key] = row
    return sorted(
        unique.values(),
        key=lambda row: (fold(row["label"]), fold(row.get("province") or "")),
    )


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


def _restore_hit(token: str, *, lat: float | None = None, lon: float | None = None) -> dict | None:
    """Ciudad de Georef que coincide con el id de los avisos y cae cerca de esos pines."""
    query = token.replace("-", " ")
    hits: list[dict] = []
    for place in search_city_places(query, limit=6):
        parsed = _from_georef_place(place)
        if not parsed or not _search_hit_ok(parsed) or not _official_name_matches(query, parsed):
            continue
        if lat is not None and lon is not None and parsed.get("lat") is not None and parsed.get("lon") is not None:
            if distance_km(lat, lon, float(parsed["lat"]), float(parsed["lon"])) > 80:
                continue
        hits.append(parsed)
    if not hits:
        return None

    def rank(row: dict) -> tuple[int, int]:
        prov = province_slug(str(row.get("province") or ""))
        aligned = token == str(row.get("id") or "") or (bool(prov) and token.endswith(f"-{prov}"))
        named = _same_place_token(token, str(row.get("label") or ""))
        return (1 if aligned else 0, 1 if named else 0)

    return max(hits, key=rank)


def _listing_centroids() -> dict[str, tuple[float, float]]:
    try:
        with store.connect() as conn:
            rows = conn.execute(
                """
                SELECT city, AVG(lat), AVG(lon)
                FROM listings
                WHERE city IS NOT NULL AND city != ''
                  AND lat IS NOT NULL AND lon IS NOT NULL
                GROUP BY city
                """
            ).fetchall()
    except Exception:
        return {}
    out: dict[str, tuple[float, float]] = {}
    for row in rows:
        cid = str(row[0] or "")
        if not cid:
            continue
        try:
            out[cid] = (float(row[1]), float(row[2]))
        except (TypeError, ValueError):
            continue
    return out


def _restore_loaded_cities() -> None:
    """Vuelve a registrar ciudades que tienen avisos y la limpieza había sacado."""
    if os.environ.get("PROPMAP_TEST") == "1":
        return
    try:
        counts = store.city_listing_counts()
    except Exception:
        log.exception("no pude leer ciudades cargadas")
        return
    centroids = _listing_centroids()
    empty = empty_view_ids()
    restored = 0
    for cid, n in counts.items():
        token = _canonical_listed_id(cid) or cid
        if not token or token in empty or token in _LISTED_SKIP or is_cache_artifact_id(token):
            continue
        if not listing_count_for_catalog(n):
            continue
        cfg = CITIES.get(token) or {}
        if _has_map_coords(cfg):
            continue
        center = centroids.get(cid) or centroids.get(token)
        lat, lon = center if center else (None, None)
        try:
            hit = _restore_hit(token, lat=lat, lon=lon)
        except Exception:
            log.exception("no pude reponer %s", token)
            continue
        if not hit:
            continue
        _register_view_city(
            token,
            label=str(hit.get("label") or token),
            lat=float(hit["lat"]),
            lon=float(hit["lon"]),
            province=str(hit.get("province") or ""),
            zoom=int(hit.get("zoom") or 13),
            slug=hit.get("slug"),
            meta=hit,
        )
        restored += 1
    if not restored:
        return
    log.info("ciudades cargadas repuestas: %s", restored)
    try:
        from .listings_cache import rewrite_snap_cities

        rewrite_snap_cities()
    except Exception:
        log.exception("no pude actualizar el desplegable de ciudades")


def _kick_restore_loaded_cities() -> None:
    global _restored_loaded
    if _restored_loaded or os.environ.get("PROPMAP_TEST") == "1":
        return
    _restored_loaded = True

    def _job() -> None:
        try:
            _restore_loaded_cities()
        except Exception:
            log.exception("no pude reponer ciudades cargadas")

    threading.Thread(target=_job, daemon=True, name="restore-cities").start()


def load_custom_places() -> None:
    if os.environ.get("PROPMAP_TEST") == "1":
        ensure_default_city()
        return
    _load_extents()
    _load_extra_slugs()
    raw = store.get_meta("custom_places")
    if raw:
        try:
            rows = json.loads(raw)
        except json.JSONDecodeError:
            rows = []
        if isinstance(rows, list):
            skipped = False
            for row in rows:
                if not isinstance(row, dict) or not row.get("id"):
                    continue
                if _junk_place_row(str(row["id"]), row):
                    skipped = True
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
                _apply_place_meta(str(row["id"]), row)
            if skipped and os.environ.get("PROPMAP_TEST") != "1":
                _persist()
    ensure_default_city()
    _unlist_ghost_places()
    _kick_restore_loaded_cities()
    _kick_unofficial_purge()


def _persist() -> None:
    rows = []
    for cfg in list(CITIES.values()):
        if cfg.get("builtin"):
            continue
        if is_cache_artifact_id(cfg.get("id")):
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
                "kind": cfg.get("kind") or "",
                "addresstype": cfg.get("addresstype") or cfg.get("kind") or "",
                "municipio": cfg.get("municipio") or "",
                "localidad_censal": cfg.get("localidad_censal") or "",
                "categoria": cfg.get("categoria") or "",
            }
        )
    store.set_meta("custom_places", json.dumps(rows, ensure_ascii=False))


def _name_hit(folded: str, names: list[str]) -> bool:
    compact_q = folded.replace("-", " ").replace(" ", "")
    for name in names:
        n = fold(name)
        if not n:
            continue
        spaced = n.replace("-", " ")
        if n == folded or spaced == folded.replace("-", " "):
            return True
        compact_n = spaced.replace(" ", "")
        if len(compact_q) >= 2 and compact_n.startswith(compact_q):
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

    if not str(cfg.get("province") or "").strip():
        cached = lookup_place(str(cfg.get("label") or cfg.get("id") or ""), remote=False)
        if cached and cached.get("province"):
            apply_city_province(str(cfg.get("id") or ""), str(cached.get("province") or ""))
    pretty = _province_display(str(cfg.get("province") or ""))
    return {
        "id": cfg["id"],
        "label": cfg["label"],
        "hint": _place_hint(cfg),
        "province_label": "" if _is_caba_province(pretty) else pretty,
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
    province = province_slug(str(place.get("province") or ""))
    city_id = _place_id(name, province)
    slug = "capital-federal" if _is_caba_province(province) else slug_place(name)
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
        "kind": str(place.get("kind") or "localidad"),
        "municipio": str(place.get("municipio") or ""),
        "localidad_censal": str(place.get("localidad_censal") or ""),
        "categoria": str(place.get("categoria") or ""),
        "hint": f"{name}, {place.get('province') or 'Argentina'}",
        "province_label": str(place.get("province") or "").strip(),
    }


_SUBURB_TYPES = {"suburb", "neighbourhood", "neighborhood", "quarter", "hamlet", "village"}


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
    if addresstype in _BLOCKED_KINDS:
        return None
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
    state = fold(addr.get("state") or addr.get("state_district") or "")
    province = province_slug(state) if state else ""
    city_id = _place_id(name, province)
    if _is_caba_province(province) and addresstype not in _SUBURB_TYPES:
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
    slug = (
        "capital-federal"
        if _is_caba_province(province) and addresstype not in _SUBURB_TYPES
        else slug_place(name)
    )
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
        "province_label": _province_display(province) if province else "",
    }


def _place_names(city_id: str, cfg: dict | None = None, extra: list[str] | None = None) -> set[str]:
    row = cfg if cfg is not None else (CITIES.get(city_id) or {})
    names = {fold(city_id), fold(row.get("label") or "")}
    for alias in row.get("aliases") or []:
        names.add(fold(alias))
    for item in extra or []:
        names.add(fold(item))
    return {name for name in names if name}


def _names_overlap(left: set[str], right: set[str]) -> bool:
    if left & right:
        return True
    for a in left:
        for b in right:
            if len(a) >= 5 and len(b) >= 5 and (a in b or b in a):
                return True
    return False


def _adopt_existing_city(parsed: dict) -> str:
    """Reusa un id ya cargado solo si es el mismo lugar, no un homónimo de otra provincia."""
    from .place_api import _same_province

    addresstype = parsed.get("addresstype") or ""
    if addresstype in _SUBURB_TYPES:
        return parsed["id"]
    parsed_id = parsed.get("id") or ""
    parsed_names = _place_names(parsed_id, extra=[str(parsed.get("label") or "")])
    parsed_prov = province_slug(str(parsed.get("province") or ""))
    best = None
    best_d = 1e9
    for city_id, cfg in list(CITIES.items()):
        if not _names_overlap(parsed_names, _place_names(city_id, cfg)):
            continue
        cfg_prov = province_slug(str(cfg.get("province") or ""))
        if parsed_prov and cfg_prov and not _same_province(parsed_prov, cfg_prov):
            continue
        dist = distance_km(parsed["lat"], parsed["lon"], cfg["lat"], cfg["lon"])
        limit = min(float(cfg.get("radius_km") or 15), 14)
        if dist <= limit and dist < best_d:
            best, best_d = city_id, dist
    return best or parsed_id


def hydrate_place_extent(city_id: str, query: str = "") -> None:
    cfg = CITIES.get(city_id) or {}
    if not _has_map_coords(cfg):
        return
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
    for city_id, cfg in list(CITIES.items()):
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
    for city_id, cfg in list(CITIES.items()):
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


_WATER_TYPES = {
    "river",
    "stream",
    "canal",
    "drain",
    "dock",
    "basin",
    "harbour",
    "harbor",
    "bay",
    "lagoon",
    "reservoir",
    "pond",
    "water",
    "wetland",
    "coastline",
    "strait",
    "sea",
    "ocean",
    "shoal",
    "oxbow",
}
_rev_mem: dict[str, dict[str, Any] | None] = {}


def reverse_is_water(lat: float, lon: float) -> bool | None:
    """Nominatim reverse: True si el punto es agua. None si no se pudo consultar."""
    key = f"{round(float(lat), 5)}:{round(float(lon), 5)}"
    if key in _rev_mem:
        row = _rev_mem[key]
        return None if row is None else bool(row.get("water"))
    if os.environ.get("PROPMAP_TEST") == "1":
        return None
    try:
        raw = store.get_meta(f"nom:rev:{key}")
        cached = json.loads(raw) if raw else None
        if isinstance(cached, dict) and "water" in cached:
            _rev_mem[key] = cached
            return bool(cached["water"])
    except Exception:
        pass
    try:
        with httpx.Client(headers=HEADERS, timeout=18.0) as client:
            response = client.get(
                NOMINATIM_REVERSE,
                params={
                    "lat": f"{lat:.6f}",
                    "lon": f"{lon:.6f}",
                    "format": "json",
                    "zoom": 18,
                    "addressdetails": 1,
                },
            )
            response.raise_for_status()
            data = response.json()
        time.sleep(1.05)
    except Exception:
        _rev_mem[key] = None
        return None
    if not isinstance(data, dict):
        _rev_mem[key] = None
        return None
    cls = str(data.get("class") or data.get("category") or "").lower()
    typ = str(data.get("type") or data.get("addresstype") or "").lower()
    extra = data.get("extratags") if isinstance(data.get("extratags"), dict) else {}
    wet = cls == "waterway" or typ in _WATER_TYPES or (cls == "natural" and typ in _WATER_TYPES)
    if extra.get("natural") == "water" or extra.get("waterway"):
        wet = True
    payload = {"water": wet, "class": cls, "type": typ}
    _rev_mem[key] = payload
    try:
        store.set_meta(f"nom:rev:{key}", json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass
    return wet


def _overpass(query: str) -> dict | None:
    try:
        with httpx.Client(headers=HEADERS, timeout=90.0) as client:
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
_outline_lock = threading.Lock()
_outline_inflight: set[str] = set()


def osm_pending(city_id: str | None) -> bool:
    if not city_id:
        return False
    with _osm_lock:
        return city_id in _osm_inflight


def _lonlat_to_latlon(coords: list) -> list[list[float]]:
    ring: list[list[float]] = []
    for point in coords or []:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        try:
            lon, lat = float(point[0]), float(point[1])
        except (TypeError, ValueError):
            continue
        ring.append([lat, lon])
    if len(ring) >= 4 and ring[0] != ring[-1]:
        ring.append(ring[0])
    return ring


def rings_from_geojson(geom: dict | None) -> list[list[list[float]]]:
    if not isinstance(geom, dict):
        return []
    kind = str(geom.get("type") or "")
    coords = geom.get("coordinates")
    if kind == "Feature":
        return rings_from_geojson(geom.get("geometry") if isinstance(geom.get("geometry"), dict) else None)
    if kind == "GeometryCollection":
        out: list[list[list[float]]] = []
        for part in geom.get("geometries") or []:
            if isinstance(part, dict):
                out.extend(rings_from_geojson(part))
        return out
    if kind == "Polygon" and isinstance(coords, list) and coords:
        ring = _lonlat_to_latlon(coords[0])
        return [ring] if len(ring) >= 4 else []
    if kind == "MultiPolygon" and isinstance(coords, list):
        out = []
        for poly in coords:
            if isinstance(poly, list) and poly:
                ring = _lonlat_to_latlon(poly[0])
                if len(ring) >= 4:
                    out.append(ring)
        return out
    return []


def _rings_extent_km(rings: list[list[list[float]]], lat: float, lon: float) -> float:
    lats: list[float] = []
    lons: list[float] = []
    for ring in rings:
        for point in ring:
            if len(point) >= 2:
                lats.append(float(point[0]))
                lons.append(float(point[1]))
    if len(lats) < 4:
        return 0.0
    return _extent_radius_km((min(lats), min(lons), max(lats), max(lons)), lat, lon)


def _outline_queries(city_id: str) -> list[str]:
    cfg = CITIES.get(city_id) or {}
    seen: set[str] = set()
    out: list[str] = []
    for raw in (
        cfg.get("label"),
        *(cfg.get("aliases") or []),
        city_id.replace("-", " "),
    ):
        text = str(raw or "").strip()
        if len(text) < 3:
            continue
        key = fold(text)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(text)
    out.sort(key=lambda item: (-len(item), item))
    return out


def _outline_from_nominatim(city_id: str, lat: float, lon: float) -> list[list[list[float]]] | None:
    cfg = CITIES.get(city_id) or {}
    target = float(cfg.get("radius_km") or 16)
    hi = min(80.0, max(40.0, target * 2.5))
    best: list[list[list[float]]] | None = None
    best_span = -1.0
    for q in _outline_queries(city_id)[:6]:
        rows = _nominatim(
            {
                "q": q,
                "countrycodes": "ar",
                "format": "json",
                "addressdetails": 1,
                "polygon_geojson": 1,
                "limit": 5,
            }
        )
        for row in rows:
            parsed = _from_nominatim(row)
            if not parsed:
                continue
            addresstype = fold(str(row.get("addresstype") or row.get("type") or parsed.get("addresstype") or ""))
            if addresstype in _SUBURB_TYPES:
                continue
            rings = rings_from_geojson(row.get("geojson") if isinstance(row.get("geojson"), dict) else None)
            if not rings:
                continue
            if distance_km(parsed["lat"], parsed["lon"], lat, lon) > 45:
                continue
            if not _point_in_rings(lat, lon, rings):
                continue
            span = _rings_extent_km(rings, lat, lon)
            if span < 3 or span > hi:
                continue
            if span > best_span:
                best, best_span = rings, span
        if best:
            break
    return best


def _outline_from_overpass(lat: float, lon: float, city_id: str) -> list[list[list[float]]] | None:
    cfg = CITIES.get(city_id) or {}
    target = float(cfg.get("radius_km") or 16)
    hi = min(80.0, max(40.0, target * 2.5))
    query = f"""
[out:json][timeout:40];
is_in({lat},{lon})->.a;
rel(pivot.a)["boundary"="administrative"]["admin_level"~"4|6|7|8"];
out geom;
"""
    data = _overpass(query)
    if not isinstance(data, dict):
        return None
    best: list[list[list[float]]] | None = None
    best_span = -1.0
    for el in data.get("elements") or []:
        outers: list[list[list[float]]] = []
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
        ring = _simplify_ring(_stitch_ways(outers)[0], max_pts=240)
        if len(ring) < 8:
            continue
        rings = [ring]
        if not _point_in_rings(lat, lon, rings):
            continue
        span = _rings_extent_km(rings, lat, lon)
        if span < 3 or span > hi:
            continue
        if span > best_span:
            best, best_span = rings, span
    return best


def fetch_city_outline(city_id: str) -> list[list[list[float]]] | None:
    if os.environ.get("PROPMAP_TEST") == "1":
        return city_outline_rings(city_id) or None
    cfg = CITIES.get(city_id) or {}
    lat = cfg.get("lat")
    lon = cfg.get("lon")
    if lat is None or lon is None:
        from .geo import city_center

        lat, lon = city_center(city_id)
    rings = _outline_from_nominatim(city_id, float(lat), float(lon))
    if not rings:
        rings = _outline_from_overpass(float(lat), float(lon), city_id)
    if not rings:
        return None
    return [_simplify_ring(ring, max_pts=240) for ring in rings if len(ring) >= 4]


def _sync_barrios_to_outline(city_id: str) -> None:
    from .geo import city_polygons, remember_city_polygons

    rings = city_outline_rings(city_id)
    if not rings:
        return
    rows = city_polygons(city_id)
    if not rows:
        return
    kept = []
    for row in rows:
        try:
            lat = float(row.get("lat"))
            lon = float(row.get("lon"))
        except (TypeError, ValueError):
            ring = row.get("ring") or []
            if len(ring) < 4:
                continue
            lat, lon = float(ring[0][0]), float(ring[0][1])
        if _point_in_rings(lat, lon, rings):
            kept.append(row)
    if len(kept) == len(rows):
        return
    store.set_meta(f"osm_barrios:{city_id}", json.dumps(kept, ensure_ascii=False))
    remember_city_polygons(city_id, kept)


def _save_city_outline(city_id: str, rings: list[list[list[float]]]) -> None:
    remember_city_outline(city_id, rings)
    if not city_outline_rings(city_id):
        return
    store.set_meta(
        f"osm_outline:{city_id}",
        json.dumps({"rings": rings, "source": "nominatim"}, ensure_ascii=False),
    )
    _sync_barrios_to_outline(city_id)
    try:
        from .listings_cache import invalidate_city_geo

        invalidate_city_geo(city_id)
    except Exception:
        pass


def ensure_city_outline(city_id: str | None, *, blocking: bool = False) -> list[list[list[float]]]:
    if not city_id or city_id in {"fuera", "otros"}:
        return []
    have = city_outline_rings(city_id)
    if have:
        return have
    if os.environ.get("PROPMAP_TEST") == "1":
        return []

    def _job() -> None:
        try:
            rings = fetch_city_outline(city_id)
            if rings:
                _save_city_outline(city_id, rings)
        except Exception:
            log.exception("city outline %s", city_id)
        finally:
            with _outline_lock:
                _outline_inflight.discard(city_id)

    start = False
    with _outline_lock:
        if city_id in _outline_inflight:
            if not blocking:
                return []
        else:
            _outline_inflight.add(city_id)
            start = True
    if blocking:
        if start:
            _job()
        else:
            while True:
                with _outline_lock:
                    if city_id not in _outline_inflight:
                        break
                time.sleep(0.05)
        return city_outline_rings(city_id)
    if start:
        threading.Thread(target=_job, daemon=True, name=f"city-outline-{city_id}").start()
    return []


def ensure_osm_barrios(city_id: str | None, *, blocking: bool = False) -> list[dict]:
    """Carga polígonos de barrios de OSM para cualquier ciudad y los cachea."""
    from .geo import CITIES, city_center, city_polygons, remember_city_polygons

    if not city_id or city_id in {"fuera", "otros"}:
        return []
    ensure_city_outline(city_id, blocking=blocking)
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
            if not city_outline_rings(city_id):
                rings = fetch_city_outline(city_id)
                if rings:
                    _save_city_outline(city_id, rings)
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
            _sync_barrios_to_outline(city_id)
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


def fetch_osm_water_polygons(city_id: str) -> list[list[list[float]]] | None:
    """Polígonos de agua OSM (ríos, dársenas) de la ciudad. No un catálogo local."""
    cfg = CITIES.get(city_id or "") or {}
    box = cfg.get("bbox")
    if box:
        south, west, north, east = box
        area = f"({south},{west},{north},{east})"
    else:
        lat, lon = cfg.get("lat"), cfg.get("lon")
        if lat is None or lon is None:
            try:
                from .geo import city_center

                lat, lon = city_center(city_id)
            except Exception:
                return None
        radius = int(min(max(float(cfg.get("radius_km") or 16) * 1000, 8000), 28000))
        area = f"(around:{radius},{lat},{lon})"
    query = f"""
[out:json][timeout:50];
(
  way["natural"="water"]{area};
  way["waterway"="riverbank"]{area};
  way["waterway"="dock"]{area};
  rel["natural"="water"]{area};
);
out body geom;
"""
    data = _overpass(query)
    if data is None:
        return None
    rings: list[list[list[float]]] = []
    for el in data.get("elements") or []:
        geom = el.get("geometry") or []
        pts = [[float(p["lat"]), float(p["lon"])] for p in geom if "lat" in p and "lon" in p]
        if len(pts) >= 4:
            if pts[0] != pts[-1]:
                pts.append(pts[0])
            rings.append([[round(a, 5), round(b, 5)] for a, b in pts])
            continue
        outers = []
        for member in el.get("members") or []:
            if member.get("type") != "way" or member.get("role") not in {"outer", ""}:
                continue
            mgeom = member.get("geometry") or []
            mpts = [[float(p["lat"]), float(p["lon"])] for p in mgeom if "lat" in p and "lon" in p]
            if len(mpts) >= 2:
                outers.append(mpts)
        if not outers:
            continue
        for ring in _stitch_ways(outers):
            if len(ring) < 4:
                continue
            closed = ring if ring[0] == ring[-1] else ring + [ring[0]]
            rings.append([[round(a, 5), round(b, 5)] for a, b in closed])
    return rings


def ensure_osm_water(city_id: str | None, *, blocking: bool = False) -> list[list[list[float]]]:
    from .geo import remember_water_rings

    if not city_id or city_id in {"fuera", "otros"}:
        return []
    store.init()
    raw = store.get_meta(f"osm_water:{city_id}")
    if raw:
        try:
            rings = json.loads(raw)
        except json.JSONDecodeError:
            rings = []
        if isinstance(rings, list) and rings:
            remember_water_rings(city_id, rings)
            return rings
    if os.environ.get("PROPMAP_TEST") == "1":
        return []

    def _job() -> None:
        try:
            rows = fetch_osm_water_polygons(city_id)
            if rows is None:
                return
            store.set_meta(f"osm_water:{city_id}", json.dumps(rows, ensure_ascii=False))
            if rows:
                remember_water_rings(city_id, rows)
        except Exception:
            log.exception("osm water %s", city_id)

    if blocking:
        _job()
        raw = store.get_meta(f"osm_water:{city_id}")
        try:
            rings = json.loads(raw) if raw else []
        except json.JSONDecodeError:
            rings = []
        return rings if isinstance(rings, list) else []
    threading.Thread(target=_job, daemon=True, name=f"osm-water-{city_id}").start()
    return []


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
