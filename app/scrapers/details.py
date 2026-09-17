from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone
from html import unescape
from io import BytesIO
from urllib.parse import unquote, urljoin

from ..features import analyze, extract_features
from ..http_client import decode_js_object, fetch_bytes, fetch_text
from ..models import Listing
from ..text_quality import address_quality, clean_portal_address, looks_like_intersection, title_quality
from . import attach_location_facts, detect_type, parse_number

DETAILS_PARSER = "9"
PDF_HREF = re.compile(r'href=["\']([^"\']+\.pdf[^"\']*)["\']', re.I)
ZP_MAP_LAT = re.compile(
    r"(?:const|let|var)\s+mapLatOf\s*=\s*[\"']([A-Za-z0-9+/=]+)[\"']",
    re.I,
)
ZP_MAP_LNG = re.compile(
    r"(?:const|let|var)\s+mapLngOf\s*=\s*[\"']([A-Za-z0-9+/=]+)[\"']",
    re.I,
)
ZP_ADDR_VIS = re.compile(
    r"""['"]address['"]\s*:\s*\{\s*['"]name['"]\s*:\s*['"]([^'"]+)['"]"""
    r"""\s*,\s*['"]visibility['"]\s*:\s*['"](\w+)['"]""",
    re.I,
)
JSON_LD = re.compile(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.I | re.S)
ML_COORDS = re.compile(
    r'"latitude"\s*:\s*"(-?\d+\.\d+)"\s*,\s*"longitude"\s*:\s*"(-?\d+\.\d+)"',
    re.I,
)
ML_COORDS_SWAP = re.compile(
    r'"longitude"\s*:\s*"(-?\d+\.\d+)"\s*,\s*"latitude"\s*:\s*"(-?\d+\.\d+)"',
    re.I,
)
MAP_CENTER = re.compile(r"[?&]center=(-?\d+\.\d+)(?:%2C|,)(-?\d+\.\d+)", re.I)
MAP_MARKERS = re.compile(
    r"[?&]markers(?:[^=]*=)*(-?\d+\.\d+)(?:%2C|,)(-?\d+\.\d+)",
    re.I,
)
GMAPS_AT = re.compile(r"maps\.[^/\"']+/[^\"']*@(-?\d+\.\d+),(-?\d+\.\d+)", re.I)
GMAPS_2D3D = re.compile(r"!2d(-?\d+\.\d+)!3d(-?\d+\.\d+)")
GENERIC_LATLON = re.compile(
    r'data-(?:lat|latitude)=["\'](-?\d+\.\d+)["\'][^>]{0,80}data-(?:lng|lon|longitude)=["\'](-?\d+\.\d+)["\']',
    re.I,
)
PROPERATI_DESC = re.compile(
    r'id=["\']description-text["\'][^>]*>([\s\S]*?)</div>',
    re.I,
)
PROPERATI_MAP = re.compile(
    r"mapData:\s*\{[\s\S]{0,900}?latitude:\s*[\"'](-?[\d.]+)[\"']"
    r"[\s\S]{0,200}?longitude:\s*[\"'](-?[\d.]+)[\"']"
    r"[\s\S]{0,500}?address:\s*[\"']([^\"']*)[\"']"
    r"[\s\S]{0,250}?visibility:\s*[\"'](\w+)[\"']",
    re.I,
)
AP_MAP_TAG = re.compile(
    r"<div[^>]*(?:data-location-map|leaflet-container)[^>]*>",
    re.I,
)
AP_LAT = re.compile(r'data-latitude=["\']([^"\']+)["\']', re.I)
AP_LON = re.compile(r'data-longitude=["\']([^"\']+)["\']', re.I)
AP_LOCATION_LABEL = re.compile(
    r'class="location-label"[\s\S]{0,500}?<span>([^<]{6,220})</span>',
    re.I,
)
AP_DESC = re.compile(
    r"<h2[^>]*>\s*Descripci(?:&oacute;|ó)n\s*</h2>([\s\S]{80,8000}?)(?:<h2|<section)",
    re.I,
)


