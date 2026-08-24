from __future__ import annotations

import re
import unicodedata


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text.lower()).strip()


WEAK_PLACES = {
    "puerto madryn", "trelew", "rawson", "gaiman", "playa union",
    "microcentro", "chubut", "biedma", "capital federal", "buenos aires",
    "argentina", "patagonia",
}


def address_quality(text: str) -> int:
    t = (text or "").strip()
    folded = fold(t)
    if len(folded) < 6:
        return 0
    if folded in WEAK_PLACES:
        return 0
    score = 1
    if re.search(r"\d{1,5}", t):
        score += 4
    if re.search(r"\b(calle|av\.?|avenida|pasaje|ruta)\b", folded):
        score += 3
    if "," in t:
        score += 1
    if any(place in folded for place in WEAK_PLACES) and score < 5:
        score -= 1
    return score


def title_quality(text: str) -> int:
    folded = fold(text)
    if len(folded) < 8:
        return 0
    if "publicado hace" in folded:
        return 0
    generic = {
        "casa en venta", "departamento en venta", "terreno en venta",
        "ph en venta", "inmueble en venta", "propiedad en venta",
    }
    if folded in generic:
        return 1
    score = min(24, len(folded) // 6)
    if "multifamiliar" in folded or "duplex" in folded or "dúplex" in folded:
        score += 2
    return score


def clean_portal_address(text: str) -> str:
    raw = re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip(" ,")
    raw = re.sub(r"\b[A-Z]\d{4}[A-Z]?\b", "", raw)
    parts = [p.strip(" ,") for p in raw.split(",")]
    out: list[str] = []
    seen: set[str] = set()
    skip = {"argentina", ""}
    for part in parts:
        key = fold(part)
        if key in skip or key in seen:
            continue
        seen.add(key)
        out.append(part)
    return ", ".join(out)
