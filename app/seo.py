"""Señales técnicas para que un buscador pueda rastrear e indexar PropMap."""

from __future__ import annotations

import html
import json
import os
import re
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
ORIGIN_TOKEN = "__ORIGIN__"
CITY_NAV_TOKEN = "__CITY_NAV__"
FALLBACK_ORIGIN = "https://propmap.com.ar"
OG_IMAGE = "/static/og.png"
_INDEX_ROBOTS = 'content="index, follow, max-image-preview:large"'
_FOLLOW_ROBOTS = 'content="index, follow"'
_NOINDEX_ROBOTS = 'content="noindex, follow"'
_MIN_TYPE = 4
_CACHE_SEC = 60.0
_rows_at = 0.0
_rows_memo: list | None = None
_KIND = {
    "casa": ("Casas", "Casas en venta"),
    "departamento": ("Departamentos", "Departamentos en venta"),
    "ph": ("PH y dúplex", "PH y dúplex en venta"),
    "terreno": ("Terrenos", "Terrenos en venta"),
    "local": ("Locales", "Locales en venta"),
    "oficina": ("Oficinas", "Oficinas en venta"),
    "galpon": ("Galpones", "Galpones en venta"),
}
_KIND_ORDER = tuple(_KIND)

_NOINDEX_EXACT = {"/px.gif", "/matomo.js", "/matomo.php"}
_NOINDEX_PREFIXES = (
    "/api",
    "/stats",
    "/tablero",
    "/flujo",
    "/admin",
    "/verificar",
    "/q/",
)


def noindex_path(path: str) -> bool:
    """Rutas que se pueden pedir, pero no son páginas para el índice."""
    if path in _NOINDEX_EXACT:
        return True
    return path.startswith(_NOINDEX_PREFIXES)


def wants_hsts(scope: dict) -> bool:
    proto = ""
    for key, value in scope.get("headers") or []:
        if key.lower() == b"x-forwarded-proto":
            proto = value.decode("latin-1").split(",")[0].strip().lower()
            break
    scheme = proto or str(scope.get("scheme") or "")
    return scheme == "https"


def origin_for(request) -> str:
    from .accounts import public_url

    parsed = urlparse((public_url(request) or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host or ".." in host:
        return FALLBACK_ORIGIN
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port}"


def render_html(filename: str, request, *, indexable: bool = True) -> str:
    text = (STATIC / filename).read_text(encoding="utf-8")
    text = text.replace(ORIGIN_TOKEN, origin_for(request))
    if not indexable:
        text = text.replace(_INDEX_ROBOTS, _NOINDEX_ROBOTS).replace(_FOLLOW_ROBOTS, _NOINDEX_ROBOTS)
    if CITY_NAV_TOKEN in text:
        text = text.replace(CITY_NAV_TOKEN, footer_nav())
    return text


def robots_body(request) -> str:
    origin = origin_for(request)
    return (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /stats\n"
        "Disallow: /stats/\n"
        "Disallow: /tablero\n"
        "Disallow: /flujo\n"
        "Disallow: /admin\n"
        "Disallow: /verificar\n"
        "\n"
        f"Sitemap: {origin}/sitemap.xml\n"
        f"Sitemap: {origin}/sitemap-cities.xml\n"
    )


def sitemap_xml(request) -> str:
    origin = origin_for(request)
    pages = (
        ("/", STATIC / "index.html", "weekly", "1.0"),
        ("/legal", STATIC / "legal.html", "monthly", "0.3"),
    )
    chunks = []
    for path, file, freq, priority in pages:
        stamp = datetime.fromtimestamp(file.stat().st_mtime, timezone.utc).date().isoformat()
        chunks.append(
            "  <url>\n"
            f"    <loc>{origin}{path}</loc>\n"
            f"    <lastmod>{stamp}</lastmod>\n"
            f"    <changefreq>{freq}</changefreq>\n"
            f"    <priority>{priority}</priority>\n"
            "  </url>"
        )
    body = "\n".join(chunks)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{body}\n"
        "</urlset>\n"
    )


