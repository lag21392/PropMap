from __future__ import annotations

import json
import re

from lxml import html as lhtml

from ..http_client import fetch_text
from ..models import Listing
from ..text_quality import clean_portal_address
from . import detect_type, locate_item, paginate, parse_number
from .urls import mercadolibre_urls


def scrape(progress=lambda _m: None, city: str | None = None, should_stop=None, on_chunk=None) -> list[Listing]:
    from ..geo import default_city

    city = city or default_city()
    listings: list[Listing] = []
    for url, ptype in mercadolibre_urls(city):
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


def _page(url: str, fallback_type: str, page: int, city: str) -> list[Listing]:
    page_url = url if page == 1 else url.rstrip("/") + f"/_Desde_{1 + (page - 1) * 48}"
    html = fetch_text(page_url)
    if "ingresa a" in html.lower() and "tu cuenta" in html.lower() and "RealEstateListing" not in html:
        return []
    by_id: dict[str, Listing] = {}
    for item in _from_cards(html, fallback_type, city):
        by_id[item.source_id] = item
    for item in _from_ld(html, fallback_type, city):
        current = by_id.get(item.source_id)
        if not current:
            by_id[item.source_id] = item
            continue
        if not current.price and item.price:
            current.price = item.price
            current.currency = item.currency
        if not current.image and item.image:
            current.image = item.image
        if item.lat and item.lon and not current.lat:
            current.lat, current.lon = item.lat, item.lon
        if len(item.description or "") > len(current.description or ""):
            current.description = item.description
    return [locate_item(item) for item in by_id.values()]


def _from_cards(html: str, fallback_type: str, city: str) -> list[Listing]:
    try:
        tree = lhtml.fromstring(html)
    except Exception:
        return []
    items: list[Listing] = []
    seen: set[str] = set()
    for card in tree.xpath('//*[contains(@class,"poly-card__content")]'):
        hrefs = card.xpath('.//a[contains(@href,"MLA")]/@href')
        if not hrefs:
            continue
        url_item = hrefs[0].split("#")[0].strip()
        source_id = _id_from_url(url_item)
        if not source_id or source_id in seen:
            continue
        seen.add(source_id)
        title = _first_text(card, "poly-component__title") or "Propiedad en venta"
        address = clean_portal_address(_first_text(card, "poly-component__location"))
        price_text = _first_text(card, "poly-component__price") or _first_text(card, "andes-money-amount__fraction")
        currency_text = _first_text(card, "andes-money-amount__currency-symbol")
        currency = "USD" if "US" in (currency_text or price_text or "").upper() else "ARS"
        image = ""
        imgs = card.xpath(".//img/@src | .//img/@data-src | .//img/@data-srcset")
        if imgs:
            image = str(imgs[0]).split(" ")[0]
        items.append(
            Listing(
                source="mercadolibre",
                source_id=source_id,
                url=url_item,
                title=title,
                property_type=detect_type(f"{title} {url_item} {address}", fallback_type),
                price=parse_number(price_text),
                currency=currency,
                address=address,
                image=image,
                description=title[:900],
                city=city,
                extra={"photos": [image]} if image else {},
            )
        )
    return items


def _from_ld(html: str, fallback_type: str, city: str) -> list[Listing]:
    items: list[Listing] = []
    for payload in _ld_payloads(html):
        graph = payload.get("@graph") if isinstance(payload, dict) else payload
        if not isinstance(graph, list):
            continue
        for raw in graph:
            if not isinstance(raw, dict) or raw.get("@type") != "RealEstateListing":
                continue
            offer = raw.get("offers") or {}
            url_item = (offer.get("url") or raw.get("mainEntityOfPage") or raw.get("url") or "").strip()
            source_id = _id_from_url(url_item)
            if not source_id:
                continue
            address = raw.get("address") or {}
            floor = raw.get("floorSize") or {}
            seller = raw.get("seller") or {}
            geo = raw.get("geo") or {}
            street = clean_portal_address(
                " ".join(
                    part
                    for part in [address.get("streetAddress") or "", address.get("addressLocality") or ""]
                    if part
                )
            )
            title = raw.get("name") or "Propiedad en venta"
            items.append(
                Listing(
                    source="mercadolibre",
                    source_id=source_id,
                    url=url_item,
                    title=title,
                    property_type=detect_type(f"{title} {url_item}", fallback_type),
                    price=parse_number(str(offer.get("price") or "")),
                    currency=str(offer.get("priceCurrency") or "USD"),
                    address=street,
                    lat=_safe_float(geo.get("latitude")),
                    lon=_safe_float(geo.get("longitude")),
                    covered_m2=parse_number(str(floor.get("value") or "")),
                    bedrooms=raw.get("numberOfRooms"),
                    rooms=raw.get("numberOfRooms"),
                    image=raw.get("image") or "",
                    publisher=(seller.get("name") or "").strip(),
                    published_at=str(raw.get("datePosted") or ""),
                    description=(raw.get("description") or title)[:900],
                    city=city,
                )
            )
    return items


def _first_text(node, class_name: str) -> str:
    bits = node.xpath(f'.//*[contains(@class,"{class_name}")]//text()')
    return " ".join(bit.strip() for bit in bits if bit.strip())


def _ld_payloads(html: str) -> list[dict | list]:
    out: list[dict | list] = []
    for match in re.finditer(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        html,
        re.I | re.S,
    ):
        try:
            data = json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            continue
        if isinstance(data, (dict, list)):
            out.append(data)
    return out


def _id_from_url(url: str) -> str:
    match = re.search(r"(MLA-?\d+)", url or "", re.I)
    return match.group(1).replace("-", "") if match else ""


def _safe_float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if abs(number) > 1 else None
