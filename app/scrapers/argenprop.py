from __future__ import annotations

import re

from ..models import Listing
from . import detect_type, first_int, locate_item, paginate, parse_number, tree
from .urls import argenprop_urls

BASE = "https://www.argenprop.com"


def scrape(progress=lambda _m: None, city: str | None = None, should_stop=None, on_chunk=None) -> list[Listing]:
    from ..geo import default_city

    city = city or default_city()
    listings: list[Listing] = []
    for url, ptype in argenprop_urls(city):
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

    city = city or default_city()
    page_url = url if page == 1 else f"{url}?pagina-{page}"
    doc = tree(page_url)
    cards = doc.xpath('//*[@data-item-card]')
    items: list[Listing] = []
    for card in cards:
        source_id = str(card.get("data-item-card") or card.get("idaviso") or "")
        href = card.get("href") or ""
        if not source_id or not href:
            continue
        text = " ".join(card.text_content().split())
        currency = "USD"
        if re.search(r"\$ ?\d", text) and "USD" not in text and "U$S" not in text and "US$" not in text:
            currency = "ARS"
        price_attr = parse_number(card.get("montonormalizado") or card.get("montooperacion"))
        price = price_attr or parse_number(
            " ".join(card.xpath('.//*[contains(@class,"card__price")]//text()'))
        )
        address = " ".join(card.xpath('.//*[contains(@class,"card__address")]//text()')).strip()
        title = " ".join(card.xpath('.//*[contains(@class,"card__title")]//text()')).strip() or text[:120]
        img = (card.xpath(".//img/@src") or card.xpath(".//img/@data-src") or [""])[0]
        bedrooms = first_int(text, r"(\d+)\s*dorm")
        bathrooms = first_int(text, r"(\d+)\s*baño")
        covered = first_int(text, r"(\d+(?:[.,]\d+)?)\s*m[²2]?\s*(?:cubiertos?|cub)")
        lot = first_int(text, r"(\d+(?:[.,]\d+)?)\s*m[²2]?\s*(?:de\s*)?(?:terreno|lote|totales?)")
        if covered is None and lot is None:
            covered = first_int(text, r"(\d+(?:[.,]\d+)?)\s*m")
        age = first_int(text, r"(\d+)\s*años")
        publisher = " ".join(card.xpath('.//*[contains(@class,"card__agent")]//text()')).strip()
        item = Listing(
            source="argenprop",
            source_id=source_id,
            url=href if href.startswith("http") else BASE + href,
            title=title or f"{fallback_type.title()} en Puerto Madryn",
            property_type=detect_type(f"{title} {href} {text}", fallback_type),
            price=price,
            currency=currency,
            address=address or title,
            covered_m2=float(covered) if covered else None,
            total_m2=float(lot) if lot else None,
            bedrooms=bedrooms,
            bathrooms=bathrooms,
            rooms=bedrooms + 1 if bedrooms else first_int(text, r"(\d+)\s*amb"),
            age_years=age,
            image=img,
            publisher=publisher,
            description=text[:800],
            city=city,
            extra={"photos": [img]} if img else {},
        )
        items.append(locate_item(item))
    return items
