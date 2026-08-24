from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from . import crawl

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


def fetch_text(url: str, timeout: float = 35.0, retries: int = 3, paced: bool = True) -> str:
    last_error: Exception | None = None
    host = urlparse(url).netloc
    for attempt in range(retries):
        if paced:
            crawl.wait()
            if crawl.aborted():
                raise RuntimeError("búsqueda pausada")
        try:
            with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=timeout) as client:
                response = client.get(url)
                crawl.note_http(response.status_code, host)
                if response.status_code in {429, 503}:
                    last_error = RuntimeError(f"HTTP {response.status_code}")
                    continue
                if response.status_code == 403:
                    return _fetch_urllib(url, timeout)
                response.raise_for_status()
                return response.text
        except Exception as exc:
            last_error = exc
            time.sleep(1.2 * (attempt + 1))
    try:
        if paced:
            crawl.wait()
        return _fetch_urllib(url, timeout)
    except Exception:
        pass
    raise RuntimeError(f"No se pudo leer {url}: {last_error}")


def _fetch_urllib(url: str, timeout: float) -> str:
    import urllib.request

    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if getattr(response, "status", 200) >= 400:
            raise RuntimeError(f"HTTP {response.status}")
        return response.read().decode("utf-8", "replace")


def fetch_bytes(url: str, timeout: float = 35.0, paced: bool = True) -> bytes:
    if paced:
        crawl.wait()
        if crawl.aborted():
            raise RuntimeError("búsqueda pausada")
    with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=timeout) as client:
        response = client.get(url)
        crawl.note_http(response.status_code, urlparse(url).netloc)
        response.raise_for_status()
        return response.content


def fetch_json(url: str, timeout: float = 20.0) -> Any:
    with httpx.Client(headers={**HEADERS, "Accept": "application/json"}, timeout=timeout) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.json()


def decode_js_object(text: str, marker: str) -> dict[str, Any] | None:
    idx = text.find(marker)
    if idx < 0:
        return None
    start = text.find("{", idx)
    if start < 0:
        return None
    decoder = json.JSONDecoder()
    try:
        data, _end = decoder.raw_decode(text[start:])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


decode_js_object = decode_js_object