def sitemap_cities_xml(request) -> str:
    origin = origin_for(request)
    chunks = []
    for row in iter_cities():
        chunks.append(_url_node(origin, f"/ciudad/{row['id']}", row["lastmod"], "weekly", "0.8"))
        for kind in row["types"]:
            chunks.append(
                _url_node(origin, f"/ciudad/{row['id']}/{kind}", row["lastmod"], "weekly", "0.6")
            )
    body = "\n".join(chunks)
    inner = f"\n{body}\n" if body else "\n"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{inner}</urlset>\n"
    )


def footer_nav() -> str:
    try:
        rows = iter_cities()
    except Exception:
        rows = []
    parts = ['<a href="/ciudades">Ciudades con avisos</a>']
    for row in rows[:8]:
        parts.append(f'<a href="/ciudad/{html.escape(row["id"], quote=True)}">{html.escape(row["label"])}</a>')
    return " · ".join(parts)


def cities_page(request) -> tuple[str, int, dict]:
    query = (request.url.query or "").strip()
    needle = ""
    if query:
        for part in query.split("&"):
            if part.startswith("q="):
                from urllib.parse import unquote_plus

                needle = unquote_plus(part.split("=", 1)[1]).strip()
                break
    rows = _filter_cities(iter_cities(), needle)
    indexable = not query
    origin = origin_for(request)
    title = "Ciudades con avisos en venta | PropMap"
    description = "Listado de ciudades con casas y departamentos en venta en PropMap, con enlace al mapa y a cada tipo."
    if needle:
        title = f"Ciudades: {needle} | PropMap"
        description = f"Ciudades de PropMap que coinciden con {needle}."
    body = _document(
        origin,
        title=title,
        description=description,
        canonical=f"{origin}/ciudades",
        indexable=indexable,
        crumbs=[("Mapa", "/"), ("Ciudades", "/ciudades")],
        main=_directory_main(rows, needle),
    )
    headers = {"Cache-Control": "no-store"}
    if not indexable:
        headers["X-Robots-Tag"] = "noindex, follow"
    return body, 200, headers


def city_page(request, slug: str, tipo: str | None = None) -> tuple[str, int, dict]:
    kind = _parse_kind(tipo)
    row = find_city(slug) if kind is not None else None
    if kind is None or row is None:
        return _missing(request), 404, {"Cache-Control": "no-store", "X-Robots-Tag": "noindex, follow"}
    if slug != row["id"]:
        dest = f"/ciudad/{row['id']}" + (f"/{kind}" if kind else "")
        return dest, 301, {}
    if kind and kind not in row["types"]:
        return _missing(request), 404, {"Cache-Control": "no-store", "X-Robots-Tag": "noindex, follow"}
    facts = city_facts(row, kind)
    origin = origin_for(request)
    path = f"/ciudad/{row['id']}" + (f"/{kind}" if kind else "")
    indexable = not request.url.query
    body = _document(
        origin,
        title=facts["title"],
        description=facts["description"],
        canonical=f"{origin}{path}",
        indexable=indexable,
        crumbs=facts["crumbs"],
        main=facts["html"],
        extra_ld=facts["item_list"],
    )
    headers = {"Cache-Control": "no-store"}
    if not indexable:
        headers["X-Robots-Tag"] = "noindex, follow"
    return body, 200, headers


def iter_cities() -> list[dict]:
    global _rows_at, _rows_memo
    testing = os.environ.get("PROPMAP_TEST") == "1"
    now = time.monotonic()
    if not testing and _rows_memo is not None and now - _rows_at < _CACHE_SEC:
        return _rows_memo
    rows = _load_cities()
    if not testing:
        _rows_memo = rows
        _rows_at = now
    return rows


def find_city(token: str) -> dict | None:
    slug = (token or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9-]{1,80}", slug):
        return None
    rows = iter_cities()
    for row in rows:
        if row["id"] == slug:
            return row
    for row in rows:
        if row.get("slug") and row["slug"] != row["id"] and row["slug"] == slug:
            return row
    return None


