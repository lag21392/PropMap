from __future__ import annotations

import re
from typing import Any

from .geo import CITIES, fold, offset_by_number, parse_street, street_names_match

_SKIP = {
    "venta", "alquiler", "departamento", "depto", "casa", "terreno", "ph",
    "ambiente", "ambientes", "dormitorio", "dormitorios", "cochera", "garage",
    "lindo", "excelente", "oportunidad", "cocina", "comedor", "living",
    "patio", "jardin", "jardín", "balcon", "balcón", "estacionamiento",
    "whatsapp", "calidad", "dimensiones", "amplio", "unico", "único",
}
CORNER_RE = re.compile(
    r"(?:esquina(?:\s+de)?|esq\.?)\s+"
    r"(?:calle\s+|av(?:enida|\.)?\s+|pasaje\s+)?"
    r"([a-záéíóúüñ0-9.]{2,}(?:\s+[a-záéíóúüñ0-9.]{2,}){0,4})"
    r"\s+y\s+"
    r"(?:calle\s+|av(?:enida|\.)?\s+|pasaje\s+)?"
    r"([a-záéíóúüñ0-9.]{2,}(?:\s+[a-záéíóúüñ0-9.]{2,}){0,4})",
    re.I,
)
AND_STREET_RE = re.compile(
    r"(?:(?P<pre>av(?:enida|\.)?|calle|pasaje)\s+)"
    r"(?P<a>[a-záéíóúüñ0-9.]{3,}(?:\s+[a-záéíóúüñ0-9.]{2,}){0,5})"
    r"\s+y\s+"
    r"(?:av(?:enida|\.)?\s+|calle\s+|pasaje\s+)?"
    r"(?P<b>[a-záéíóúüñ0-9.]{2,}(?:\s+[a-záéíóúüñ0-9.]{2,}){0,3})"
    r"(?=(?:\s*\([^)]+\)|\s*,|\s*$))",
    re.I,
)
PAREN_CORNER_RE = re.compile(
    r"(?P<a>[a-záéíóúüñ0-9.]{3,}(?:\s+[a-záéíóúüñ0-9.]{2,}){0,5})"
    r"\s+y\s+"
    r"(?P<b>[a-záéíóúüñ0-9.]{2,}(?:\s+[a-záéíóúüñ0-9.]{2,}){0,3})"
    r"\s*\([^)]{3,60}\)",
    re.I,
)
BETWEEN_RE = re.compile(
    r"entre\s+(?:calle\s+|av(?:enida|\.)?\s+)?"
    r"([a-záéíóúüñ0-9.]{2,}(?:\s+[a-záéíóúüñ0-9.]{2,}){0,3})"
    r"\s+y\s+"
    r"(?:calle\s+|av(?:enida|\.)?\s+)?"
    r"([a-záéíóúüñ0-9.]{2,}(?:\s+[a-záéíóúüñ0-9.]{2,}){0,3})",
    re.I,
)


def _clean_street(name: str) -> str:
    token = fold(name)
    token = re.sub(r"\b(calle|av\.?|avenida|y|la|el|los|las)\b", " ", token)
    token = re.sub(r"[^a-z0-9áéíóúüñ ]+", " ", token)
    token = re.sub(r"\s+", " ", token).strip()
    return token


def _useful(name: str) -> bool:
    token = _clean_street(name)
    if len(token) < 3:
        return False
    return not any(word in _SKIP for word in token.split())


def parse_plain_locations(text: str) -> dict[str, Any]:
    blob = text or ""
    street, number = parse_street(blob)
    corners: list[tuple[str, str]] = []
    for match in CORNER_RE.finditer(blob):
        left, right = _clean_street(match.group(1)), _clean_street(match.group(2))
        if _useful(left) and _useful(right):
            corners.append((left, right))
    for match in (*AND_STREET_RE.finditer(blob), *PAREN_CORNER_RE.finditer(blob)):
        left, right = _clean_street(match.group("a")), _clean_street(match.group("b"))
        if not (_useful(left) and _useful(right)):
            continue
        if (left, right) not in corners and (right, left) not in corners:
            corners.append((left, right))
    betweens: list[tuple[str, str]] = []
    for match in BETWEEN_RE.finditer(blob):
        left, right = _clean_street(match.group(1)), _clean_street(match.group(2))
        if _useful(left) and _useful(right):
            betweens.append((left, right))
    return {
        "street": street or "",
        "number": number,
        "corners": corners,
        "between": betweens,
    }


