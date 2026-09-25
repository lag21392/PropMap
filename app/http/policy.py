"""Host blocking and portal routing policy."""

from __future__ import annotations

import time
from urllib.parse import urlparse

from .validators import _looks_like_listing, PROXY_HOSTS, LOCAL_HOSTS

_blocked_until: dict[str, float] = {}

def reset_fetch_state() -> None:
    _blocked_until.clear()
    from app.egress import reset as reset_egress
    from app.ops import reset as reset_ops

    reset_egress()
    reset_ops()

def host_is_blocked(host: str) -> bool:
    return _host_is_blocked(host)

def _host_is_blocked(host: str) -> bool:
    until = _blocked_until.get((host or "").lower(), 0)
    return until > time.time()

def _mark_blocked(host: str, seconds: float = 25 * 60) -> None:
    key = (host or "").lower()
    if not key:
        return
    _blocked_until[key] = max(_blocked_until.get(key, 0.0), time.time() + seconds)
    try:
        from app import rate_limit
        rate_limit.circuit_breaker().record_failure(host.lower())
    except Exception:
        pass

def portal_way(url: str = "", listing_id: str = "") -> str:
    """Cómo salir de este aviso: traductor, otra IP (Tor) o IP local."""
    host = urlparse(url or "").netloc.lower()
    if not host:
        src = (listing_id or "").split(":", 1)[0].lower()
        if src in {"properati", "zonaprop", "argenprop", "mercadolibre"}:
            return "translate"
        return "tor"
    if any(part in host for part in PROXY_HOSTS):
        return "translate"
    if _looks_like_listing(url) and any(part in host for part in LOCAL_HOSTS):
        return "translate"
    return "tor"

def portal_host(url: str = "", listing_id: str = "") -> str:
    if url:
        return urlparse(url).netloc.lower()
    src = (listing_id or "").split(":", 1)[0].lower()
    return {
        "zonaprop": "www.zonaprop.com.ar",
        "argenprop": "www.argenprop.com",
        "properati": "www.properati.com.ar",
        "mercadolibre": "mercadolibre.com.ar",
    }.get(src, "")
