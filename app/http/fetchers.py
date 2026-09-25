"""High-level fetchers with policy, cache and fallbacks."""

from __future__ import annotations

import asyncio
import time
from urllib.parse import urlparse

import httpx

from . import client
from . import cache
from . import policy
from . import observability
from . import validators
from .errors import PageGone
from .fallbacks import _fetch_translate_proxy, _fetch_urllib, _try_blocked_fallback, _try_hidden_fetch, _try_translate_listing
from .stealth import _try_stealth_fetch
from app import rate_limit
from app.concurrency import get_io_executor

def _remember_ok(url: str, listing: bool, text: str, lane: str, host: str) -> str:
    observability._observe(host, lane, 200)
    try:
        rate_limit.circuit_breaker().record_success(host.lower())
    except Exception:
        pass
    return cache._remember_page(url, text)

def fetch_text(url: str, timeout: float = 20.0, retries: int = 3, paced: bool = True) -> str:
    """Sale por el carril que ya sabemos que anda. Un 404/410 da de baja el aviso."""
    import time, logging
    t0 = time.time()
    from app.egress import can_fetch, lanes

    cached = cache._read_page_cache(url)
    if cached:
        logging.info("fetch_text cache hit %s", url[:80])
        return cached

    host = urlparse(url).netloc
    host_key = host.lower()
    cb = rate_limit.circuit_breaker()
    rl = rate_limit.rate_limiter()

    if not cb.can_execute(host_key):
        raise RuntimeError(f"{host} circuit breaker OPEN - too many failures")
    if not rl.allow(host_key):
        wait_s = rl.wait_time(host_key)
        raise RuntimeError(f"{host} rate limited - wait {wait_s:.1f}s")

    listing = validators._looks_like_listing(url)
    way = policy.portal_way(url)

    if (
        way != "translate"
        and policy._host_is_blocked(host)
        and not listing
        and not validators._host_needs_proxy(url)
        and (len(lanes()) <= 1 or not can_fetch(host))
    ):
        raise RuntimeError(f"{host} en pausa (403)")

    if way == "translate":
        text = _fetch_via_translate(url, timeout, paced, listing)
    elif way == "local":
        text = _fetch_via_local(url, timeout, paced, listing)
    else:
        text = _fetch_via_tor(url, timeout, paced, listing, retries)
    t1 = time.time()
    logging.info("fetch_text %s ms=%.1f", url[:80], (t1-t0)*1000)
    return text

def _fetch_via_translate(url: str, timeout: float, paced: bool, listing: bool) -> str:
    host = urlparse(url).netloc
    proxied = _try_blocked_fallback(url, timeout, paced)
    if proxied is None:
        proxied = _try_translate_listing(url, timeout, paced)
    if proxied is not None and (not listing or validators._listing_html_ok(url, proxied)):
        return _remember_ok(url, listing, proxied, "translate", host)
    stealth = _try_stealth_fetch(url, timeout)
    if stealth and (not listing or validators._listing_html_ok(url, stealth)):
        return _remember_ok(url, listing, stealth, "stealth", host)
    hidden = _try_hidden_fetch(url, timeout, paced, listing)
    if hidden is not None:
        return _remember_ok(url, listing, hidden, "tor", host)
    return _fetch_via_local(url, timeout, paced, listing, fallbacks=False)

async def _fetch_via_local_async(url: str, timeout: float, paced: bool, listing: bool, fallbacks: bool = True) -> str:
    from app.egress import acquire_local, release_local, use_local
    from app import crawl

    host = urlparse(url).netloc
    last_error: Exception | None = None
    claimed = use_local() and acquire_local(host, timeout=(0.4 if paced else 0.0))
    if claimed:
        try:
            if paced and crawl.aborted():
                raise RuntimeError("búsqueda pausada")
            response = await client._async_httpx_get(url, timeout, proxy=None)
            crawl.note_http(response.status_code, host, lane="direct")
            observability._observe(host, "direct", response.status_code)
            if response.status_code in {404, 410}:
                raise PageGone(f"HTTP {response.status_code}")
            if response.status_code in {401, 403, 405}:
                policy._mark_blocked(host)
            response.raise_for_status()
            text = response.text
            if not listing or validators._listing_html_ok(url, text):
                return cache._remember_page(url, text)
            last_error = RuntimeError("ficha incompleta")
        except PageGone:
            raise
        except Exception as exc:
            last_error = exc
            if listing or not policy._host_is_blocked(host):
                try:
                    text = _fetch_urllib(url, timeout)
                    if not listing or validators._listing_html_ok(url, text):
                        return _remember_ok(url, listing, text, "urllib", host)
                except PageGone:
                    raise
                except Exception as urllib_exc:
                    last_error = urllib_exc
        finally:
            release_local(host, hold=paced)
    if fallbacks:
        proxied = _try_translate_listing(url, timeout, paced)
        if proxied is not None:
            return _remember_ok(url, listing, proxied, "translate", host)
        stealth = _try_stealth_fetch(url, timeout)
        if stealth and (not listing or validators._listing_html_ok(url, stealth)):
            return _remember_ok(url, listing, stealth, "stealth", host)
    raise RuntimeError(f"No se pudo leer {url}: {last_error or 'IP local no disponible'}")


