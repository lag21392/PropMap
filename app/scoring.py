from __future__ import annotations

import math
import re
import statistics
from collections import defaultdict

from html import unescape

from .geo import approx_slot, fingerprint, fold, infer_barrio
from .models import Listing
from .features import analyze, fill_areas, scan_red_flags

USD_FALLBACK = 1350.0
SCORE_VERSION = "10"
PLACEHOLDER_USD = 200
PRICE_BOUNDS = {
    "departamento": (8_000, 8_000_000),
    "ph": (10_000, 2_500_000),
    "casa": (12_000, 5_000_000),
    "terreno": (1_500, 12_000_000),
}
UNREAL_M2 = {
    "terreno": (8.0, 1_800.0),
    "departamento": (180.0, 12_000.0),
    "ph": (140.0, 8_000.0),
    "casa": (35.0, 5_000.0),
}
USD_IN_TEXT = re.compile(r"(?:u\$s|us\$|usd)\s*([\d\.]{3,12})", re.I)
CATALOG_HINTS = (
    "lotes en venta",
    "lotes desde",
    "loteo ",
    "anticipos desde",
    "anticipo desde",
    "planes de hasta",
    "cuotas en dolares",
    "cuotas en dólares",
    "consultanos por el plano",
    "disponibilidad actualizada",
    "lotes residenciales y comerciales",
    "opciones residenciales y comerciales",
    "dos desarrollos",
)
CATALOG_PRICE = re.compile(
    r"(lotes?\s+desde|desde\s+(?:usd|u\$s|us\$)|precios?\s+desde)",
    re.I,
)


def to_usd(price: float | None, currency: str, usd_ars: float) -> float | None:
    if price is None or price <= 0:
        return None
    cur = (currency or "USD").upper()
    if cur in {"USD", "U$S", "US$"}:
        return float(price)
    if cur in {"ARS", "ARS$", "$"}:
        # En venta en Madryn, "$ 90.000" casi siempre es dólares, no pesos.
        if 8_000 <= price <= 900_000:
            return float(price)
        if usd_ars <= 0:
            usd_ars = USD_FALLBACK
        return float(price) / usd_ars
    return float(price)


def _clean_text(text: str | None) -> str:
    cur = text or ""
    for _ in range(3):
        nxt = unescape(cur)
        if nxt == cur:
            break
        cur = nxt
    return cur.replace("\xa0", " ").replace("  ", " ").strip()


def is_catalog_ad(item: Listing) -> bool:
    blob = fold(f"{item.title or ''} {item.description or ''} {item.address or ''}")
    if any(hint in blob for hint in CATALOG_HINTS):
        return True
    if CATALOG_PRICE.search(blob) and item.property_type == "terreno":
        return True
    if blob.count("lote") >= 3 and ("financi" in blob or "cuota" in blob):
        return True
    return False


def unrealistic_unit_price(item: Listing) -> bool:
    if not item.price_m2:
        return False
    lo, hi = UNREAL_M2.get(item.property_type, (20.0, 8_000.0))
    return item.price_m2 < lo or item.price_m2 > hi


def excluded_from_comps(item: Listing) -> bool:
    return (
        bool((item.extra or {}).get("exclude_from_comps"))
        or is_catalog_ad(item)
        or unrealistic_unit_price(item)
    )


def _note_fix(item: Listing, message: str) -> None:
    extra = dict(item.extra or {})
    fixes = list(extra.get("data_fixes") or [])
    if message not in fixes:
        fixes.append(message)
    extra["data_fixes"] = fixes
    item.extra = extra


def _price_from_text(item: Listing) -> tuple[float, str] | None:
    from .scrapers import parse_number

    blob = f"{item.title or ''} {item.description or ''} {(item.extra or {}).get('pdf_text') or ''}"
    found: list[float] = []
    for match in USD_IN_TEXT.finditer(blob):
        value = parse_number(match.group(1))
        if value and value >= 3_000:
            found.append(value)
    if not found:
        return None
    return max(found), "USD"