GONE_SNIPPETS = (
    "ya no está disponible",
    "ya no esta disponible",
    "publicación finalizada",
    "publicacion finalizada",
    "aviso no encontrado",
    "propiedad no encontrada",
    "no encontramos esta propiedad",
    "listing is no longer available",
    "this listing is no longer",
    "publicación inactiva",
    "publicacion inactiva",
    "este artículo no existe",
    "este articulo no existe",
    "el aviso fue dado de baja",
    "esta publicación fue finalizada",
    "esta publicacion fue finalizada",
)


def listing_page_is_gone(html: str) -> bool:
    low = (html or "").lower()
    if len(low) < 40:
        return False
    return any(snip in low for snip in GONE_SNIPPETS)


def _drop_gone(item: Listing) -> Listing:
    extra = dict(item.extra or {})
    extra["gone"] = True
    item.extra = extra
    from ..store import drop_listings

    drop_listings([item.id])
    return item


def enrich_details(item: Listing, should_stop=lambda: False) -> Listing:
    if should_stop() or not item.url:
        return analyze(item)
    from ..freshness import needs_detail_fetch

    if not needs_detail_fetch(item):
        return analyze(item)
    from ..http_client import PageGone

    try:
        html = fetch_text(item.url)
    except PageGone:
        return _drop_gone(item)
    except Exception as exc:
        msg = str(exc).lower()
        if "404" in msg or "410" in msg:
            return _drop_gone(item)
        return analyze(item)
    if listing_page_is_gone(html):
        return _drop_gone(item)
    _from_zonaprop_state(item, html)
    _from_mercadolibre(item, html)
    _from_properati(item, html)
    _from_argenprop(item, html)
    _from_json_ld(item, html)
    _from_visible_html(item, html)
    _coords_from_html(item, html)
    if should_stop():
        return analyze(item)
    pdf_text = _pdf_bits(html, item.url)
    extra = dict(item.extra or {})
    if pdf_text:
        extra["pdf_text"] = pdf_text[:4000]
        if not item.description:
            item.description = pdf_text[:1200]
        elif pdf_text[:200] not in item.description:
            item.description = (item.description + "\n" + pdf_text[:800])[:2000]
    extra["details_at"] = datetime.now(timezone.utc).isoformat()
    extra["details_parser"] = DETAILS_PARSER
    extra["detail_price"] = item.price
    extra["detail_m2"] = item.covered_m2 or item.total_m2
    item.extra = extra
    item.details_scraped = True
    from . import locate_item

    locate_item(item)
    return analyze(item)


def _from_zonaprop_state(item: Listing, html: str) -> None:
    url = (item.url or "").lower()
    if item.source != "zonaprop" and "zonaprop.com" not in url:
        return
    state = decode_js_object(html, "window.__PRELOADED_STATE__") or {}
    posting = (
        (state.get("postingStore") or {}).get("posting")
        or (state.get("viewPostingStore") or {}).get("posting")
        or state.get("posting")
        or {}
    )
    if isinstance(posting, dict) and posting:
        desc = posting.get("descriptionNormalized") or posting.get("description") or ""
        if desc and len(str(desc)) > len(item.description or ""):
            item.description = unescape(str(desc))[:2000]
        loc = posting.get("postingLocation") or {}
        geo = ((loc.get("postingGeolocation") or {}).get("geolocation") or {})
        lat = _num(geo.get("latitude"))
        lon = _num(geo.get("longitude"))
        if lat and lon:
            item.lat, item.lon = lat, lon
        address = ((loc.get("address") or {}).get("name") or "").strip()
        if address:
            _set_address(item, address)
        from . import portal_neighborhood

        neighborhood = portal_neighborhood(loc.get("neighborhood"), loc.get("barrio"), loc.get("zone"))
        if neighborhood:
            extra_loc = dict(item.extra or {})
            extra_loc["portal_barrio"] = neighborhood
            extra_loc["barrio"] = neighborhood
            item.extra = extra_loc
        expenses = posting.get("expenses") or {}
        amount = parse_number(str(expenses.get("amount") or expenses.get("formattedAmount") or ""))
        extra = dict(item.extra or {})
        if amount:
            extra["expenses"] = amount
        amenities = list(extra.get("amenities") or [])
        features = posting.get("mainFeatures") or posting.get("generalFeatures") or {}
        if isinstance(features, dict):
            for node in features.values():
                if not isinstance(node, dict):
                    continue
                label = str(node.get("label") or node.get("value") or "").strip()
                if label and label not in amenities:
                    amenities.append(label)
        extra["amenities"] = amenities
        item.extra = extra
        pictures = ((posting.get("visiblePictures") or {}).get("pictures") or [])
        photo_urls = []
        for pic in pictures:
            if not isinstance(pic, dict):
                continue
            href = pic.get("url730x532") or pic.get("url360x266") or pic.get("url") or ""
            if href:
                photo_urls.append(href)
        if photo_urls and not item.image:
            item.image = photo_urls[0]
        if photo_urls:
            from ..dedupe import add_photos

            add_photos(item, photo_urls)
    _from_zonaprop_map(item, html)


