"""Validators for URLs and HTML."""

from __future__ import annotations

import re
from urllib.parse import urlparse

_DETAIL_URL = re.compile(
    r"/detalle/|/propiedades/clasificado/|/MLA-|/inmueble/|--\d{5,}(?:[/?#]|$)",
    re.I,
)

PROXY_HOSTS = ("properati.com",)
LOCAL_HOSTS = ("zonaprop.com", "argenprop.com", "mercadolibre.com")

def _looks_like_listing(url: str) -> bool:
    return bool(_DETAIL_URL.search(url or ""))

def _listing_html_ok(url: str, text: str) -> bool:
    """No cachear ni dar por buena una ficha recortada (403/captcha de 2 KB)."""
    if not text:
        return False
    host = urlparse(url).netloc.lower()
    lowered = text.lower()
    if len(text) < 800:
        return False
    if "access denied" in lowered and "realestatelisting" not in lowered and len(text) < 8000:
        return False
    if "argenprop" in host:
        return len(text) >= 8000 and (
            "data-location-map" in lowered or "data-latitude" in lowered
        )
    if "mercadolibre" in host:
        if "ingresa a" in lowered and "tu cuenta" in lowered and "realestatelisting" not in lowered:
            return False
    return True

def _host_needs_proxy(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(part in host for part in PROXY_HOSTS)

def proxy_html_looks_valid(text: str) -> bool:
    if not text or len(text) < 4000:
        return False
    lowered = text.lower()
    if "access denied" in lowered and len(text) < 800:
        return False
    return (
        "mapdata" in lowered
        or "adlocationdata" in lowered
        or "data-url" in lowered
        or "snippet" in lowered
    )
