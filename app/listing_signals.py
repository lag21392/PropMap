"""Señales de aviso (crédito, dueño, expensas, urgencia, entorno, estado).

El frontend ve campos de producto. Adentro se mezclan amenities del portal,
regex sobre el texto, el LLM y Laya (una pasada, solo para huecos).
"""
from __future__ import annotations

import logging
import re
from typing import Any

from .geo import fold
from .laya_client import CHOICE_MIN, LayaDecision, decide
from .models import Listing

SIGNALS_VER = "2"
REFILL_N = 80
log = logging.getLogger(__name__)
_refill_announced = False

OWNER_RE = re.compile(
    r"due[nñ]o\s+directo|propietario\s+vende|particular\s+vende|"
    r"sin\s+comisi[oó]n(?:es)?(?:\s+inmobiliaria)?|no\s+hay\s+inmobiliaria|"
    r"venta\s+directa(?:\s+del\s+propietario)?",
    re.I,
)
AGENCY_RE = re.compile(
    r"\binmobiliaria\b|\bmartillero\b|corredor\s+inmobiliario|\breal\s*estate\b",
    re.I,
)
URGENCY_RE = re.compile(
    r"vendo\s+urgente|venta\s+urgente|urgente\s+por\s+vender|"
    r"necesito\s+vender|me\s+voy\s+del\s+pa[ií]s|liquidaci[oó]n\s+urgente",
    re.I,
)
QUIET_RE = re.compile(
    r"silencios[ao]s?|muy\s+tranquil[ao]s?|zona\s+tranquil[ao]|"
    r"barrio\s+tranquil[ao]|calle\s+cortada|sin\s+ruido|poco\s+ruido",
    re.I,
)
LOW_EXPENSES_RE = re.compile(
    r"expensas\s+bajas|bajas\s+expensas|sin\s+expensas|no\s+paga\s+expensas|"
    r"expensas\s+nulas",
    re.I,
)
CONDITION_HINTS = (
    (re.compile(r"a\s+estrenar|\baestrenar\b", re.I), "a estrenar"),
    (re.compile(r"\ben\s+pozo\b|en\s+construcci[oó]n", re.I), "en pozo"),
    (re.compile(r"reciclado\s+a\s+nuevo|\breciclada?\b", re.I), "reciclado"),
    (re.compile(r"a\s+reciclar|a\s+refaccionar", re.I), "a reciclar"),
)
BALCONY_RE = re.compile(r"\bbalc[oó]n(?:es|cito)?\b", re.I)
NO_BALCONY_RE = re.compile(r"sin\s+balc[oó]n|no\s+tiene\s+balc[oó]n", re.I)
BRIGHT_RE = re.compile(
    r"mucha\s+luz|muy\s+luminos[ao]s?|\bluminos[ao]s?\b|luz\s+natural|"
    r"excelente\s+luminosidad|gran\s+luminosidad|gran\s+ingreso\s+de\s+luz|"
    r"lleno\s+de\s+luz|ambientes?\s+luminos",
    re.I,
)
GROWING_RE = re.compile(
    r"\b(?:barrios?|zonas?|sectors?)\b[\s\w]{0,24}?(?:en|con|de)\s+(?:mayor\s+)?(?:crecimiento|expansi[oó]n|desarrollo|auge)|"
    r"en\s+pleno\s+crecimiento",
    re.I,
)
OPEN_VIEW_RE = re.compile(
    r"vista\s+abierta|vistas?\s+abiertas|vista\s+panor[aá]mica|vista\s+despejada|"
    r"vista\s+al\s+mar|vista\s+al\s+golfo|frente\s+al\s+mar|vista\s+al\s+r[ií]o|"
    r"vista\s+al\s+lago|vista\s+plena",
    re.I,
)
PATIO_RE = re.compile(
    r"\bpatios?\b|jard[ií]n\s+propio|con\s+jard[ií]n|amplio\s+jard[ií]n|jard[ií]n\s+al\s+frente",
    re.I,
)
NO_PATIO_RE = re.compile(r"sin\s+patio|no\s+tiene\s+patio", re.I)
GARAGE_RE = re.compile(
    r"\bcocheras?\b|garage\s+propio|\bcon\s+garage\b|estacionamiento\s+propio",
    re.I,
)
NO_GARAGE_RE = re.compile(r"sin\s+cochera|no\s+tiene\s+cochera", re.I)
TERRACE_RE = re.compile(
    r"terraza\s+propia|terraza\s+exclusiva|terraza\s+descubierta|amplia\s+terraza|\bcon\s+terraza\b",
    re.I,
)
NO_TERRACE_RE = re.compile(
    r"terraza.{0,24}com[uú]n|terraza\s+del\s+edificio|sin\s+terraza",
    re.I,
)
REWRITE_KEYS = {
    "environment",
    "urgent_sale",
    "has_balcony",
    "bright",
    "growing_area",
    "open_view",
    "has_patio",
    "has_garage",
    "has_terrace",
}
CONDITION_MAP = {
    "brand_new": "a estrenar",
    "a estrenar": "a estrenar",
    "nuevo": "a estrenar",
    "under_construction": "en pozo",
    "en pozo": "en pozo",
    "en construcción": "en pozo",
    "en construccion": "en pozo",
    "recycled": "reciclado",
    "reciclado": "reciclado",
    "reciclada": "reciclado",
    "good": "bueno",
    "bueno": "bueno",
    "buena": "bueno",
    "to_rebuild": "a reciclar",
    "a reciclar": "a reciclar",
    "a refaccionar": "a reciclar",
}
GOOD_CONDITION = {"a estrenar", "reciclado", "bueno"}
SIGNAL_KEYS = (
    "mortgage_credit",
    "owner_direct",
    "low_expenses",
    "urgent_sale",
    "environment",
    "condition",
    "has_balcony",
    "bright",
    "growing_area",
    "open_view",
    "has_patio",
    "has_garage",
    "has_terrace",
)


