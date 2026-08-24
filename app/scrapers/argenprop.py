from __future__ import annotations

import re

from ..models import Listing
from . import detect_type, first_int, locate_item, paginate, parse_number, tree
from .urls import argenprop_urls

BASE = "https://www.argenprop.com"


def scrape(progress=lambda _m: None, city: str = "puerto-madryn", should_stop=None, on_chunk=None) -> list[Listing]:
    CITY_SEARCHES = {
        "puerto-madryn": [
            (f"{BASE}/casas/venta/puerto-madryn", "casa"),
            (f"{BASE}/departamentos/venta/puerto-madryn", "departamento"),
            (f"{BASE}/ph/venta/puerto-madryn", "ph"),
            (f"{BASE}/terrenos/venta/puerto-madryn", "terreno"),
            (f"{BASE}/terrenos/venta/el-doradillo", "terreno"),
            (f"{BASE}/casas/venta/el-doradillo", "casa"),
            (f"{BASE}/terrenos/venta/punta-cuevas", "terreno"),
            (f"{BASE}/casas/venta/punta-cuevas", "casa"),
            (f"{BASE}/casas/venta/playa-parana", "casa"),
            (f"{BASE}/terrenos/venta/cerro-avanzado", "terreno"),
        ],
        "trelew": [
            (f"{BASE}/casas/venta/trelew", "casa"),
            (f"{BASE}/departamentos/venta/trelew", "departamento"),
            (f"{BASE}/ph/venta/trelew", "ph"),
            (f"{BASE}/terrenos/venta/trelew", "terreno"),
        ],
        "rawson": [
            (f"{BASE}/casas/venta/rawson", "casa"),
            (f"{BASE}/departamentos/venta/rawson", "departamento"),
            (f"{BASE}/ph/venta/rawson", "ph"),
            (f"{BASE}/terrenos/venta/rawson", "terreno"),
            (f"{BASE}/casas/venta/playa-union", "casa"),
        ],
        "gaiman": [
            (f"{BASE}/casas/venta/gaiman", "casa"),
            (f"{BASE}/departamentos/venta/gaiman", "departamento"),
            (f"{BASE}/ph/venta/gaiman", "ph"),
            (f"{BASE}/terrenos/venta/gaiman", "terreno"),
        ],
        "playa-union": [
            (f"{BASE}/casas/venta/playa-union", "casa"),
            (f"{BASE}/departamentos/venta/playa-union", "departamento"),
            (f"{BASE}/ph/venta/playa-union", "ph"),
            (f"{BASE}/terrenos/venta/playa-union", "terreno"),
        ],
        "microcentro-caba": [
            (f"{BASE}/departamentos/venta/microcentro", "departamento"),
            (f"{BASE}/departamentos/venta/san-nicolas", "departamento"),
            (f"{BASE}/ph/venta/san-nicolas", "ph"),
            (f"{BASE}/terrenos/venta/microcentro", "terreno"),
        ],
    }
    listings: list[Listing] = []
    for url, ptype in argenprop_urls(city, CITY_SEARCHES):
        if should_stop and should_stop():
            break
        progress(f"Argenprop · {city} · {ptype}s")
        try:
            listings.extend(
                paginate(
                    lambda page, u=url, t=ptype, c=city: _page(u, t, page, c),
                    should_stop=should_stop,
                    on_chunk=on_chunk,
                )
            )
        except Exception as exc:
            progress(f"Argenprop {city} {ptype}: {exc}")
    return listings


def _page(url: str, fallback_type: str, page: int, city: str = "puerto-madryn") -> list[Listing]:
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
        )
        items.append(locate_item(item))
    return items