def city_facts(row: dict, kind: str = "") -> dict:
    listings = _listings_for(row["ids"])
    if kind:
        listings = [item for item in listings if item["property_type"] == kind]
    median = _median([item["price_m2"] for item in listings if _usable_m2(item["price_m2"])])
    barrios = _barrio_rows(listings)
    samples = _samples(listings)
    lead = _lead(row["types"] if not kind else [kind])
    where = _where(row)
    label = row["label"]
    title = f"{lead} en {label} | PropMap"
    description = _describe(lead, label, len(listings) if kind else row["n"], median)
    paragraphs = _paragraphs(row, listings, barrios, median, kind, lead)
    paragraphs = _pad_words(paragraphs, row, barrios, median, kind, lead)
    crumbs = [("Mapa", "/"), (label, f"/ciudad/{row['id']}")]
    if kind:
        crumbs.append((_KIND[kind][0], f"/ciudad/{row['id']}/{kind}"))
    return {
        "title": title,
        "description": description,
        "crumbs": crumbs,
        "html": _city_main(row, facts_n=row["n"] if not kind else len(listings), median=median, barrios=barrios, samples=samples, paragraphs=paragraphs, kind=kind, lead=lead, where=where),
        "item_list": _item_list(samples, row),
    }


def _url_node(origin: str, path: str, lastmod: str, freq: str, priority: str) -> str:
    loc = f"{origin}{path}"
    lines = [
        "  <url>",
        f"    <loc>{html.escape(loc)}</loc>",
        f"    <lastmod>{lastmod}</lastmod>",
        f"    <changefreq>{freq}</changefreq>",
        f"    <priority>{priority}</priority>",
        "  </url>",
    ]
    return "\n".join(lines)


def _load_cities() -> list[dict]:
    from .geo import _LOOSE_CITIES
    from .places import MIN_CATALOG_LISTINGS, _canonical_listed_id, is_cache_artifact_id
    from . import store

    try:
        store.init()
        with store.connect() as conn:
            fetched = conn.execute(
                """
                SELECT city, property_type, COUNT(*) AS n, MAX(scraped_at) AS seen
                FROM listings
                WHERE city IS NOT NULL AND city != ''
                GROUP BY city, property_type
                """
            ).fetchall()
            barrio_rows = conn.execute(
                """
                SELECT city, barrio, COUNT(*) AS n
                FROM listings
                WHERE city IS NOT NULL AND city != ''
                  AND barrio IS NOT NULL AND barrio != ''
                GROUP BY city, barrio
                """
            ).fetchall()
    except Exception:
        return []
    buckets: dict[str, dict] = {}
    for raw in fetched:
        city = str(raw["city"] or "").strip()
        if not city or is_cache_artifact_id(city):
            continue
        cid = _canonical_listed_id(city) or city
        if not cid or cid in _LOOSE_CITIES or is_cache_artifact_id(cid):
            continue
        bucket = buckets.setdefault(cid, {"n": 0, "types": {}, "ids": set(), "last": "", "barrios": {}})
        count = int(raw["n"] or 0)
        bucket["n"] += count
        bucket["ids"].add(city)
        bucket["ids"].add(cid)
        ptype = str(raw["property_type"] or "")
        if ptype in _KIND:
            bucket["types"][ptype] = bucket["types"].get(ptype, 0) + count
        seen = str(raw["seen"] or "")
        if seen > bucket["last"]:
            bucket["last"] = seen
    for raw in barrio_rows:
        city = str(raw["city"] or "").strip()
        name = str(raw["barrio"] or "").strip()
        if not city or not _barrio_ok(name) or is_cache_artifact_id(city):
            continue
        cid = _canonical_listed_id(city) or city
        bucket = buckets.get(cid)
        if not bucket:
            continue
        current = bucket["barrios"].get(name, 0)
        bucket["barrios"][name] = current + int(raw["n"] or 0)
    out = []
    for cid, bucket in buckets.items():
        if bucket["n"] < MIN_CATALOG_LISTINGS:
            continue
        types = [key for key in _KIND_ORDER if bucket["types"].get(key, 0) >= _MIN_TYPE]
        place = _place_row(cid)
        ranked = sorted(bucket["barrios"].items(), key=lambda item: (-item[1], item[0].casefold()))
        place.update(
            {
                "n": bucket["n"],
                "types": types,
                "ids": sorted(bucket["ids"]),
                "lastmod": _day(bucket["last"]),
                "barrio_links": [
                    {"name": name, "anchor": _anchor(name), "n": count} for name, count in ranked[:5]
                ],
            }
        )
        out.append(place)
    out.sort(key=lambda row: (row["label"].casefold(), row["id"]))
    return out


