from __future__ import annotations

import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, field

from .geo import default_city, fold
from .layout import classify_comp, classify_listing
from .models import Listing
from .scoring import USD_FALLBACK, is_catalog_ad, useful_m2

RENT_MODEL_VERSION = "11"
DEFAULT_OCCUPANCY = 0.45
MONTHLY_VACANCY = 0.08
MONTHLY_COST = 0.08
TEMPORAL_COST = 0.22
ASK_HAIRCUT = 0.88
FALLBACK_GROSS = 0.04
GROSS_CAP = 0.045
TYPICAL_M2 = {
    ("departamento", "0"): 28.0,
    ("departamento", "1"): 38.0,
    ("departamento", "2"): 58.0,
    ("departamento", "3+"): 85.0,
    ("departamento", "na"): 50.0,
    ("casa", "0"): 40.0,
    ("casa", "1"): 60.0,
    ("casa", "2"): 85.0,
    ("casa", "3+"): 130.0,
    ("casa", "na"): 110.0,
    ("ph", "0"): 35.0,
    ("ph", "1"): 45.0,
    ("ph", "2"): 70.0,
    ("ph", "3+"): 95.0,
    ("ph", "na"): 70.0,
}
_GENERIC_PLACES = {
    "sin clasificar",
    "sin barrio",
    "sin zona",
    "la ciudad",
    "centro",
    "norte",
    "sur",
    "este",
    "oeste",
    "microcentro",
}
_EN_SALE_RENT = re.compile(r"\ben\s+(?:venta|alquiler)\s+en\s+(.{3,48})", re.I)
_PLACE_CUT = re.compile(r"\s+(?:tipo|amoblad|,|\(|de\s+\d|con\s+\d)\b", re.I)
_model_cache: dict[str, "RentModel"] = {}


def rent_to_usd(price: float | None, currency: str, usd_ars: float) -> float | None:
    if price is None or price <= 0:
        return None
    rate = usd_ars if usd_ars and usd_ars > 100 else USD_FALLBACK
    cur = (currency or "ARS").upper()
    if cur in {"USD", "U$S", "US$"}:
        if price >= 4000:
            return float(price) / rate
        return float(price)
    return float(price) / rate


def infer_period(price_usd: float | None, title: str = "", description: str = "", hinted: str = "monthly") -> str:
    blob = fold(f"{title} {description}")
    if any(w in blob for w in ("por noche", "la noche", "por dia", "por día", "/noche", "night")):
        return "nightly"
    if "semana" in blob or "/sem" in blob:
        return "weekly"
    if hinted in {"nightly", "weekly"} and price_usd and price_usd > 280:
        return "monthly"
    if hinted == "nightly" and price_usd and price_usd <= 280:
        return "nightly"
    if hinted == "weekly" and price_usd and price_usd <= 280:
        return "weekly"
    if price_usd and price_usd <= 180:
        return "nightly"
    return "monthly"


def occupancy_for(city: str) -> float:
    return DEFAULT_OCCUPANCY


def _median(values: list[float]) -> float | None:
    clean = sorted(v for v in values if v and v > 0)
    if not clean:
        return None
    if len(clean) >= 8:
        lo = int(len(clean) * 0.15)
        hi = int(len(clean) * 0.85) or len(clean)
        clean = clean[lo:hi] or clean
    return float(statistics.median(clean))


def _beds_bucket(beds: int | None) -> str:
    if beds is None:
        return "na"
    if beds <= 0:
        return "0"
    if beds == 1:
        return "1"
    if beds == 2:
        return "2"
    return "3+"


def _comp_beds(row: dict) -> str:
    return classify_comp(row).bucket


def _nightly_from_comp(price_usd: float, period: str) -> float | None:
    if period == "nightly":
        return price_usd if 18 <= price_usd <= 220 else None
    if period == "weekly":
        if price_usd > 220:
            return None
        return price_usd / 7
    return None


def _comp_m2(row: dict) -> float | None:
    try:
        m2 = float(row.get("covered_m2") or 0)
    except (TypeError, ValueError):
        return None
    if 15 <= m2 <= 800:
        return m2
    return None


def _generic_name(name: str | None, city: str | None = None) -> bool:
    token = fold(name or "")
    if not token or token in _GENERIC_PLACES:
        return True
    if city:
        slug = fold((city or "").replace("-", " "))
        if token == slug or token == fold(_city_label(city)):
            return True
    return False


def _city_label(city: str) -> str:
    from .geo import CITIES

    info = CITIES.get(city or "")
    if info and info.get("label"):
        return str(info["label"])
    return (city or "la ciudad").replace("-", " ").title()


