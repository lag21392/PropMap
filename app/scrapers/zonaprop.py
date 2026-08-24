from __future__ import annotations

from ..http_client import decode_js_object, fetch_text
from ..models import Listing
from . import detect_type as detect_type
from . import locate_item, paginate, parse_number
from .urls import zonaprop_paths

BASE = "https://www.zonaprop.com.ar"
CITY_SEARCHES = {
    "puerto-madryn": [
        ("casas-venta-puerto-madryn", "casa"),
        ("departamentos-venta-puerto-madryn", "departamento"),
        ("ph-venta-puerto-madryn", "ph"),
        ("terrenos-venta-puerto-madryn", "terreno"),
        ("inmuebles-venta-el-doradillo", ""),
        ("terrenos-venta-el-doradillo", "terreno"),
        ("casas-venta-el-doradillo", "casa"),
        ("inmuebles-venta-punta-cuevas", ""),
        ("terrenos-venta-punta-cuevas", "terreno"),
        ("inmuebles-venta-playa-parana", ""),
        ("casas-venta-playa-parana", "casa"),
        ("terrenos-venta-cerro-avanzado", "terreno"),
        ("inmuebles-venta-cerro-avanzado", ""),
        ("inmuebles-venta-puerto-madryn", ""),
    ],
    "trelew": [
        ("casas-venta-trelew", "casa"),
        ("departamentos-venta-trelew", "departamento"),
        ("ph-venta-trelew", "ph"),
        ("terrenos-venta-trelew", "terreno"),
        ("inmuebles-venta-trelew", ""),
    ],
    "rawson": [
        ("casas-venta-rawson", "casa"),
        ("departamentos-venta-rawson", "departamento"),
        ("ph-venta-rawson", "ph"),
        ("terrenos-venta-rawson", "terreno"),
        ("casas-venta-playa-union", "casa"),
        ("terrenos-venta-playa-union", "terreno"),
        ("inmuebles-venta-rawson", ""),
    ],
    "gaiman": [
        ("casas-venta-gaiman", "casa"),
        ("departamentos-venta-gaiman", "departamento"),
        ("ph-venta-gaiman", "ph"),
        ("terrenos-venta-gaiman", "terreno"),
        ("inmuebles-venta-gaiman", ""),
    ],
    "playa-union": [
        ("casas-venta-playa-union", "casa"),
        ("departamentos-venta-playa-union", "departamento"),
        ("ph-venta-playa-union", "ph"),
        ("terrenos-venta-playa-union", "terreno"),
        ("inmuebles-venta-playa-union", ""),
    ],
    "microcentro-caba": [
        ("departamentos-venta-microcentro", "departamento"),
        ("departamentos-venta-san-nicolas", "departamento"),
        ("departamentos-venta-monserrat", "departamento"),
        ("departamentos-venta-retiro", "departamento"),
        ("ph-venta-san-nicolas", "ph"),
        ("ph-venta-microcentro", "ph"),
        ("terrenos-venta-microcentro", "terreno"),
        ("terrenos-venta-san-nicolas", "terreno"),
    ],
}


def scrape(progress=lambda _m: None, city: str = "puerto-madryn", should_stop=None, on_chunk=None) -> list[Listing]:
    listings: list[Listing] = []
    for slug, ptype in zonaprop_paths(city, CITY_SEARCHES):
        if should_stop and should_stop():
            break
        progress(f"ZonaProp · {city} · {ptype or 'inmuebles'}")
        try:
            listings.extend(
                paginate(
                    lambda page, s=slug, t=ptype, c=city: _page(s, t, page, c),
                    should_stop=should_stop,
                    on_chunk=on_chunk,
                )
            )
        except Exception as exc:
            progress(f"ZonaProp {city} {ptype}: {exc}")
    return listings


