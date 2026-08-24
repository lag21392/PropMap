from __future__ import annotations

import json
import re

from lxml import html as lhtml

from ..http_client import fetch_text
from ..models import Listing
from . import detect_type, first_int, locate_item, paginate, parse_number
from .urls import properati_urls

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)

CITY_SEARCHES = {
    "puerto-madryn": [
        ("https://www.properati.com.ar/s/puerto-madryn/casa/venta", "casa"),
        ("https://www.properati.com.ar/s/puerto-madryn/departamento/venta", "departamento"),
        ("https://www.properati.com.ar/s/puerto-madryn/ph/venta", "ph"),
        ("https://www.properati.com.ar/s/puerto-madryn/terreno/venta", "terreno"),
        ("https://www.properati.com.ar/s/el-doradillo/terreno/venta", "terreno"),
        ("https://www.properati.com.ar/s/el-doradillo/casa/venta", "casa"),
        ("https://www.properati.com.ar/s/punta-cuevas/casa/venta", "casa"),
        ("https://www.properati.com.ar/s/punta-cuevas/terreno/venta", "terreno"),
        ("https://www.properati.com.ar/s/playa-parana/casa/venta", "casa"),
        ("https://www.properati.com.ar/s/cerro-avanzado/terreno/venta", "terreno"),
    ],
    "trelew": [
        ("https://www.properati.com.ar/s/trelew/casa/venta", "casa"),
        ("https://www.properati.com.ar/s/trelew/departamento/venta", "departamento"),
        ("https://www.properati.com.ar/s/trelew/ph/venta", "ph"),
        ("https://www.properati.com.ar/s/trelew/terreno/venta", "terreno"),
    ],
    "rawson": [
        ("https://www.properati.com.ar/s/rawson/casa/venta", "casa"),
        ("https://www.properati.com.ar/s/rawson/departamento/venta", "departamento"),
        ("https://www.properati.com.ar/s/rawson/ph/venta", "ph"),
        ("https://www.properati.com.ar/s/rawson/terreno/venta", "terreno"),
        ("https://www.properati.com.ar/s/playa-union/casa/venta", "casa"),
        ("https://www.properati.com.ar/s/playa-union/terreno/venta", "terreno"),
    ],
    "gaiman": [
        ("https://www.properati.com.ar/s/gaiman/casa/venta", "casa"),
        ("https://www.properati.com.ar/s/gaiman/departamento/venta", "departamento"),
        ("https://www.properati.com.ar/s/gaiman/ph/venta", "ph"),
        ("https://www.properati.com.ar/s/gaiman/terreno/venta", "terreno"),
    ],
    "playa-union": [
        ("https://www.properati.com.ar/s/playa-union/casa/venta", "casa"),
        ("https://www.properati.com.ar/s/playa-union/departamento/venta", "departamento"),
        ("https://www.properati.com.ar/s/playa-union/ph/venta", "ph"),
        ("https://www.properati.com.ar/s/playa-union/terreno/venta", "terreno"),
    ],
    "microcentro-caba": [
        ("https://www.properati.com.ar/s/microcentro/departamento/venta", "departamento"),
        ("https://www.properati.com.ar/s/san-nicolas/departamento/venta", "departamento"),
        ("https://www.properati.com.ar/s/monserrat/departamento/venta", "departamento"),
        ("https://www.properati.com.ar/s/microcentro/terreno/venta", "terreno"),
    ],
}


def scrape(progress=lambda _m: None, city: str = "puerto-madryn", should_stop=None, on_chunk=None) -> list[Listing]:
    listings: list[Listing] = []
    for url, ptype in properati_urls(city, CITY_SEARCHES):
        if should_stop and should_stop():
            break
        progress(f"Properati · {city} · {ptype}s")
        try:
            listings.extend(
                paginate(
                    lambda page, u=url, t=ptype, c=city: _page(u, t, page, c),
                    should_stop=should_stop,
                    on_chunk=on_chunk,
                )
            )
        except Exception as exc:
            progress(f"Properati {city} {ptype}: {exc}")
    return listings


def _page(url: str, fallback_type: str, page: int, city: str = "puerto-madryn") -> list[Listing]:
    page_url = url if page == 1 else f"{url}/{page}"
    html = fetch_text(page_url)
    doc = lhtml.fromstring(html)
    geo_by_id = _ld_geo(html)
    cards = doc.xpath('//article[contains(@class,"snippet") or @data-url]')
    items: list[Listing] = []
    for card in cards:
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
        ).strip() or "Puerto Madryn"
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
    t = (text or "").strip().lower()
    if len(t) < 8:
        return False
    weak = {"puerto madryn", "trelew", "chubut", "argentina", "capital federal", "biedma"}
    parts = [p.strip(" ,") for p in re.split(r"[,/]", t) if p.strip()]
    if parts and all(p in weak for p in parts):
        return False
    return bool(re.search(r"\d|&|calle|av\.|avenida|pasaje", t, re.I))


def _as_float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if abs(number) > 1 else None


def _m2(text: str) -> float | None:
    return _areas(text)[0]
