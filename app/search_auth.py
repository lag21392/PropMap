from __future__ import annotations

import os
import secrets

from fastapi import HTTPException


def search_password() -> str:
    return (os.environ.get("SEARCH_PASSWORD") or "").strip()


def _candidates() -> list[str]:
    seen: list[str] = []
    for key in ("SEARCH_PASSWORD", "MATOMO_PASSWORD"):
        value = (os.environ.get(key) or "").strip()
        if value and value not in seen:
            seen.append(value)
    return seen


def password_matches(got: str | None) -> bool:
    provided = (got or "").encode()
    if not provided:
        return False
    for expected in _candidates():
        want = expected.encode()
        if len(provided) == len(want) and secrets.compare_digest(provided, want):
            return True
    return False


def require_search_password(got: str | None) -> None:
    if password_matches(got):
        return
    raise HTTPException(status_code=401, detail="Contraseña incorrecta")


def _filter_value(filters: dict, *keys):
    for key in keys:
        value = filters.get(key)
        if value is None or value == "" or value == []:
            continue
        return value
    return None


def _num(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _listing_m2(item) -> float:
    lot = _num(getattr(item, "total_m2", None))
    covered = _num(getattr(item, "covered_m2", None))
    if getattr(item, "property_type", "") == "terreno":
        return lot or covered
    return covered or lot


def _bedrooms_of(item) -> float | None:
    kind = getattr(item, "property_type", "") or ""
    if kind in {"terreno", "local", "oficina", "galpon"}:
        return None
    beds = getattr(item, "bedrooms", None)
    if beds is not None:
        try:
            return float(beds)
        except (TypeError, ValueError):
            pass
    rooms = getattr(item, "rooms", None)
    try:
        rooms_n = float(rooms)
    except (TypeError, ValueError):
        return None
    if rooms_n > 0:
        return max(0, rooms_n - 1)
    return None


def _rooms_of(item) -> float | None:
    kind = getattr(item, "property_type", "") or ""
    if kind in {"terreno", "local", "oficina", "galpon"}:
        return None
    rooms = getattr(item, "rooms", None)
    try:
        rooms_n = float(rooms)
    except (TypeError, ValueError):
        rooms_n = 0
    if rooms_n > 0:
        return rooms_n
    beds = _bedrooms_of(item)
    if beds is not None and beds > 0:
        return beds + 1
    return None


def _baths_of(item) -> float | None:
    if getattr(item, "property_type", "") == "terreno":
        return None
    baths = getattr(item, "bathrooms", None)
    try:
        baths_n = float(baths)
    except (TypeError, ValueError):
        return None
    return baths_n if baths_n >= 0 else None


def _deal_score(item) -> float:
    extra = getattr(item, "extra", None) or {}
    raw = extra.get("deal_score")
    if raw not in (None, ""):
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    pct = getattr(item, "vs_barrio_pct", None)
    if pct is not None:
        try:
            return max(0, min(100, round((38 + float(pct)) * 10) / 10))
        except (TypeError, ValueError):
            pass
    label = getattr(item, "deal_label", "") or ""
    if label == "oportunidad":
        return 70
    if label == "bueno":
        return 48
    return 0


def _signal(item, name):
    extra = getattr(item, "extra", None) or {}
    if name in extra:
        return extra.get(name)
    if not extra.get("signals_ver"):
        from .listing_signals import apply_signals

        apply_signals(item)
        extra = item.extra or {}
    return extra.get(name)


def _passes_traits(item, traits: list) -> bool:
    if not traits:
        return True
    wanted = set(traits)
    if "credit" in wanted and _signal(item, "mortgage_credit") is not True:
        return False
    if "owner" in wanted and _signal(item, "owner_direct") is not True:
        return False
    if "expenses" in wanted and _signal(item, "low_expenses") is not True:
        return False
    if "urgent" in wanted and _signal(item, "urgent_sale") is not True:
        return False
    if "quiet" in wanted and _signal(item, "environment") != "quiet":
        return False
    if "good" in wanted:
        cond = str(_signal(item, "condition") or "").strip().lower()
        if cond not in {"a estrenar", "reciclado"}:
            return False
    if "balcony" in wanted and _signal(item, "has_balcony") is not True:
        return False
    if "bright" in wanted and _signal(item, "bright") is not True:
        return False
    if "growing" in wanted and _signal(item, "growing_area") is not True:
        return False
    if "view" in wanted and _signal(item, "open_view") is not True:
        return False
    if "patio" in wanted and _signal(item, "has_patio") is not True:
        return False
    if "garage" in wanted and _signal(item, "has_garage") is not True:
        return False
    if "terrace" in wanted and _signal(item, "has_terrace") is not True:
        return False
    return True


def alert_cities(filters: dict | None) -> list[str]:
    """Ciudades pedidas por una alerta, con las claves del front o las viejas."""
    filters = filters or {}
    raw = _filter_value(filters, "cityFilter", "city")
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if isinstance(raw, (list, tuple)):
        return [str(city).strip() for city in raw if str(city).strip()]
    return []


def apply_filters(listings: list, filters: dict, *, city_scoped: bool = False) -> list:
    """Filtra avisos con las mismas reglas que el mapa.

    Acepta las claves del front (cityFilter, maxPrice, dealBar) y las viejas
    (city, price_max). dealBar es un puntaje mínimo, igual que el rango de la UI.
    Si city_scoped es verdadero, la ciudad ya se resolvió al cargar los avisos.
    """
    if not filters:
        return listings

    city = None if city_scoped else _filter_value(filters, "cityFilter", "city")
    if isinstance(city, (list, tuple)):
        city = city[0] if city else None
    kind = _filter_value(filters, "typeFilter", "type", "property_type")
    barrio = _filter_value(filters, "barrioFilter", "barrio")
    zona = _filter_value(filters, "zonaFilter", "zona")
    max_price = _num(_filter_value(filters, "maxPrice", "price_max"))
    min_m2 = _num(_filter_value(filters, "minM2", "min_m2"))
    max_m2 = _num(_filter_value(filters, "maxM2", "max_m2"))
    min_beds = _num(_filter_value(filters, "minBeds", "min_beds"))
    min_rooms = _num(_filter_value(filters, "minRooms", "min_rooms"))
    min_baths = _num(_filter_value(filters, "minBaths", "min_baths"))
    min_deal = _num(_filter_value(filters, "dealBar", "min_deal"))
    traits = filters.get("traits") or []
    if isinstance(traits, str):
        traits = [traits]
    fav_only = bool(filters.get("favOnly"))

    result = []
    for item in listings:
        if city and getattr(item, "city", "") != city:
            continue
        if kind and getattr(item, "property_type", "") != kind:
            continue
        if barrio and getattr(item, "barrio", "") != barrio:
            continue
        if zona and getattr(item, "zona", "") != zona:
            continue
        price = getattr(item, "price_usd", None)
        if max_price and (price is None or float(price) > max_price):
            continue
        if min_m2 or max_m2:
            meters = _listing_m2(item)
            if not meters:
                continue
            if min_m2 and meters < min_m2:
                continue
            if max_m2 and meters > max_m2:
                continue
        if kind != "terreno":
            if min_beds:
                beds = _bedrooms_of(item)
                if beds is None or beds < min_beds:
                    continue
            if min_rooms:
                rooms = _rooms_of(item)
                if rooms is None or rooms < min_rooms:
                    continue
            if min_baths:
                baths = _baths_of(item)
                if baths is None or baths < min_baths:
                    continue
        if min_deal and _deal_score(item) < min_deal:
            continue
        if not _passes_traits(item, traits):
            continue
        if fav_only and not getattr(item, "favorite", False):
            continue
        result.append(item)
    return result
