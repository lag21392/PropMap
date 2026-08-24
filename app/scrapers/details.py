from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from html import unescape
from io import BytesIO
from urllib.parse import unquote, urljoin

from ..features import analyze, extract_features
from ..http_client import decode_js_object, fetch_bytes, fetch_text
from ..models import Listing
from ..text_quality import address_quality, clean_portal_address, title_quality
from . import detect_type, parse_number

DETAILS_PARSER = "4"
PDF_HREF = re.compile(r'href=["\']([^"\']+\.pdf[^"\']*)["\']', re.I)
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
GENERIC_LATLON = re.compile(
    r'data-(?:lat|latitude)=["\'](-?\d+\.\d+)["\'][^>]{0,80}data-(?:lng|lon|longitude)=["\'](-?\d+\.\d+)["\']',
    re.I,
)
PROPERATI_MAP = re.compile(
    r"mapData:\s*\{[\s\S]{0,900}?latitude:\s*[\"'](-?[\d.]+)[\"']"
    r"[\s\S]{0,200}?longitude:\s*[\"'](-?[\d.]+)[\"']"
    r"[\s\S]{0,500}?address:\s*[\"']([^\"']*)[\"']"
    r"[\s\S]{0,250}?visibility:\s*[\"'](\w+)[\"']",
    re.I,
)


def enrich_details(item: Listing, should_stop=lambda: False) -> Listing:
    if should_stop() or not item.url:
        return analyze(item)
    from ..freshness import needs_detail_fetch

    if not needs_detail_fetch(item):
        return analyze(item)
    try:
        html = fetch_text(item.url)
    except Exception:
        return analyze(item)
    _from_zonaprop_state(item, html)
    _from_mercadolibre(item, html)
    _from_properati(item, html)
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
    state = decode_js_object(html, "window.__PRELOADED_STATE__") or {}
    posting = (
        (state.get("postingStore") or {}).get("posting")
        or (state.get("viewPostingStore") or {}).get("posting")
        or state.get("posting")
        or {}
    )
    if not isinstance(posting, dict) or not posting:
        return
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
    if pictures and not item.image:
        item.image = pictures[0].get("url730x532") or pictures[0].get("url360x266") or item.image


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
    found = PROPERATI_MAP.search(html)
    if not found:
        return
    visibility = (found.group(4) or "").lower()
    address = unescape(found.group(3) or "").replace("\\u0026", "&")
    if address:
        _set_address(item, address)
    if visibility == "accurate":
        _set_coords(item, found.group(1), found.group(2))


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
    generic = GENERIC_LATLON.search(html)
    if generic:
        _set_coords(item, generic.group(1), generic.group(2))
        return
    center = MAP_CENTER.search(unquote(html))
    if center:
        _set_coords(item, center.group(1), center.group(2))


def _set_coords(item: Listing, lat, lon) -> None:
    parsed_lat, parsed_lon = _num(lat), _num(lon)
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
    if address_quality(cand) > address_quality(item.address or ""):
        item.address = cand


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