def _fetch_via_local(url: str, timeout: float, paced: bool, listing: bool, fallbacks: bool = True) -> str:
    """Sync wrapper for _fetch_via_local_async."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # If we're already in an async context, run in thread pool
        executor = get_io_executor()
        future = executor.submit(asyncio.run, _fetch_via_local_async(url, timeout, paced, listing, fallbacks))
        return future.result()
    else:
        return asyncio.run(_fetch_via_local_async(url, timeout, paced, listing, fallbacks))


async def _fetch_via_tor_async(url: str, timeout: float, paced: bool, listing: bool, retries: int) -> str:
    """Async version of _fetch_via_tor using async client."""
    from app.egress import acquire_local, can_fetch, lanes, note_error, note_local_use, pick, release_local, use_local
    from app import crawl

    host = urlparse(url).netloc
    last_error: Exception | None = None
    hidden = [lane for lane in lanes() if lane.kind != "direct"]
    tries = min(4, len(hidden)) if hidden else (1 if validators._host_needs_proxy(url) else retries)
    skip_direct = policy._host_is_blocked(host) and validators._host_needs_proxy(url) and not can_fetch(host)
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
                client_async = client._get_or_create_async_client(timeout, proxy=lane.proxy)
                response = await client_async.get(url)
                crawl.note_http(response.status_code, host, lane=lane.id)
                observability._observe(host, lane.id, response.status_code)
                if response.status_code in {404, 410}:
                    raise PageGone(f"HTTP {response.status_code}")
                if response.status_code in {429, 503}:
                    last_error = RuntimeError(f"HTTP {response.status_code}")
                    tried.add(lane.id)
                    continue
                if response.status_code in {401, 403, 405}:
                    tried.add(lane.id)
                    if not pick(host, exclude=tried):
                        policy._mark_blocked(host)
                    nxt = pick(host, exclude=tried)
                    if nxt is not None and not (hidden and nxt.kind == "direct"):
                        continue
                    break
                response.raise_for_status()
                text = response.text
                if listing and not validators._listing_html_ok(url, text):
                    last_error = RuntimeError("ficha incompleta")
                    tried.add(lane.id)
                    continue
                return cache._remember_page(url, text)
            except PageGone:
                raise
            except Exception as exc:
                last_error = exc
                note_error(lane.id)
                tried.add(lane.id)
                await asyncio.sleep(1.2 * (attempt + 1))
    stealth = _try_stealth_fetch(url, timeout)
    if stealth and (not listing or validators._listing_html_ok(url, stealth)):
        return _remember_ok(url, listing, stealth, "stealth", host)
    proxied = _try_translate_listing(url, timeout, paced)
    if proxied is not None:
        return _remember_ok(url, listing, proxied, "translate", host)
    if (
        hidden
        and use_local()
        and (listing or not policy._host_is_blocked(host) or validators._host_needs_proxy(url))
        and acquire_local(host)
    ):
        try:
            if paced and crawl.aborted():
                raise RuntimeError("búsqueda pausada")
            client_async = client._get_or_create_async_client(timeout, proxy=None)
            response = await client_async.get(url)
            crawl.note_http(response.status_code, host, lane="direct")
            observability._observe(host, "direct", response.status_code)
            if response.status_code in {404, 410}:
                raise PageGone(f"HTTP {response.status_code}")
            if response.status_code in {401, 403, 405}:
                policy._mark_blocked(host)
            response.raise_for_status()
            text = response.text
            if not listing or validators._listing_html_ok(url, text):
                return cache._remember_page(url, text)
            last_error = RuntimeError("ficha incompleta")
        except PageGone:
            raise
        except Exception as exc:
            last_error = exc
            if listing or not policy._host_is_blocked(host) or validators._host_needs_proxy(url):
                try:
                    text = await asyncio.to_thread(_fetch_urllib, url, timeout)
                    if not listing or validators._listing_html_ok(url, text):
                        return _remember_ok(url, listing, text, "urllib", host)
                except PageGone:
                    raise
                except Exception as urllib_exc:
                    last_error = urllib_exc
        finally:
            release_local(host, hold=paced)
    raise RuntimeError(f"No se pudo leer {url}: {last_error or 'IP local no disponible'}")


def _fetch_via_tor(url: str, timeout: float, paced: bool, listing: bool, retries: int) -> str:
    """Sync wrapper for _fetch_via_tor_async."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # If we're already in an async context, run in thread pool
        executor = get_io_executor()
        future = executor.submit(asyncio.run, _fetch_via_tor_async(url, timeout, paced, listing, retries))
        return future.result()
    else:
        return asyncio.run(_fetch_via_tor_async(url, timeout, paced, listing, retries))