def _place_row(cid: str) -> dict:
    from .geo import CITIES
    from .places import public_place

    if cid in CITIES:
        raw = public_place(cid)
        return {
            "id": cid,
            "label": str(raw.get("label") or cid),
            "slug": str(raw.get("slug") or cid),
            "province_label": str(raw.get("province_label") or ""),
        }
    label = re.sub(r"[-_]+", " ", cid).strip().title() or cid
    return {"id": cid, "label": label, "slug": cid, "province_label": ""}


def _listings_for(ids: list[str]) -> list[dict]:
    from . import store

    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    try:
        with store.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, title, barrio, property_type, price_usd, price_m2, score
                FROM listings
                WHERE city IN ({marks})
                """,
                tuple(ids),
            ).fetchall()
    except Exception:
        return []
    out = []
    for row in rows:
        out.append(
            {
                "id": str(row["id"] or ""),
                "title": str(row["title"] or "").strip(),
                "barrio": str(row["barrio"] or "").strip(),
                "property_type": str(row["property_type"] or ""),
                "price_usd": row["price_usd"],
                "price_m2": row["price_m2"],
                "score": row["score"],
            }
        )
    return out


def _median(values: list[float]) -> int | None:
    clean = sorted(float(v) for v in values if v)
    if len(clean) < 5:
        return None
    lo = int(len(clean) * 0.15)
    hi = int(len(clean) * 0.85) or len(clean)
    cut = clean[lo:hi] or clean
    return int(round(statistics.median(cut)))


def _usable_m2(value) -> bool:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return False
    return 20 <= n <= 8000


def _barrio_ok(name: str) -> bool:
    from .geo import fold

    text = (name or "").strip()
    if not text:
        return False
    return fold(text) not in {"sin clasificar", "sin barrio", "s/d", "-"}


def _barrio_rows(listings: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for item in listings:
        name = item["barrio"]
        if not _barrio_ok(name):
            continue
        groups.setdefault(name, []).append(item)
    rows = []
    for name, items in groups.items():
        rows.append(
            {
                "name": name,
                "anchor": _anchor(name),
                "n": len(items),
                "median": _median([item["price_m2"] for item in items if _usable_m2(item["price_m2"])]),
            }
        )
    rows.sort(key=lambda row: (-row["n"], row["name"].casefold()))
    return rows[:6]


def _samples(listings: list[dict]) -> list[dict]:
    named = [item for item in listings if item["title"]]
    named.sort(key=lambda item: (-(float(item["score"]) if item["score"] is not None else -1), item["title"]))
    return named[:8]


def _lead(types: list[str]) -> str:
    if "casa" in types and "departamento" in types:
        return "Casas y departamentos en venta"
    if types:
        return _KIND[types[0]][1]
    return "Inmuebles en venta"


def _where(row: dict) -> str:
    from .geo import fold

    prov = (row.get("province_label") or "").strip()
    label = row["label"]
    if prov and fold(prov) != fold(label):
        return f"{label}, {prov}"
    return label


def _describe(lead: str, label: str, n: int, median: int | None) -> str:
    if median:
        return (
            f"{lead} en {label}: mediana {median} USD/m² sobre {n} avisos. "
            "Compará el precio por metro y el score de ganga en el mapa."
        )
    return (
        f"{lead} en {label}: {n} avisos publicados en el mapa. "
        "Compará el USD/m² de cada uno y el score de ganga con la zona."
    )


def _paragraphs(row, listings, barrios, median, kind, lead) -> list[str]:
    label = row["label"]
    where = _where(row)
    n = len(listings) if kind else row["n"]
    opening = f"En {where} hay {n} avisos de inmuebles en venta reunidos en PropMap. "
    if median:
        opening += f"La mediana del metro usable, recortando los extremos, es {_money(median)} USD/m². "
    opening += "Esa cifra sale de avisos ya publicados. No es una tasación de una propiedad concreta."
    type_bits = []
    counts = {}
    for item in listings:
        ptype = item["property_type"]
        if ptype in _KIND:
            counts[ptype] = counts.get(ptype, 0) + 1
    for key in _KIND_ORDER:
        count = counts.get(key, 0)
        if count:
            chunk = f"{_KIND[key][0].lower()} ({count})"
            med = _median([item["price_m2"] for item in listings if item["property_type"] == key and _usable_m2(item["price_m2"])])
            if med:
                chunk += f", mediana {_money(med)} USD/m²"
            type_bits.append(chunk)
    typed = ""
    if type_bits:
        typed = f"El reparto en {label} queda así: {', '.join(type_bits)}. "
    typed += (
        "Cada aviso del mapa muestra el precio en dólares, los metros y un score de ganga. "
        f"Un score alto quiere decir que el USD/m² está por debajo de otros avisos del mismo tipo y un tamaño parecido en {label}."
    )
    if barrios:
        names = ", ".join(item["name"] for item in barrios[:5])
        top = barrios[0]
        zones = f"Los barrios con más avisos publicados son {names}. "
        if top["median"]:
            zones += f"En {top['name']} la mediana ronda {_money(top['median'])} USD/m² sobre {top['n']} avisos. "
        else:
            zones += f"{top['name']} concentra {top['n']} de esas publicaciones. "
        zones += "Ese orden no mide plusvalía ni calidad del barrio: solo dice dónde hay más texto publicado para comparar."
    else:
        zones = (
            f"En {label} la mayoría de los avisos todavía no trae un barrio reconocible. "
            "La mediana se calcula igual con el conjunto de la ciudad, y el mapa sigue sirviendo para abrir cada ficha."
        )
    score = (
        "El score compara el USD/m² de un aviso con otros del mismo tipo y un tamaño parecido en esta ciudad. "
        "No es una tasación, ni un consejo de compra, ni un ingreso de alquiler. "
        "Si el metro cuadrado es imposible para ese tipo, el aviso queda como dato raro y no entra en la mediana. "
        "Los precios pueden estar viejos o mal cargados en el portal de origen: antes de decidir, abrí el aviso en su sitio."
    )
    kind_line = ""
    if kind:
        kind_line = (
            f"Esta página deja solo {_KIND[kind][0].lower()} en {label}. "
            f"El resto de los tipos de {label} está en la página de la ciudad, para no mezclar un lote con un departamento al mirar el metro."
        )
    guide = (
        f"Desde acá se abre el mapa de {label} ya ubicado, se puede bajar a un barrio o quedarse en un tipo. "
        "Esas vistas del mapa llevan parámetros en la dirección y no se indexan: la página estable para compartir es esta. "
        "PropMap es un visor informativo. No es una inmobiliaria y no intermedia la operación."
    )
    return [p for p in (opening, typed, zones, score, kind_line, guide) if p]


def _pad_words(paragraphs, row, barrios, median, kind, lead) -> list[str]:
    if _word_count(" ".join(paragraphs)) >= 200:
        return paragraphs
    label = row["label"]
    extra = (
        f"Para leer {label} conviene mirar tres cosas juntas: cuántos avisos hay, "
        f"la mediana de USD/m²"
        + (f" ({_money(median)})" if median else "")
        + " y el barrio donde se concentra la publicación. "
        f"{lead} en {label} no promete que un aviso esté barato en términos absolutos: "
        "solo lo ubica contra los demás que el mapa ya juntó en la misma ciudad. "
        "Si un barrio tiene pocos avisos, su mediana se mueve con cada alta o baja y hay que tomarla como una foto del día."
    )
    if barrios:
        extra += " Los enlaces de cada barrio vuelven a esta ciudad y, desde ahí, a cada tipo con suficiente muestra."
    paragraphs.append(extra)
    return paragraphs


def _word_count(text: str) -> int:
    return len(re.findall(r"[0-9A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+", text))


def _city_main(row, *, facts_n, median, barrios, samples, paragraphs, kind, lead, where) -> str:
    cid = row["id"]
    label = html.escape(row["label"])
    figure = ""
    if median:
        figure = (
            f'<p class="city-figure"><span class="city-figure-num">{_money(median)}</span>'
            f'<span class="city-figure-unit">USD/m² mediana en {label}</span></p>'
        )
    score = next((p for p in paragraphs if p.startswith("El score compara")), paragraphs[-1])
    prose = "".join(f"<p>{html.escape(p)}</p>" for p in paragraphs if p is not score)
    types = _type_nav(row, kind)
    barrio_html = _barrio_html(row, barrios, kind)
    items = _sample_html(samples, cid)
    others = _other_cities(cid)
    h1 = html.escape(f"{lead} en {row['label']}")
    kicker = html.escape(where)
    map_href = f"/?ciudad={quote(cid)}" + (f"&tipo={quote(kind)}" if kind else "")
    return f"""
    <p class="eyebrow">{kicker} · {facts_n} avisos</p>
    <h1>{h1}</h1>
    {figure}
    <p class="city-jump"><a href="{html.escape(map_href, quote=True)}">Abrir {label} en el mapa</a></p>
    {types}
    {prose}
    {barrio_html}
    <h2>Cómo se calcula el score</h2>
    <p>{html.escape(score)}</p>
    {items}
    {others}
    """


def _type_nav(row, kind: str) -> str:
    links = [f'<a href="/ciudad/{html.escape(row["id"], quote=True)}">Todos los tipos</a>']
    for key in row["types"]:
        href = f"/ciudad/{row['id']}/{key}"
        mark = ' aria-current="page"' if key == kind else ""
        links.append(f'<a href="{href}"{mark}>{html.escape(_KIND[key][0])}</a>')
    return f'<nav class="city-nav" aria-label="Tipos en {html.escape(row["label"])}">{"".join(links)}</nav>'


def _barrio_html(row, barrios, kind: str) -> str:
    if not barrios:
        return ""
    cid = row["id"]
    blocks = []
    for barrio in barrios:
        name = html.escape(barrio["name"])
        bits = [f"{barrio['n']} avisos"]
        if barrio["median"]:
            bits.append(f"mediana {_money(barrio['median'])} USD/m²")
        links = []
        if kind:
            links.append(
                f'<a href="/ciudad/{cid}">Todos los tipos en {html.escape(row["label"])}</a>'
            )
        href = "/?ciudad=" + quote(cid) + "&barrio=" + quote(barrio["name"])
        if kind:
            href += "&tipo=" + quote(kind)
        map_name = _KIND[kind][0] if kind else "Avisos"
        links.append(
            f'<a href="{html.escape(href, quote=True)}">{html.escape(map_name)} de {name} en el mapa</a>'
        )
        blocks.append(
            f'<section id="barrio-{html.escape(barrio["anchor"], quote=True)}">'
            f"<h3>{name}</h3><p>{html.escape('. '.join(bits))}.</p>"
            f'<p class="city-nav">{"".join(links)}</p></section>'
        )
    return "<h2>Barrios</h2>" + "".join(blocks)


def _sample_html(samples, cid: str) -> str:
    if not samples:
        return ""
    items = []
    for item in samples:
        dom = _dom_id(item["id"])
        title = html.escape(item["title"])
        meta = []
        if _barrio_ok(item["barrio"]):
            meta.append(item["barrio"])
        if item["price_usd"]:
            meta.append(f"USD {_money(item['price_usd'])}")
        if _usable_m2(item["price_m2"]):
            meta.append(f"{_money(item['price_m2'])} USD/m²")
        detail = html.escape(" · ".join(meta))
        href = f"/?ciudad={quote(cid)}&aviso={quote(item['id'])}"
        items.append(
            f'<li id="{dom}"><a href="{html.escape(href, quote=True)}">{title}</a>'
            + (f"<span>{detail}</span>" if detail else "")
            + "</li>"
        )
    return '<h2>Avisos para comparar</h2><ol class="city-list">' + "".join(items) + "</ol>"


def _other_cities(current: str) -> str:
    rows = [row for row in iter_cities() if row["id"] != current][:12]
    if not rows:
        return ""
    links = "".join(
        f'<a href="/ciudad/{html.escape(row["id"], quote=True)}">{html.escape(row["label"])}</a>' for row in rows
    )
    return f'<h2>Otras ciudades</h2><nav class="city-nav" aria-label="Otras ciudades">{links}</nav>'


def _item_list(samples, row) -> dict | None:
    if not samples:
        return None
    elements = []
    for index, item in enumerate(samples, start=1):
        node = {
            "@type": "ListItem",
            "position": index,
            "name": item["title"],
                "url": f"#{_dom_id(item['id'])}",
            "item": {
                "@type": "RealEstateListing",
                "name": item["title"],
                "address": {
                    "@type": "PostalAddress",
                    "addressLocality": row["label"],
                    "addressCountry": "AR",
                },
            },
        }
        if row.get("province_label"):
            node["item"]["address"]["addressRegion"] = row["province_label"]
        if item["price_usd"]:
            node["item"]["offers"] = {
                "@type": "Offer",
                "priceCurrency": "USD",
                "price": int(round(float(item["price_usd"]))),
            }
        elements.append(node)
    return {"@type": "ItemList", "name": f"Avisos en {row['label']}", "itemListElement": elements}


def _directory_main(rows: list[dict], needle: str) -> str:
    if needle and not rows:
        return (
            '<p class="eyebrow">Búsqueda</p>'
            f"<h1>Ninguna ciudad coincide con {html.escape(needle)}</h1>"
            "<p>Probá con el nombre de la localidad, sin la provincia. "
            '<a href="/ciudades">Ver todas las ciudades</a>.</p>'
        )
    heading = "Ciudades con avisos en venta" if not needle else f"Ciudades que coinciden con {needle}"
    items = []
    for row in rows:
        types = "".join(
            f'<li><a href="/ciudad/{html.escape(row["id"], quote=True)}/{key}">{html.escape(_KIND[key][0])}</a></li>'
            for key in row["types"]
        )
        barrios = "".join(
            f'<li><a href="/ciudad/{html.escape(row["id"], quote=True)}#barrio-{html.escape(barrio["anchor"], quote=True)}">{html.escape(barrio["name"])}</a></li>'
            for barrio in row.get("barrio_links") or []
        )
        items.append(
            "<li>"
            f'<a href="/ciudad/{html.escape(row["id"], quote=True)}">{html.escape(row["label"])}</a>'
            f' <span>{row["n"]} avisos</span>'
            + (f"<ul>{types}</ul>" if types else "")
            + (f"<ul>{barrios}</ul>" if barrios else "")
            + "</li>"
        )
    intro = (
        "<p>Cada ciudad tiene una página propia, con la mediana de USD/m², los barrios con más avisos "
        "y un enlace al mapa. Los tipos con pocos avisos no tienen página aparte.</p>"
    )
    listing = "<ul class=\"seo-dir\">" + "".join(items) + "</ul>" if items else "<p>Todavía no hay una ciudad con suficientes avisos publicados.</p>"
    return f'<p class="eyebrow">Argentina</p><h1>{html.escape(heading)}</h1>{intro}{listing}'


def _filter_cities(rows: list[dict], needle: str) -> list[dict]:
    from .geo import fold

    if not needle:
        return rows
    token = fold(needle)
    return [
        row
        for row in rows
        if token in fold(row["label"]) or token in fold(row["id"]) or token in fold(row.get("province_label") or "")
    ]


def _document(origin, *, title, description, canonical, indexable, crumbs, main, extra_ld=None) -> str:
    robots = "index, follow, max-image-preview:large" if indexable else "noindex, follow"
    graph = [
        {
            "@type": "WebPage",
            "@id": canonical,
            "name": title,
            "url": canonical,
            "inLanguage": "es-AR",
            "description": description,
            "isPartOf": {"@id": f"{origin}/#website"},
        },
        _crumbs_ld(origin, crumbs),
    ]
    if extra_ld:
        fixed = _absolutize_items(extra_ld, canonical)
        graph.append(fixed)
    payload = json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False).replace("<", "\\u003c")
    crumb_nav = " · ".join(
        f'<a href="{html.escape(origin + path if path.startswith("/") else path, quote=True)}">{html.escape(name)}</a>'
        for name, path in crumbs
    )
    return f"""<!DOCTYPE html>
