from __future__ import annotations

import logging
import os
import re
import threading
from typing import Callable

from lxml import html as lhtml

from ..geo import foreign_locality, locate, parse_street, pin_listing_city, portal_outside_city, street_names_match
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


_YES_VAL = {"si", "sí", "yes", "true", "1", "ok"}
_NO_VAL = {"no", "false", "0"}


def iter_feature_pairs(node: object):
    """Recorre mainFeatures / generalFeatures anidados de Navent y rinde (label, value)."""
    if isinstance(node, dict):
        feats = node.get("features")
        if isinstance(feats, list):
            for feat in feats:
                yield from iter_feature_pairs(feat)
            return
        label = str(node.get("label") or node.get("name") or "").strip()
        if label:
            yield label, node.get("value")
            return
        for child in node.values():
            if isinstance(child, (dict, list)):
                yield from iter_feature_pairs(child)
    elif isinstance(node, list):
        for child in node:
            yield from iter_feature_pairs(child)


def collect_feature_labels(*blobs: object) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    for blob in blobs:
        for label, value in iter_feature_pairs(blob):
            token = str(value or "").strip().lower()
            if token in _NO_VAL:
                continue
            key = label.lower()
            if key in seen:
                continue
            seen.add(key)
            labels.append(label)
    return labels


def structured_credit(*blobs: object) -> bool | None:
    found = None
    for blob in blobs:
        for label, value in iter_feature_pairs(blob):
            if not re.search(r"apto\s+cr[eé]dito|cr[eé]dito\s+hipotecario|apto\s+bancario", label, re.I):
                continue
            token = str(value or "").strip().lower()
            if token in _NO_VAL:
                return False
            found = True
    return found


def publisher_bits(raw: dict | None) -> dict:
    pub = (raw or {}).get("publisher") if isinstance(raw, dict) else None
    if not isinstance(pub, dict):
        return {}
    name = str(pub.get("name") or "").strip()
    url = str(pub.get("url") or pub.get("urlFriendly") or "").strip()
    kind = str(pub.get("publisherType") or pub.get("type") or pub.get("realEstateType") or "").strip()
    out: dict = {}
    if url:
        out["publisher_url"] = url
    if kind:
        out["publisher_kind"] = kind
    low = f"{name} {url} {kind}".lower()
    kind_l = kind.lower()
    if any(tok in kind_l for tok in ("particular", "owner", "dueño", "dueno")):
        out["publisher_direct"] = True
    elif any(tok in low for tok in ("inmobiliaria", "realestate", "agency", "martillero")):
        out["publisher_direct"] = False
    return out


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


_LOT_RE = re.compile(
    r"\b(terrenos?|lotes?|fracci[oó]n(?:es)?|hect[aá]reas?)\b|\bvecltrin\b",
    re.I,
)
_DWELLING_HEAD = re.compile(
    r"^\s*(?:venta\s+de\s+|vendo\s+)?"
    r"(?:casas?|chalet|quinta|departamento|depto|apartamento|monoambiente|ph|d[uú]plex|triplex)\b",
    re.I,
)


def detect_type(text: str, fallback: str) -> str:
    from ..property_kind import dwelling_on_lot, dwelling_type_from_text

    t = (text or "").lower()
    if "departamento" in t or "depto" in t or "apartamento" in t or "monoambiente" in t:
        return "departamento"
    if re.search(r"\bph\b", t) or "duplex" in t or "dúplex" in t or "triplex" in t:
        return "ph"
    built = dwelling_type_from_text(text)
    if built:
        return built
    lot = fallback == "terreno" or bool(_LOT_RE.search(t))
    if lot and not _DWELLING_HEAD.search((text or "").strip()) and not dwelling_on_lot(text):
        return "terreno"
    if "casa" in t or "chalet" in t or "quinta" in t or "multifamiliar" in t:
        return "casa"
    if lot:
        return "terreno"
    if re.search(r"\bgalp[oó]n\b|nave industrial", t):
        return "galpon"
    if re.search(r"\boficina", t):
        return "oficina"
    if re.search(r"local comercial|\blocal\b", t):
        return "local"
    return fallback


def page_workers() -> int:
    """Cuántas páginas de un mismo portal se piden a la vez (un carril Tor cada una)."""
    if os.environ.get("PROPMAP_TEST") == "1":
        return 1
    try:
        from ..egress import lane_count

        return max(1, min(4, lane_count()))
    except Exception:
        return 1


