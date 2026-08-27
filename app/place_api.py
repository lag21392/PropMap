from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

GEOREF = "https://apis.datos.gob.ar/georef/api"
HEADERS = {
    "User-Agent": "PropMap/1.0 (local real-estate map; no commercial use)",
    "Accept": "application/json",
}
_NOT_A_PLACE = frozenset(
    {
        "argentina",
        "venta",
        "departamento",
        "casa",
        "ph",
        "terreno",
        "lote",
        "lotes",
        "duplex",
        "oportunidad",
        "financiacion",
        "financiación",
        "alquiler",
        "complex",
        "complejo",
        "barrio privado",
        "zona norte",
        "zona sur",
        "zona oeste",
        "zona este",
        "centro",
        "norte",
        "sur",
        "este",
        "oeste",
    }
)
_EN_PLACE = re.compile(
    r"\ben(?:\s+venta\s+en|\s+alquiler\s+en)?\s+([a-záéíóúñ0-9][a-záéíóúñ0-9\s\.\-]{2,42})",
    re.I,
)
_BARRIO_PLACE = re.compile(
    r"\bbarrio(?:\s+privado)?\s+([a-záéíóúñ0-9][a-záéíóúñ0-9\s\.\-]{2,42})",
    re.I,
)
_COMMA_CHUNK = re.compile(r"[^,/|]+")
_CUT = re.compile(r"\s+(?:con|para|de|del|al|y|e/|esquina|cod:|financi)\b|\s*[\(\[]", re.I)

_mem: dict[str, dict[str, Any] | None] = {}
_provinces: list[dict[str, Any]] | None = None


def remember(name: str, place: dict[str, Any] | None) -> None:
    token = _fold(name)
    if token:
        _mem[token] = None if place is None else dict(place)


def reset_cache() -> None:
    _mem.clear()
    global _provinces
    _provinces = None


def listing_places(item, *, remote: bool = True) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    cities = _listing_scope_cities(item)
    for raw, hint in _candidates(item):
        place = _local_barrio_place(raw, cities)
        if not place:
            place = lookup_place(raw, province_hint=hint or _scope_province(cities), remote=remote)
        if not place:
            continue
        key = _fold(str(place.get("name") or raw))
        if not key or key in seen:
            continue
        seen.add(key)
        found.append(place)
    return found


def _listing_scope_cities(item) -> list[str]:
    from .geo import default_city, listing_mentions_city

    extra = getattr(item, "extra", None) or {}
    cities: list[str] = []
    for cid in (getattr(item, "city", None), extra.get("search_city")):
        cid = str(cid or "").strip()
        if not cid or cid in {"fuera", "otros", "argentina"}:
            continue
        if cid not in cities:
            cities.append(cid)
    home = default_city()
    if home not in cities:
        try:
            if listing_mentions_city(item, home):
                cities.append(home)
        except Exception:
            pass
    return cities


def _scope_province(cities: list[str]) -> str | None:
    from .geo import CITIES

    for cid in cities:
        cfg = CITIES.get(cid) or {}
        hint = str(cfg.get("province") or cfg.get("label") or "").strip()
        if hint:
            return hint
    return None


def _local_barrio_place(name: str, cities: list[str]) -> dict[str, Any] | None:
    from .geo import CITIES, barrios_for

    token = _fold(name)
    if not token or token in _NOT_A_PLACE:
        return None
    for cid in cities:
        for barrio in barrios_for(cid):
            names = [barrio.get("name") or "", *(barrio.get("aliases") or [])]
            if not any(_fold(n) == token for n in names if n):
                continue
            cfg = CITIES.get(cid) or {}
            lat, lon = barrio.get("lat"), barrio.get("lon")
            if lat is None or lon is None:
                continue
            return {
                "name": barrio.get("name") or name,
                "kind": "barrio",
                "province": str(cfg.get("province") or ""),
                "lat": lat,
                "lon": lon,
            }
    return None


