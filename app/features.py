from __future__ import annotations

import re

from .models import Listing

TYPE_LABEL = {
    "casa": "Casa",
    "departamento": "Depto",
    "ph": "PH",
    "terreno": "Terreno",
    "local": "Local",
    "oficina": "Oficina",
    "galpon": "Galpón",
}
CREDIT_RE = re.compile(
    r"apto\s+cr[eé]dito|cr[eé]dito\s+hipotecario|\buva\b|procrear|apto\s+bancario",
    re.I,
)

FEATURE_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("cochera", ("cochera", "garage", "estacionamiento", "parking")),
    ("balcón", ("balcon", "balcón", "terraza")),
    ("patio", ("patio", "jardin", "jardín", "parque")),
    ("pileta", ("pileta", "piscina", "pool")),
    ("parrilla", ("parrilla", "quincho", "asador")),
    ("ascensor", ("ascensor", "elevador")),
    ("luminoso", ("luminoso", "luminosa", "luz natural", "mucho sol")),
    ("vista al mar", ("vista al mar", "vista al golfo", "frente al mar", "al agua")),
    ("apto crédito", ("apto credito", "apto crédito", "apto bancario", "credito hipotecario", "crédito hipotecario", "procrear")),
    ("reciclado", ("reciclado", "reciclada", "a nuevo", "refaccionado")),
    ("calefacción", ("calefaccion", "calefacción", "piso radiante")),
    ("aire acondicionado", ("aire acondicionado", "split")),
    ("seguridad", ("seguridad 24", "barrio cerrado", "country", "portero", "vigilancia")),
    ("expensas bajas", ("expensas bajas", "bajas expensas", "sin expensas")),
    ("cocina integrada", ("cocina integrada", "cocina americana")),
    ("lavadero", ("lavadero", "laundry")),
    ("servicios", ("todos los servicios", "luz y agua", "cloacas", "gas natural", "con asfalto")),
    ("escritura", ("escritura", "escriturado", "titulo perfecto", "título perfecto")),
]


RED_FLAGS: list[tuple[str, tuple[str, ...]]] = [
    ("posible error de carga", ("u$s 1", "usd 1 ", "precio 1 ", "$ 1 ", "consultar precio 1")),
    ("a demoler / ruina", ("a demoler", "para demoler", "en ruina", "a reciclar urgente")),
    ("problema legal", ("usucapion", "usucapión", "embargado", "con embargo", "en juicio", "okupa", "sin escritura", "sin escriturar", "posesion veinte", "posesión veinte")),
    ("no es venta pura", ("se permuta", "se toma propiedad", "financiacion 100", "financiación 100")),
    ("lote irregular", ("lote irregular", "terreno irregular", "zona inundable", "bajo cota")),
]


def extract_features(text: str) -> list[str]:
    blob = (text or "").lower()
    found: list[str] = []
    for label, keys in FEATURE_RULES:
        if any(key in blob for key in keys) and label not in found:
            found.append(label)
    return found


def extract_expenses(text: str) -> float | None:
    match = re.search(r"expensas[^0-9]{0,12}(\d[\d\.]{2,8})", text or "", re.I)
    if not match:
        return None
    raw = match.group(1).replace(".", "")
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if 1000 <= value <= 2_000_000 else None


def analyze(item: Listing) -> Listing:
    extra = dict(item.extra or {})
    blob = " ".join(
        [
            item.title or "",
            item.address or "",
            item.description or "",
            extra.get("pdf_text") or "",
            " ".join(extra.get("amenities") or []),
        ]
    )
    amenities = list(dict.fromkeys((extra.get("amenities") or []) + extract_features(blob)))
    extra["amenities"] = amenities
    if extra.get("expenses") in (None, 0, "0"):
        expenses = extract_expenses(blob)
        if expenses:
            extra["expenses"] = expenses
    if extra.get("mortgage_credit") is None and CREDIT_RE.search(blob):
        extra["mortgage_credit"] = True
    item.extra = extra
    fill_areas(item, blob)
    from .layout import apply_layout_counts

    apply_layout_counts(item)
    item.quality_score = _quality(item, amenities)
    item.quality_label = _quality_label(item.quality_score)
    return item


def extract_uncovered(text: str) -> float | None:
    blob = text or ""
    found: list[float] = []
    for pattern in (
        r"(\d[\d\.]{1,6})\s*m[²2]?\s*(?:descubiertos?|semicubiertos?|semi[\s-]?cubiertos?)",
        r"(?:descubiertos?|semicubiertos?|semi[\s-]?cubiertos?)\D{0,16}(\d[\d\.]{1,6})\s*m[²2]",
    ):
        found.extend(_area_all(blob, pattern))
    return max(found) if found else None


def combine_areas(
    covered: float | None,
    total: float | None,
    uncovered: float | None = None,
    *,
    property_type: str = "",
) -> tuple[float | None, float | None]:
    """Cubiertos y totales. Si hay descubiertos y no hay lote distinto, se suman."""
    cov = covered if covered and covered > 0 else None
    tot = total if total and total > 0 else None
    unc = uncovered if uncovered and uncovered > 0 else None
    if cov and tot and (tot + 0.5) < cov:
        tot = None
    copied = bool(cov and tot and abs(tot - cov) < 1)
    if cov and unc:
        summed = cov + unc
        if not tot or copied or (tot < summed and tot <= cov + 15):
            tot = summed
    elif copied:
        tot = None
    if cov and not tot and property_type in {"departamento", "ph", "oficina", "local"}:
        tot = cov
    return cov, tot