def paginate(fetch_page, max_pages: int | None = None, should_stop=None, on_chunk=None) -> list:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from .. import freshness
    from ..crawl import list_page_limit

    items = []
    seen: set[str] = set()
    limit = max_pages if max_pages is not None else list_page_limit()
    width = page_workers()
    page = 1
    while page <= limit:
        if should_stop and should_stop():
            break
        batch = list(range(page, min(limit, page + width - 1) + 1))
        fetched: dict[int, list] = {}
        if len(batch) == 1:
            fetched[batch[0]] = fetch_page(batch[0]) or []
        else:
            with ThreadPoolExecutor(max_workers=len(batch)) as pool:
                futs = {pool.submit(fetch_page, num): num for num in batch}
                for fut in as_completed(futs):
                    num = futs[fut]
                    try:
                        fetched[num] = fut.result() or []
                    except Exception:
                        fetched[num] = []
        stop = False
        for num in batch:
            chunk = fetched.get(num) or []
            if not chunk:
                stop = True
                break
            fresh = [x for x in chunk if x.source_id not in seen]
            if not fresh:
                stop = True
                break
            unknown = [x for x in fresh if not freshness.is_known(x.id)]
            for x in fresh:
                seen.add(x.source_id)
            items.extend(fresh)
            if on_chunk:
                on_chunk(fresh)
            freshness.note_ids(x.id for x in fresh)
            if len(chunk) < 8 or not unknown:
                stop = True
                break
        if stop:
            break
        page += len(batch)
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


_LEAD_SKIP = {
    "excelente", "monoambiente", "departamento", "depto", "casa", "ph",
    "terreno", "lote", "venta", "alquiler", "cubiertos", "cuotas", "dormitorios",
}


def _description_lead_address(text: str) -> tuple[str, int | None]:
    """Si la descripción arranca con 'CALLE 123:', esa es la dirección."""
    from ..geo import fold

    first = (text or "").strip().split("\n", 1)[0].strip()
    if not first:
        return "", None
    head = first.split(":", 1)[0].strip() if ":" in first else first
    if not head or len(head) > 40:
        return "", None
    street, number = parse_street(head)
    if not street or not number:
        return "", None
    words = fold(head).split()
    if any(word in _LEAD_SKIP for word in words):
        return "", None
    return street, number


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
    named, height = parse_street(item.address or "")
    if not (named and height):
        named, height = parse_street(place_blob)
    lead_street, lead_number = _description_lead_address(item.description or "")
    street = lead_street or named or found_place.get("street") or extra.get("street") or ""
    number = lead_number or height or found_place.get("number") or extra.get("street_number")
    from ..text_quality import is_plot_label, is_plot_street_name

    if street and number and not is_plot_street_name(str(street)) and not is_plot_label(str(street), str(number)):
        extra["street"] = street
        extra["street_number"] = number
        cur_s, cur_n = parse_street(item.address or "")
        lead_wins = bool(lead_street and lead_number)
        if lead_wins or (not (cur_s and cur_n) and not is_plot_label(item.address or "")):
            item.address = f"{street} {number}"
    elif is_plot_street_name(str(street or extra.get("street") or "")) or is_plot_label(
        item.address or "", str(street or extra.get("street") or ""), str(number or extra.get("street_number") or "")
    ):
        extra.pop("street", None)
        extra.pop("street_number", None)
    if found_all.get("corners"):
        extra.setdefault("intersection", " y ".join(found_all["corners"][0]))
    inter = str(extra.get("intersection") or "")
    if inter and " y " in inter.lower():
        from ..geo_tools import _useful

        left, right = re.split(r"\s+y\s+", inter, maxsplit=1, flags=re.I)
        if street_names_match(left, right):
            extra.pop("intersection", None)
        elif not (_useful(left) and _useful(right)):
            if not (re.fullmatch(r"\d{1,4}", left.strip()) and re.fullmatch(r"\d{1,4}", right.strip())):
                extra.pop("intersection", None)
    if found_all.get("between"):
        extra.setdefault("between", " y ".join(found_all["between"][0]))
    if looks_like_intersection(item.address or "") and not (street and number):
        extra.setdefault("intersection", (item.address or "").strip())
    item.extra = extra
    return extra


