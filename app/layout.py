from __future__ import annotations

import re
from dataclasses import dataclass

from .geo import fold
from .models import Listing
from .scoring import useful_m2

_STUDIO_RE = re.compile(r"\bmonoamb|\bmono ambiente\b|\b(1|un)\s+ambientes?\b")
_ONE_DORM_RE = re.compile(r"\b(1|un)\s+dorm")
_AMB_RE = re.compile(r"\b(\d{1,2})\s*ambientes?\b")
_DWELLING = frozenset({"casa", "departamento", "ph"})


@dataclass(frozen=True)
class Layout:
    """Distribución inferida. bucket 0 = monoambiente."""

    bucket: str
    beds: int | None
    rooms: int | None
    confidence: str
    conflict: bool
    label: str


def _as_int(value) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


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


def classify(
    *,
    title: str = "",
    description: str = "",
    rooms: int | None = None,
    bedrooms: int | None = None,
    m2: float | None = None,
    property_type: str = "",
) -> Layout:
    blob = fold(f"{title} {description}")
    rooms_n = _as_int(rooms)
    beds_raw = _as_int(bedrooms)
    studio_words = bool(_STUDIO_RE.search(blob) or "monoambiente" in blob)
    one_dorm = bool(_ONE_DORM_RE.search(blob))
    compact = bool(m2 and m2 <= 36)
    large = bool(m2 and m2 > 40)

    if studio_words and (one_dorm or beds_raw == 1) and large:
        return Layout("1", 1, rooms_n or 2, "low", True, "1 amb amplio")
    if one_dorm and not studio_words:
        return _from_beds(1, rooms_n, "high", False)
    if studio_words or (rooms_n == 1 and not one_dorm and (m2 is None or compact)):
        if large and not studio_words:
            return _from_beds(1, rooms_n, "medium", False)
        return Layout("0", 0, rooms_n or 1, "high" if studio_words or compact else "medium", False, "monoamb")
    if rooms_n == 1 and compact and (beds_raw is None or beds_raw <= 1):
        return Layout("0", 0, rooms_n or 1, "medium", False, "monoamb")

    from_rooms = None
    if rooms_n is not None:
        from_rooms = max(1, rooms_n - 1) if rooms_n >= 2 else 1
    if beds_raw is not None and from_rooms is not None:
        beds = min(beds_raw, from_rooms)
    elif beds_raw is not None:
        beds = beds_raw
    elif from_rooms is not None:
        beds = from_rooms
    else:
        beds = _beds_from_m2(m2, property_type)
    conf = "high" if beds_raw is not None or rooms_n is not None else ("medium" if beds is not None else "low")
    return _from_beds(beds, rooms_n, conf, False)


def _from_beds(beds: int | None, rooms: int | None, confidence: str, conflict: bool) -> Layout:
    if rooms is None:
        if beds is not None and beds <= 0:
            rooms = 1
        elif beds:
            rooms = beds + 1
    bucket = _beds_bucket(beds)
    if bucket == "0":
        label = "monoamb"
    elif rooms and rooms > 0:
        label = f"{int(rooms)} amb"
    elif beds:
        label = f"{bucket} dorm"
    else:
        label = "sin amb"
    return Layout(bucket, beds, rooms, confidence, conflict, label)


def _amb_from_text(title: str = "", description: str = "") -> int | None:
    blob = fold(f"{title} {description}")
    hit = _AMB_RE.search(blob)
    if not hit:
        return None
    return _as_int(hit.group(1))


def apply_layout_counts(item: Listing) -> Listing:
    """Si hay dormitorios y no ambientes, en Argentina el mínimo es dormis + estar."""
    if (item.property_type or "") not in _DWELLING:
        return item
    if item.rooms is not None:
        return item
    named = _amb_from_text(item.title or "", item.description or "")
    if named:
        item.rooms = named
        return item
    layout = classify_listing(item)
    if layout.rooms:
        item.rooms = int(layout.rooms)
    return item


def _beds_from_m2(m2: float | None, property_type: str) -> int | None:
    if not m2:
        return None
    if property_type == "departamento":
        if m2 <= 32:
            return 0
        if m2 < 40:
            return 1
        if m2 < 65:
            return 2
        return 3
    if m2 < 60:
        return 1
    if m2 < 90:
        return 2
    return 3


def classify_listing(item: Listing) -> Layout:
    return classify(
        title=item.title or "",
        description=item.description or "",
        rooms=item.rooms,
        bedrooms=item.bedrooms,
        m2=useful_m2(item),
        property_type=item.property_type or "",
    )


def classify_comp(row: dict) -> Layout:
    try:
        m2 = float(row.get("covered_m2") or 0)
    except (TypeError, ValueError):
        m2 = 0.0
    return classify(
        title=row.get("title") or "",
        description=str((row.get("extra") or {}).get("description") or "") if isinstance(row.get("extra"), dict) else "",
        rooms=row.get("rooms"),
        bedrooms=row.get("bedrooms"),
        m2=m2 if 15 <= m2 <= 800 else None,
        property_type=row.get("property_type") or "",
    )