def _from_zonaprop_map(item: Listing, html: str) -> None:
    """Pin del mapa de la ficha: ZonaProp ya no manda __PRELOADED_STATE__ y cifra lat/lon en base64."""
    extra = dict(item.extra or {})
    addr = ZP_ADDR_VIS.search(html)
    visibility = (addr.group(2) if addr else "").strip().lower()
    if addr:
        _set_address(item, addr.group(1))
    lat = lon = None
    lat_m = ZP_MAP_LAT.search(html)
    lng_m = ZP_MAP_LNG.search(html)
    if lat_m and lng_m:
        lat, lon = _b64_coord(lat_m.group(1)), _b64_coord(lng_m.group(1))
    if lat and lon:
        _set_coords(item, lat, lon)
        extra["portal_lat"] = item.lat
        extra["portal_lon"] = item.lon
    if visibility in {"exact", "accurate"} or (not visibility and lat and lon):
        extra["map_visibility"] = visibility or "exact"
        extra["portal_exact"] = True
        extra["portal_approx"] = False
    elif visibility:
        extra["map_visibility"] = visibility
        extra["portal_exact"] = False
        extra["portal_approx"] = True
        item.has_exact_location = False
    item.extra = extra


def _b64_coord(raw: str) -> float | None:
    try:
        text = base64.b64decode(raw).decode("ascii").strip()
    except Exception:
        return None
    return _num(text)


def _from_mercadolibre(item: Listing, html: str) -> None:
    url = (item.url or "").lower()
    if item.source != "mercadolibre" and "mercadolibre.com" not in url:
        return
    headline = re.search(r'<h1[^>]*class="[^"]*ui-pdp-title[^"]*"[^>]*>([^<]{8,180})</h1>', html, re.I)
    if headline and title_quality(unescape(headline.group(1))) > title_quality(item.title or ""):
        item.title = unescape(headline.group(1)).strip()
    subtitle = re.search(r'ui-pdp-subtitle[^>]*>([^<]{8,120})<', html, re.I)
    if subtitle and "publicado" in subtitle.group(1).lower() and not item.published_at:
        item.published_at = unescape(subtitle.group(1)).strip()
    for pattern in (
        r'ui-vip-location[\s\S]{0,4000}?ui-pdp-media__title[^>]*>\s*<span>([^<]{6,180})</span>',
        r'ui-vip-location__subtitle[\s\S]{0,4000}?<span>([^<]{6,180})</span>',
        r'"target"\s*:\s*"location_and_points"[\s\S]{0,400}?"text"\s*:\s*"([^"]{6,180})"',
        r'poly-component__location[^>]*>([^<]{6,180})<',
    ):
        loc_block = re.search(pattern, html, re.I)
        if loc_block:
            _set_address(item, loc_block.group(1))
            break
    if not address_quality(item.address or ""):
        label = re.search(r'"text"\s*:\s*"((?:CALLE|AVENIDA|AV\.?|PASAJE) [^"]{4,80})"', html, re.I)
        if label:
            _set_address(item, label.group(1))
    found = ML_COORDS.search(html)
    if found:
        _set_coords(item, found.group(1), found.group(2))
    else:
        swapped = ML_COORDS_SWAP.search(html)
        if swapped:
            _set_coords(item, swapped.group(2), swapped.group(1))
    dorms = re.search(r'"text"\s*:\s*"(\d+)\s*dorm', html, re.I)
    baths = re.search(r'"text"\s*:\s*"(\d+(?:[.,]\d+)?)\s*baño', html, re.I)
    covered = re.search(r'"text"\s*:\s*"(\d[\d\.]*)\s*m.\s*cubiertos?"', html, re.I)
    total = re.search(r'"text"\s*:\s*"(\d[\d\.]*)\s*m.\s*totales?"', html, re.I)
    if dorms and item.bedrooms is None:
        item.bedrooms = int(dorms.group(1))
    if baths and item.bathrooms is None:
        item.bathrooms = float(baths.group(1).replace(",", "."))
    if covered and not item.covered_m2:
        item.covered_m2 = parse_number(covered.group(1))
    if total and not item.total_m2:
        item.total_m2 = parse_number(total.group(1))
    item.property_type = detect_type(f"{item.title} {item.address} {item.property_type}", item.property_type or "casa")