def _place_name(item: Listing, kind: str = "") -> str:
    barrio = (item.barrio or "").strip()
    zona = (item.zona or "").strip()
    city = item.city or ""
    if kind.startswith("local-barrio") and not _generic_name(barrio, city):
        return barrio
    if kind.startswith("local-zona") and not _generic_name(zona, city):
        return zona
    if kind in {"similar-m2", "local-beds", "local-type", "local-city", ""}:
        if kind.startswith("local-barrio"):
            return barrio
        return _city_label(city)
    if not _generic_name(barrio, city) and kind.startswith("local"):
        return barrio
    return _city_label(city)


def _type_word(ptype: str) -> str:
    return {
        "casa": "casas",
        "departamento": "deptos",
        "ph": "PH",
    }.get(ptype or "", "")


def _comp_matches_city(row: dict) -> bool:
    city = (row.get("city") or "").strip()
    if not city:
        return True
    title = row.get("title") or ""
    match = _EN_SALE_RENT.search(title)
    if not match:
        return True
    place = _PLACE_CUT.split(match.group(1), maxsplit=1)[0].strip(" -.,")
    if len(place) < 3:
        return True
    head = fold(place).split()[:1]
    if head and head[0] in {"alquiler", "venta", "departamento", "casa", "ph"}:
        return True
    try:
        from .geo import resolve_city, same_place_ids

        cid = resolve_city(place)
    except Exception:
        return True
    if not cid or cid in {"fuera", "otros", "argentina"}:
        return True
    if cid == city or cid in (same_place_ids(city) or {city}):
        return True
    try:
        from .geo import city_center, in_city_radius

        lat, lon = city_center(cid)
    except Exception:
        return False
    return in_city_radius(lat, lon, city)


def _period_rows(comps: list[dict], period: str) -> list[dict]:
    rows = [row for row in comps if _comp_matches_city(row)]
    if period == "nightly":
        nights = []
        for row in rows:
            hinted = row.get("period") or "nightly"
            price = float(row.get("price_usd") or 0)
            if hinted == "weekly" and price > 280:
                continue
            if hinted not in {"nightly", "weekly"} or not price:
                continue
            night = _nightly_from_comp(price, hinted)
            if night and 18 <= night <= 220:
                nights.append({**row, "price_usd": night, "period": "nightly"})
        return nights
    monthly = []
    for row in rows:
        hinted = row.get("period") or "monthly"
        try:
            price = float(row.get("price_usd") or 0)
        except (TypeError, ValueError):
            continue
        if hinted == "weekly" and price > 280:
            hinted = "monthly"
        if hinted == "monthly" and 80 <= price <= 2500:
            monthly.append({**row, "period": "monthly", "price_usd": price})
    return _trim_rent_outliers(_prefer_ars_monthly(monthly))


def _trim_rent_outliers(rows: list[dict]) -> list[dict]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        groups[(row.get("city") or "", row.get("property_type") or "")].append(row)
    keep: list[dict] = []
    for items in groups.values():
        prices = [float(row["price_usd"]) for row in items]
        if len(prices) < 8:
            keep.extend(items)
            continue
        med = statistics.median(prices)
        lo, hi = med * 0.35, med * 2.2
        keep.extend(row for row in items if lo <= float(row["price_usd"]) <= hi)
    return keep


def infer_beds(item: Listing) -> int | None:
    return classify_listing(item).beds


def infer_m2(item: Listing, typical: float | None = None) -> float | None:
    raw = useful_m2(item)
    if raw:
        return raw
    return typical


def _is_ars_comp(row: dict) -> bool:
    return str(row.get("currency") or "").upper() in {"ARS", "$"}


def _prefer_ars_monthly(rows: list[dict]) -> list[dict]:
    """El contrato tradicional está en pesos. Los avisos en USD suelen ser más caros."""
    by_city: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_city[row.get("city") or ""].append(row)
    out: list[dict] = []
    for group in by_city.values():
        ars = [row for row in group if _is_ars_comp(row)]
        out.extend(ars if len(ars) >= 5 else group)
    return out


def _size_rent(unit: float, listing_m2: float | None, ref_m2: float | None, bucket: str) -> float:
    """USD/m² × m² si el tamaño es parecido; si no, el alquiler no escala 1:1."""
    if listing_m2 and ref_m2 and 0.7 <= listing_m2 / ref_m2 <= 1.3:
        return unit * listing_m2
    if listing_m2 and not ref_m2:
        return unit * listing_m2
    if not ref_m2:
        return unit * (listing_m2 or 0)
    ratio = (listing_m2 / ref_m2) if listing_m2 else 1.0
    cap = 1.18 if bucket == "0" else 1.32
    floor = 0.80 if bucket == "0" else 0.72
    exp = 0.32 if bucket == "0" else 0.4
    return unit * ref_m2 * min(cap, max(floor, ratio ** exp))


def _gross_cap_usd(model: "RentModel", item: Listing, listing_m2: float | None) -> float | None:
    city = item.city or ""
    sale = model.sale_m2.get(city) if city else None
    if sale and listing_m2:
        return sale * listing_m2 * GROSS_CAP / 12
    return None