<html lang="es-AR">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta name="theme-color" content="#2f5346" />
  <title>{html.escape(title)}</title>
  <meta name="description" content="{html.escape(description, quote=True)}" />
  <meta name="robots" content="{robots}" />
  <link rel="canonical" href="{html.escape(canonical, quote=True)}" />
  <link rel="icon" href="/static/favicon.svg" type="image/svg+xml" />
  <meta property="og:type" content="website" />
  <meta property="og:locale" content="es_AR" />
  <meta property="og:site_name" content="PropMap" />
  <meta property="og:title" content="{html.escape(title, quote=True)}" />
  <meta property="og:description" content="{html.escape(description, quote=True)}" />
  <meta property="og:url" content="{html.escape(canonical, quote=True)}" />
  <meta property="og:image" content="{origin}{OG_IMAGE}" />
  <meta property="og:image:width" content="1200" />
  <meta property="og:image:height" content="630" />
  <meta property="og:image:alt" content="PropMap, mapa de casas en venta con score de ganga" />
  <meta name="twitter:card" content="summary_large_image" />
  <meta name="twitter:title" content="{html.escape(title, quote=True)}" />
  <meta name="twitter:description" content="{html.escape(description, quote=True)}" />
  <meta name="twitter:image" content="{origin}{OG_IMAGE}" />
  <script type="application/ld+json">
  {payload}
  </script>
  <link rel="stylesheet" href="/static/styles.css?v=ui38" />
