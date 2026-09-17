from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

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

PROXY_HEADERS = {
    **HEADERS,
    # El cluster .es de TurboPages responde 400; .en entrega el HTML de la ficha.
    "Accept-Language": "en-US,en;q=0.9",
}
PROXY_HOSTS = ("properati.com",)
LOCAL_HOSTS = ("zonaprop.com", "argenprop.com")
TRANSLATE_PROXY = "https://translate.yandex.com/translate"
TRANSLATE_LANGS = ("es-en", "es-es")
SGAI_SCRAPE = "https://v2-api.scrapegraphai.com/api/scrape"
_DETAIL_URL = re.compile(
    r"/detalle/|/propiedades/clasificado/|/MLA-|/inmueble/|--\d{5,}(?:[/?#]|$)",
    re.I,
)
_blocked_until: dict[str, float] = {}
_tls = threading.local()
PAGE_CACHE_TTL = 12 * 3600


class PageGone(RuntimeError):
    """El aviso ya no está (404/410): reintentarlo no sirve, hay que darlo de baja."""


def reset_fetch_state() -> None:
    _blocked_until.clear()
    from .egress import reset as reset_egress
    from .ops import reset as reset_ops

    reset_egress()
    reset_ops()


def host_is_blocked(host: str) -> bool:
    return _host_is_blocked(host)


def _observe(host: str, lane: str, status: int) -> None:
    try:
        from .ops import note

        note("http", lane=lane or "direct", host=(host or "")[:60], status=int(status or 0))
    except Exception:
        return


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
    hdrs = headers or HEADERS
    wait_s = max(timeout, 55.0 if proxy else 35.0)
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
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
        clients[key] = client
    return client.get(url)


def portal_way(url: str = "", listing_id: str = "") -> str:
    """Cómo salir de este aviso: traductor, IP local o Tor.

    Properati entra por Yandex. Las fichas de ZonaProp y Argenprop van por
    la IP de casa. El resto (Mercado Libre, listados) usa Tor.
    """
    host = urlparse(url or "").netloc.lower()
    if not host:
        src = (listing_id or "").split(":", 1)[0].lower()
        if src == "properati":
            return "translate"
        if src in {"zonaprop", "argenprop"}:
            return "local"
        return "tor"
    if any(part in host for part in PROXY_HOSTS):
        return "translate"
    if _looks_like_listing(url) and any(part in host for part in LOCAL_HOSTS):
        return "local"
    return "tor"


def portal_host(url: str = "", listing_id: str = "") -> str:
    if url:
        return urlparse(url).netloc.lower()
    src = (listing_id or "").split(":", 1)[0].lower()
    return {
        "zonaprop": "www.zonaprop.com.ar",
        "argenprop": "www.argenprop.com",
        "properati": "www.properati.com.ar",
        "mercadolibre": "www.mercadolibre.com.ar",
    }.get(src, "")


def fetch_text(url: str, timeout: float = 35.0, retries: int = 3, paced: bool = True) -> str:
    """Sale por el carril que ya sabemos que anda. Un 404/410 da de baja el aviso."""
    from .egress import can_fetch, lanes

    cached = _read_page_cache(url)
    if cached:
        return cached
    host = urlparse(url).netloc
    listing = _looks_like_listing(url)
    way = portal_way(url)
    if (
        way != "translate"
        and _host_is_blocked(host)
        and not listing
        and not _host_needs_proxy(url)
        and (len(lanes()) <= 1 or not can_fetch(host))
    ):
        raise RuntimeError(f"{host} en pausa (403)")
    if way == "translate":
        return _fetch_via_translate(url, timeout, paced, listing)
    if way == "local":
        return _fetch_via_local(url, timeout, paced, listing)
    return _fetch_via_tor(url, timeout, paced, listing, retries)


def _remember_ok(url: str, listing: bool, text: str, lane: str, host: str) -> str:
    _observe(host, lane, 200)
    return _remember_page(url, text)


