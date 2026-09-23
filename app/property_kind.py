"""Lote vacío vs vivienda: el portal a menudo etiqueta 'terreno' una casa sobre lote."""
from __future__ import annotations

import re

from .models import Listing

VACANT_INTENT_RE = re.compile(
    r"para\s+construir|ideal\s+para\s+construir|para\s+edificar|"
    r"proyecto\s+de\s+casa|lote\s+bald[ií]o|terreno\s+bald[ií]o|"
    r"sin\s+(?:construcci[oó]n|edificaci[oó]n)|para\s+que\s+construyas|"
    r"listo\s+para\s+construir",
    re.I,
)
HOUSE_ON_LOT_RE = re.compile(
    r"(?:lote|terreno|fracci[oó]n)\s+con\s+(?:casa|vivienda|chalet|caba[nñ]a|d[uú]plex|\bph\b)|"
    r"(?:casa|vivienda|chalet)\s+(?:construida\s+)?(?:sobre|en)\s+(?:el\s+)?(?:lote|terreno)|"
    r"vivienda\s+existente|construcci[oó]n\s+existente|"
    r"casa\s+a\s+terminar",
    re.I,
)
PH_RE = re.compile(r"\bph\b|d[uú]plex|triplex", re.I)
TYPE_FIX_NOTE = "no es lote vacío: el aviso describe una vivienda"


def vacant_lot_intent(text: str) -> bool:
    return bool(VACANT_INTENT_RE.search(text or ""))


def dwelling_on_lot(text: str) -> bool:
    blob = text or ""
    if vacant_lot_intent(blob):
        return False
    return bool(HOUSE_ON_LOT_RE.search(blob))


def dwelling_type_from_text(text: str) -> str:
    if not dwelling_on_lot(text):
        return ""
    return "ph" if PH_RE.search(text or "") else "casa"


def false_lot_type(item: Listing) -> str:
    """Si está como terreno pero el aviso es una vivienda, devolver casa/ph."""
    if (item.property_type or "") != "terreno":
        return ""
    extra = item.extra or {}
    blob = " ".join(
        p
        for p in (item.title or "", item.address or "", item.description or "", extra.get("pdf_text") or "")
        if p
    )
    if vacant_lot_intent(blob):
        return ""
    guessed = dwelling_type_from_text(blob)
    if guessed:
        return guessed
    bedrooms = item.bedrooms or 0
    bathrooms = item.bathrooms or 0
    covered = item.covered_m2 or 0
    if bedrooms >= 1 and bathrooms >= 1 and covered >= 30:
        return "ph" if PH_RE.search(blob) else "casa"
    return ""