def _keep_portal_map_pin(item: Listing) -> None:
    extra = dict(item.extra or {})
    try:
        plat, plon = float(extra["portal_lat"]), float(extra["portal_lon"])
        lat, lon = float(item.lat), float(item.lon)
    except (KeyError, TypeError, ValueError):
        return
    from ..geo_tools import _usable_street_pin
    from ..geo import _mapped_water, distance_km

    home = str(item.city or extra.get("search_city") or "")
    if _mapped_water(lat, lon, home) and not _mapped_water(plat, plon, home):
        item.lat, item.lon = plat, plon
        extra["pin_kind"] = "saved"
        extra["location_kind"] = "approx"
        item.has_exact_location = False
        from ..geo import barrio_from_pin

        calc, zona = barrio_from_pin(plat, plon, home)
        if calc and calc != "Sin clasificar":
            item.barrio = calc
            if zona:
                item.zona = zona
        item.extra = extra
        return
    if portal_outside_city(item, home):
        item.lat, item.lon = plat, plon
        extra["pin_kind"] = "saved"
        if extra.get("portal_exact") and not extra.get("portal_approx"):
            extra["location_kind"] = "exact"
            item.has_exact_location = True
        else:
            extra["location_kind"] = "approx"
            item.has_exact_location = False
        item.extra = extra
        return
    if _usable_street_pin(str(extra.get("street") or ""), extra.get("street_number")):
        if not (
            extra.get("portal_exact")
            and not extra.get("portal_approx")
            and distance_km(lat, lon, plat, plon) > 1.5
        ):
            return
    if distance_km(lat, lon, plat, plon) < 0.25:
        return
    item.lat, item.lon = plat, plon
    extra["pin_kind"] = "saved"
    if extra.get("portal_exact") and not extra.get("portal_approx"):
        extra["location_kind"] = "exact"
        item.has_exact_location = True
    else:
        extra["location_kind"] = "approx"
        item.has_exact_location = False
    from ..geo import barrio_from_pin

    calc, zona = barrio_from_pin(plat, plon, home)
    if calc and calc != "Sin clasificar":
        item.barrio = calc
        if zona:
            item.zona = zona
    item.extra = extra


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
    from ..geo import apply_recovered_location, barrio_from_pin, default_city

    apply_recovered_location(item)
    extra = attach_location_facts(item)
    search_city = item.city or default_city()
    hint = None
    if item.lat is not None and item.lon is not None:
        calc, _zona = barrio_from_pin(item.lat, item.lon, search_city)
        if calc != "Sin clasificar":
            hint = calc
    extracted: dict = {}
    from ..geo_tools import _usable_street_pin

    if extra.get("street") and extra.get("street_number") and _usable_street_pin(str(extra.get("street") or ""), extra.get("street_number")):
        extracted["street"] = extra["street"]
        extracted["number"] = extra["street_number"]
    inter = str(extra.get("intersection") or "")
    if " y " in inter.lower():
        from ..geo_tools import _useful

        left, right = re.split(r"\s+y\s+", inter, maxsplit=1, flags=re.I)
        if _useful(left) and _useful(right) and not street_names_match(left, right):
            extracted["corner_a"] = left
            extracted["corner_b"] = right
    between = str(extra.get("between") or "")
    if between:
        extracted["between"] = between
    if extra.get("portal_exact"):
        extracted["portal_exact"] = True
    if extra.get("portal_approx"):
        extracted["portal_approx"] = True
    if extra.get("portal_lat") is not None and extra.get("portal_lon") is not None:
        extracted["portal_lat"] = extra["portal_lat"]
        extracted["portal_lon"] = extra["portal_lon"]
    barrio, zona, lat, lon, exact, pin_kind = locate(
        item.id,
        item.lat,
        item.lon,
        item.title,
        item.address,
        item.description,
        item.publisher,
        extra.get("intersection") or "",
        extra.get("between") or "",
        city=search_city,
        barrio_hint=hint or None,
        allow_approx=not foreign_locality(item, search_city),
        extracted=extracted or None,
    )
    had_street = bool(
        extra.get("street")
        and extra.get("street_number")
        and _usable_street_pin(str(extra.get("street") or ""), extra.get("street_number"))
    )
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
    had_street = bool(
        extra.get("street")
        and extra.get("street_number")
        and _usable_street_pin(str(extra.get("street") or ""), extra.get("street_number"))
    )
    if had_street:
        exact = True
        extra["location_kind"] = "exact"
        pin_kind = "address"
    elif pin_kind == "address":
        if extra.get("portal_exact") and not extra.get("portal_approx"):
            extra["location_kind"] = "exact"
            pin_kind = "saved"
            exact = True
        else:
            exact = False
            extra["location_kind"] = "approx"
    elif pin_kind == "intersection":
        exact = True
        extra["location_kind"] = "intersection"
        if not extra.get("intersection") and extra.get("between"):
            extra["intersection"] = extra["between"]
    elif extra.get("portal_approx"):
        exact = False
        extra["location_kind"] = "approx"
    elif exact:
        extra["location_kind"] = "exact"
    elif extra.get("intersection") and not had_street:
        extra["location_kind"] = "intersection"
    elif extra.get("between") and not had_street:
        extra["location_kind"] = "intersection"
        extra.setdefault("intersection", extra["between"])
    elif lat is not None:
        extra["location_kind"] = "approx"
    else:
        extra["location_kind"] = "unknown"
    extra["pin_kind"] = pin_kind
    item.extra = extra
    item.has_exact_location = exact
    _keep_portal_map_pin(item)
    pin_listing_city(item, remote=False)
    apply_recovered_location(item)
    _keep_portal_map_pin(item)
    from ..geo import drop_water_pin

    drop_water_pin(item)
    if item.barrio and item.barrio != "Sin clasificar":
        from ..geo import remember_barrio

        remember_barrio(item.city, item.barrio, item.lat, item.lon)
    return item


