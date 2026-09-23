from __future__ import annotations

import time
from typing import Any

from .access import compute_access, pin_grade, pin_is_precise
from .layout import classify_listing
from .models import Listing
from .osm_poi import CAT_LABEL
from .scoring import useful_m2

PROFILE_VERSION = "6"
AXES = ("price_m2", "zona", "ambientes", "alquiler", "servicios")
LOT_AXES = ("price_m2", "zona", "servicios")
AXIS_LABELS = {
    "price_m2": "USD/m²",
    "zona": "Zona",
    "ambientes": "Ambientes",
    "alquiler": "Alquiler",
    "servicios": "POIs cercanos",
}


def _clip(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return round(max(lo, min(hi, value)), 1)


def _axis(score: float | None, confidence: str, note: str, **extra: Any) -> dict[str, Any]:
    return {"score": None if score is None else _clip(score), "confidence": confidence, "note": note, **extra}


def _price_axis(item: Listing) -> dict[str, Any]:
    vs = item.vs_barrio_pct
    peers = int((item.extra or {}).get("peer_count") or 0)
    if vs is None:
        return _axis(None, "low", "sin comparables de venta")
    cheaper = float(vs)
    score = 50 + max(-48, min(48, cheaper * 1.75))
    conf = "high" if peers >= 6 and pin_is_precise(item) else ("medium" if peers >= 4 else "low")
    scope = (item.extra or {}).get("comp_scope") or "comparables"
    if cheaper > 0.4:
        note = f"{cheaper:.0f}% más barato el m² que {scope}"
    elif cheaper < -0.4:
        note = f"{abs(cheaper):.0f}% más caro el m² que {scope}"
    else:
        note = f"m² en línea con {scope}"
    return _axis(score, conf, note, peers=peers)


def _zona_axis(item: Listing) -> dict[str, Any]:
    if not pin_is_precise(item):
        return _axis(None, "none", "hace falta ubicación exacta")
    extra = item.extra or {}
    peers = int(extra.get("peer_count") or 0)
    barrio = (item.barrio or "").strip()
    score = 48.0
    if barrio and barrio.lower() != "sin clasificar":
        score += 10
    score += min(22.0, peers * 2.2)
    if extra.get("comp_scope"):
        score += 6
    conf = "high" if peers >= 6 else ("medium" if peers >= 3 else "low")
    return _axis(score, conf, extra.get("comp_scope") or barrio or "zona del pin", peers=peers)


def _ambientes_axis(item: Listing) -> dict[str, Any]:
    layout = classify_listing(item)
    m2 = useful_m2(item)
    rooms = layout.rooms or (1 if layout.bucket == "0" else None)
    if layout.conflict:
        return _axis(42, "low", "la ficha mezcla monoambiente y 1 dormitorio")
    if not m2 or not rooms:
        return _axis(None, "low", "faltan m² o ambientes")
    per = m2 / max(1, rooms)
    if item.property_type == "casa":
        ideal, span = 28.0, 18.0
    else:
        ideal, span = 24.0, 12.0
    score = 86 - min(40.0, abs(per - ideal) / span * 40)
    if layout.bucket == "0" and m2 > 40:
        score -= 8
    conf = layout.confidence
    return _axis(score, conf, f"{layout.label} · {per:.0f} m²/amb")


def _alquiler_axis(item: Listing) -> dict[str, Any]:
    extra = item.extra or {}
    score = extra.get("rental_score")
    n = int(extra.get("rental_month_n") or 0) + int(extra.get("rental_night_n") or 0)
    if score is None:
        return _axis(None, "low", extra.get("rental_month_scope") or "sin comps de alquiler")
    conf = extra.get("rent_confidence") or ("low" if n < 3 else "medium")
    if extra.get("layout_conflict"):
        conf = "low"
        score = min(float(score), 55)
    note = extra.get("rental_month_scope") or extra.get("rental_label") or ""
    if extra.get("monthly_rent_lo") and extra.get("monthly_rent_hi"):
        note = f"USD {extra['monthly_rent_lo']:.0f}–{extra['monthly_rent_hi']:.0f}/mes · {note}"
    return _axis(float(score), conf, note, n=n)


def _servicios_axis(item: Listing, access: dict[str, Any]) -> dict[str, Any]:
    if not access.get("precise"):
        return _axis(None, "none", access.get("reason") or "hace falta ubicación exacta")
    score = access.get("score")
    if score is None:
        return _axis(None, "low", access.get("reason") or "todavía no medimos los POIs a pie")
    bits = []
    for key, row in (access.get("categories") or {}).items():
        if not row.get("present"):
            continue
        km = row.get("km")
        label = CAT_LABEL.get(key, key)
        if km is not None:
            bits.append(f"{label} {int(round(km * 1000))} m" if km < 1 else f"{label} {km:.1f} km")
    meters = int(round(float(access.get("walk_km") or 0.6) * 1000))
    note = access.get("reason") or (" · ".join(bits) if bits else f"nada a menos de {meters} m")
    return _axis(float(score), "high", note, details=access.get("categories"), nearby=access.get("nearby") or [])


def _pending_state(axis: dict[str, Any]) -> str:
    note = (axis.get("note") or "").lower()
    if axis.get("confidence") == "none":
        return "blocked"
    if "faltan m²" in note or "ubicación exacta" in note or "pin aproximado" in note:
        return "blocked"
    return "pending"


def axes_for(item: Listing) -> tuple[str, ...]:
    if (item.property_type or "") == "terreno":
        return LOT_AXES
    return AXES


def pending_axes(axes: dict[str, Any] | None, order: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    rows = []
    for key in order or AXES:
        axis = (axes or {}).get(key) or {}
        if axis.get("score") is not None:
            continue
        rows.append(
            {
                "key": key,
                "label": AXIS_LABELS.get(key, key),
                "note": axis.get("note") or "todavía no se calculó",
                "state": _pending_state(axis),
            }
        )
    return rows


def total_score(axes: dict[str, Any] | None, order: tuple[str, ...] | None = None) -> dict[str, Any]:
    keys = order or AXES
    scores = []
    for key in keys:
        score = ((axes or {}).get(key) or {}).get("score")
        if score is not None:
            scores.append(float(score))
    pending = pending_axes(axes, keys)
    if not scores:
        return {"score": None, "n": 0, "of": len(keys), "pending": pending}
    return {
        "score": round(sum(scores) / len(scores), 1),
        "n": len(scores),
        "of": len(keys),
        "pending": pending,
    }


def stamp_profile(profile: dict[str, Any]) -> dict[str, Any]:
    axes = profile.get("axes") if isinstance(profile.get("axes"), dict) else {}
    raw_order = profile.get("order")
    order = tuple(raw_order) if isinstance(raw_order, (list, tuple)) and raw_order else AXES
    total = total_score(axes, order)
    profile["order"] = list(order)
    profile["version"] = profile.get("version") or PROFILE_VERSION
    profile["labels"] = {key: AXIS_LABELS[key] for key in order if key in AXIS_LABELS}
    profile["total"] = total.get("score")
    profile["total_n"] = total.get("n")
    profile["pending"] = total.get("pending") or []
    return profile


def compute_profile(item: Listing, pois: dict[str, list[dict]] | None = None) -> dict[str, Any]:
    extra = item.extra or {}
    stored = extra.get("access") if isinstance(extra.get("access"), dict) else {}
    have_pois = bool(pois) and any(pois.get(key) for key in pois)
    from .access import stored_access_ok

    if have_pois:
        access = compute_access(item, pois)
    elif stored_access_ok(item, stored):
        access = stored
    else:
        access = compute_access(item, pois)
    order = axes_for(item)
    builders = {
        "price_m2": lambda: _price_axis(item),
        "zona": lambda: _zona_axis(item),
        "ambientes": lambda: _ambientes_axis(item),
        "alquiler": lambda: _alquiler_axis(item),
        "servicios": lambda: _servicios_axis(item, access),
    }
    axes = {key: builders[key]() for key in order}
    return stamp_profile(
        {
            "version": PROFILE_VERSION,
            "order": list(order),
            "pin_grade": pin_grade(item),
            "axes": axes,
            "labels": {key: AXIS_LABELS[key] for key in order},
            "access": access,
        }
    )


def overlay_live_access(item: Listing, data: dict[str, Any]) -> dict[str, Any]:
    extra = item.extra or {}
    access = extra.get("access") if isinstance(extra.get("access"), dict) else {}
    data["access"] = access
    data["nearby"] = access.get("nearby") or []
    return data


def apply_profiles(listings: list[Listing]) -> list[Listing]:
    from .access import stored_access_ok
    from .osm_poi import city_pois

    pois_by_city: dict[str, dict[str, list[dict]]] = {}
    for i, item in enumerate(listings):
        extra = dict(item.extra or {})
        city = item.city or ""
        need_pois = pin_is_precise(item) and not stored_access_ok(item)
        pois: dict[str, list[dict]] = {}
        if need_pois:
            if city not in pois_by_city:
                pois_by_city[city] = city_pois(city)
            pois = pois_by_city[city]
        profile = compute_profile(item, pois)
        extra["profile"] = profile
        extra["pin_grade"] = profile.get("pin_grade")
        if profile.get("access") and (not need_pois or any(pois.get(key) for key in pois)):
            extra["access"] = profile["access"]
        item.extra = extra
        if i and i % 80 == 0:
            time.sleep(0.02)
    return listings