def listing_blob(item: Listing) -> str:
    extra = item.extra or {}
    return " ".join(
        p
        for p in (
            item.title or "",
            item.address or "",
            item.publisher or "",
            item.description or "",
            extra.get("pdf_text") or "",
            " ".join(extra.get("amenities") or []),
        )
        if p
    )


def _copy_text(item: Listing) -> str:
    extra = item.extra or {}
    return " ".join(
        p
        for p in (item.title or "", item.address or "", item.description or "", extra.get("pdf_text") or "")
        if p
    )


def listing_state(item: Listing) -> dict[str, Any]:
    extra = item.extra or {}
    return {
        "titulo": item.title or "",
        "direccion": item.address or "",
        "tipo": item.property_type or "",
        "barrio": item.barrio or "",
        "publicador": item.publisher or "",
        "ambientes": item.rooms,
        "dormitorios": item.bedrooms,
        "banos": item.bathrooms,
        "m2_cubiertos": item.covered_m2,
        "m2_lote": item.total_m2,
        "expensas": extra.get("expenses"),
        "amenities": extra.get("amenities") or [],
        "descripcion": (item.description or "")[:1800],
    }


def _norm_condition(raw: Any) -> str:
    token = str(raw or "").strip().lower()
    return CONDITION_MAP.get(token, "")


def _portal_amenity(amenities: list[str], *labels: str) -> bool:
    """Etiquetas del portal (Balcón, Luminoso). No las que inventó extract_features en minúscula."""
    want = {fold(label) for label in labels if label}
    for raw in amenities:
        text = str(raw or "").strip()
        if not text or text == text.lower():
            continue
        if fold(text) in want:
            return True
    return False


def _flag(
    positive: re.Pattern[str],
    blob: str,
    *,
    amenity: bool = False,
    negative: re.Pattern[str] | None = None,
) -> bool | None:
    if negative and negative.search(blob):
        return False
    if amenity or positive.search(blob):
        return True
    return None