def _comp_unit(row: dict) -> float | None:
    m2 = _comp_m2(row)
    try:
        price = float(row.get("price_usd") or 0)
    except (TypeError, ValueError):
        return None
    if m2 and price > 0:
        return price / m2
    return None


def rentable(item: Listing) -> bool:
    if item.property_type == "terreno":
        return False
    title = fold(item.title or "")
    if "terreno" in title or "loteo" in title:
        return False
    if "lote" in title and not any(w in title for w in ("casa", "depto", "departamento", "ph", "duplex")):
        return False
    if is_catalog_ad(item):
        return False
    return True


def night_from_month(month: float, occ: float) -> float:
    return month * 1.15 / (30 * max(0.35, occ))


def month_from_night(night: float, occ: float) -> float:
    return night * 30 * max(0.35, occ) / 1.15


@dataclass
class PeriodSlice:
    n: int = 0
    city_median: dict[str, float] = field(default_factory=dict)
    type_median: dict[str, float] = field(default_factory=dict)
    type_beds: dict[str, float] = field(default_factory=dict)
    city_type: dict[str, float] = field(default_factory=dict)
    city_type_beds: dict[str, float] = field(default_factory=dict)
    barrio: dict[str, float] = field(default_factory=dict)
    zona: dict[str, float] = field(default_factory=dict)
    barrio_type: dict[str, float] = field(default_factory=dict)
    barrio_type_beds: dict[str, float] = field(default_factory=dict)
    zona_type: dict[str, float] = field(default_factory=dict)
    zona_type_beds: dict[str, float] = field(default_factory=dict)
    unit_type: dict[str, float] = field(default_factory=dict)
    city_unit: dict[str, float] = field(default_factory=dict)
    typical_m2: dict[str, float] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)


@dataclass
class RentModel:
    fingerprint: str
    monthly: PeriodSlice
    nightly: PeriodSlice
    sale_m2: dict[str, float]
    ref_sale_m2: float | None
    cities_with_rent: set[str]
    monthly_rows: list[dict] = field(default_factory=list)
    nightly_rows: list[dict] = field(default_factory=list)


def _fill_slice(rows: list[dict]) -> PeriodSlice:
    slice_ = PeriodSlice(n=len(rows))
    buckets: dict[str, list[float]] = defaultdict(list)
    m2_buckets: dict[str, list[float]] = defaultdict(list)
    unit_buckets: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        price = float(row["price_usd"])
        city = row.get("city") or ""
        ptype = row.get("property_type") or ""
        beds = _comp_beds(row)
        barrio = (row.get("barrio") or "").strip()
        zona = (row.get("zona") or "").strip()
        buckets["all"].append(price)
        if city:
            buckets[f"c:{city}"].append(price)
        if ptype:
            buckets[f"t:{ptype}"].append(price)
            buckets[f"tb:{ptype}|{beds}"].append(price)
        if city and ptype:
            buckets[f"ct:{city}|{ptype}"].append(price)
            buckets[f"ctb:{city}|{ptype}|{beds}"].append(price)
        if city and not _generic_name(barrio, city):
            buckets[f"b:{city}|{barrio}"].append(price)
            if ptype:
                buckets[f"bt:{city}|{barrio}|{ptype}"].append(price)
                buckets[f"btb:{city}|{barrio}|{ptype}|{beds}"].append(price)
        if city and not _generic_name(zona, city):
            buckets[f"z:{city}|{zona}"].append(price)
            if ptype:
                buckets[f"zt:{city}|{zona}|{ptype}"].append(price)
                buckets[f"ztb:{city}|{zona}|{ptype}|{beds}"].append(price)
        m2 = _comp_m2(row)
        if m2:
            unit = price / m2
            if city and ptype:
                m2_buckets[f"{city}|{ptype}|{beds}"].append(m2)
                m2_buckets[f"{city}|{ptype}"].append(m2)
                unit_buckets[f"ct:{city}|{ptype}"].append(unit)
                unit_buckets[f"ctb:{city}|{ptype}|{beds}"].append(unit)
            if city and ptype and not _generic_name(barrio, city):
                unit_buckets[f"bt:{city}|{barrio}|{ptype}"].append(unit)
                unit_buckets[f"btb:{city}|{barrio}|{ptype}|{beds}"].append(unit)

    def take(prefix: str, dest: dict[str, float], min_n: int) -> None:
        for key, values in buckets.items():
            head, _, tail = key.partition(":")
            if f"{head}:" != prefix:
                continue
            if len(values) < min_n:
                continue
            med = _median(values)
            if med:
                dest[tail] = med
                slice_.counts[key] = len(values)

    take("c:", slice_.city_median, 2)
    take("t:", slice_.type_median, 2)
    take("tb:", slice_.type_beds, 2)
    take("ct:", slice_.city_type, 2)
    take("ctb:", slice_.city_type_beds, 2)
    take("b:", slice_.barrio, 2)
    take("z:", slice_.zona, 2)
    take("bt:", slice_.barrio_type, 2)
    take("btb:", slice_.barrio_type_beds, 2)
    take("zt:", slice_.zona_type, 2)
    take("ztb:", slice_.zona_type_beds, 2)
    for key, values in unit_buckets.items():
        med = _median(values)
        if not med or len(values) < 2:
            continue
        head, _, tail = key.partition(":")
        slice_.counts[f"u:{tail}"] = len(values)
        if head in {"ct", "ctb", "bt", "btb"}:
            slice_.city_unit[tail] = med
        else:
            slice_.unit_type[tail] = med
    for key, values in m2_buckets.items():
        med = _median(values)
        if med and len(values) >= 2:
            slice_.typical_m2[key] = med
    return slice_


