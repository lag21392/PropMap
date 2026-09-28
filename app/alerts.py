from __future__ import annotations
import json
import uuid
import time
from pathlib import Path
from typing import Any

DATA = Path(__file__).parent.parent / "data" / "alerts.json"

# Tier limits
FREE_TIER_LIMITS = {
    "max_alerts": 1,
    "max_cities_per_alert": 1,
    "frequency_hours": 24,
    "history_days": 7,
    "channels": ["email"],
}

PRO_TIER_LIMITS = {
    "max_alerts": 10,
    "max_cities_per_alert": 5,
    "frequency_hours": 1,  # hourly for pro
    "history_days": 90,
    "channels": ["email", "telegram"],
}

TIER_LIMITS = {
    "free": FREE_TIER_LIMITS,
    "pro": PRO_TIER_LIMITS,
}

def load():
    if not DATA.exists(): return []
    try: return json.loads(DATA.read_text())
    except: return []

def save(lst): DATA.parent.mkdir(parents=True, exist_ok=True); DATA.write_text(json.dumps(lst))

def get_tier_limits(tier: str) -> dict[str, Any]:
    """Get limits for a given tier."""
    return TIER_LIMITS.get(tier, FREE_TIER_LIMITS)

def validate_alert_limits(user_id: str, tier: str, new_filters: dict) -> tuple[bool, str]:
    """Validate if user can create this alert based on their tier."""
    from .search_auth import alert_cities

    limits = get_tier_limits(tier)
    alerts = [a for a in load() if a.get("user_id") == user_id and a.get("active", True)]

    if len(alerts) >= limits["max_alerts"]:
        return False, f"Límite de alertas alcanzado ({limits['max_alerts']} para tier {tier})"

    cities = alert_cities(new_filters)
    if not cities:
        return False, "Elegí una ciudad para la alerta"
    if len(cities) > limits["max_cities_per_alert"]:
        return False, f"Máximo {limits['max_cities_per_alert']} ciudades por alerta para tier {tier}"

    return True, ""

def add_alert(user_id, filters, tier: str = "free"):
    lst = load()
    
    # Validate limits
    ok, error = validate_alert_limits(user_id, tier, filters)
    if not ok:
        return {"ok": False, "error": error}
    
    alert = {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "filters": filters,
        "tier": tier,
        "active": True,
        "created": time.time(),
        "last_sent": None
    }
    lst.append(alert)
    save(lst)
    return {"ok": True, "alert": alert}

def list_alerts(user_id): return [a for a in load() if a["user_id"]==user_id]

def get_alert(alert_id):
    for a in load():
        if a["id"] == alert_id:
            return a
    return None

def _owned(alert: dict, user_id: str | None) -> bool:
    return user_id is None or alert.get("user_id") == user_id

def update_alert(alert_id, user_id: str | None = None, **fields):
    lst = load()
    for a in lst:
        if a.get("id") == alert_id and _owned(a, user_id):
            a.update(fields)
            save(lst)
            return {"ok": True, "alert": a}
    return {"ok": False, "error": "not found"}

def delete_alert(alert_id, user_id: str | None = None):
    lst = load()
    new_lst = []
    found = False
    for a in lst:
        if a.get("id") == alert_id:
            if not _owned(a, user_id):
                return {"ok": False, "error": "not found"}
            found = True
            continue
        new_lst.append(a)
    if not found:
        return {"ok": False, "error": "not found"}
    save(new_lst)
    return {"ok": True}

def _sample_listing(item) -> dict:
    return {
        "id": item.id,
        "title": item.title,
        "price_usd": item.price_usd,
        "city": item.city,
        "barrio": item.barrio,
        "url": item.url,
    }

def listings_for_filters(filters: dict) -> list:
    """Carga solo las ciudades de la alerta, sin recorrer toda la base."""
    from .search_auth import alert_cities
    from .store import fetch_for_city

    listings = []
    seen: set[str] = set()
    for city in alert_cities(filters):
        for item in fetch_for_city(city):
            if item.id in seen:
                continue
            seen.add(item.id)
            listings.append(item)
    return listings

def test_alert(filters, limit=5):
    """Prueba los filtros contra los avisos de esa ciudad."""
    from .search_auth import alert_cities, apply_filters

    if not alert_cities(filters):
        return {"ok": False, "error": "Elegí una ciudad para la alerta", "count": 0, "sample": []}
    filtered = apply_filters(listings_for_filters(filters), filters, city_scoped=True)
    return {
        "ok": True,
        "count": len(filtered),
        "sample": [_sample_listing(item) for item in filtered[:limit]],
    }

def get_user_tier(user_id: str) -> str:
    """Get user's current tier from accounts table."""
    from app.accounts import _get_by_id
    row = _get_by_id(user_id)
    if row:
        # sqlite3.Row doesn't have .get(), use dict-like access
        stripe_status = row["stripe_subscription_status"] if "stripe_subscription_status" in row.keys() else ""
        if stripe_status == "active":
            return "pro"
    return "free"

def set_user_tier(user_id: str, tier: str, stripe_customer_id: str = "", stripe_subscription_id: str = "", stripe_subscription_status: str = "") -> bool:
    """Update user's tier and Stripe info in accounts table."""
    from app import store
    store.init()
    with store._write:
        with store.connect() as conn:
            conn.execute(
                """UPDATE accounts SET
                    stripe_customer_id = COALESCE(NULLIF(?, ''), stripe_customer_id),
                    stripe_subscription_id = COALESCE(NULLIF(?, ''), stripe_subscription_id),
                    stripe_subscription_status = COALESCE(NULLIF(?, ''), stripe_subscription_status),
                    tier = ?
                WHERE id = ?""",
                (stripe_customer_id, stripe_subscription_id, stripe_subscription_status, tier, user_id)
            )
            conn.commit()
    return True
