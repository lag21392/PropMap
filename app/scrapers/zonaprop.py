from __future__ import annotations

from ..http_client import decode_js_object, fetch_text
from ..models import Listing
from . import detect_type as detect_type
from . import locate_item, paginate, parse_number, portal_neighborhood
from .urls import zonaprop_paths

BASE = "https://www.zonaprop.com.ar"


def scrape(progress=lambda _m: None, city: str | None = None, should_stop=None, on_chunk=None) -> list[Listing]:
    from ..geo import default_city

    city = city or default_city()
    listings: list[Listing] = []
    for slug, ptype in zonaprop_paths(city):
        if should_stop and should_stop():
            break
        progress(f"Revisando {ptype or 'inmuebles'}…")
        try:
            listings.extend(
                paginate(
                    lambda page, s=slug, t=ptype, c=city: _page(s, t, page, c),
                    should_stop=should_stop,
                    on_chunk=on_chunk,
                )
            )
        except Exception:
            progress(f"Un lote no respondió, sigo…")
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
    addr_node = loc.get("address") or {}
    if isinstance(addr_node, dict):
        address = addr_node.get("name") or ""
        visibility = str(addr_node.get("visibility") or "").strip().lower()
    else:
        address = str(addr_node or "")
        visibility = ""
    neighborhood = portal_neighborhood(loc.get("neighborhood"), loc.get("barrio"), loc.get("zone"))
    pictures = ((raw.get("visiblePictures") or {}).get("pictures") or [])
    image = ""
    photo_urls = []
    for pic in pictures:
        if not isinstance(pic, dict):
            continue
        href = pic.get("url730x532") or pic.get("url360x266") or pic.get("url") or ""
        if href:
            photo_urls.append(href)
    if photo_urls:
        image = photo_urls[0]
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
    if photo_urls:
        extra["photos"] = photo_urls[:24]
    if neighborhood:
        extra["barrio"] = neighborhood
        extra["portal_barrio"] = neighborhood
    if visibility:
        extra["map_visibility"] = visibility
        if visibility in {"exact", "accurate"}:
            extra["portal_exact"] = True
            extra["portal_approx"] = False
        else:
            extra["portal_exact"] = False
            extra["portal_approx"] = True
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
