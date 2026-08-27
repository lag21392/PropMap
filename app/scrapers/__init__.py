from __future__ import annotations

import re
from typing import Callable

from lxml import html as lhtml

from ..geo import foreign_locality, locate, parse_street, pin_listing_city
from ..http_client import fetch_text
from ..models import Listing
from ..text_quality import looks_like_intersection

Progress = Callable[[str], None]


def parse_number(text: str | None) -> float | None:
    if not text:
        return None
    raw = str(text)
    raw = raw.replace("\xa0", " ").replace("U$S", "").replace("US$", "").replace("USD", "")
    raw = raw.replace("$", "")
    match = re.search(r"(\d[\d\.\s]*\d|\d)", raw)
    if not match:
        return None
    token = match.group(1).replace(" ", "")
    if token.count(".") > 1:
        token = token.replace(".", "")
    elif token.count(",") == 1 and token.count(".") == 0:
        token = token.replace(",", ".")
    elif "," in token and "." in token:
        token = token.replace(".", "").replace(",", ".")
    else:
        token = token.replace(".", "")
    try:
        value = float(token)
    except ValueError:
        return None
    return value if value > 0 else None


def first_int(text: str, pattern: str) -> int | None:
    match = re.search(pattern, text, re.I)
    if not match:
        return None
    try:
        return int(float(match.group(1).replace(",", ".")))
    except ValueError:
        return None


def first_float(text: str, pattern: str) -> float | None:
    match = re.search(pattern, text, re.I)
    if not match:
        return None
    try:
        return float(match.group(1).replace(".", "").replace(",", ".")) if match.group(1).count(".") > 1 else float(match.group(1).replace(",", "."))
    except ValueError:
        return None


def detect_type(text: str, fallback: str) -> str:
    t = (text or "").lower()
    if "departamento" in t or "depto" in t or "apartamento" in t or "monoambiente" in t:
        return "departamento"
    if re.search(r"\bph\b", t) or "duplex" in t or "dúplex" in t or "triplex" in t:
        return "ph"
    if "casa" in t or "chalet" in t or "quinta" in t or "multifamiliar" in t:
        return "casa"
    if fallback == "terreno" or "terreno" in t or re.search(r"\blote\b", t) or "hectarea" in t or "hectárea" in t:
        return "terreno"
    if re.search(r"\bgalp[oó]n\b|nave industrial", t):
        return "galpon"
    if re.search(r"\boficina", t):
        return "oficina"
    if re.search(r"local comercial|\blocal\b", t):
        return "local"
    return fallback


def paginate(fetch_page, max_pages: int | None = None, should_stop=None, on_chunk=None) -> list:
    from .. import freshness
    from ..crawl import list_page_limit

    items = []
    seen: set[str] = set()
    limit = max_pages if max_pages is not None else list_page_limit()
    for page in range(1, limit + 1):
        if should_stop and should_stop():
            break
        chunk = fetch_page(page)
        if not chunk:
            break
        fresh = [x for x in chunk if x.source_id not in seen]
        if not fresh:
            break
        unknown = [x for x in fresh if not freshness.is_known(x.id)]
        for x in fresh:
            seen.add(x.source_id)
        items.extend(fresh)
        if on_chunk:
            on_chunk(fresh)
        freshness.note_ids(x.id for x in fresh)
        if len(chunk) < 8:
            break
        if not unknown:
            break
    return items


def tree(url: str):
    return lhtml.fromstring(fetch_text(url))


def portal_neighborhood(*nodes) -> str:
    for node in nodes:
        if isinstance(node, dict):
            name = str(node.get("name") or node.get("label") or "").strip()
            if name:
                return name
        elif node:
            name = str(node).strip()
            if name:
                return name
    return ""