def _from_text(item: Listing) -> dict[str, Any]:
    extra = item.extra or {}
    blob = listing_blob(item)
    text = _copy_text(item)
    amenities = [str(a).lower() for a in (extra.get("amenities") or [])]
    portal = [str(a) for a in (extra.get("amenities") or [])]
    from .llm_fields import NO_CREDIT_RE
    from .features import CREDIT_RE

    credit = None
    if extra.get("publisher_direct") is True:
        owner = True
    elif extra.get("publisher_direct") is False:
        owner = False
    else:
        owner = None
    if OWNER_RE.search(blob):
        owner = True
    elif item.publisher and AGENCY_RE.search(item.publisher) and not OWNER_RE.search(blob):
        owner = False

    if NO_CREDIT_RE.search(blob):
        credit = False
    elif CREDIT_RE.search(blob) or any("apto crédito" in a or "apto credito" in a for a in amenities):
        credit = True

    low_exp = None
    if LOW_EXPENSES_RE.search(blob) or any("expensas bajas" in a or "sin expensas" in a for a in amenities):
        low_exp = True

    env = "quiet" if QUIET_RE.search(blob) else ""

    condition = ""
    for label in amenities:
        mapped = _norm_condition(label)
        if mapped:
            condition = mapped
            break
    if not condition:
        for hint, mapped in CONDITION_HINTS:
            if hint.search(blob):
                condition = mapped
                break

    return {
        "mortgage_credit": credit,
        "owner_direct": owner,
        "low_expenses": low_exp,
        "urgent_sale": True if URGENCY_RE.search(blob) else None,
        "environment": env,
        "condition": condition,
        "has_balcony": _flag(
            BALCONY_RE, text, amenity=_portal_amenity(portal, "balcón", "balcon"), negative=NO_BALCONY_RE
        ),
        "bright": _flag(
            BRIGHT_RE, text, amenity=_portal_amenity(portal, "luminoso", "luminosa")
        ),
        "growing_area": _flag(GROWING_RE, text),
        "open_view": _flag(OPEN_VIEW_RE, text, amenity=_portal_amenity(portal, "vista abierta", "vista panorámica")),
        "has_patio": _flag(
            PATIO_RE,
            text,
            amenity=_portal_amenity(portal, "patio", "jardín", "jardin"),
            negative=NO_PATIO_RE,
        ),
        "has_garage": True if item.parking else _flag(
            GARAGE_RE, text, amenity=_portal_amenity(portal, "cochera", "garage", "estacionamiento"), negative=NO_GARAGE_RE
        ),
        "has_terrace": _flag(
            TERRACE_RE,
            text,
            amenity=_portal_amenity(portal, "terraza"),
            negative=NO_TERRACE_RE,
        ),
    }


def _trusted_bool(flag: Any, conf: Any) -> bool | None:
    if flag is None:
        return None
    if (conf or 0) < CHOICE_MIN:
        return None
    return bool(flag)


def _from_laya_fields(item: Listing) -> dict[str, Any]:
    extra = item.extra or {}
    cond = _norm_condition(item.laya_property_condition)
    if item.laya_condition_confidence is not None and item.laya_condition_confidence < CHOICE_MIN:
        cond = ""
    env = (item.laya_environment_noise or "").strip()
    if env != "quiet" or (
        item.laya_noise_confidence is not None and item.laya_noise_confidence < CHOICE_MIN
    ):
        env = ""
    return {
        "mortgage_credit": _trusted_bool(item.laya_is_mortgage_eligible, item.laya_mortgage_confidence),
        "owner_direct": _trusted_bool(item.laya_is_owner_direct, item.laya_owner_confidence),
        "low_expenses": _trusted_bool(item.laya_has_low_expenses, item.laya_expenses_confidence),
        "urgent_sale": _trusted_bool(item.laya_shows_urgency, item.laya_urgency_confidence),
        "environment": env,
        "condition": cond,
        "has_balcony": _trusted_bool(extra.get("laya_has_balcony"), extra.get("laya_balcony_confidence")),
        "bright": _trusted_bool(extra.get("laya_is_bright"), extra.get("laya_bright_confidence")),
        "growing_area": _trusted_bool(extra.get("laya_growing_area"), extra.get("laya_growing_confidence")),
        "open_view": _trusted_bool(extra.get("laya_open_view"), extra.get("laya_view_confidence")),
        "has_patio": _trusted_bool(extra.get("laya_has_patio"), extra.get("laya_patio_confidence")),
        "has_garage": _trusted_bool(extra.get("laya_has_garage"), extra.get("laya_garage_confidence")),
        "has_terrace": _trusted_bool(extra.get("laya_has_terrace"), extra.get("laya_terrace_confidence")),
    }


