"""HTTP client management with async and sync clients."""

from __future__ import annotations

import os
import asyncio
import threading
from typing import Any

import httpx

from . import policy

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
}

PROXY_HEADERS = {
    **HEADERS,
    "Accept-Language": "en-US,en;q=0.9",
}

PROXY_HOSTS = ("properati.com",)
LOCAL_HOSTS = ("zonaprop.com", "argenprop.com", "mercadolibre.com")

# Thread-local storage for sync clients (backward compatibility)
_tls = threading.local()

# Global async client instances (one per event loop)
_async_clients: dict[int, dict[str, httpx.AsyncClient]] = {}
_async_client_lock = threading.Lock()


def _client_kwargs(timeout: float, headers: dict[str, str], proxy: str | None) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "headers": headers,
        "follow_redirects": True,
        "timeout": timeout,
    }
    if proxy:
        kwargs["proxy"] = proxy
    return kwargs


def _httpx_get(
    url: str,
    timeout: float,
    headers: dict[str, str] | None = None,
    proxy: str | None = None,
) -> httpx.Response:
    """Synchronous HTTP GET - maintains backward compatibility."""
    hdrs = headers or HEADERS
    wait_s = max(timeout, 25.0 if proxy else 20.0)
    if os.environ.get("PROPMAP_TEST") == "1":
        with httpx.Client(**_client_kwargs(timeout, hdrs, proxy)) as client:
            return client.get(url)
    clients = getattr(_tls, "clients", None)
    if not isinstance(clients, dict):
        clients = {}
        _tls.clients = clients
    key = f"{id(hdrs) if hdrs is HEADERS else 'custom'}|{proxy or ''}"
    client = clients.get(key)
    if client is None:
        client = httpx.Client(
            **_client_kwargs(wait_s, hdrs, proxy),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
        clients[key] = client
    return client.get(url)


def _get_async_client_key(headers: dict[str, str] | None, proxy: str | None) -> str:
    """Generate a key for async client caching."""
    hdrs = headers or HEADERS
    return f"{id(hdrs) if hdrs is HEADERS else 'custom'}|{proxy or ''}"


def _get_or_create_async_client(
    timeout: float,
    headers: dict[str, str] | None = None,
    proxy: str | None = None,
) -> httpx.AsyncClient:
    """Get or create an async HTTP client for the current event loop."""
    loop_id = id(asyncio.get_running_loop())
    key = _get_async_client_key(headers, proxy)
    
    with _async_client_lock:
        if loop_id not in _async_clients:
            _async_clients[loop_id] = {}
        
        clients = _async_clients[loop_id]
        if key not in clients:
            hdrs = headers or HEADERS
            wait_s = max(timeout, 25.0 if proxy else 20.0)
            clients[key] = httpx.AsyncClient(
                **_client_kwargs(wait_s, hdrs, proxy),
                limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            )
        return clients[key]


async def _async_httpx_get(
    url: str,
    timeout: float,
    headers: dict[str, str] | None = None,
    proxy: str | None = None,
) -> httpx.Response:
    """Asynchronous HTTP GET."""
    if os.environ.get("PROPMAP_TEST") == "1":
        # Mock mode: use sync client inside thread
        def _sync_get():
            with httpx.Client(**_client_kwargs(timeout, headers or HEADERS, proxy)) as client:
                return client.get(url)
        return await asyncio.to_thread(_sync_get)
    client = _get_or_create_async_client(timeout, headers, proxy)
    return await client.get(url)


# Backward compatibility exports
__all__ = [
    "HEADERS",
    "PROXY_HEADERS", 
    "PROXY_HOSTS",
    "LOCAL_HOSTS",
    "_httpx_get",
    "_async_httpx_get",
]