def _from_argenprop(item: Listing, html: str) -> None:
    """El mapa de la ficha usa data-latitude/data-longitude con coma decimal argentina."""
    url = (item.url or "").lower()
    if item.source != "argenprop" and "argenprop.com" not in url:
        return
    tag = ""
    found = AP_MAP_TAG.search(html or "")
    if found:
        tag = found.group(0)
    lat_m = AP_LAT.search(tag) or AP_LAT.search(html or "")
    lon_m = AP_LON.search(tag) or AP_LON.search(html or "")
    extra = dict(item.extra or {})
    label = AP_LOCATION_LABEL.search(html or "")
    if label:
        _set_address(item, unescape(label.group(1)))
        extra = dict(item.extra or {})
    desc = AP_DESC.search(html or "")
    if desc:
        _set_description(item, desc.group(1))
    if not (lat_m and lon_m):
        item.extra = extra
        return
    lat, lon = _coord(lat_m.group(1)), _coord(lon_m.group(1))
    if lat is None or lon is None:
        item.extra = extra
        return
    _set_coords(item, lat, lon)
    extra = dict(item.extra or {})
    extra["portal_lat"] = item.lat
    extra["portal_lon"] = item.lon
    extra["portal_map"] = "ficha"
    extra["portal_exact"] = False
    extra["portal_approx"] = True
    extra["map_visibility"] = "approximate"
    item.has_exact_location = False
    item.extra = extra


def _coord(raw) -> float | None:
    text = str(raw or "").strip().replace(" ", "")
    if re.fullmatch(r"-?\d{1,3},\d{2,8}", text):
        text = text.replace(",", ".")
    return _num(text)


def _from_json_ld(item: Listing, html: str) -> None:
    for match in JSON_LD.finditer(html):
        raw = unescape(match.group(1).strip())
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        nodes: list = []
        if isinstance(data, list):
            nodes = data
        elif isinstance(data, dict):
            nodes = list(data.get("@graph") or [])
            nodes.append(data)
        for node in nodes:
            if not isinstance(node, dict):
                continue
            addr = node.get("address")
            if isinstance(addr, dict):
                line = " ".join(
                    part for part in [
                        addr.get("streetAddress"),
                        addr.get("addressLocality"),
                    ]
                    if part
                ).strip()
                if addr.get("streetAddress"):
                    _set_address(item, line)
            geo = node.get("geo") if isinstance(node.get("geo"), dict) else {}
            extra_geo = item.extra or {}
            if not extra_geo.get("portal_approx") and not extra_geo.get("portal_exact"):
                _set_coords(item, geo.get("latitude"), geo.get("longitude"))
            floor = node.get("floorSize") if isinstance(node.get("floorSize"), dict) else {}
            area = parse_number(str(floor.get("value") or ""))
            if area and not item.covered_m2:
                item.covered_m2 = area
            rooms = node.get("numberOfRooms") or node.get("numberOfBedrooms")
            if rooms and item.bedrooms is None:
                try:
                    item.bedrooms = int(float(rooms))
                except (TypeError, ValueError):
                    pass
            desc = node.get("description")
            if isinstance(desc, str):
                text = re.sub(r"\s+", " ", unescape(desc)).strip()
                if len(text) > len(item.description or ""):
                    item.description = text[:2000]


