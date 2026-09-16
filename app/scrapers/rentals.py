from __future__ import annotations

from .. import crawl
from ..geo import fold
from ..models import Listing
from ..yields import infer_period, rent_to_usd
from .airbnb import scrape_airbnb
from .urls import argenprop_rental_urls, mercadolibre_rental_urls, properati_rental_urls, zonaprop_rental_paths


def scrape_rentals(city: str, progress=lambda _m: None, should_stop=None, usd_ars: float = 0) -> int:
    from .. import store
    from .argenprop import _page as ap_page
    from .mercadolibre import _page as ml_page
    from .properati import _page as pr_page
    from .zonaprop import _page as zp_page

    rows: list[dict] = []
    jobs = [
        (zonaprop_rental_paths(city), zp_page),
        (argenprop_rental_urls(city), ap_page),
        (properati_rental_urls(city), pr_page),
        (mercadolibre_rental_urls(city), ml_page),
    ]
    progress("Bajando alquileres para estimar la renta…")
    with crawl.without_pace():
        for targets, pager in jobs:
            if should_stop and should_stop():
                break
            for target, ptype, hinted in targets:
                if should_stop and should_stop():
                    break
                chunk = _pages(
                    lambda page, t=target, p=ptype, c=city, fn=pager: fn(t, p, page, c),
                    max_pages=8 if hinted == "monthly" else 2,
                    should_stop=should_stop,
                )
                for item in chunk:
                    row = _from_listing(item, city, hinted, usd_ars)
                    if row:
                        rows.append(row)
        progress("Bajando alquileres temporales…")
        try:
            rows.extend(scrape_airbnb(city, usd_ars, should_stop=should_stop))
        except Exception:
            pass
    unique = {}
    for row in rows:
        unique[row["id"]] = row
    store.replace_city_rentals(city, list(unique.values()))
    monthly = sum(1 for row in unique.values() if row.get("period") == "monthly")
    nightly = sum(1 for row in unique.values() if row.get("period") == "nightly")
    progress(f"Renta: {monthly} alquileres mensuales y {nightly} temporales para comparar.")
    return len(unique)


def _pages(fetch_page, max_pages: int, should_stop=None) -> list:
    items = []
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        if should_stop and should_stop():
            break
        try:
            chunk = fetch_page(page) or []
        except Exception:
            break
        fresh = [item for item in chunk if item.source_id not in seen]
        if not fresh:
            break
        for item in fresh:
            seen.add(item.source_id)
        items.extend(fresh)
        if len(chunk) < 6:
            break
    return items


def _from_listing(item: Listing, city: str, hinted: str, usd_ars: float) -> dict | None:
    if item.property_type == "terreno":
        return None
    price_usd = rent_to_usd(item.price, item.currency, usd_ars)
    if not price_usd:
        return None
    period = infer_period(price_usd, item.title, item.description, hinted)
    blob = fold(f"{item.title} {item.url} {item.description}")
    if "tempor" in blob and period == "monthly" and price_usd <= 280:
        period = "nightly"
    return {
        "id": f"rent:{item.source}:{item.source_id}",
        "source": item.source,
        "source_id": item.source_id,
        "url": item.url,
        "title": item.title,
        "property_type": item.property_type or "departamento",
        "price": item.price,
        "currency": item.currency,
        "price_usd": price_usd,
        "period": period,
        "address": item.address,
        "barrio": item.barrio,
        "zona": item.zona,
        "lat": item.lat,
        "lon": item.lon,
        "covered_m2": item.covered_m2,
        "bedrooms": item.bedrooms,
        "city": city or item.city,
        "extra": {"hinted": hinted},
    }
