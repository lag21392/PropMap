from __future__ import annotations

import re
from typing import Callable

from lxml import html as lhtml

from ..geo import locate
from ..http_client import fetch_text
from ..models import Listing

Progress = Callable[[str], None]


def parse_number(text: str | None) -> float | None:
    if not text:
        return None
    raw = str(text)
    raw = raw.replace("\xa0", " ").replace("U$S", "").replace("US$", "").replace("USD", "")
    raw = raw.replace("$", "")
    match = re.search(r"(\d[\d\.\s]*\d|\d)", raw)
    if not match:
        return None
    token = match.group(1).replace(" ", "")
    if token.count(".") > 1:
        token = token.replace(".", "")
    elif token.count(",") == 1 and token.count(".") == 0:
        token = token.replace(",", ".")
    elif "," in token and "." in token:
        token = token.replace(".", "").replace(",", ".")
    else:
        token = token.replace(".", "")
    try:
        value = float(token)
    except ValueError:
        return None
    return value if value > 0 else None


def first_int(text: str, pattern: str) -> int | None:
    match = re.search(pattern, text, re.I)
    if not match:
        return None
    try:
        return int(float(match.group(1).replace(",", ".")))
    except ValueError:
        return None


def first_float(text: str, pattern: str) -> float | None:
    match = re.search(pattern, text, re.I)
    if not match:
        return None
    try:
        return float(match.group(1).replace(".", "").replace(",", ".")) if match.group(1).count(".") > 1 else float(match.group(1).replace(",", "."))
    except ValueError:
        return None


def detect_type(text: str, fallback: str) -> str:
    t = (text or "").lower()
    if "departamento" in t or "depto" in t or "apartamento" in t or "monoambiente" in t:
        return "departamento"
    if re.search(r"\bph\b", t) or "duplex" in t or "dúplex" in t or "triplex" in t:
        return "ph"
    if "casa" in t or "chalet" in t or "quinta" in t or "multifamiliar" in t:
        return "casa"
    if fallback == "terreno" or "terreno" in t or re.search(r"\blote\b", t) or "hectarea" in t or "hectárea" in t:
        return "terreno"
    return fallback


def paginate(fetch_page, max_pages: int | None = None, should_stop=None, on_chunk=None) -> list:
    from .. import freshness
    from ..crawl import list_page_limit

    items = []
    seen: set[str] = set()
    limit = max_pages if max_pages is not None else list_page_limit()
    for page in range(1, limit + 1):
        if should_stop and should_stop():
            break
        chunk = fetch_page(page)
        if not chunk:
            break
        fresh = [x for x in chunk if x.source_id not in seen]
        if not fresh:
            break
        unknown = [x for x in fresh if not freshness.is_known(x.id)]
        for x in fresh:
            seen.add(x.source_id)
        items.extend(fresh)
        if on_chunk:
            on_chunk(fresh)
        freshness.note_ids(x.id for x in fresh)
        if len(chunk) < 8:
            break
        if not unknown:
            break
    return items


def tree(url: str):
    return lhtml.fromstring(fetch_text(url))


def locate_item(item: Listing) -> Listing:
    if item.price is not None and item.price <= 200:
        item.price = None
    barrio, zona, lat, lon, exact = locate(
        item.id,
        item.lat,
        item.lon,
        item.title,
        item.address,
        item.description,
        item.publisher,
        city=item.city or "puerto-madryn",
    )
    item.barrio, item.zona, item.lat, item.lon = barrio, zona, lat, lon
    item.has_exact_location = exact
    return item


first_int = first_int
detect_type = detect_type
parse_number = parse_number
locate_item = locate_item