log = logging.getLogger(__name__)
_repair_lock = threading.Lock()
_repair_inflight: set[str] = set()


def repair_far_portal_pins(city_id: str) -> int:
    """Si el geocode quedó a kilómetros del pin EXACT del portal, reubica."""
    if not city_id or city_id in {"fuera", "otros"} or os.environ.get("PROPMAP_TEST") == "1":
        return 0
    from .. import store
    from ..geo import distance_km, same_place_ids

    store.init()
    wanted = [cid for cid in same_place_ids(city_id) if cid] or [city_id]
    items = store.fetch_by_cities(wanted)
    dirty: list[Listing] = []
    updated = 0
    for item in items:
        extra = item.extra or {}
        if not extra.get("portal_exact") or extra.get("portal_approx"):
            continue
        try:
            plat, plon = float(extra["portal_lat"]), float(extra["portal_lon"])
            lat, lon = float(item.lat), float(item.lon)
        except (KeyError, TypeError, ValueError):
            continue
        if distance_km(lat, lon, plat, plon) <= 1.5:
            continue
        attach_location_facts(item)
        extra = dict(item.extra or {})
        item.lat, item.lon = plat, plon
        extra["location_kind"] = "exact"
        extra["pin_kind"] = "saved"
        item.has_exact_location = True
        from ..geo import barrio_from_pin

        calc, zona = barrio_from_pin(plat, plon, city_id)
        if calc and calc != "Sin clasificar":
            item.barrio = calc
            if zona:
                item.zona = zona
        item.extra = extra
        dirty.append(item)
        if len(dirty) >= 40:
            store.update_scores(dirty)
            updated += len(dirty)
            dirty = []
    if dirty:
        store.update_scores(dirty)
        updated += len(dirty)
    return updated


def repair_water_pins(city_id: str) -> int:
    """Saca pines que quedaron en el río y los vuelve al pin del portal en tierra."""
    if not city_id or city_id in {"fuera", "otros"} or os.environ.get("PROPMAP_TEST") == "1":
        return 0
    from .. import store
    from ..geo import drop_water_pin, in_water, same_place_ids
    from ..places import ensure_osm_water

    store.init()
    rings = ensure_osm_water(city_id, blocking=True)
    if not rings:
        return 0
    wanted = [cid for cid in same_place_ids(city_id) if cid] or [city_id]
    items = store.fetch_by_cities(wanted)
    dirty: list[Listing] = []
    updated = 0
    for item in items:
        try:
            lat, lon = float(item.lat), float(item.lon)
        except (TypeError, ValueError):
            continue
        if not in_water(lat, lon, city_id):
            continue
        attach_location_facts(item)
        drop_water_pin(item)
        dirty.append(item)
        if len(dirty) >= 40:
            store.update_scores(dirty)
            updated += len(dirty)
            dirty = []
    if dirty:
        store.update_scores(dirty)
        updated += len(dirty)
    return updated


def kick_repair_far_pins(city_id: str | None) -> None:
    cid = (city_id or "").strip()
    if not cid or cid in {"fuera", "otros"} or os.environ.get("PROPMAP_TEST") == "1":
        return
    with _repair_lock:
        if cid in _repair_inflight:
            return
        _repair_inflight.add(cid)

    def _job() -> None:
        try:
            n = repair_far_portal_pins(cid)
            if n:
                log.info("repair far pins %s n=%s", cid, n)
            w = repair_water_pins(cid)
            if w:
                log.info("repair water pins %s n=%s", cid, w)
        except Exception:
            log.exception("repair far pins %s", cid)
        finally:
            with _repair_lock:
                _repair_inflight.discard(cid)

    threading.Thread(target=_job, daemon=True, name=f"repin-{cid}").start()


first_int = first_int
detect_type = detect_type
parse_number = parse_number
locate_item = locate_item