def fetch_bytes(url: str, timeout: float = 20.0, paced: bool = True) -> bytes:
    from app.egress import pick
    from app import crawl

    async def _fetch():
        host = urlparse(url).netloc
        lane = pick(host)
        if paced:
            crawl.wait(host=host, lane=(lane.id if lane else "direct"))
            if crawl.aborted():
                raise RuntimeError("búsqueda pausada")
        client_async = client._get_or_create_async_client(timeout, proxy=lane.proxy if lane else None)
        response = await client_async.get(url)
        crawl.note_http(response.status_code, host, lane=(lane.id if lane else "direct"))
        observability._observe(host, lane.id if lane else "direct", response.status_code)
        response.raise_for_status()
        return response.content

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        executor = get_io_executor()
        future = executor.submit(asyncio.run, _fetch())
        return future.result()
    else:
        return asyncio.run(_fetch())

def fetch_json(url: str, timeout: float = 20.0):
    """Fetch JSON using shared async client."""
    async def _fetch():
        async_client = client._get_or_create_async_client(
            timeout,
            headers={**client.HEADERS, "Accept": "application/json"},
            proxy=None,
        )
        response = await async_client.get(url)
        response.raise_for_status()
        return response.json()
    
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    
    if loop and loop.is_running():
        # If we're already in an async context, we can't use asyncio.run
        # Create a new event loop in a thread
        executor = get_io_executor()
        future = executor.submit(asyncio.run, _fetch())
        return future.result()
    else:
        return asyncio.run(_fetch())


async def fetch_bytes_async(url: str, timeout: float = 20.0, paced: bool = True) -> bytes:
    from app.egress import pick
    from app import crawl
    host = urlparse(url).netloc
    lane = pick(host)
    if paced:
        crawl.wait(host=host, lane=(lane.id if lane else "direct"))
        if crawl.aborted():
            raise RuntimeError("búsqueda pausada")
    client_async = client._get_or_create_async_client(timeout, proxy=lane.proxy if lane else None)
    response = await client_async.get(url)
    crawl.note_http(response.status_code, host, lane=(lane.id if lane else "direct"))
    observability._observe(host, lane.id if lane else "direct", response.status_code)
    response.raise_for_status()
    return response.content


async def fetch_json_async(url: str, timeout: float = 20.0):
    async_client = client._get_or_create_async_client(
        timeout,
        headers={**client.HEADERS, "Accept": "application/json"},
        proxy=None,
    )
    response = await async_client.get(url)
    response.raise_for_status()
    return response.json()


async def fetch_text_async(url: str, timeout: float = 20.0, retries: int = 3, paced: bool = True) -> str:
    """Async version of fetch_text using async client where possible."""
    from app.egress import can_fetch, lanes
    from .cache import _read_page_cache

    cached = _read_page_cache(url)
    if cached:
        return cached

    host = urlparse(url).netloc
    host_key = host.lower()
    cb = rate_limit.circuit_breaker()
    rl = rate_limit.rate_limiter()

    if not cb.can_execute(host_key):
        raise RuntimeError(f"{host} circuit breaker OPEN - too many failures")
    if not rl.allow(host_key):
        wait_s = rl.wait_time(host_key)
        raise RuntimeError(f"{host} rate limited - wait {wait_s:.1f}s")

    listing = validators._looks_like_listing(url)
    way = policy.portal_way(url)

    if (
        way != "translate"
        and policy._host_is_blocked(host)
        and not listing
        and not validators._host_needs_proxy(url)
        and (len(lanes()) <= 1 or not can_fetch(host))
    ):
        raise RuntimeError(f"{host} en pausa (403)")

    if way == "translate":
        # translate proxy still sync
        return _fetch_via_translate(url, timeout, paced, listing)
    if way == "local":
        return await _fetch_via_local_async(url, timeout, paced, listing)
    # tor path now async
    return await _fetch_via_tor_async(url, timeout, paced, listing, retries)