def comps_fingerprint(comps: list[dict], sale_m2: dict[str, float]) -> str:
    total = int(sum(float(row.get("price_usd") or 0) for row in comps))
    sale_bit = int(sum(sale_m2.values())) if sale_m2 else 0
    return f"{RENT_MODEL_VERSION}:{len(comps)}:{total}:{sale_bit}"


def build_rent_model(comps: list[dict], sale_m2: dict[str, float] | None = None) -> RentModel:
    sale_m2 = {key: value for key, value in (sale_m2 or {}).items() if value}
    monthly_rows = _period_rows(comps, "monthly")
    nightly_rows = _period_rows(comps, "nightly")
    monthly = _fill_slice(monthly_rows)
    nightly = _fill_slice(nightly_rows)
    cities_with_rent = set(monthly.city_median)
    ref_sales = [sale_m2[city] for city in cities_with_rent if city in sale_m2]
    return RentModel(
        fingerprint=comps_fingerprint(comps, sale_m2),
        monthly=monthly,
        nightly=nightly,
        sale_m2=sale_m2,
        ref_sale_m2=_median(ref_sales),
        cities_with_rent=set(monthly.city_median),
        monthly_rows=monthly_rows,
        nightly_rows=nightly_rows,
    )


def get_rent_model(comps: list[dict], sale_m2: dict[str, float] | None = None) -> RentModel:
    sale_m2 = sale_m2 or {}
    fp = comps_fingerprint(comps, sale_m2)
    cached = _model_cache.get(fp)
    if cached:
        return cached
    model = build_rent_model(comps, sale_m2)
    _model_cache.clear()
    _model_cache[fp] = model
    return model


def _spread(values: list[float]) -> tuple[float | None, float | None, float | None]:
    clean = sorted(v for v in values if v and v > 0)
    if not clean:
        return None, None, None
    mid = _median(clean)
    if len(clean) == 1:
        return mid, mid, mid
    lo_i = int((len(clean) - 1) * 0.25)
    hi_i = int((len(clean) - 1) * 0.75)
    return clean[lo_i], mid, clean[hi_i]


def _similar_rows(rows: list[dict], item: Listing, bucket: str) -> list[dict]:
    city = item.city or ""
    ptype = item.property_type or ""
    m2 = useful_m2(item)
    same: list[dict] = []
    for row in rows:
        if city and (row.get("city") or "") != city:
            continue
        if ptype and (row.get("property_type") or "") != ptype:
            continue
        if _comp_beds(row) != bucket:
            continue
        same.append(row)
    if not m2 or len(same) < 3:
        return same
    lo, hi = m2 * 0.7, m2 * 1.3
    sized = [row for row in same if (cm := _comp_m2(row)) and lo <= cm <= hi]
    return sized if len(sized) >= 3 else same