def sanitize_item(item: Listing, usd_ars: float) -> Listing:
    from .scrapers import detect_type

    extra = dict(item.extra or {})
    extra["data_fixes"] = []
    item.extra = extra
    item.title = _clean_text(item.title)
    item.description = _clean_text(item.description)
    item.address = _clean_text(item.address)
    guessed = detect_type(f"{item.title or ''} {item.description or ''}", item.property_type)
    if guessed and guessed != item.property_type:
        _note_fix(item, f"tipo corregido a {guessed}")
        item.property_type = guessed

    recovered = _price_from_text(item)
    raw = item.price
    if raw is not None and raw <= PLACEHOLDER_USD:
        if recovered:
            item.price, item.currency = recovered
            _note_fix(item, "precio placeholder (USD 1) reemplazado por el de la ficha")
        else:
            item.price = None
            item.price_usd = None
            _note_fix(item, "precio placeholder (p. ej. USD 1) anulado")
            raw = None

    price_usd = to_usd(item.price, item.currency, usd_ars) if item.price else None
    lo, hi = PRICE_BOUNDS.get(item.property_type, (5_000, 8_000_000))
    if price_usd is not None and price_usd < lo:
        if recovered:
            rec_usd = to_usd(recovered[0], recovered[1], usd_ars)
            if rec_usd and lo <= rec_usd <= hi:
                item.price, item.currency = recovered
                price_usd = rec_usd
                _note_fix(item, "precio irreal reemplazado por el de la ficha")
            else:
                item.price = None
                _note_fix(item, "precio demasiado bajo, se dejó sin precio")
        else:
            item.price = None
            _note_fix(item, "precio demasiado bajo, se dejó sin precio")
            price_usd = None
    elif price_usd is not None and price_usd > hi:
        scaled = price_usd / 1000
        if price_usd >= 20_000_000 and lo <= scaled <= hi:
            item.price = scaled
            item.currency = "USD"
            _note_fix(item, "precio con ceros de más, se corrigió")
        else:
            item.price = None
            _note_fix(item, "precio fuera de rango, se dejó sin precio")
    if item.price is None:
        item.price_usd = None
        item.price_m2 = None
    sanitize_areas(item)
    if is_catalog_ad(item):
        extra = dict(item.extra or {})
        extra["exclude_from_comps"] = True
        extra["is_loteo"] = True
        item.extra = extra
        _note_fix(item, "aviso de loteo / varios lotes: fuera de la mediana")
        barrio, zona, _lat, _lon = infer_barrio(
            item.title, item.address, item.description, city=item.city or "puerto-madryn"
        )
        if barrio != "Sin clasificar":
            item.barrio, item.zona = barrio, zona
            item.lat, item.lon = approx_slot(barrio, zona, item.id, item.city or "puerto-madryn")
            item.has_exact_location = False
    return item


def sanitize_areas(item: Listing) -> Listing:
    covered, lot = item.covered_m2, item.total_m2
    if covered and item.price and abs(covered - item.price) < 1 and covered > 200:
        item.covered_m2 = None
        _note_fix(item, "los m² cubiertos eran el precio, se anularon")
        covered = None
    if item.property_type in {"departamento", "ph"}:
        if covered and covered > 400:
            item.covered_m2 = None
            _note_fix(item, "m² cubiertos imposibles para un depto/PH")
        if lot and lot > 2_000:
            item.total_m2 = None
            _note_fix(item, "m² de lote imposibles para un depto/PH")
        if covered and covered < 12:
            item.covered_m2 = None
            _note_fix(item, "m² cubiertos demasiado chicos, se anularon")
    elif item.property_type == "casa":
        if covered and covered < 15:
            item.covered_m2 = None
            _note_fix(item, "m² cubiertos irreales para una casa")
        if covered and covered > 1_500:
            if not lot or lot < covered:
                item.total_m2 = covered
            item.covered_m2 = None
            _note_fix(item, "superficie grande pasada a terreno/lote")
    elif item.property_type == "terreno":
        size = lot or covered
        if size and size < 40:
            item.total_m2 = None
            item.covered_m2 = None
            _note_fix(item, "superficie de lote irreal, se anuló")
        elif covered and (not lot or lot < 40):
            item.total_m2 = covered
            item.covered_m2 = None
    if item.bedrooms is not None and (item.bedrooms < 0 or item.bedrooms > 20):
        item.bedrooms = None
        _note_fix(item, "dormitorios fuera de rango")
    if item.bathrooms is not None and (item.bathrooms < 0 or item.bathrooms > 15):
        item.bathrooms = None
        _note_fix(item, "baños fuera de rango")
    return item


def useful_m2(item: Listing) -> float | None:
    if item.property_type == "terreno":
        lot = item.total_m2 or item.covered_m2
        if lot and 40 <= lot <= 200_000:
            return lot
        return None
    covered, lot = item.covered_m2, item.total_m2
    if item.property_type in {"departamento", "ph"}:
        if covered and 12 <= covered <= 400:
            return covered
        if lot and 12 <= lot <= 250:
            return lot
        return None
    if covered and 15 <= covered <= 800:
        return covered
    if lot and 18 <= lot <= 600:
        return lot
    return None