def _from_properati(item: Listing, html: str) -> None:
    url = (item.url or "").lower()
    if item.source != "properati" and "properati.com" not in url:
        return
    box = PROPERATI_DESC.search(html)
    if box:
        _set_description(item, box.group(1))
    block = _js_block(html, "mapData")
    lat = lon = address = visibility = ""
    if block:
        lat = _coord_field(block, "latitude")
        lon = _coord_field(block, "longitude")
        address = _quoted_field(block, "address")
        visibility = _quoted_field(block, "visibility").lower()
    if not (lat and lon) or not address:
        found = PROPERATI_MAP.search(html)
        if found:
            if not (lat and lon):
                lat, lon = found.group(1), found.group(2)
            if not address:
                address = unescape(found.group(3) or "").replace("\\u0026", "&")
            if not visibility:
                visibility = (found.group(4) or "").lower()
    extra = dict(item.extra or {})
    if not (lat and lon):
        # El clic en "Ver mapa" abre el mismo widget Navent / mapa estático.
        _from_zonaprop_map(item, html)
        extra = dict(item.extra or {})
        if item.lat and item.lon:
            lat, lon = str(item.lat), str(item.lon)
        visibility = visibility or str(extra.get("map_visibility") or "").lower()
    if not (lat and lon):
        wlat, wlon = _map_widget_coords(html)
        if wlat and wlon:
            lat, lon = wlat, wlon
            visibility = visibility or "approximate"
    if visibility:
        extra["map_visibility"] = visibility
    if re.search(r"enableApproximateArea\s*:\s*true", block or html, re.I):
        extra["portal_approx_area"] = True
    item.extra = extra
    if address:
        _set_address(item, _street_line(address))
    extra = dict(item.extra or extra)
    if lat and lon:
        plat, plon = _coord(lat), _coord(lon)
        if plat is not None and plon is not None:
            extra["portal_lat"] = plat
            extra["portal_lon"] = plon
        _set_coords(item, lat, lon)
        extra["portal_map"] = "ver_mapa"
        approx = bool(extra.get("portal_approx_area")) or visibility not in {"accurate", "exact"}
        extra["portal_approx"] = approx
        if approx:
            extra["portal_exact"] = False
            item.has_exact_location = False
        elif visibility in {"accurate", "exact"}:
            extra["portal_exact"] = True
    item.extra = extra


def _from_visible_html(item: Listing, html: str) -> None:
    for pattern in (
        r'itemprop=["\']streetAddress["\'][^>]*>([^<]{6,160})<',
        r'class="[^"]*card__address[^"]*"[^>]*>([^<]{6,160})<',
        r'class="[^"]*title-location[^"]*"[^>]*>([^<]{6,160})<',
        r'data-test=["\']snippet__location["\'][^>]*>([^<]{6,160})<',
    ):
        found = re.search(pattern, html, re.I)
        if found:
            _set_address(item, unescape(found.group(1)))
            break
    text = unescape(re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I))
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    extra = dict(item.extra or {})
    extra["amenities"] = list(dict.fromkeys((extra.get("amenities") or []) + extract_features(text)))
    item.extra = extra
    if not item.description and len(text) > 80:
        item.description = text[:1200]


def _coords_from_html(item: Listing, html: str) -> None:
    if item.lat and item.lon:
        return
    if (item.extra or {}).get("portal_approx"):
        return
    lat, lon = _map_widget_coords(html)
    if lat and lon:
        _set_coords(item, lat, lon)


def _map_widget_coords(html: str) -> tuple[str, str]:
    """Centro del mapa que se ve al tocar Ver mapa: static map, iframe o data-lat."""
    blob = unquote(html or "")
    tag = AP_MAP_TAG.search(html or "")
    if tag:
        lat_m = AP_LAT.search(tag.group(0))
        lon_m = AP_LON.search(tag.group(0))
        if lat_m and lon_m:
            lat, lon = _coord(lat_m.group(1)), _coord(lon_m.group(1))
            if lat is not None and lon is not None:
                return str(lat), str(lon)
    for pattern in (MAP_CENTER, MAP_MARKERS, GMAPS_AT, GENERIC_LATLON):
        found = pattern.search(blob) or pattern.search(html or "")
        if found:
            return found.group(1), found.group(2)
    embed = GMAPS_2D3D.search(blob)
    if embed:
        return embed.group(2), embed.group(1)
    return "", ""