def _predict_period(model: RentModel, item: Listing, period: str) -> tuple[float | None, str, int, dict]:
    slice_ = model.monthly if period == "monthly" else model.nightly
    rows = model.monthly_rows if period == "monthly" else model.nightly_rows
    city = item.city or ""
    ptype = item.property_type or ""
    layout = classify_listing(item)
    bucket = layout.bucket
    beds = layout.beds
    barrio = (item.barrio or "").strip()
    zona = (item.zona or "").strip()
    observed_m2 = None
    if city and ptype:
        observed_m2 = slice_.typical_m2.get(f"{city}|{ptype}|{bucket}") or slice_.typical_m2.get(f"{city}|{ptype}")
    catalog_m2 = TYPICAL_M2.get((ptype, bucket))
    typical = observed_m2 or catalog_m2
    if bucket == "0" and catalog_m2:
        typical = max(typical or 0, catalog_m2)
    listing_m2 = infer_m2(item, typical)
    meta: dict = {"layout": layout.label, "conflict": layout.conflict, "confidence": layout.confidence}

    similar = _similar_rows(rows, item, bucket)
    similar_prices = [float(row["price_usd"]) for row in similar if row.get("price_usd")]
    similar_units = [unit for row in similar if (unit := _comp_unit(row))]
    sized = False
    if listing_m2 and len(similar) >= 3:
        lo_m, hi_m = listing_m2 * 0.7, listing_m2 * 1.3
        sized = sum(1 for row in similar if (cm := _comp_m2(row)) and lo_m <= cm <= hi_m) >= 3

    est = None
    n = 0
    kind = ""
    used_unit = False
    used_similar_price = False
    if listing_m2 and similar_units and len(similar_units) >= 3 and (layout.conflict or sized):
        unit = _median(similar_units)
        ref = _median([_comp_m2(row) for row in similar if _comp_m2(row)]) if not sized else listing_m2
        est = _size_rent(unit, listing_m2, ref or listing_m2, bucket)
        n = len(similar_units)
        kind = "similar-m2"
        used_unit = True
        lo_u, _mid, hi_u = _spread(similar_units)
        if lo_u and hi_u:
            meta["lo"] = _size_rent(lo_u, listing_m2, ref or listing_m2, bucket)
            meta["hi"] = _size_rent(hi_u, listing_m2, ref or listing_m2, bucket)

    if est is None and listing_m2:
        unit_keys: list[tuple[float | None, int, str]] = []
        if city and ptype and not _generic_name(barrio, city):
            unit_keys.append(
                (
                    slice_.city_unit.get(f"{city}|{barrio}|{ptype}|{bucket}"),
                    slice_.counts.get(f"u:{city}|{barrio}|{ptype}|{bucket}", 0),
                    "local-barrio-beds",
                )
            )
            unit_keys.append(
                (
                    slice_.city_unit.get(f"{city}|{barrio}|{ptype}"),
                    slice_.counts.get(f"u:{city}|{barrio}|{ptype}", 0),
                    "local-barrio-type",
                )
            )
        if city and ptype:
            unit_keys.append(
                (
                    slice_.city_unit.get(f"{city}|{ptype}|{bucket}"),
                    slice_.counts.get(f"u:{city}|{ptype}|{bucket}", 0),
                    "local-beds",
                )
            )
            unit_keys.append(
                (
                    slice_.city_unit.get(f"{city}|{ptype}"),
                    slice_.counts.get(f"u:{city}|{ptype}", 0),
                    "local-type",
                )
            )
        for unit, count, label in unit_keys:
            if not unit:
                continue
            est = _size_rent(unit, listing_m2, typical, bucket)
            n, kind = count, label
            used_unit = True
            break

    if est is None and similar_prices:
        est = _median(similar_prices)
        n = len(similar_prices)
        kind = "local-beds"
        used_similar_price = True
        lo, _mid, hi = _spread(similar_prices)
        meta["lo"] = lo
        meta["hi"] = hi

    if est is None:
        keys: list[tuple[float | None, int, str]] = []
        if city and ptype and not _generic_name(barrio, city):
            keys.append(
                (
                    slice_.barrio_type_beds.get(f"{city}|{barrio}|{ptype}|{bucket}"),
                    slice_.counts.get(f"btb:{city}|{barrio}|{ptype}|{bucket}", 0),
                    "local-barrio-beds",
                )
            )
        if city and ptype and not _generic_name(zona, city):
            keys.append(
                (
                    slice_.zona_type_beds.get(f"{city}|{zona}|{ptype}|{bucket}"),
                    slice_.counts.get(f"ztb:{city}|{zona}|{ptype}|{bucket}", 0),
                    "local-zona-beds",
                )
            )
        keys.append(
            (
                slice_.city_type_beds.get(f"{city}|{ptype}|{bucket}"),
                slice_.counts.get(f"ctb:{city}|{ptype}|{bucket}", 0),
                "local-beds",
            )
        )
        if not layout.conflict and city and ptype and not _generic_name(barrio, city):
            keys.append(
                (
                    slice_.barrio_type.get(f"{city}|{barrio}|{ptype}"),
                    slice_.counts.get(f"bt:{city}|{barrio}|{ptype}", 0),
                    "local-barrio-type",
                )
            )
        if not layout.conflict and city and ptype and not _generic_name(zona, city):
            keys.append(
                (
                    slice_.zona_type.get(f"{city}|{zona}|{ptype}"),
                    slice_.counts.get(f"zt:{city}|{zona}|{ptype}", 0),
                    "local-zona-type",
                )
            )
        keys.extend(
            [
                (
                    slice_.city_type.get(f"{city}|{ptype}"),
                    slice_.counts.get(f"ct:{city}|{ptype}", 0),
                    "local-type",
                ),
                (slice_.city_median.get(city), slice_.counts.get(f"c:{city}", 0), "local-city"),
            ]
        )
        for rent, count, label in keys:
            if rent:
                est, n, kind = rent, count, label
                break

    if est is None and period == "monthly" and item.price_usd:
        est = item.price_usd * FALLBACK_GROSS / 12
        n = 0
        kind = "yield"

    if est is None:
        return None, "", 0, meta

    used_place = kind.startswith("local-barrio") or kind.startswith("local-zona")
    if kind != "similar-m2" and not used_place and city and not _generic_name(barrio, city) and not layout.conflict:
        barrio_med = slice_.barrio_type.get(f"{city}|{barrio}|{ptype}") or slice_.barrio.get(f"{city}|{barrio}")
        city_ref = slice_.city_type.get(f"{city}|{ptype}") or slice_.city_median.get(city)
        n_place = max(
            slice_.counts.get(f"bt:{city}|{barrio}|{ptype}", 0),
            slice_.counts.get(f"b:{city}|{barrio}", 0),
        )
        if barrio_med and city_ref and n_place >= 3:
            est *= min(1.14, max(0.86, barrio_med / city_ref))
            n = max(n, n_place)
    elif kind != "similar-m2" and not used_place and city and not _generic_name(zona, city) and not layout.conflict:
        zona_med = slice_.zona_type.get(f"{city}|{zona}|{ptype}") or slice_.zona.get(f"{city}|{zona}")
        city_ref = slice_.city_type.get(f"{city}|{ptype}") or slice_.city_median.get(city)
        n_place = max(
            slice_.counts.get(f"zt:{city}|{zona}|{ptype}", 0),
            slice_.counts.get(f"z:{city}|{zona}", 0),
        )
        if zona_med and city_ref and n_place >= 3:
            est *= min(1.14, max(0.86, zona_med / city_ref))

    skip_scale = used_unit or used_similar_price or (kind == "similar-m2" and sized)
    ref_m2 = observed_m2 or typical
    if bucket == "0" and catalog_m2:
        ref_m2 = max(ref_m2 or 0, catalog_m2)
    if listing_m2 and ref_m2 and kind != "yield" and not skip_scale:
        ratio = listing_m2 / ref_m2
        cap = 1.18 if bucket == "0" else 1.32
        floor = 0.80 if bucket == "0" else 0.72
        est *= min(cap, max(floor, ratio ** (0.32 if bucket == "0" else 0.4)))

    if kind != "similar-m2":
        bucket_cap = None
        if city and ptype and not _generic_name(barrio, city):
            bucket_cap = slice_.barrio_type_beds.get(f"{city}|{barrio}|{ptype}|{bucket}")
        if bucket_cap is None and city and ptype and not _generic_name(zona, city):
            bucket_cap = slice_.zona_type_beds.get(f"{city}|{zona}|{ptype}|{bucket}")
        if bucket_cap is None:
            bucket_cap = slice_.city_type_beds.get(f"{city}|{ptype}|{bucket}") or slice_.city_type.get(f"{city}|{ptype}")
        if bucket_cap and kind != "yield":
            est = min(bucket_cap * 1.38, max(bucket_cap * 0.70, est))

    if kind != "yield":
        est *= ASK_HAIRCUT
        if meta.get("lo"):
            meta["lo"] = float(meta["lo"]) * ASK_HAIRCUT
        if meta.get("hi"):
            meta["hi"] = float(meta["hi"]) * ASK_HAIRCUT
        if period == "monthly" and n < 5:
            cap = _gross_cap_usd(model, item, listing_m2)
            if cap and est > cap:
                est = cap

    blob = fold(f"{item.title} {item.description} {' '.join((item.extra or {}).get('amenities') or [])}")
    if not layout.conflict:
        if item.parking or "cochera" in blob:
            est *= 1.03
        if "pileta" in blob or "piscina" in blob:
            est *= 1.04 if period == "nightly" else 1.02
        if item.age_years and item.age_years >= 40:
            est *= 0.95
        if item.bathrooms and item.bathrooms >= 3 and (beds or 0) >= 3:
            est *= 1.02

    if period == "nightly":
        est = min(220.0, max(18.0, est))
    else:
        est = min(2200.0, max(80.0, est))

    if similar_prices and "lo" not in meta:
        lo, _mid, hi = _spread(similar_prices)
        if lo and hi:
            meta["lo"] = round(lo, 2)
            meta["hi"] = round(hi, 2)
    if meta.get("lo") is None:
        meta["lo"] = round(est * (0.82 if n < 5 or layout.conflict else 0.9), 2)
        meta["hi"] = round(est * (1.18 if n < 5 or layout.conflict else 1.1), 2)

    conf = layout.confidence
    if n < 3 or layout.conflict:
        conf = "low"
    elif n < 8:
        conf = "medium" if conf == "high" else conf
    meta["confidence"] = conf
    meta["kind"] = kind

    place = _place_name(item, kind)
    if kind == "similar-m2":
        names = {
            (row.get("barrio") or "").strip()
            for row in similar
            if not _generic_name((row.get("barrio") or "").strip(), city)
        }
        if len(names) == 1:
            place = next(iter(names))
    bits = [place]
    kind_word = _type_word(ptype)
    if kind_word:
        bits.append(kind_word)
    bits.append(layout.label)
    if listing_m2:
        bits.append(f"{listing_m2:.0f} m²")
    if kind == "yield":
        bits.append("por precio de venta")
    if layout.conflict:
        bits.append("señales mixtas")
    return round(est, 2), "según " + ", ".join(bits), n, meta


