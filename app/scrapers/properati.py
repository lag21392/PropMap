from __future__ import annotations

import json
import re

from lxml import html as lhtml

from ..http_client import fetch_text
from ..models import Listing
from ..text_quality import address_quality
from . import detect_type, first_int, locate_item, paginate, parse_number
from .urls import properati_urls

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def scrape(progress=lambda _m: None, city: str | None = None, should_stop=None, on_chunk=None) -> list[Listing]:
    from ..geo import default_city

    city = city or default_city()
    listings: list[Listing] = []
    for url, ptype in properati_urls(city):
        if should_stop and should_stop():
            break
        progress(f"Revisando {ptype}s…")
        try:
            listings.extend(
                paginate(
                    lambda page, u=url, t=ptype, c=city: _page(u, t, page, c),
                    should_stop=should_stop,
                    on_chunk=on_chunk,
                )
            )
        except Exception:
            progress("Un lote no respondió, sigo…")
    return listings


def _page(url: str, fallback_type: str, page: int, city: str | None = None) -> list[Listing]:
    from ..geo import default_city
    from . import iterparse_html

    city = city or default_city()
    page_url = url if page == 1 else f"{url}/{page}"
    html = fetch_text(page_url)
    max_bytes = 2 * 1024 * 1024
    if len(html) > max_bytes:
        html = html[:max_bytes]
    geo_by_id = _ld_geo(html)
    items: list[Listing] = []
    from lxml import html as lhtml
    for card_el in iterparse_html(html, tag='article'):
        class_attr = card_el.get('class') or ''
        if not (card_el.get('data-url') or 'snippet' in class_attr.lower()):
            continue
        card_html = lhtml.tostring(card_el, encoding='unicode')
        card = lhtml.fromstring(card_html)
        href = card.get("data-url") or (card.xpath(".//a/@href") or [""])[0]
        source_id = card.get("data-idanuncio") or href.rsplit("/", 1)[-1]
        if not href or not source_id:
            continue
        text = " ".join(card.text_content().split())
        title = " ".join(
            card.xpath('.//a[contains(@class,"title")]//text() | .//*[@data-test="snippet__title"]//text()')
        ).strip() or text[:120]
        img = (card.xpath(".//img/@src") or [""])[0]
        price_text = " ".join(card.xpath('.//*[@data-test="snippet__price"]//text() | .//*[contains(@class,"price")]//text()')).strip()
        currency = "USD"
        if re.search(r"\b(ARS|\$)\b", price_text) and not re.search(r"USD|U\$S|US\$", price_text):
            currency = "ARS"
        if re.search(r"USD|U\$S|US\$", price_text or text):
            currency = "USD"
        price = parse_number(price_text) or parse_number(
            re.search(r"(USD|U\$S|US\$|\$)\s*[\d\.\s]+", text).group(0)
            if re.search(r"(USD|U\$S|US\$|\$)\s*[\d\.\s]+", text)
            else ""
        )
        address = " ".join(
            card.xpath('.//*[@data-test="snippet__location"]//text() | .//*[contains(@class,"location")]//text()')
        ).strip()
        publisher_name = " ".join(card.xpath('.//*[@data-test="agency-name"]//text()')).strip()
        geo = _match_ld(geo_by_id, href, img, text)
        if geo and geo.get("address") and _usable_address(geo["address"]):
            address = geo["address"]
        lat = geo.get("lat") if geo else None
        lon = geo.get("lon") if geo else None
        item = Listing(
            source="properati",
            source_id=str(source_id),
            url=href if href.startswith("http") else "https://www.properati.com.ar" + href,
            title=title,
            property_type=detect_type(f"{title} {text}", fallback_type),
            price=price,
            currency=currency,
            address=address,
            covered_m2=_areas(text)[0],
            total_m2=_areas(text)[1],
            bedrooms=first_int(text, r"(\d+)\s*dormitorio"),
            bathrooms=first_int(text, r"(\d+(?:[.,]\d+)?)\s*baño"),
            parking=1 if re.search(r"garage|cochera", text, re.I) else None,
            image=img,
            publisher=publisher_name,
            description=text[:800],
            city=city,
            lat=lat,
            lon=lon,
            extra={"photos": [img]} if img else {},
        )
        items.append(locate_item(item))
    return items


def _areas(text: str) -> tuple[float | None, float | None]:
    lot = None
    covered = None
    lot_match = re.search(r"(\d[\d\.]{0,6})\s*m[²2]?\s*(?:de\s*)?(?:terreno|lote|totales?)", text, re.I)
    cov_match = re.search(r"(\d[\d\.]{0,6})\s*m[²2]?\s*(?:cubiertos?|cub)", text, re.I)
    if lot_match:
        lot = parse_number(lot_match.group(1))
    if cov_match:
        covered = parse_number(cov_match.group(1))
    if covered is None and lot is None:
        generic = re.search(r"(\d[\d\.]{0,6})\s*m", text, re.I)
        if generic:
            covered = parse_number(generic.group(1))
    return covered, lot


def _ld_geo(html: str) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for match in re.finditer(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.I | re.S,
    ):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        nodes: list = []
        if isinstance(data, dict):
            nodes = list(data.get("about") or [])
            nodes.append(data)
        elif isinstance(data, list):
            nodes = data
        for node in nodes:
            if not isinstance(node, dict):
                continue
            geo = node.get("geo") if isinstance(node.get("geo"), dict) else {}
            addr = node.get("address") if isinstance(node.get("address"), dict) else {}
            payload = {
                "lat": _as_float(geo.get("latitude")),
                "lon": _as_float(geo.get("longitude")),
                "address": (addr.get("streetAddress") or "").strip(),
            }
            if not payload["lat"] and not payload["address"]:
                continue
            for uid in _UUID.findall(str(node.get("image") or "")):
                found[uid.lower()] = payload
    return found


def _match_ld(index: dict[str, dict], href: str, img: str, text: str) -> dict | None:
    blob = f"{href} {img} {text}"
    for uid in _UUID.findall(blob):
        hit = index.get(uid.lower())
        if hit:
            return hit
    return None


def _usable_address(text: str) -> bool:
    return address_quality(text) >= 5


def _as_float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if abs(number) > 1 else None


def _m2(text: str) -> float | None:
    return _areas(text)[0]