def place_conflicts_city(place: dict[str, Any], city: str) -> bool:
    from .geo import CITIES, city_center, city_radius_km, distance_km, own_barrio_names, own_place_names

    token = _fold(str(place.get("name") or ""))
    own = own_place_names(city) | own_barrio_names(city)
    if token and token in own:
        return False
    if token and len(token) >= 8:
        for name in own:
            if len(name) >= 8 and (token in name or name in token):
                return False
    cfg = CITIES.get(city) or {}
    own_prov = _fold(str(cfg.get("province") or ""))
    place_prov = _province_slug(str(place.get("province") or ""))
    lat, lon = place.get("lat"), place.get("lon")
    if lat is not None and lon is not None:
        from .geo import city_for_point, same_place_ids

        sit = city_for_point(float(lat), float(lon))
        if sit and sit in same_place_ids(city):
            return False
    if own_prov and place_prov and not _same_province(own_prov, place_prov):
        return True
    if lat is None or lon is None:
        return False
    clat, clon = city_center(city)
    limit = max(float(city_radius_km(city) or 25), 25) * 1.6
    return distance_km(float(lat), float(lon), clat, clon) > limit


def search_localidades(query: str, limit: int = 8) -> list[dict[str, Any]]:
    q = (query or "").strip()
    if len(q) < 2:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    kinds = (("/localidades", "localidades", "localidad"), ("/municipios", "municipios", "municipio"))
    for path, key, kind in kinds:
        data = _georef(path, {"nombre": q, "max": limit, "campos": "completo"})
        for row in data.get(key) or []:
            place = _from_georef(row, kind)
            if not place or place.get("lat") is None or place.get("lon") is None:
                continue
            token = _fold(str(place.get("name") or ""))
            if not token or token in seen:
                continue
            seen.add(token)
            out.append(place)
            if len(out) >= limit:
                return out
    return out


def lookup_place(name: str, province_hint: str | None = None, *, remote: bool = True) -> dict[str, Any] | None:
    token = _fold(name)
    if not token or token in _NOT_A_PLACE or len(token) < 3:
        return None
    cached = _read_cache(token)
    if cached is not None or token in _mem:
        place = cached if cached is not None else _mem.get(token)
        if place and province_hint and not _hint_ok(place, province_hint):
            if not remote:
                return place
            return _lookup_remote(name, province_hint) or place
        return place
    if not remote:
        return None
    place = _lookup_remote(name, province_hint)
    _write_cache(token, place)
    return place


def province_slug(name: str) -> str:
    return _province_slug(name)


def provinces() -> list[dict[str, Any]]:
    global _provinces
    if _provinces is not None:
        return _provinces
    cached = _read_json("georef:provincias")
    if isinstance(cached, list) and cached:
        _provinces = cached
        return _provinces
    data = _georef("/provincias", {"max": 30, "campos": "id,nombre"})
    rows = []
    for row in data.get("provincias") or []:
        nombre = str(row.get("nombre") or "").strip()
        if not nombre:
            continue
        rows.append({"id": str(row.get("id") or ""), "nombre": nombre, "slug": _fold(nombre).replace(" ", "-")})
    if rows:
        _write_json("georef:provincias", rows)
        _provinces = rows
    else:
        _provinces = []
    return _provinces


def _candidates(item) -> list[tuple[str, str | None]]:
    from .geo import parse_street

    parts = [
        str(getattr(item, "title", "") or ""),
        str(getattr(item, "address", "") or ""),
        str(getattr(item, "barrio", "") or ""),
    ]
    text = " · ".join(p for p in parts if p.strip())
    out: list[tuple[str, str | None]] = []
    seen: set[str] = set()

    def add(raw: str, hint: str | None = None) -> None:
        street, number = parse_street(raw or "")
        if street and number:
            return
        name = _clean_name(raw)
        if not name:
            return
        street, number = parse_street(name)
        if street and number:
            return
        token = _fold(name)
        if not token or token in seen or token in _NOT_A_PLACE:
            return
        seen.add(token)
        out.append((name, _clean_name(hint) if hint else None))

    for match in _EN_PLACE.finditer(text):
        add(match.group(1))
    for match in _BARRIO_PLACE.finditer(text):
        add(match.group(1))
    chunks = [_clean_name(part) for part in _COMMA_CHUNK.findall(text)]
    chunks = [c for c in chunks if c]
    for idx, chunk in enumerate(chunks):
        nxt = chunks[idx + 1] if idx + 1 < len(chunks) else None
        add(chunk, nxt if nxt and _looks_province(nxt) else None)
        if "/" in chunk:
            for bit in chunk.split("/"):
                add(bit)
        for bit in re.split(r"\s*[-–·•|]\s*", chunk):
            if bit != chunk:
                add(bit)
    barrio = _clean_name(str(getattr(item, "barrio", "") or ""))
    if barrio and len(barrio) <= 40 and len(barrio.split()) <= 4:
        add(barrio)
    return out