def attach_location_facts(item: Listing) -> dict:
    """Guarda calle+altura, esquina y 'entre' si el aviso las trae; no se pisan entre sí."""
    extra = dict(item.extra or {})
    place_blob = " ".join(p for p in (item.title, item.address) if p)
    full_blob = " ".join(
        p
        for p in (
            item.title,
            item.address,
            item.description,
            extra.get("intersection"),
            extra.get("between"),
        )
        if p
    )
    from ..geo_tools import parse_plain_locations

    found_place = parse_plain_locations(place_blob)
    found_all = parse_plain_locations(full_blob)
    named, height = parse_street(place_blob)
    street = named or found_place.get("street") or extra.get("street") or ""
    number = height or found_place.get("number") or extra.get("street_number")
    if street and number:
        extra["street"] = street
        extra["street_number"] = number
        cur_s, cur_n = parse_street(item.address or "")
        if not (cur_s and cur_n):
            item.address = f"{street} {number}"
    if found_all.get("corners"):
        extra.setdefault("intersection", " y ".join(found_all["corners"][0]))
    if found_all.get("between"):
        extra.setdefault("between", " y ".join(found_all["between"][0]))
    if looks_like_intersection(item.address or "") and not (street and number):
        extra.setdefault("intersection", (item.address or "").strip())
    item.extra = extra
    return extra


def locate_item(item: Listing) -> Listing:
    try:
        return _locate_item(item)
    except Exception:
        extra = dict(item.extra or {})
        extra.setdefault("location_kind", "unknown")
        item.extra = extra
        return item


def _locate_item(item: Listing) -> Listing:
    if item.price is not None and item.price <= 200:
        item.price = None
    hint = portal_neighborhood((item.extra or {}).get("barrio"))
    if item.barrio and item.barrio != "Sin clasificar":
        hint = hint or item.barrio
    from ..geo import default_city

    extra = attach_location_facts(item)
    search_city = item.city or default_city()
    extracted: dict = {}
    if extra.get("street") and extra.get("street_number"):
        extracted["street"] = extra["street"]
        extracted["number"] = extra["street_number"]
    inter = str(extra.get("intersection") or "")
    if " y " in inter.lower():
        left, right = re.split(r"\s+y\s+", inter, maxsplit=1, flags=re.I)
        extracted["corner_a"] = left
        extracted["corner_b"] = right
    if extra.get("portal_exact"):
        extracted["portal_exact"] = True
    barrio, zona, lat, lon, exact, pin_kind = locate(
        item.id,
        item.lat,
        item.lon,
        item.title,
        item.address,
        item.description,
        item.publisher,
        extra.get("intersection") or "",
        city=search_city,
        barrio_hint=hint or None,
        allow_approx=not foreign_locality(item, search_city),
        extracted=extracted or None,
    )
    had_street = bool(extra.get("street") and extra.get("street_number"))
    if (item.source or "").lower() == "properati":
        if not had_street and pin_kind != "intersection":
            exact = False
            if pin_kind == "address":
                pin_kind = "saved"
    if extra.get("portal_approx") and pin_kind not in {"address", "intersection"}:
        exact = False
        if pin_kind == "address":
            pin_kind = "saved"
    item.barrio, item.zona, item.lat, item.lon = barrio, zona, lat, lon
    extra = attach_location_facts(item)
    had_street = bool(extra.get("street") and extra.get("street_number"))
    if pin_kind == "address":
        exact = True
        extra["location_kind"] = "exact"
    elif pin_kind == "intersection":
        exact = True
        extra["location_kind"] = "intersection"
    elif exact:
        extra["location_kind"] = "exact"
    elif extra.get("intersection") and not had_street:
        extra["location_kind"] = "intersection"
    elif lat is not None:
        extra["location_kind"] = "approx"
    else:
        extra["location_kind"] = "unknown"
    extra["pin_kind"] = pin_kind
    item.extra = extra
    item.has_exact_location = exact
    if item.barrio and item.barrio != "Sin clasificar":
        from ..geo import remember_barrio

        remember_barrio(item.city, item.barrio, item.lat, item.lon)
    pin_listing_city(item)
    return item


first_int = first_int
detect_type = detect_type
parse_number = parse_number
locate_item = locate_item