def _llm_bits(item: Listing) -> dict[str, Any]:
    extra = item.extra or {}
    llm = extra.get("llm") if isinstance(extra.get("llm"), dict) else {}
    return {
        "mortgage_credit": extra.get("mortgage_credit"),
        "owner_direct": extra.get("owner_direct"),
        "low_expenses": extra.get("low_expenses"),
        "urgent_sale": extra.get("urgent_sale"),
        "environment": extra.get("environment") or "",
        "condition": extra.get("condition") or _norm_condition(llm.get("condition")),
        "has_balcony": extra.get("has_balcony"),
        "bright": extra.get("bright"),
        "growing_area": extra.get("growing_area"),
        "open_view": extra.get("open_view"),
        "has_patio": extra.get("has_patio"),
        "has_garage": extra.get("has_garage"),
        "has_terrace": extra.get("has_terrace"),
    }


def _first_bool(*values: Any) -> bool | None:
    for value in values:
        if value is True or value is False:
            return value
    return None


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def signals_for(item: Listing) -> dict[str, Any]:
    stored = _llm_bits(item)
    guessed = _from_text(item)
    laya = _from_laya_fields(item)
    return {
        "mortgage_credit": _first_bool(
            stored["mortgage_credit"], guessed["mortgage_credit"], laya["mortgage_credit"]
        ),
        "owner_direct": _first_bool(stored["owner_direct"], guessed["owner_direct"], laya["owner_direct"]),
        "low_expenses": _first_bool(stored["low_expenses"], guessed["low_expenses"], laya["low_expenses"]),
        "urgent_sale": _first_bool(guessed["urgent_sale"], laya["urgent_sale"]),
        "environment": _first_text(guessed["environment"], laya["environment"]),
        "condition": _first_text(stored["condition"], guessed["condition"], laya["condition"]),
        "has_balcony": _first_bool(guessed["has_balcony"], laya["has_balcony"]),
        "bright": _first_bool(guessed["bright"], laya["bright"]),
        "growing_area": _first_bool(guessed["growing_area"], laya["growing_area"]),
        "open_view": _first_bool(guessed["open_view"], laya["open_view"]),
        "has_patio": _first_bool(guessed["has_patio"], laya["has_patio"]),
        "has_garage": _first_bool(guessed["has_garage"], laya["has_garage"]),
        "has_terrace": _first_bool(guessed["has_terrace"], laya["has_terrace"]),
    }


def apply_signals(item: Listing) -> Listing:
    resolved = signals_for(item)
    extra = dict(item.extra or {})
    for key in SIGNAL_KEYS:
        value = resolved.get(key)
        if key in REWRITE_KEYS:
            extra[key] = value or "" if key == "environment" else value
            continue
        if value in {None, ""}:
            continue
        extra[key] = value
    item.extra = extra
    apply_type_correction(item)
    extra = dict(item.extra or {})
    extra["signals_ver"] = SIGNALS_VER
    item.extra = extra
    return item


def needs_signals(item: Listing) -> bool:
    extra = item.extra or {}
    if extra.get("duplicate_of") or extra.get("dedupe_hidden"):
        return False
    return str(extra.get("signals_ver") or "") != SIGNALS_VER


def refill(prefer_city: str = "") -> int:
    """Corrige etiquetas y tipo del backlog. Lo nuevo entra por analyze/scrape."""
    global _refill_announced
    from . import store

    items = store.fetch_signals_backlog(REFILL_N, prefer_city=prefer_city, ver=SIGNALS_VER)
    take = [item for item in items if needs_signals(item)]
    if not take:
        return 0
    for item in take:
        apply_signals(item)
    store.upsert_listings(take, notify=False)
    more = len(items) >= REFILL_N
    if not _refill_announced:
        log.warning("encolé la corrección de etiquetas (lote de %s)", len(take))
        _refill_announced = True
    elif not more:
        log.warning("terminé la corrección de etiquetas (%s avisos en el último lote)", len(take))
    return len(take)


def _decisions_map(decisions: list[LayaDecision]) -> dict[str, LayaDecision]:
    return {d.question_key: d for d in decisions}