def _lookup_remote(name: str, province_hint: str | None) -> dict[str, Any] | None:
    hint = _fold(province_hint or "")
    for path, key in (("/localidades", "localidades"), ("/municipios", "municipios"), ("/asentamientos", "asentamientos")):
        data = _georef(path, {"nombre": name, "max": 8, "campos": "completo"})
        rows = [_from_georef(row, key[:-1]) for row in data.get(key) or []]
        rows = [row for row in rows if row]
        if not rows:
            continue
        if hint:
            hinted = [row for row in rows if _hint_ok(row, province_hint or "")]
            if hinted:
                return hinted[0]
        exact = [row for row in rows if _fold(str(row.get("name") or "")) == _fold(name)]
        pick = exact[0] if exact else rows[0]
        if len({_fold(str(r.get("province") or "")) for r in rows}) > 1 and not exact and not hint:
            continue
        return pick
    return _nominatim_place(name, province_hint)


def _nominatim_place(name: str, province_hint: str | None) -> dict[str, Any] | None:
    from .places import _from_nominatim, _nominatim

    q = f"{name}, {province_hint}, Argentina" if province_hint else f"{name}, Argentina"
    rows = _nominatim({"q": q, "countrycodes": "ar", "format": "json", "addressdetails": 1, "limit": 3})
    for row in rows:
        parsed = _from_nominatim(row)
        if not parsed:
            continue
        return {
            "name": parsed.get("label") or name,
            "kind": "nominatim",
            "province": parsed.get("province") or "",
            "lat": parsed.get("lat"),
            "lon": parsed.get("lon"),
        }
    return None


_STREET_KIND_ALIASES = {
    "pasaje": ("pasaje", "pje"),
    "pje": ("pasaje", "pje"),
    "calle": ("calle",),
    "avenida": ("avenida", "av"),
    "av": ("avenida", "av"),
}
_STREET_KIND_RE = re.compile(r"^(pje|pasaje|calle|avenida|av)\s+")


def _street_name_variants(street: str) -> list[str]:
    token = _fold(street)
    words = token.split()
    if not words:
        return []
    kind = words[0] if words[0] in _STREET_KIND_ALIASES else ""
    bare = " ".join(words[1:]) if kind else token
    out: list[str] = []

    def add(name: str) -> None:
        folded = _fold(name)
        if folded and folded not in out:
            out.append(folded)

    add(token)
    if kind:
        for alias in _STREET_KIND_ALIASES[kind]:
            add(f"{alias} {bare}")
        add(bare)
    return out


def _calle_bare(name: str) -> str:
    return _STREET_KIND_RE.sub("", _fold(name)).strip()


def _calle_name_hit(query: str, row: dict[str, Any]) -> bool:
    q_bare = _calle_bare(query)
    n_bare = _calle_bare(str(row.get("nombre") or ""))
    return bool(q_bare and n_bare and q_bare == n_bare and len(q_bare) >= 3)


def _calle_in_city(row: dict[str, Any], city_id: str) -> bool:
    from .geo import CITIES, in_city_radius

    if not city_id:
        return True
    cfg = CITIES.get(city_id) or {}
    want = _fold(str(cfg.get("label") or ""))
    censal = row.get("localidad_censal") or {}
    got = _fold(str(censal.get("nombre") or row.get("localidad_censal_nombre") or ""))
    if want and got:
        return want == got or want in got or got in want
    ubi = row.get("ubicacion") or row.get("centroide") or {}
    try:
        lat, lon = float(ubi["lat"]), float(ubi["lon"])
    except (TypeError, ValueError, KeyError):
        return not want
    return in_city_radius(lat, lon, city_id)


def _altura_span(row: dict[str, Any]) -> tuple[int, int] | None:
    nums: list[int] = []
    alt = row.get("altura")
    if isinstance(alt, dict):
        for side in alt.values():
            if isinstance(side, dict):
                for value in side.values():
                    try:
                        nums.append(int(value))
                    except (TypeError, ValueError):
                        pass
    for key in (
        "altura_inicio_derecha",
        "altura_inicio_izquierda",
        "altura_fin_derecha",
        "altura_fin_izquierda",
    ):
        try:
            nums.append(int(row[key]))
        except (TypeError, ValueError, KeyError):
            pass
    nums = [n for n in nums if n > 0]
    if not nums:
        return None
    return min(nums), max(nums)