def validate_street(name: str, city: str = "") -> dict[str, Any]:
    token = _clean_street(name)
    if not _useful(token):
        return {"ok": False, "reason": "calle invalida"}
    from .places import geocode_local

    hits = geocode_local(f"calle {token}", city)
    if not hits:
        return {"ok": False, "street": token, "reason": "no encontrada"}
    best = hits[0]
    return {"ok": bool(best.get("ok")), "street": token, **best}


def validate_address(street: str, number: int | str, city: str = "") -> dict[str, Any]:
    token = _clean_street(street)
    try:
        num = int(str(number).strip())
    except (TypeError, ValueError):
        num = 0
    if not _useful(token) or num <= 0:
        return {"ok": False, "reason": "direccion incompleta"}
    from .place_api import geocode_direccion

    official = geocode_direccion(token, num, city)
    if official and official.get("lat") is not None and official.get("lon") is not None:
        return {
            "ok": True,
            "approx": False,
            "street": token,
            "number": num,
            "lat": official["lat"],
            "lon": official["lon"],
            "label": official.get("label") or f"{token} {num}",
        }
    from .places import geocode_local

    hits = geocode_local(f"{token} {num}", city)
    if not hits:
        street_hit = validate_street(token, city)
        if street_hit.get("ok") and street_hit.get("lat") is not None:
            lat, lon = offset_by_number(float(street_hit["lat"]), float(street_hit["lon"]), num)
            return {
                "ok": True,
                "approx": True,
                "street": token,
                "number": num,
                "lat": lat,
                "lon": lon,
                "label": f"{token} {num}",
            }
        return {"ok": False, "street": token, "number": num, "reason": "no encontrada"}
    best = hits[0]
    return {"ok": bool(best.get("ok")), "street": token, "number": num, "approx": False, **best}


def validate_corner(calle_a: str, calle_b: str, city: str = "") -> dict[str, Any]:
    left, right = _clean_street(calle_a), _clean_street(calle_b)
    if not _useful(left) or not _useful(right):
        return {"ok": False, "reason": "esquina invalida"}
    if street_names_match(left, right):
        return {"ok": False, "reason": "misma calle"}
    from .place_api import geocode_interseccion

    official = geocode_interseccion(left, right, city)
    if official and official.get("lat") is not None and official.get("lon") is not None:
        return {
            "ok": True,
            "approx": False,
            "calle_a": left,
            "calle_b": right,
            "lat": official["lat"],
            "lon": official["lon"],
            "label": official.get("label") or f"{left} y {right}",
        }
    from .places import geocode_local

    queries = [
        f"{left} y {right}",
        f"esquina {left} y {right}",
        f"{left} & {right}",
    ]
    for query in queries:
        hits = geocode_local(query, city)
        if hits and hits[0].get("ok"):
            return {
                "ok": True,
                "calle_a": left,
                "calle_b": right,
                **hits[0],
            }
    return {"ok": False, "calle_a": left, "calle_b": right, "reason": "esquina no encontrada"}


def city_label(city: str) -> str:
    cfg = CITIES.get(city) or {}
    return str(cfg.get("label") or city or "")


def run_location_tools(text: str, city: str, extracted: dict[str, Any] | None = None) -> dict[str, Any]:
    found = parse_plain_locations(text)
    extra = extracted or {}
    street = _clean_street(str(extra.get("street") or found.get("street") or ""))
    number = extra.get("street_number") or extra.get("number") or found.get("number")
    corner_a = _clean_street(str(extra.get("corner_a") or extra.get("calle_a") or ""))
    corner_b = _clean_street(str(extra.get("corner_b") or extra.get("calle_b") or ""))
    if not corner_a and found["corners"]:
        corner_a, corner_b = found["corners"][0]
    checked: list[dict[str, Any]] = []
    geo = None
    if street and number:
        hit = validate_address(street, number, city)
        checked.append({"tool": "validar_direccion", **hit})
        if hit.get("ok"):
            geo = {**hit, "pin_kind": "address"}
    if geo is None and corner_a and corner_b:
        hit = validate_corner(corner_a, corner_b, city)
        checked.append({"tool": "validar_esquina", **hit})
        if hit.get("ok"):
            geo = {**hit, "pin_kind": "intersection"}
    return {"found": found, "checked": checked, "geo": geo}
