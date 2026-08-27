from __future__ import annotations

import json
import re

from ..geo import CITIES
from ..http_client import fetch_text
from ..yields import rent_to_usd
from . import parse_number


def scrape_airbnb(city: str, usd_ars: float, should_stop=None) -> list[dict]:
    if should_stop and should_stop():
        return []
    cfg = CITIES.get(city) or {}
    label = (cfg.get("label") or city).replace(" ", "-")
    province = str(cfg.get("province") or "").replace("-", " ").title()
    if (cfg.get("province") or "") == "capital-federal":
        slug = "Buenos-Aires--Capital-Federal--Argentina"
    elif province:
        slug = f"{label}--{province}--Argentina"
    else:
        slug = f"{label}--Argentina"
    url = f"https://www.airbnb.com.ar/s/{slug}/homes"
    try:
        html = fetch_text(url, timeout=25, retries=2)
    except Exception:
        return []
    rows = _from_next_data(html, city, usd_ars)
    if not rows:
        rows = _from_prices(html, city, usd_ars)
    return rows


def _from_next_data(html: str, city: str, usd_ars: float) -> list[dict]:
    marker = 'id="data-deferred-state-0"'
    idx = html.find(marker)
    blob = ""
    if idx >= 0:
        start = html.find(">", idx)
        end = html.find("</script>", start)
        blob = html[start + 1 : end] if start > 0 and end > start else ""
    if not blob:
        match = re.search(r'<script id="data-deferred-state[^"]*"[^>]*>(\{.*?})</script>', html)
        blob = match.group(1) if match else ""
    if not blob:
        return []
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return []
    found: list[dict] = []
    _walk(data, found)
    rows = []
    seen = set()
    for item in found:
        sid = str(item.get("id") or item.get("listingId") or "")
        if not sid or sid in seen:
            continue
        price = _stay_price(item)
        if not price:
            continue
        price_usd = rent_to_usd(price, item.get("currency") or "USD", usd_ars)
        if not price_usd or not (18 <= price_usd <= 400):
            continue
        seen.add(sid)
        title = item.get("name") or item.get("title") or "Alquiler temporal"
        rows.append(
            {
                "id": f"rent:airbnb:{sid}",
                "source": "airbnb",
                "source_id": sid,
                "url": f"https://www.airbnb.com.ar/rooms/{sid}",
                "title": str(title)[:180],
                "property_type": "departamento",
                "price": price,
                "currency": item.get("currency") or "USD",
                "price_usd": price_usd,
                "period": "nightly",
                "address": item.get("city") or "",
                "barrio": "",
                "zona": "",
                "lat": None,
                "lon": None,
                "covered_m2": None,
                "bedrooms": item.get("bedrooms") or item.get("beds"),
                "city": city,
                "extra": {"kind": "temporal"},
            }
        )
    return rows


def _walk(node, found: list) -> None:
    if isinstance(node, dict):
        if "listingId" in node or (node.get("__typename") in {"StaySearchResult", "StayListing"} and node.get("id")):
            found.append(node)
        if "pricingQuote" in node and (node.get("id") or node.get("listingId")):
            found.append(node)
        for value in node.values():
            _walk(value, found)
    elif isinstance(node, list):
        for value in node:
            _walk(value, found)


def _stay_price(item: dict) -> float | None:
    quote = item.get("pricingQuote") or item.get("price") or {}
    if isinstance(quote, dict):
        for key in ("structuredStayDisplayPrice", "rate", "price", "amount"):
            value = quote.get(key)
            if isinstance(value, dict):
                amount = value.get("amount") or value.get("price") or value.get("total")
                if amount:
                    return parse_number(str(amount))
            if isinstance(value, (int, float)):
                return float(value)
        text = quote.get("priceString") or quote.get("accessibilityLabel") or ""
        return parse_number(str(text))
    return parse_number(str(quote)) if quote else None


def _from_prices(html: str, city: str, usd_ars: float) -> list[dict]:
    texts = re.findall(r"(?:USD|US\$|U\$S|\$)\s*([\d\.]+)", html)
    nights = []
    for raw in texts:
        value = parse_number(raw)
        usd = rent_to_usd(value, "USD" if "USD" in html[max(0, html.find(raw) - 6): html.find(raw) + 8] else "ARS", usd_ars)
        if usd and 18 <= usd <= 280:
            nights.append(usd)
    rows = []
    for idx, price_usd in enumerate(nights[:24]):
        rows.append(
            {
                "id": f"rent:airbnb:sample-{city}-{idx}",
                "source": "airbnb",
                "source_id": f"{city}-{idx}",
                "url": "",
                "title": "Alquiler temporal",
                "property_type": "departamento",
                "price": price_usd,
                "currency": "USD",
                "price_usd": price_usd,
                "period": "nightly",
                "address": "",
                "barrio": "",
                "zona": "",
                "lat": None,
                "lon": None,
                "covered_m2": None,
                "bedrooms": None,
                "city": city,
                "extra": {"kind": "temporal", "sample": True},
            }
        )
    return rows