def _direccion_lookup(street: str, num: int, city_id: str) -> dict[str, Any] | None:
    from .geo import CITIES, in_city_radius

    cfg = CITIES.get(city_id) or {}
    direccion = f"{street} {num}"
    attempts: list[dict[str, Any]] = []
    loc = str(cfg.get("label") or "").strip()
    prov = str(cfg.get("province") or "").strip()
    if loc:
        attempts.append({"direccion": direccion, "localidad": loc, "max": 5, "campos": "completo"})
    if prov:
        attempts.append({"direccion": direccion, "provincia": prov, "max": 8, "campos": "completo"})
    if not city_id:
        attempts.append({"direccion": direccion, "max": 8, "campos": "completo"})
    for params in attempts:
        data = _georef("/direcciones", params)
        for row in data.get("direcciones") or []:
            ubi = row.get("ubicacion") or {}
            try:
                lat, lon = float(ubi["lat"]), float(ubi["lon"])
            except (TypeError, ValueError, KeyError):
                continue
            if city_id and (CITIES.get(city_id) or {}) and not in_city_radius(lat, lon, city_id):
                continue
            return {
                "lat": lat,
                "lon": lon,
                "label": str(row.get("nomenclatura") or direccion),
                "street": str((row.get("calle") or {}).get("nombre") or street),
                "number": num,
            }
    return None


def _pick_calle(street: str, city_id: str) -> dict[str, Any] | None:
    from .geo import CITIES

    cfg = CITIES.get(city_id) or {}
    prov = str(cfg.get("province") or "").strip()
    names = _street_name_variants(street) or [_fold(street)]
    for nombre in names:
        params: dict[str, Any] = {"nombre": nombre, "max": 10, "campos": "completo"}
        if prov:
            params["provincia"] = prov
        data = _georef("/calles", params)
        for row in data.get("calles") or []:
            if not isinstance(row, dict):
                continue
            if not (_calle_name_hit(nombre, row) or _calle_name_hit(street, row)):
                continue
            if not _calle_in_city(row, city_id):
                continue
            return row
    return None


def geocode_direccion(street: str, number: int | str, city_id: str = "") -> dict[str, Any] | None:
    """Calle y altura vía Georef /direcciones. No usa catálogos locales."""
    try:
        num = int(str(number).strip())
    except (TypeError, ValueError):
        return None
    token = f"dir:{_fold(street)}:{num}:{_fold(city_id)}"
    if token in _mem:
        cached = _mem[token]
        return None if cached is None else dict(cached)
    disk = _read_json(f"georef:dir:{token}")
    if isinstance(disk, dict) and disk.get("lat") is not None:
        _mem[token] = disk
        return dict(disk)

    best = None
    for name in _street_name_variants(street) or [_fold(street)]:
        best = _direccion_lookup(name, num, city_id)
        if best:
            break
    if not best:
        calle = _pick_calle(street, city_id)
        if calle:
            official = str(calle.get("nombre") or street)
            best = _direccion_lookup(official, num, city_id)
            if not best:
                span = _altura_span(calle)
                if span:
                    lo, hi = span
                    clamped = min(max(num, lo), hi)
                    if clamped != num:
                        best = _direccion_lookup(official, clamped, city_id)
    _mem[token] = best
    if best:
        _write_json(f"georef:dir:{token}", best)
    return None if best is None else dict(best)