def apply_unit_price(item: Listing, usd_ars: float) -> Listing:
    if item.price:
        item.price_usd = to_usd(item.price, item.currency, usd_ars)
    m2 = useful_m2(item)
    item.price_m2 = (item.price_usd / m2) if item.price_usd and m2 else None
    return item


def _median(values: list[float]) -> float | None:
    clean = sorted(v for v in values if v and v > 0)
    if len(clean) < 2:
        return None
    if len(clean) >= 8:
        lo = int(len(clean) * 0.15)
        hi = int(len(clean) * 0.85) or len(clean)
        clean = clean[lo:hi] or clean
    return float(statistics.median(clean))


def _quantiles(values: list[float]) -> tuple[float, float] | None:
    clean = sorted(v for v in values if v and v > 0)
    if len(clean) < 6:
        return None
    q1, _q2, q3 = statistics.quantiles(clean, n=4, method="inclusive")
    return float(q1), float(q3)


def _trim_outliers(values: list[float]) -> list[float]:
    bounds = _quantiles(values)
    if not bounds:
        return [v for v in values if v and v > 0]
    q1, q3 = bounds
    iqr = q3 - q1
    if iqr <= 0:
        return [v for v in values if v and v > 0]
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    trimmed = [v for v in values if lo <= v <= hi]
    return trimmed if len(trimmed) >= 3 else [v for v in values if v and v > 0]


def _robust_median(values: list[float]) -> float | None:
    return _median(_trim_outliers(values))


def _size_bucket(item: Listing) -> str:
    m2 = useful_m2(item)
    if item.property_type == "terreno":
        if not m2:
            return "sin-m2"
        if m2 < 500:
            return "lote-urbano"
        if m2 < 2500:
            return "lote-grande"
        return "chacra"
    if item.property_type in {"departamento", "ph"}:
        if not m2:
            return "sin-m2"
        if m2 < 45:
            return "chico"
        if m2 < 80:
            return "medio"
        return "grande"
    if item.property_type == "casa":
        if not m2:
            return "sin-m2"
        if m2 < 90:
            return "chica"
        if m2 < 180:
            return "media"
        return "grande"
    return "otro"


def _first_peers(*groups: list[float]) -> list[float]:
    ranked = [g for g in groups if g]
    if not ranked:
        return []
    for group in ranked:
        if len(group) >= 4:
            return group
    return ranked[0]


def _dist_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6_371_000 * math.asin(min(1.0, math.sqrt(h)))


def _local_radius(item: Listing) -> float:
    if item.property_type == "terreno":
        return 1500.0
    if item.property_type == "casa":
        return 900.0
    return 650.0


def _nearby_m2(item: Listing, pool: list[Listing]) -> list[float]:
    if not item.lat or not item.lon:
        return []
    radius = _local_radius(item)
    bucket = _size_bucket(item)
    city = item.city or "puerto-madryn"
    same: list[float] = []
    any_size: list[float] = []
    for other in pool:
        if other.id == item.id:
            continue
        if (other.city or "puerto-madryn") != city:
            continue
        if other.property_type != item.property_type:
            continue
        if not other.lat or not other.lon or not other.price_m2:
            continue
        if _dist_m((item.lat, item.lon), (other.lat, other.lon)) > radius:
            continue
        any_size.append(other.price_m2)
        if _size_bucket(other) == bucket:
            same.append(other.price_m2)
    if len(same) >= 4:
        return same
    return any_size