def estimate_rent(
    comps: list[dict], item: Listing, period: str, sale_m2: dict[str, float] | None = None
) -> tuple[float | None, str, int]:
    model = build_rent_model(comps, sale_m2)
    rent, scope, n, _meta = _predict_period(model, item, period)
    return rent, scope, n


def pick_comp(comps: list[dict], item: Listing, period: str) -> tuple[float | None, str, int]:
    return estimate_rent(comps, item, period)


def listing_rent_fp(item: Listing, model_fp: str) -> str:
    m2 = useful_m2(item) or 0
    return "|".join(
        [
            model_fp,
            item.city or "",
            item.property_type or "",
            item.barrio or "",
            item.zona or "",
            str(item.bedrooms or ""),
            str(item.rooms or ""),
            str(int(m2)),
            str(int(item.price_usd or 0)),
            str(int(item.price_m2 or 0)),
            str(item.parking or 0),
            str(item.bathrooms or 0),
            str(item.age_years or ""),
        ]
    )


def apply_yields(
    listings: list[Listing],
    comps: list[dict] | None = None,
    usd_ars: float | None = None,
    sale_m2: dict[str, float] | None = None,
    persist: bool | None = None,
) -> list[Listing]:
    from . import store

    store.init()
    if persist is None:
        persist = comps is None
    comps = comps if comps is not None else store.fetch_rentals()
    if sale_m2 is None and persist:
        sale_m2 = store.sale_m2_by_city()
    model = get_rent_model(comps, sale_m2 or {})
    dirty: list[Listing] = []
    for item in listings:
        extra = dict(item.extra or {})
        fp = listing_rent_fp(item, model.fingerprint)
        if extra.get("rent_fp") == fp and extra.get("rent_model_version") == RENT_MODEL_VERSION:
            if rentable(item) and (extra.get("monthly_rent_usd") or extra.get("nightly_usd")):
                item.extra = extra
                continue
            if not rentable(item) and extra.get("monthly_rent_usd") is None:
                item.extra = extra
                continue
        if not rentable(item):
            extra.update(_empty_yield())
            extra["rent_fp"] = fp
            extra["rent_model_version"] = RENT_MODEL_VERSION
            item.extra = extra
            dirty.append(item)
            continue
        city = item.city or default_city()
        occ = occupancy_for(city)
        month, month_scope, month_n, month_meta = _predict_period(model, item, "monthly")
        night, night_scope, night_n, night_meta = _predict_period(model, item, "nightly")
        if month and not night:
            night = round(min(220.0, max(18.0, night_from_month(month, occ))), 2)
            night_scope = month_scope + " · derivado del contrato"
            night_n = month_n
        elif night and not month:
            month = round(min(2200.0, max(80.0, month_from_night(night, occ))), 2)
            month_scope = night_scope + " · derivado del temporal"
            month_n = night_n
        monthly_yield = None
        temporal_yield = None
        if month and item.price_usd:
            net = month * 12 * (1 - MONTHLY_VACANCY - MONTHLY_COST)
            monthly_yield = round(min(20.0, max(0.0, 100 * net / item.price_usd)), 2)
        if night and item.price_usd:
            net = night * 365 * occ * (1 - TEMPORAL_COST)
            temporal_yield = round(min(35.0, max(0.0, 100 * net / item.price_usd)), 2)
        score, label, reasons = rental_score(item, monthly_yield, temporal_yield, month_n, night_n, occ)
        extra.update(
            {
                "monthly_rent_usd": month,
                "monthly_yield_pct": monthly_yield,
                "nightly_usd": night,
                "temporal_yield_pct": temporal_yield,
                "occupancy_pct": round(occ * 100),
                "rental_score": score,
                "rental_label": label,
                "rental_reasons": reasons,
                "rental_month_scope": month_scope,
                "rental_night_scope": night_scope,
                "rental_month_n": month_n,
                "rental_night_n": night_n,
                "monthly_rent_lo": month_meta.get("lo"),
                "monthly_rent_hi": month_meta.get("hi"),
                "rent_confidence": month_meta.get("confidence") or night_meta.get("confidence") or "",
                "layout_label": month_meta.get("layout") or "",
                "layout_conflict": bool(month_meta.get("conflict")),
                "rent_fp": fp,
                "rent_model_version": RENT_MODEL_VERSION,
            }
        )
        item.extra = extra
        dirty.append(item)
    from .profile import apply_profiles

    apply_profiles(listings)
    if persist:
        store.update_extras(listings)
        store.set_meta("rent_model_version", RENT_MODEL_VERSION)
        store.set_meta("rent_model_fp", model.fingerprint)
    return listings