</head>
<body class="legal-page city-page">
  <a class="skip-link" href="#cityMain">Ir al contenido</a>
  <header class="legal-head">
    <a class="logo" href="/">
      <span class="logo-mark" aria-hidden="true"></span>
      <span class="logo-word">PROPMAP</span>
    </a>
    <nav class="city-nav" aria-label="Sitio">
      <a href="/">Mapa</a>
      <a href="/ciudades">Ciudades</a>
      <a href="/legal">Términos y privacidad</a>
    </nav>
  </header>
  <main id="cityMain" class="legal-doc">
    <nav class="legal-toc" aria-label="Miga de pan">{crumb_nav}</nav>
    {main}
  </main>
  <footer class="legal-bar">
    <p>© 2026 PropMap. Los avisos pertenecen a sus titulares. Uso informativo, no es una tasación ni una inmobiliaria.</p>
    <p>{footer_nav()} · <a href="/legal">Términos y privacidad</a></p>
  </footer>
</body>
</html>
"""


def _crumbs_ld(origin: str, crumbs: list[tuple[str, str]]) -> dict:
    elements = []
    for index, (name, path) in enumerate(crumbs, start=1):
        elements.append(
            {
                "@type": "ListItem",
                "position": index,
                "name": name,
                "item": origin + path if path.startswith("/") else path,
            }
        )
    return {"@type": "BreadcrumbList", "itemListElement": elements}


def _absolutize_items(node: dict, canonical: str) -> dict:
    copied = json.loads(json.dumps(node))
    for element in copied.get("itemListElement") or []:
        url = str(element.get("url") or "")
        if url.startswith("#"):
            element["url"] = canonical + url
            item = element.get("item") or {}
            item["url"] = canonical + url
            element["item"] = item
    return copied


def _missing(request) -> str:
    origin = origin_for(request)
    return _document(
        origin,
        title="Ciudad no encontrada | PropMap",
        description="Esa ciudad no tiene una página pública en PropMap.",
        canonical=f"{origin}/ciudades",
        indexable=False,
        crumbs=[("Mapa", "/"), ("Ciudades", "/ciudades")],
        main=(
            "<h1>Esa ciudad no tiene página</h1>"
            "<p>Puede que el nombre no coincida con una localidad que ya tenga avisos suficientes. "
            '<a href="/ciudades">Ver las ciudades publicadas</a> o <a href="/">volver al mapa</a>.</p>'
        ),
    )


def _parse_kind(tipo: str | None) -> str | None:
    if tipo is None or tipo == "":
        return ""
    key = tipo.strip().lower()
    if key not in _KIND:
        return None
    return key


def _day(raw: str) -> str:
    text = (raw or "")[:10]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    return datetime.now(timezone.utc).date().isoformat()


def _money(value) -> str:
    return f"{int(round(float(value))):,}".replace(",", ".")


def _anchor(name: str) -> str:
    from .geo import fold

    base = fold(name).replace(" ", "-")
    base = re.sub(r"[^a-z0-9-]+", "-", base).strip("-")
    return base or "barrio"


def _dom_id(raw: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (raw or "").lower()).strip("-")
    return f"aviso-{base or 'item'}"