def geocode_interseccion(calle_a: str, calle_b: str, city_id: str = "") -> dict[str, Any] | None:
    """Esquina vía Georef /intersecciones."""
    left, right = _fold(calle_a), _fold(calle_b)
    if not left or not right:
        return None
    token = f"esq:{left}:{right}:{_fold(city_id)}"
    alt = f"esq:{right}:{left}:{_fold(city_id)}"
    if token in _mem:
        cached = _mem[token]
        return None if cached is None else dict(cached)
    if alt in _mem:
        cached = _mem[alt]
        return None if cached is None else dict(cached)
    disk = _read_json(f"georef:{token}") or _read_json(f"georef:{alt}")
    if isinstance(disk, dict) and disk.get("lat") is not None:
        _mem[token] = disk
        return dict(disk)
    from .geo import CITIES, in_city_radius

    cfg = CITIES.get(city_id) or {}
    loc = str(cfg.get("label") or "").strip()
    prov = str(cfg.get("province") or "").strip()
    attempts: list[dict[str, Any]] = []
    for a, b in ((calle_a, calle_b), (calle_b, calle_a)):
        base = {"calle": a, "calle_cruce": b, "max": 5, "campos": "completo"}
        if loc:
            attempts.append({**base, "localidad": loc})
        if prov:
            attempts.append({**base, "provincia": prov})
        attempts.append(base)
    best = None
    for params in attempts:
        data = _georef("/intersecciones", params)
        rows = data.get("intersecciones") or data.get("direcciones") or []
        for row in rows:
            ubi = row.get("ubicacion") or {}
            try:
                lat, lon = float(ubi["lat"]), float(ubi["lon"])
            except (TypeError, ValueError, KeyError):
                continue
            if city_id and (CITIES.get(city_id) or {}) and not in_city_radius(lat, lon, city_id):
                continue
            best = {
                "lat": lat,
                "lon": lon,
                "label": str(row.get("nomenclatura") or f"{calle_a} y {calle_b}"),
                "calle_a": calle_a,
                "calle_b": calle_b,
            }
            break
        if best:
            break
    _mem[token] = best
    if best:
        _write_json(f"georef:{token}", best)
    return None if best is None else dict(best)


def _from_georef(row: dict[str, Any], kind: str) -> dict[str, Any] | None:
    name = str(row.get("nombre") or "").strip()
    if not name:
        return None
    centro = row.get("centroide") or {}
    prov = row.get("provincia") or {}
    try:
        lat = float(centro["lat"]) if centro.get("lat") is not None else None
        lon = float(centro["lon"]) if centro.get("lon") is not None else None
    except (TypeError, ValueError):
        lat = lon = None
    return {
        "name": name,
        "kind": kind,
        "province": str(prov.get("nombre") or ""),
        "lat": lat,
        "lon": lon,
        "id": str(row.get("id") or ""),
    }


def _georef(path: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        with httpx.Client(headers=HEADERS, timeout=12.0) as client:
            response = client.get(GEOREF + path, params=params)
            response.raise_for_status()
            data = response.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _clean_name(raw: str | None) -> str:
    text = _CUT.split(raw or "", 1)[0]
    text = re.sub(r"\s+", " ", text).strip(" .,;:-")
    if len(text) < 3 or len(text) > 48:
        return ""
    return text


def _looks_province(name: str) -> bool:
    token = _fold(name)
    if not token:
        return False
    for row in provinces():
        if token == _fold(row.get("nombre") or "") or token == row.get("slug"):
            return True
    return False


def _hint_ok(place: dict[str, Any], hint: str) -> bool:
    return _same_province(_province_slug(str(place.get("province") or "")), _province_slug(hint))


def _same_province(a: str, b: str) -> bool:
    left, right = _fold(a).replace(" ", "-"), _fold(b).replace(" ", "-")
    if not left or not right:
        return False
    if left == right:
        return True
    caba = {"capital-federal", "caba", "ciudad-autonoma-de-buenos-aires", "buenos-aires"}
    return left in caba and right in caba


def _province_slug(name: str) -> str:
    token = _fold(name)
    if not token:
        return ""
    for row in provinces():
        slug = str(row.get("slug") or "")
        folded = _fold(row.get("nombre") or "")
        if token in {slug, folded} or token.replace(" ", "-") == slug:
            return slug
        if len(token) >= 5 and (token in folded or folded in token):
            return slug
    return token.replace(" ", "-")


def _read_cache(token: str) -> dict[str, Any] | None:
    if token in _mem:
        return _mem[token]
    raw = _read_json(f"georef:place:{token}")
    if isinstance(raw, dict) and raw.get("name"):
        _mem[token] = raw
        return raw
    if raw == {"missing": True}:
        _mem[token] = None
        return None
    return None


def _write_cache(token: str, place: dict[str, Any] | None) -> None:
    _mem[token] = place
    _write_json(f"georef:place:{token}", place or {"missing": True})


def _read_json(key: str):
    if os.environ.get("PROPMAP_TEST") == "1":
        return None
    try:
        from . import store

        raw = store.get_meta(key)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _write_json(key: str, value: Any) -> None:
    if os.environ.get("PROPMAP_TEST") == "1":
        return
    try:
        from . import store

        store.set_meta(key, json.dumps(value, ensure_ascii=False))
    except Exception:
        return


def _fold(text: str) -> str:
    from .geo import fold

    return fold(text)