def _page(slug: str, fallback_type: str, page: int, city: str) -> list[Listing]:
    url = f"{BASE}/{slug}.html" if page == 1 else f"{BASE}/{slug}-pagina-{page}.html"
    html = fetch_text(url)
    state = decode_js_object(html, "window.__PRELOADED_STATE__") or {}
    postings = (
        (state.get("listStore") or {}).get("listPostings")
        or (state.get("listStore") or {}).get("listPostingsMap")
        or []
    )
    if isinstance(postings, dict):
        postings = list(postings.values())
    items: list[Listing] = []
    for raw in postings:
        if not isinstance(raw, dict):
            continue
        parsed = _parse(raw, fallback_type, city)
        if parsed:
            items.append(locate_item(parsed))
    return items


def _feat(raw: dict, code: str) -> float | None:
    features = raw.get("mainFeatures") or {}
    node = features.get(code) if isinstance(features, dict) else None
    if not isinstance(node, dict):
        return None
    return parse_number(str(node.get("value") or ""))


def _parse(raw: dict, fallback_type: str, city: str) -> Listing | None:
    source_id = str(raw.get("postingId") or raw.get("postingCode") or "")
    url = raw.get("url") or ""
    if not source_id or not url:
        return None
    prices = []
    for op in raw.get("priceOperationTypes") or []:
        prices.extend(op.get("prices") or [])
    price_node = prices[0] if prices else {}
    currency = str(price_node.get("currency") or "USD").replace("U$S", "USD")
    if currency in {"$", "ARS"}:
        currency = "ARS"
    if "USD" in currency.upper() or currency in {"U$S", "US$"}:
        currency = "USD"
    loc = raw.get("postingLocation") or {}
    geo = ((loc.get("postingGeolocation") or {}).get("geolocation") or {})
    address = (loc.get("address") or {}).get("name") or ""
    pictures = ((raw.get("visiblePictures") or {}).get("pictures") or [])
    image = ""
    if pictures:
        image = pictures[0].get("url360x266") or pictures[0].get("url730x532") or ""
    publisher = ((raw.get("publisher") or {}).get("name") or "").strip()
    title = raw.get("title") or raw.get("generatedTitle") or "Propiedad en venta"
    text = " ".join(
        [
            title,
            raw.get("generatedTitle") or "",
            address,
            raw.get("descriptionNormalized") or "",
            url,
        ]
    )
    ptype = detect_type(text, fallback_type or "")
    allowed = {"casa", "departamento", "ph", "terreno"}
    if ptype not in allowed:
        if fallback_type in allowed:
            ptype = fallback_type
        else:
            return None
    amenities = []
    features = raw.get("mainFeatures") or {}
    if isinstance(features, dict):
        for node in features.values():
            if isinstance(node, dict):
                label = str(node.get("label") or node.get("value") or "").strip()
                if label:
                    amenities.append(label)
    expenses = raw.get("expenses") or {}
    extra = {"amenities": amenities}
    amount = parse_number(str(expenses.get("amount") or expenses.get("formattedAmount") or ""))
    if amount:
        extra["expenses"] = amount
    return Listing(
        source="zonaprop",
        source_id=source_id,
        url=url if url.startswith("http") else BASE + url,
        title=title,
        property_type=ptype,
        price=parse_number(str(price_node.get("amount") or "")),
        currency=currency,
        address=address,
        lat=_safe_float(geo.get("latitude")),
        lon=_safe_float(geo.get("longitude")),
        covered_m2=_feat(raw, "CFT101"),
        total_m2=_feat(raw, "CFT100"),
        rooms=_as_int(_feat(raw, "CFT1")),
        bedrooms=_as_int(_feat(raw, "CFT2")),
        bathrooms=_feat(raw, "CFT3"),
        parking=_as_int(_feat(raw, "CFT7")),
        age_years=_as_int(_feat(raw, "CFT5")),
        image=image,
        publisher=publisher,
        description=(raw.get("descriptionNormalized") or "")[:900],
        published_at=str(raw.get("modified_date") or ""),
        city=city,
        extra=extra,
    )


def _safe_float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if abs(number) > 1 else None


def _as_int(value: float | None) -> int | None:
    if value is None:
        return None
    return int(value)