def _empty_yield() -> dict:
    return {
        "monthly_rent_usd": None,
        "monthly_yield_pct": None,
        "nightly_usd": None,
        "temporal_yield_pct": None,
        "occupancy_pct": None,
        "rental_score": None,
        "rental_label": "",
        "rental_reasons": [],
        "rental_month_scope": "",
        "rental_night_scope": "",
        "rental_month_n": 0,
        "rental_night_n": 0,
        "monthly_rent_lo": None,
        "monthly_rent_hi": None,
        "rent_confidence": "",
        "layout_label": "",
        "layout_conflict": False,
    }


def rental_score(
    item: Listing,
    monthly_yield: float | None,
    temporal_yield: float | None,
    month_n: int,
    night_n: int,
    occ: float,
) -> tuple[float | None, str, list[str]]:
    if monthly_yield is None and temporal_yield is None:
        return None, "", ["Todavía no hay comps de alquiler para estimar la renta."]
    score = 38.0
    reasons: list[str] = []
    if monthly_yield is not None:
        score += max(-18, min(28, (monthly_yield - 5) * 4.5))
        reasons.append(f"mensual neto ~{monthly_yield:.1f}% anual")
    if temporal_yield is not None:
        score += max(-16, min(30, (temporal_yield - 8) * 2.6))
        reasons.append(f"temporal neto ~{temporal_yield:.1f}% anual (ocupación {round(occ * 100)}%)")
    beds = classify_listing(item).beds
    m2 = useful_m2(item)
    if item.property_type == "departamento" and beds in {1, 2}:
        score += 6
        reasons.append("depto 1-2 dorm: encaja bien para temporal")
    if item.property_type in {"casa", "ph"} and beds and beds >= 2:
        score += 4
        reasons.append("casa/PH con dormitorios: más fácil de alquilar mensual")
    if m2 and 35 <= m2 <= 90 and item.property_type == "departamento":
        score += 3
    if item.parking:
        score += 2
        reasons.append("tiene cochera")
    amenities = [fold(a) for a in ((item.extra or {}).get("amenities") or [])]
    blob = fold(f"{item.title} {item.description}")
    extras = 0
    for word, label in (("wifi", "wifi"), ("parrilla", "parrilla"), ("pileta", "pileta"), ("cochera", "cochera")):
        if word in blob or any(word in a for a in amenities):
            extras += 1
            if label != "cochera" or not item.parking:
                reasons.append(label)
    score += min(6, extras * 2)
    if item.has_exact_location:
        score += 2
    if month_n + night_n < 3:
        score -= 4
        reasons.append("pocos avisos de alquiler comparables")
    score = round(max(0, min(100, score)), 1)
    best_m = monthly_yield or -1
    best_t = temporal_yield or -1
    if best_m >= 7 or best_t >= 14:
        label = "renta fuerte"
    elif best_t >= best_m + 3 and best_t >= 8:
        label = "mejor temporal"
    elif best_m >= 5:
        label = "mejor mensual"
    elif best_m >= 3 or best_t >= 6:
        label = "renta razonable"
    else:
        label = "renta floja"
    return score, label, reasons[:7]