def _fetch_via_translate(url: str, timeout: float, paced: bool, listing: bool) -> str:
    host = urlparse(url).netloc
    proxied = _try_blocked_fallback(url, timeout, paced)
    if proxied is not None and (not listing or _listing_html_ok(url, proxied)):
        return _remember_ok(url, listing, proxied, "translate", host)
    stealth = _try_stealth_fetch(url, timeout)
    if stealth and (not listing or _listing_html_ok(url, stealth)):
        return _remember_ok(url, listing, stealth, "stealth", host)
    raise RuntimeError(f"No se pudo leer {url}: el traductor no devolvió la ficha")


def _fetch_via_local(url: str, timeout: float, paced: bool, listing: bool) -> str:
    from .egress import acquire_local, local_limits, release_local, use_local

    host = urlparse(url).netloc
    last_error: Exception | None = None
    gap, _ = local_limits()
    claimed = use_local() and acquire_local(host, timeout=(gap if paced else 0.0))
    if claimed:
        try:
            if paced and crawl.aborted():
                raise RuntimeError("búsqueda pausada")
            response = _httpx_get(url, timeout, proxy=None)
            crawl.note_http(response.status_code, host, lane="direct")
            _observe(host, "direct", response.status_code)
            if response.status_code in {404, 410}:
                raise PageGone(f"HTTP {response.status_code}")
            if response.status_code in {401, 403, 405}:
                _mark_blocked(host)
            response.raise_for_status()
            text = response.text
            if not listing or _listing_html_ok(url, text):
                return _remember_page(url, text)
            last_error = RuntimeError("ficha incompleta")
        except PageGone:
            raise
        except Exception as exc:
            last_error = exc
            if listing or not _host_is_blocked(host):
                try:
                    text = _fetch_urllib(url, timeout)
                    if not listing or _listing_html_ok(url, text):
                        return _remember_ok(url, listing, text, "urllib", host)
                except PageGone:
                    raise
                except Exception as urllib_exc:
                    last_error = urllib_exc
        finally:
            release_local(host, hold=paced)
    stealth = _try_stealth_fetch(url, timeout)
    if stealth and (not listing or _listing_html_ok(url, stealth)):
        return _remember_ok(url, listing, stealth, "stealth", host)
    raise RuntimeError(f"No se pudo leer {url}: {last_error or 'IP local no disponible'}")


def _fetch_via_tor(url: str, timeout: float, paced: bool, listing: bool, retries: int) -> str:
    from .egress import acquire_local, can_fetch, lanes, note_error, note_local_use, pick, release_local, use_local

    host = urlparse(url).netloc
    last_error: Exception | None = None
    hidden = [lane for lane in lanes() if lane.kind != "direct"]
    tries = min(4, len(hidden)) if hidden else (1 if _host_needs_proxy(url) else retries)
    skip_direct = _host_is_blocked(host) and _host_needs_proxy(url) and not can_fetch(host)
    tried: set[str] = set()
    if not skip_direct:
        for attempt in range(max(1, tries)):
            lane = pick(host, exclude=tried)
            if lane is None:
                break
            if hidden and lane.kind == "direct":
                break
            if paced:
                crawl.wait(host=host, lane=lane.id)
                if crawl.aborted():
                    raise RuntimeError("búsqueda pausada")
            try:
                if not lane.proxy:
                    note_local_use(host)
                response = _httpx_get(url, timeout, proxy=lane.proxy)
                crawl.note_http(response.status_code, host, lane=lane.id)
                _observe(host, lane.id, response.status_code)
                if response.status_code in {404, 410}:
                    raise PageGone(f"HTTP {response.status_code}")
                if response.status_code in {429, 503}:
                    last_error = RuntimeError(f"HTTP {response.status_code}")
                    tried.add(lane.id)
                    continue
                if response.status_code in {401, 403, 405}:
                    tried.add(lane.id)
                    if not pick(host, exclude=tried):
                        _mark_blocked(host)
                    nxt = pick(host, exclude=tried)
                    if nxt is not None and not (hidden and nxt.kind == "direct"):
                        continue
                    break
                response.raise_for_status()
                text = response.text
                if listing and not _listing_html_ok(url, text):
                    last_error = RuntimeError("ficha incompleta")
                    tried.add(lane.id)
                    continue
                return _remember_page(url, text)
            except PageGone:
                raise
            except Exception as exc:
                last_error = exc
                note_error(lane.id)
                tried.add(lane.id)
                time.sleep(1.2 * (attempt + 1))
    stealth = _try_stealth_fetch(url, timeout)
    if stealth and (not listing or _listing_html_ok(url, stealth)):
        return _remember_ok(url, listing, stealth, "stealth", host)
    if (
        hidden
        and use_local()
        and (listing or not _host_is_blocked(host) or _host_needs_proxy(url))
        and acquire_local(host)
    ):
        try:
            if paced and crawl.aborted():
                raise RuntimeError("búsqueda pausada")
            response = _httpx_get(url, timeout, proxy=None)
            crawl.note_http(response.status_code, host, lane="direct")
            _observe(host, "direct", response.status_code)
            if response.status_code in {404, 410}:
                raise PageGone(f"HTTP {response.status_code}")
            if response.status_code in {401, 403, 405}:
                _mark_blocked(host)
            response.raise_for_status()
            text = response.text
            if not listing or _listing_html_ok(url, text):
                return _remember_page(url, text)
            last_error = RuntimeError("ficha incompleta")
        except PageGone:
            raise
        except Exception as exc:
            last_error = exc
            if listing or not _host_is_blocked(host) or _host_needs_proxy(url):
                try:
                    text = _fetch_urllib(url, timeout)
                    if not listing or _listing_html_ok(url, text):
                        return _remember_ok(url, listing, text, "urllib", host)
                except PageGone:
                    raise
                except Exception as urllib_exc:
                    last_error = urllib_exc
        finally:
            release_local(host, hold=paced)
    raise RuntimeError(f"No se pudo leer {url}: {last_error}")