def enrich(listings: list[Listing], usd_ars: float) -> list[Listing]:
    for item in listings:
        sanitize_item(item, usd_ars)
        fill_areas(item)
        sanitize_areas(item)
        apply_unit_price(item, usd_ars)
        item.fingerprint = fingerprint(
            item.title, item.address, item.price_usd, item.covered_m2, item.property_type
        )

    m2_by_barrio: dict[tuple, list[float]] = defaultdict(list)
    m2_by_zona: dict[tuple, list[float]] = defaultdict(list)
    m2_by_type_bucket: dict[tuple, list[float]] = defaultdict(list)
    m2_by_type: dict[tuple[str, str], list[float]] = defaultdict(list)
    price_by_barrio_type: dict[tuple[str, str, str], list[float]] = defaultdict(list)

    for item in listings:
        city = item.city or "puerto-madryn"
        if excluded_from_comps(item):
            continue
        bucket = _size_bucket(item)
        if item.price_m2:
            m2_by_barrio[(city, item.barrio, item.property_type, bucket)].append(item.price_m2)
            m2_by_zona[(city, item.zona, item.property_type, bucket)].append(item.price_m2)
            m2_by_type_bucket[(city, item.property_type, bucket)].append(item.price_m2)
            m2_by_type[(city, item.property_type)].append(item.price_m2)
        if item.price_usd:
            price_by_barrio_type[(city, item.barrio, item.property_type)].append(item.price_usd)

    med_m2_barrio = {k: _robust_median(v) for k, v in m2_by_barrio.items()}
    med_m2_zona = {k: _robust_median(v) for k, v in m2_by_zona.items()}
    med_m2_type_bucket = {k: _robust_median(v) for k, v in m2_by_type_bucket.items()}
    med_m2_type = {k: _robust_median(v) for k, v in m2_by_type.items()}
    med_price_barrio = {k: _robust_median(v) for k, v in price_by_barrio_type.items()}
    geo_pool = [
        row
        for row in listings
        if row.lat and row.lon and row.price_m2 and not excluded_from_comps(row)
    ]

    for item in listings:
        city = item.city or "puerto-madryn"
        bucket = _size_bucket(item)
        barrio_key = (city, item.barrio, item.property_type, bucket)
        zona_key = (city, item.zona, item.property_type, bucket)
        type_bucket_key = (city, item.property_type, bucket)
        nearby = _nearby_m2(item, geo_pool)
        nearby_ref = _robust_median(nearby) if len(nearby) >= 4 else None
        fallback_peers = _first_peers(
            m2_by_barrio.get(barrio_key) or [],
            m2_by_zona.get(zona_key) or [],
            m2_by_type_bucket.get(type_bucket_key) or [],
            m2_by_type.get((city, item.property_type)) or [],
        )
        if nearby_ref:
            peers, ref, scope = nearby, nearby_ref, f"{len(nearby)} avisos a {int(_local_radius(item))} m"
        elif med_m2_barrio.get(barrio_key):
            peers, ref, scope = fallback_peers, med_m2_barrio[barrio_key], f"barrio {item.barrio}"
        elif med_m2_zona.get(zona_key):
            peers, ref, scope = fallback_peers, med_m2_zona[zona_key], f"zona {item.zona}"
        elif med_m2_type_bucket.get(type_bucket_key):
            peers, ref, scope = fallback_peers, med_m2_type_bucket[type_bucket_key], "mismo tipo y tamaño en la ciudad"
        else:
            peers, ref, scope = fallback_peers, med_m2_type.get((city, item.property_type)), "mismo tipo en la ciudad"
        price_ref = med_price_barrio.get((city, item.barrio, item.property_type))
        vs = None
        if item.price_m2 and ref:
            vs = (ref - item.price_m2) / ref
        elif item.price_usd and price_ref:
            vs = (price_ref - item.price_usd) / price_ref
            scope = f"precio total en {item.barrio}"
        item.vs_barrio_pct = round(vs * 100, 1) if vs is not None else None
        flags = scan_red_flags(item)
        catalog = is_catalog_ad(item) or bool((item.extra or {}).get("is_loteo"))
        unreal = unrealistic_unit_price(item)
        excluded = catalog or unreal
        if excluded:
            vs = None
            item.vs_barrio_pct = None
        outlier = unreal and not catalog
        extra = dict(item.extra or {})
        extra["red_flags"] = flags
        extra["is_outlier"] = outlier
        extra["peer_count"] = len(peers)
        extra["size_bucket"] = bucket
        extra["comp_scope"] = scope
        extra["comp_ref_m2"] = round(ref, 1) if ref else None
        item.extra = extra
        item.score = 50.0 if catalog else _score(item, vs)
        item.deal_label = (
            "loteo (fuera de mediana)" if catalog else _label(item, vs, outlier, flags, len(peers))
        )
        analyze(item)
        extra = dict(item.extra or {})
        extra["red_flags"] = flags
        extra["is_outlier"] = outlier
        extra["peer_count"] = len(peers)
        extra["size_bucket"] = bucket
        extra["comp_scope"] = scope
        extra["comp_ref_m2"] = round(ref, 1) if ref else None
        extra["exclude_from_comps"] = excluded
        extra["deal_score"] = 0 if excluded else _deal_score(item, vs, outlier, flags)
        extra["deal_reasons"] = (
            ["aviso de loteo / catálogo: no entra en la mediana del barrio"]
            if excluded
            else _reasons(item, vs, outlier, flags, ref, scope)
        )
        item.extra = extra
    return listings