def yield_stats(listings: list[Listing], comps: list[dict] | None = None) -> dict:
    from . import store

    comps = comps if comps is not None else store.fetch_rentals()
    city = next((item.city for item in listings if item.city), "")
    city_comps = [row for row in comps if not city or (row.get("city") or "") == city]
    monthly_rents = [row["price_usd"] for row in city_comps if row.get("period") == "monthly" and row.get("price_usd")]
    nights = [
        _nightly_from_comp(row["price_usd"], row.get("period") or "nightly") or 0
        for row in city_comps
        if row.get("period") in {"nightly", "weekly"} and row.get("price_usd")
    ]
    nights = [n for n in nights if 18 <= n <= 220]
    myields = [float((item.extra or {}).get("monthly_yield_pct") or 0) for item in listings if (item.extra or {}).get("monthly_yield_pct")]
    tyields = [float((item.extra or {}).get("temporal_yield_pct") or 0) for item in listings if (item.extra or {}).get("temporal_yield_pct")]
    recommended = sum(1 for item in listings if ((item.extra or {}).get("rental_score") or 0) >= 62)
    occ = occupancy_for(city or default_city())
    return {
        "rent_comps_monthly": len(monthly_rents),
        "rent_comps_nightly": len(nights),
        "median_monthly_rent": round(_median(monthly_rents) or 0),
        "median_nightly": round(_median(nights) or 0, 1),
        "median_monthly_yield": round(_median(myields) or 0, 1),
        "median_temporal_yield": round(_median(tyields) or 0, 1),
        "occupancy_pct": round(occ * 100),
        "recommended": recommended,
    }