def apply_type_correction(item: Listing) -> Listing:
    """Terreno del portal que en realidad es una casa/PH."""
    from .property_kind import TYPE_FIX_NOTE, false_lot_type

    extra = dict(item.extra or {})
    kind = str(extra.get("laya_property_kind") or "").strip()
    kind_conf = float(extra.get("laya_kind_confidence") or 0)
    contains = extra.get("laya_contains_dwelling")
    contains_conf = float(extra.get("laya_dwelling_confidence") or 0)
    new_type = ""
    if item.property_type == "terreno":
        if kind in {"casa", "ph", "departamento"} and kind_conf >= 0.62:
            new_type = kind
        elif contains is True and contains_conf >= 0.62:
            new_type = "casa"
        else:
            new_type = false_lot_type(item)
    if not new_type or new_type == item.property_type:
        return item
    extra["portal_type"] = extra.get("portal_type") or item.property_type
    extra["type_fix"] = new_type
    fixes = list(extra.get("data_fixes") or [])
    if TYPE_FIX_NOTE not in fixes:
        fixes.append(TYPE_FIX_NOTE)
    extra["data_fixes"] = fixes
    item.property_type = new_type
    item.extra = extra
    return item


def _store_noul(item: Listing, found: dict[str, LayaDecision], key: str, flag_attr: str, conf_attr: str) -> None:
    hit = found.get(key)
    if not hit:
        return
    extra = dict(item.extra or {})
    extra[flag_attr] = hit.answer if isinstance(hit.answer, bool) else None
    extra[conf_attr] = hit.confidence
    item.extra = extra


def apply_laya_decisions(item: Listing, decisions: list[LayaDecision]) -> Listing:
    found = _decisions_map(decisions)
    quality = found.get("quality_score")
    if quality and quality.answer is not None:
        item.laya_quality_score = (float(quality.answer) / 4.0) * 100.0
        item.laya_quality_confidence = quality.confidence
    owner = found.get("is_owner_direct")
    if owner:
        item.laya_is_owner_direct = owner.answer if isinstance(owner.answer, bool) else None
        item.laya_owner_confidence = owner.confidence
    credit = found.get("is_mortgage_eligible")
    if credit:
        item.laya_is_mortgage_eligible = credit.answer if isinstance(credit.answer, bool) else None
        item.laya_mortgage_confidence = credit.confidence
    expenses = found.get("has_low_expenses")
    if expenses:
        item.laya_has_low_expenses = expenses.answer if isinstance(expenses.answer, bool) else None
        item.laya_expenses_confidence = expenses.confidence
    urgency = found.get("shows_urgency")
    if urgency:
        item.laya_shows_urgency = urgency.answer if isinstance(urgency.answer, bool) else None
        item.laya_urgency_confidence = urgency.confidence
    noise = found.get("environment_noise")
    if noise and noise.confidence >= CHOICE_MIN and noise.answer:
        item.laya_environment_noise = str(noise.answer)
        item.laya_noise_confidence = noise.confidence
    condition = found.get("property_condition")
    if condition and condition.confidence >= CHOICE_MIN and condition.answer:
        item.laya_property_condition = str(condition.answer)
        item.laya_condition_confidence = condition.confidence
    _store_noul(item, found, "has_balcony", "laya_has_balcony", "laya_balcony_confidence")
    _store_noul(item, found, "is_bright", "laya_is_bright", "laya_bright_confidence")
    _store_noul(item, found, "growing_area", "laya_growing_area", "laya_growing_confidence")
    _store_noul(item, found, "open_view", "laya_open_view", "laya_view_confidence")
    _store_noul(item, found, "has_patio", "laya_has_patio", "laya_patio_confidence")
    _store_noul(item, found, "has_garage", "laya_has_garage", "laya_garage_confidence")
    _store_noul(item, found, "has_terrace", "laya_has_terrace", "laya_terrace_confidence")
    _store_noul(item, found, "contains_dwelling", "laya_contains_dwelling", "laya_dwelling_confidence")
    kind = found.get("property_kind")
    if kind and kind.confidence >= CHOICE_MIN and kind.answer:
        extra = dict(item.extra or {})
        extra["laya_property_kind"] = str(kind.answer)
        extra["laya_kind_confidence"] = kind.confidence
        item.extra = extra
    return item


def enrich_with_laya(item: Listing) -> Listing:
    """Una pasada de Laya y fusión con lo que el portal/regex ya saben."""
    try:
        decisions = decide(listing_state(item))
        if decisions:
            apply_laya_decisions(item, decisions)
            extra = dict(item.extra or {})
            extra["laya_enriched"] = True
            item.extra = extra
    except Exception:
        pass
    return apply_signals(item)