def _score(item: Listing, vs: float | None) -> float:
    score = 50.0
    if vs is not None:
        score = 50 + max(-40, min(40, vs * 100))
    m2 = useful_m2(item)
    if m2 and item.property_type == "departamento" and m2 >= 70:
        score += 3
    if m2 and item.property_type == "casa" and 80 <= m2 <= 220:
        score += 3
    if item.bedrooms and item.bedrooms >= 3:
        score += 2
    if item.parking:
        score += 1
    if item.barrio in {"Centro", "Bahía Nueva", "Punta Cuevas", "Zona Sur"}:
        score += 2
    if item.price_usd and item.price_usd < 25000 and item.property_type != "terreno":
        score -= 15
    return round(max(0, min(100, score)), 1)


def _label(item: Listing, vs: float | None, outlier: bool, flags: list[str], peers: int) -> str:
    if item.price_usd is None:
        return "sin precio"
    if flags:
        return "revisar (señales)"
    if outlier:
        return "revisar (dato raro)"
    if vs is None:
        return "sin comparables"
    if vs >= 0.20 and peers >= 5:
        return "oportunidad"
    if vs >= 0.14 and peers >= 4:
        return "oportunidad"
    if vs >= 0.08:
        return "bueno"
    if vs <= -0.12:
        return "caro"
    return "mercado"


def _deal_score(item: Listing, vs: float | None, outlier: bool, flags: list[str]) -> float:
    score = 38.0
    if vs is not None:
        score += max(-30, min(40, vs * 100))
    if item.quality_score is not None:
        score += (item.quality_score - 50) * 0.15
    if outlier:
        score -= 28
    if flags:
        score -= 18 + 4 * (len(flags) - 1)
    if item.has_exact_location:
        score += 3
    return round(max(0, min(100, score)), 1)


def _reasons(
    item: Listing,
    vs: float | None,
    outlier: bool,
    flags: list[str],
    ref: float | None,
    scope: str = "",
) -> list[str]:
    bits: list[str] = []
    if vs is not None:
        bits.append(f"{'-' if vs > 0 else '+'}{abs(round(vs * 100, 1))}% vs comparables")
    if ref:
        where = f" · {scope}" if scope else ""
        bits.append(f"mediana comparable USD {round(ref)}/m²{where}")
    if outlier:
        bits.append("USD/m² fuera de lo posible para este tipo; no entra en la mediana")
    bits.extend(flags)
    if item.property_type == "terreno" and (item.total_m2 or item.covered_m2):
        bits.append(f"lote {(item.total_m2 or item.covered_m2):.0f} m²")
    if item.covered_m2 and item.property_type != "terreno":
        bits.append(f"{item.covered_m2:.0f} m² cubiertos")
    return bits


def summarize(listings: list[Listing]) -> dict:
    by_barrio: dict[tuple[str, str], list[Listing]] = defaultdict(list)
    by_zona: dict[str, list[Listing]] = defaultdict(list)
    by_type: dict[str, list[Listing]] = defaultdict(list)
    for item in listings:
        by_barrio[(item.city or "puerto-madryn", item.barrio)].append(item)
        by_zona[item.zona].append(item)
        by_type[item.property_type].append(item)

    def block(groups: dict, with_city: bool = False) -> list[dict]:
        rows = []
        for key, items in groups.items():
            name = key[1] if with_city else key
            usd = [i.price_usd for i in items if i.price_usd and not excluded_from_comps(i)]
            m2 = [i.price_m2 for i in items if i.price_m2 and not excluded_from_comps(i)]
            deals = sum(1 for i in items if i.deal_label == "oportunidad")
            row = {
                "name": name,
                "count": len(items),
                "median_usd": round(_median(usd) or 0),
                "median_m2": round(_median(m2) or 0),
                "min_usd": round(min(usd)) if usd else None,
                "max_usd": round(max(usd)) if usd else None,
                "deals": deals,
            }
            if with_city:
                row["city"] = key[0]
            rows.append(row)
        rows.sort(key=lambda r: (-r["count"], r["name"]))
        return rows

    usd_all = [i.price_usd for i in listings if i.price_usd and not excluded_from_comps(i)]
    m2_all = [i.price_m2 for i in listings if i.price_m2 and not excluded_from_comps(i)]
    return {
        "total": len(listings),
        "deals": sum(1 for i in listings if i.deal_label == "oportunidad"),
        "median_usd": round(_median(usd_all) or 0),
        "median_m2": round(_median(m2_all) or 0),
        "by_barrio": block(by_barrio, with_city=True),
        "by_zona": block(by_zona),
        "by_type": block(by_type),
    }