def translate_proxy_url(url: str, lang: str = "es-en") -> str:
    return f"{TRANSLATE_PROXY}?lang={lang}&url={quote(url, safe='')}"


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


def _host_needs_proxy(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(part in host for part in PROXY_HOSTS)


def _host_is_blocked(host: str) -> bool:
    until = _blocked_until.get((host or "").lower(), 0)
    return until > time.time()


def _mark_blocked(host: str, seconds: float = 25 * 60) -> None:
    key = (host or "").lower()
    if not key:
        return
    _blocked_until[key] = max(_blocked_until.get(key, 0.0), time.time() + seconds)


def _looks_like_listing(url: str) -> bool:
    return bool(_DETAIL_URL.search(url or ""))


def _listing_html_ok(url: str, text: str) -> bool:
    """No cachear ni dar por buena una ficha recortada (403/captcha de 2 KB)."""
    if not text:
        return False
    host = urlparse(url).netloc.lower()
    lowered = text.lower()
    if "argenprop" in host:
        return len(text) >= 8000 and (
            "data-location-map" in lowered or "data-latitude" in lowered
        )
    return len(text) > 800


def _page_cache_dir() -> Path | None:
    if os.environ.get("PROPMAP_TEST") == "1":
        return None
    from .store import DATA_DIR

    path = DATA_DIR / "page_cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_page_cache(url: str) -> str | None:
    folder = _page_cache_dir()
    if folder is None or not _looks_like_listing(url):
        return None
    path = folder / f"{hashlib.sha1(url.encode()).hexdigest()}.html"
    if not path.is_file():
        return None
    age = time.time() - path.stat().st_mtime
    if age > PAGE_CACHE_TTL:
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    if not _listing_html_ok(url, text):
        return None
    return text


def _remember_page(url: str, text: str) -> str:
    folder = _page_cache_dir()
    if folder is not None and _looks_like_listing(url) and _listing_html_ok(url, text):
        path = folder / f"{hashlib.sha1(url.encode()).hexdigest()}.html"
        try:
            path.write_text(text, encoding="utf-8")
        except OSError:
            pass
    return text


def _sgai_key() -> str:
    return (os.environ.get("SGAI_API_KEY") or os.environ.get("SGAI_APIKEY") or "").strip()


def _try_stealth_fetch(url: str, timeout: float) -> str | None:
    if os.environ.get("PROPMAP_TEST") == "1" and not os.environ.get("PROPMAP_STEALTH_TEST"):
        return None
    if not _looks_like_listing(url) or not _sgai_key():
        return None
    html = _fetch_just_scrape_cli(url, timeout)
    if html:
        return html
    return _fetch_sgai_scrape(url, timeout)


def _fetch_just_scrape_cli(url: str, timeout: float) -> str | None:
    binary = shutil.which("just-scrape")
    if not binary:
        return None
    try:
        proc = subprocess.run(
            [binary, "scrape", url, "--stealth", "--json", "--country-code", "AR"],
            capture_output=True,
            text=True,
            timeout=max(timeout, 90.0),
            env={**os.environ, "SGAI_API_KEY": _sgai_key()},
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return _html_from_stealth(payload)


def _fetch_sgai_scrape(url: str, timeout: float) -> str | None:
    key = _sgai_key()
    if not key:
        return None
    try:
        with httpx.Client(timeout=max(timeout, 90.0)) as client:
            response = client.post(
                SGAI_SCRAPE,
                headers={"SGAI-APIKEY": key, "Content-Type": "application/json"},
                json={
                    "url": url,
                    "formats": [{"type": "html"}],
                    "fetchConfig": {"stealth": True, "country": "ar", "mode": "js"},
                },
            )
            if response.status_code >= 400:
                return None
            return _html_from_stealth(response.json())
    except Exception:
        return None


def _html_from_stealth(payload: Any) -> str | None:
    if isinstance(payload, str) and len(payload) > 800:
        return payload
    if not isinstance(payload, dict):
        return None
    for key in ("html", "content", "result", "data"):
        val = payload.get(key)
        if isinstance(val, str) and len(val) > 800:
            return val
        if isinstance(val, dict):
            nested = _html_from_stealth(val)
            if nested:
                return nested
    return None


def _try_blocked_fallback(url: str, timeout: float, paced: bool) -> str | None:
    if not _host_needs_proxy(url):
        return None
    try:
        return _fetch_translate_proxy(url, timeout, paced)
    except Exception:
        return None


def _fetch_translate_proxy(url: str, timeout: float, paced: bool) -> str:
    last_error: Exception | None = None
    for lang in TRANSLATE_LANGS:
        proxy = translate_proxy_url(url, lang)
        host = urlparse(proxy).netloc
        if paced:
            crawl.wait(host=host)
            if crawl.aborted():
                raise RuntimeError("búsqueda pausada")
        try:
            with httpx.Client(headers=PROXY_HEADERS, follow_redirects=True, timeout=max(timeout, 25.0)) as client:
                response = client.get(proxy)
                crawl.note_http(response.status_code, host)
                if response.status_code in {400, 429, 503}:
                    last_error = RuntimeError(f"HTTP {response.status_code}")
                    time.sleep(1.5)
                    continue
                response.raise_for_status()
                text = response.text
            if proxy_html_looks_valid(text):
                return text
            last_error = RuntimeError("el proxy no devolvió la ficha")
        except Exception as exc:
            last_error = exc
    raise last_error or RuntimeError("el proxy no devolvió la ficha")


def _fetch_urllib(url: str, timeout: float) -> str:
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if getattr(response, "status", 200) >= 400:
                raise RuntimeError(f"HTTP {response.status}")
            return response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code in {404, 410}:
            raise PageGone(f"HTTP {exc.code}") from exc
        raise


def fetch_bytes(url: str, timeout: float = 35.0, paced: bool = True) -> bytes:
    from .egress import pick

    host = urlparse(url).netloc
    lane = pick(host)
    if paced:
        crawl.wait(host=host, lane=(lane.id if lane else "direct"))
        if crawl.aborted():
            raise RuntimeError("búsqueda pausada")
    response = _httpx_get(url, timeout, proxy=lane.proxy if lane else None)
    crawl.note_http(response.status_code, host, lane=(lane.id if lane else "direct"))
    _observe(host, lane.id if lane else "direct", response.status_code)
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