def _set_coords(item: Listing, lat, lon) -> None:
    parsed_lat, parsed_lon = _coord(lat), _coord(lon)
    if parsed_lat is None or parsed_lon is None:
        return
    if abs(parsed_lat + 38.416) < 0.05 and abs(parsed_lon + 63.616) < 0.05:
        return
    from ..geo import in_water

    if in_water(parsed_lat, parsed_lon, item.city):
        return
    item.lat, item.lon = parsed_lat, parsed_lon


def _set_address(item: Listing, candidate: str) -> None:
    cand = clean_portal_address(unescape(candidate or ""))
    if not cand:
        return
    extra = dict(item.extra or {})
    from ..geo import parse_street
    from ..geo_tools import parse_plain_locations

    found = parse_plain_locations(cand)
    if found.get("corners"):
        extra.setdefault("intersection", " y ".join(found["corners"][0]))
    if found.get("between"):
        extra.setdefault("between", " y ".join(found["between"][0]))
    st, num = parse_street(cand)
    cur_st, cur_num = parse_street(item.address or "")
    if looks_like_intersection(cand) and not (st and num):
        extra.setdefault("intersection", cand)
        extra.setdefault("approx_address", cand)
        item.extra = extra
        attach_location_facts(item)
        return
    if cur_st and cur_num and not (st and num):
        extra.setdefault("approx_address", cand)
        item.extra = extra
        return
    if st and num:
        extra["street"] = st
        extra["street_number"] = num
    if address_quality(cand) > address_quality(item.address or ""):
        item.address = cand
    item.extra = extra
    attach_location_facts(item)


def _set_description(item: Listing, raw: str) -> None:
    text = unescape(re.sub(r"<[^>]+>", " ", raw or ""))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > max(80, len(item.description or "")):
        item.description = text[:3500]


def _js_block(html: str, key: str) -> str:
    found = re.search(rf"{re.escape(key)}\s*:\s*\{{", html)
    if not found:
        return ""
    start = html.find("{", found.start())
    depth = 0
    for idx in range(start, min(len(html), start + 12000)):
        ch = html[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return html[start : idx + 1]
    return ""


def _quoted_field(block: str, name: str) -> str:
    found = re.search(rf"{re.escape(name)}\s*:\s*[\"']([^\"']*)[\"']", block, re.I)
    if not found:
        return ""
    return unescape(found.group(1)).replace("\\u0026", "&").strip()


def _coord_field(block: str, name: str) -> str:
    quoted = _quoted_field(block, name)
    if quoted:
        return quoted
    found = re.search(rf"{re.escape(name)}\s*:\s*(-?\d+\.\d+)", block, re.I)
    return found.group(1) if found else ""


def _street_line(text: str) -> str:
    cleaned = clean_portal_address(text)
    parts = [part.strip(" ,") for part in cleaned.split(",") if part.strip(" ,")]
    if not parts:
        return cleaned
    from ..geo import parse_street

    street, number = parse_street(parts[0])
    if not (street and number):
        return cleaned
    if len(parts) > 1 and len(parts[1]) <= 40:
        return f"{parts[0]}, {parts[1]}"
    return parts[0]


def _address_quality(text: str) -> int:
    return address_quality(text)


def _pdf_bits(html: str, base_url: str = "") -> str:
    chunks: list[str] = []
    for href in PDF_HREF.findall(html)[:2]:
        url = urljoin(base_url, href)
        if not url.startswith("http"):
            continue
        try:
            data = fetch_bytes(url)
        except Exception:
            continue
        extracted = _pdf_from_bytes(data)
        if extracted:
            chunks.append(extracted)
    return "\n".join(chunks)


def _pdf_from_bytes(data: bytes) -> str:
    if b"%PDF" not in data[:80]:
        return ""
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages[:6]]
        return re.sub(r"\s+", " ", " ".join(pages)).strip()[:4000]
    except Exception:
        return ""


def _num(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if abs(number) > 1 else None