def extract_areas(text: str) -> tuple[float | None, float | None]:
    blob = text or ""
    covered = _area_match(blob, r"(\d[\d\.]{1,6})\s*m[²2]?\s*(?:cubiertos?|cub)")
    lots: list[float] = []
    for pattern in (
        r"(\d[\d\.]{1,6})\s*m[²2]?\s*(?:de\s*)?(?:terreno|lote|totales?)",
        r"(?:terreno|lote|patio)\b[\s\wÁÉÍÓÚáéíóúüñ°²,.:;/-]{0,120}?(\d[\d\.]{1,6})\s*m[²2]",
    ):
        lots.extend(_area_all(blob, pattern))
    lot = max(lots) if lots else None
    covered, lot = combine_areas(covered, lot, extract_uncovered(blob))
    return covered, lot


def fill_areas(item: Listing, blob: str | None = None) -> Listing:
    text = blob or " ".join([item.title or "", item.address or "", item.description or ""])
    covered, lot = extract_areas(text)
    if not item.covered_m2 and covered:
        item.covered_m2 = covered
    if lot:
        covered_now = item.covered_m2 or covered
        copied = bool(
            item.total_m2 and covered_now and abs(float(item.total_m2) - float(covered_now)) < 1
        )
        if not item.total_m2 or copied or float(item.total_m2 or 0) + 9 < lot:
            item.total_m2 = lot
    if item.covered_m2 and not item.total_m2 and item.property_type in {"departamento", "ph", "oficina", "local"}:
        item.total_m2 = item.covered_m2
    if item.property_type == "terreno":
        if item.covered_m2 and (not item.total_m2 or item.total_m2 < 40):
            if item.covered_m2 >= 80:
                item.total_m2 = item.covered_m2
                item.covered_m2 = None
        elif item.covered_m2 and item.total_m2 and abs(item.covered_m2 - item.total_m2) < 1:
            item.covered_m2 = None
    return item


def _area_match(text: str, pattern: str) -> float | None:
    match = re.search(pattern, text or "", re.I)
    if not match:
        return None
    raw = match.group(1).replace(".", "")
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if 10 <= value <= 500_000 else None


def _area_all(text: str, pattern: str) -> list[float]:
    out: list[float] = []
    for match in re.finditer(pattern, text or "", re.I):
        raw = match.group(1).replace(".", "")
        try:
            value = float(raw)
        except ValueError:
            continue
        if 10 <= value <= 500_000:
            out.append(value)
    return out


def scan_red_flags(item: Listing) -> list[str]:
    blob = " ".join(
        [
            item.title or "",
            item.description or "",
            " ".join((item.extra or {}).get("amenities") or []),
            (item.extra or {}).get("pdf_text") or "",
        ]
    ).lower()
    found: list[str] = []
    for label, keys in RED_FLAGS:
        if any(key in blob for key in keys) and label not in found:
            found.append(label)
    if item.price_usd and item.price_usd < (4000 if item.property_type == "terreno" else 8000):
        found.append("precio demasiado bajo")
    return found


def _quality(item: Listing, amenities: list[str]) -> float:
    score = 48.0
    if item.vs_barrio_pct is not None:
        score += max(-28, min(28, float(item.vs_barrio_pct) * 0.7))
    m2 = item.covered_m2 or item.total_m2
    if item.property_type == "departamento" and m2:
        if 45 <= m2 <= 90:
            score += 8
        elif m2 >= 70:
            score += 5
    if item.property_type == "terreno":
        lot = item.total_m2 or item.covered_m2
        if lot and 80 <= lot <= 80_000:
            score += 6
        if "servicios" in amenities:
            score += 5
        if "escritura" in amenities:
            score += 4
    if item.property_type == "casa" and m2 and 80 <= m2 <= 220:
        score += 6
    if item.property_type == "ph":
        score += 3
    if item.bedrooms and item.bedrooms >= 3:
        score += 4
    if item.bathrooms and item.bathrooms >= 2:
        score += 2
    if item.parking or "cochera" in amenities:
        score += 6
    bonus = {
        "balcón": 3,
        "luminoso": 3,
        "apto crédito": 4,
        "vista al mar": 6,
        "pileta": 2,
        "patio": 3,
        "ascensor": 2,
        "reciclado": 3,
        "expensas bajas": 3,
        "seguridad": 2,
    }
    score += sum(bonus.get(name, 0) for name in amenities)
    if item.age_years is not None:
        if item.age_years <= 10:
            score += 4
        elif item.age_years >= 40:
            score -= 3
    expenses = extra_expenses(item)
    if expenses and expenses > 250000:
        score -= 4
    if item.price_usd and item.price_usd < 25000 and item.property_type != "terreno":
        score -= 18
    if item.has_exact_location:
        score += 2
    return round(max(0, min(100, score)), 1)


def extra_expenses(item: Listing) -> float | None:
    value = (item.extra or {}).get("expenses")
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _quality_label(score: float | None) -> str:
    if score is None:
        return ""
    if score >= 78:
        return "excelente relación"
    if score >= 66:
        return "muy buena relación"
    if score >= 54:
        return "buena relación"
    return "relación justa"


def type_label(property_type: str) -> str:
    return TYPE_LABEL.get(property_type, property_type or "Aviso")
