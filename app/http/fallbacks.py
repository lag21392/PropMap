"""Fallback fetch strategies."""

from __future__ import annotations

import time

import httpx
from urllib.parse import urlparse

from . import client
from . import policy
from . import observability
from . import validators
from .errors import PageGone
from .stealth import _try_stealth_fetch

TRANSLATE_PROXY = "https://translate.yandex.com/translate"
TRANSLATE_LANGS = ("es-en", "es-es")

def translate_proxy_url(url: str, lang: str = "es-en") -> str:
    from urllib.parse import quote
    return f"{TRANSLATE_PROXY}?lang={lang}&url={quote(url, safe='')}"

def _fetch_translate_proxy(url: str, timeout: float, paced: bool, listing: bool = False) -> str:
    from app import crawl

    last_error: Exception | None = None
    for lang in TRANSLATE_LANGS:
        proxy = translate_proxy_url(url, lang)
        host = urlparse(proxy).netloc
        if paced:
            if crawl.cooling(host) or crawl.host_cooling(host, "direct"):
                last_error = RuntimeError("traductor en pausa")
                break
            crawl.wait(host=host)
            if crawl.aborted():
                raise RuntimeError("búsqueda pausada")
        try:
            with httpx.Client(headers=client.PROXY_HEADERS, follow_redirects=True, timeout=max(timeout, 25.0)) as cli:
                response = cli.get(proxy)
                crawl.note_http(response.status_code, host)
                observability._observe(host, "translate", response.status_code)
                if response.status_code in {400, 429, 503}:
                    last_error = RuntimeError(f"HTTP {response.status_code}")
                    time.sleep(1.5)
                    continue
                response.raise_for_status()
                text = response.text
            if listing:
                if validators._listing_html_ok(url, text):
                    return text
            elif validators.proxy_html_looks_valid(text):
                return text
            last_error = RuntimeError("el proxy no devolvió la ficha")
        except Exception as exc:
            last_error = exc
    raise last_error or RuntimeError("el proxy no devolvió la ficha")

def _fetch_urllib(url: str, timeout: float) -> str:
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, headers=client.HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if getattr(response, "status", 200) >= 400:
                raise RuntimeError(f"HTTP {response.status}")
            return response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code in {404, 410}:
            raise PageGone(f"HTTP {exc.code}") from exc
        raise

def _try_blocked_fallback(url: str, timeout: float, paced: bool) -> str | None:
    if not validators._host_needs_proxy(url):
        return None
    try:
        return _fetch_translate_proxy(url, timeout, paced)
    except PageGone:
        raise
    except Exception:
        return None

def _try_translate_listing(url: str, timeout: float, paced: bool) -> str | None:
    if not validators._looks_like_listing(url):
        return None
    try:
        return _fetch_translate_proxy(url, timeout, paced, listing=True)
    except PageGone:
        raise
    except Exception:
        return None

def _try_hidden_fetch(url: str, timeout: float, paced: bool, listing: bool) -> str | None:
    from app.egress import lanes, note_error, pick
    from app import crawl

    host = urlparse(url).netloc
    hidden = [lane for lane in lanes() if lane.kind != "direct"]
    if not hidden:
        return None
    tried: set[str] = set()
    for _ in range(min(3, len(hidden))):
        lane = pick(host, exclude=tried)
        if lane is None or lane.kind == "direct":
            return None
        tried.add(lane.id)
        if paced:
            crawl.wait(host=host, lane=lane.id)
            if crawl.aborted():
                return None
        try:
            response = client._httpx_get(url, timeout, proxy=lane.proxy)
            crawl.note_http(response.status_code, host, lane=lane.id)
            observability._observe(host, lane.id, response.status_code)
            if response.status_code in {404, 410}:
                raise PageGone(f"HTTP {response.status_code}")
            if response.status_code in {401, 403, 405, 429, 503}:
                note_error(lane.id)
                continue
            response.raise_for_status()
            text = response.text
            if listing and not validators._listing_html_ok(url, text):
                continue
            return text
        except PageGone:
            raise
        except Exception:
            note_error(lane.id)
    return None
