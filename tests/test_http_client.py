from app.http_client import (
    PROXY_HEADERS,
    fetch_text,
    proxy_html_looks_valid,
    reset_fetch_state,
    translate_proxy_url,
)


class _Resp:
    def __init__(self, status: int, text: str, url: str = ""):
        self.status_code = status
        self.text = text
        self.url = url

    def raise_for_status(self):
        import httpx

        if self.status_code >= 400:
            request = httpx.Request("GET", self.url or "https://example.com")
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=request,
                response=httpx.Response(self.status_code, request=request),
            )


class _Client:
    def __init__(self, routes: dict[str, _Resp], **_kwargs):
        self.routes = routes

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def get(self, url: str):
        for prefix, resp in self.routes.items():
            if url.startswith(prefix) or prefix in url:
                return resp
        return _Resp(404, "missing", url)


def test_translate_proxy_url_keeps_listing_and_asks_spanish():
    listing = "https://www.properati.com.ar/detalle/14032-32-35f7-85bf7f22d1c8-19e955f-aae4-7024"
    proxy = translate_proxy_url(listing)
    assert proxy.startswith("https://translate.yandex.com/translate?")
    assert "lang=es-en" in proxy
    assert "properati.com.ar" in proxy
    assert PROXY_HEADERS["Accept-Language"].startswith("en")


def test_argenprop_ficha_url_counts_as_listing():
    from app.http_client import _listing_html_ok, _looks_like_listing

    url = "https://www.argenprop.com/casa-en-venta-en-puerto-madryn-5-ambientes--19942803"
    assert _looks_like_listing(url) is True
    stub = "<html>blocked</html>" + ("x" * 2000)
    assert _listing_html_ok(url, stub) is False
    ficha = (
        '<div class="leaflet-container" data-location-map data-latitude="-42,78182" '
        'data-longitude="-65,03802"></div>'
        + ("." * 9000)
    )
    assert _listing_html_ok(url, ficha) is True


def test_proxy_html_looks_valid_needs_map_or_cards():
    assert proxy_html_looks_valid("x") is False
    assert proxy_html_looks_valid("Access Denied") is False
    body = "mapData: { adLocationData: { coordinates: { latitude: '-42.75' } } }" + ("." * 4000)
    assert proxy_html_looks_valid(body) is True


def test_portal_way_routes_known_hosts():
    from app.http_client import portal_way

    assert portal_way("https://www.properati.com.ar/detalle/ficha") == "translate"
    assert portal_way("https://www.zonaprop.com.ar/propiedades/clasificado/x.html") == "local"
    assert portal_way("https://www.argenprop.com/casa-en-venta-en-trelew--19704628") == "local"
    assert portal_way("https://www.zonaprop.com.ar/departamentos-venta-capital-federal.html") == "tor"
    assert portal_way("https://www.mercadolibre.com.ar/MLA-123456789") == "tor"
    assert portal_way(listing_id="zonaprop:1") == "local"
    assert portal_way(listing_id="properati:1") == "translate"
    assert portal_way(listing_id="mercadolibre:1") == "tor"


def test_fetch_text_uses_translate_proxy_when_properati_returns_401(monkeypatch):
    listing = "https://www.properati.com.ar/detalle/ficha-oculta"
    html = (
        "mapData: { adLocationData: { coordinates: { latitude: '-42.756297', longitude: '-65.037425' } } }"
        + ("." * 4000)
    )
    routes = {
        "https://www.properati.com.ar/": _Resp(401, "Access Denied", listing),
        "https://translate.yandex.com/translate": _Resp(200, html, "https://translated.turbopages.org/x"),
    }

    monkeypatch.setattr("app.http_client.httpx.Client", lambda **kw: _Client(routes, **kw))
    monkeypatch.setattr("app.http_client.crawl.wait", lambda **kw: None)
    monkeypatch.setattr("app.http_client.crawl.aborted", lambda: False)
    monkeypatch.setattr("app.http_client.crawl.note_http", lambda *a, **k: None)

    text = fetch_text(listing, paced=False, retries=1)
    assert "-42.756297" in text
    assert "mapData" in text


def test_fetch_text_does_not_proxy_zonaprop(monkeypatch):
    reset_fetch_state()
    url = "https://www.zonaprop.com.ar/propiedades/clasificado/x.html"
    called = []

    def boom(*_a, **_k):
        called.append("proxy")
        raise AssertionError("zonaprop no debe ir al proxy")

    monkeypatch.setattr("app.http_client.httpx.Client", lambda **kw: _Client({
        "https://www.zonaprop.com.ar/": _Resp(401, "Access Denied", url),
    }, **kw))
    monkeypatch.setattr("app.http_client.crawl.wait", lambda **kw: None)
    monkeypatch.setattr("app.http_client.crawl.aborted", lambda: False)
    monkeypatch.setattr("app.http_client.crawl.note_http", lambda *a, **k: None)
    monkeypatch.setattr("app.http_client._fetch_translate_proxy", boom)
    monkeypatch.setattr(
        "app.http_client._fetch_urllib",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("urllib fail")),
    )
    try:
        fetch_text(url, paced=False, retries=1)
    except RuntimeError:
        pass
    assert called == []


def test_fetch_text_uses_stealth_when_proxy_fails_on_listing(monkeypatch):
    listing = "https://www.properati.com.ar/detalle/ficha-stealth"
    html = "<html>" + ("mapData" * 200) + "</html>"
    monkeypatch.setenv("PROPMAP_STEALTH_TEST", "1")
    monkeypatch.setenv("SGAI_API_KEY", "sgai-test")
    monkeypatch.setattr("app.http_client.httpx.Client", lambda **kw: _Client({
        "https://www.properati.com.ar/": _Resp(401, "Access Denied", listing),
    }, **kw))
    monkeypatch.setattr("app.http_client.crawl.wait", lambda **kw: None)
    monkeypatch.setattr("app.http_client.crawl.aborted", lambda: False)
    monkeypatch.setattr("app.http_client.crawl.note_http", lambda *a, **k: None)
    monkeypatch.setattr("app.http_client._fetch_urllib", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("urllib fail")))
    monkeypatch.setattr("app.http_client._try_blocked_fallback", lambda *_a, **_k: None)
    monkeypatch.setattr("app.http_client._fetch_just_scrape_cli", lambda *_a, **_k: None)
    monkeypatch.setattr("app.http_client._fetch_sgai_scrape", lambda *_a, **_k: html)
    text = fetch_text(listing, paced=False, retries=1)
    assert "mapData" in text


def test_fetch_text_skips_zonaprop_list_after_403(monkeypatch):
    reset_fetch_state()
    url = "https://www.zonaprop.com.ar/departamentos-venta-capital-federal.html"
    urllib_hits = []
    httpx_hits = []

    class _Counting(_Client):
        def get(self, url: str):
            httpx_hits.append(url)
            return super().get(url)

    monkeypatch.setattr(
        "app.http_client.httpx.Client",
        lambda **kw: _Counting({"https://www.zonaprop.com.ar/": _Resp(403, "Forbidden", url)}, **kw),
    )
    monkeypatch.setattr("app.http_client.crawl.wait", lambda **kw: None)
    monkeypatch.setattr("app.http_client.crawl.aborted", lambda: False)
    monkeypatch.setattr("app.http_client.crawl.note_http", lambda *a, **k: None)
    monkeypatch.setattr(
        "app.http_client._fetch_urllib",
        lambda *_a, **_k: urllib_hits.append("urllib") or (_ for _ in ()).throw(RuntimeError("no urllib")),
    )
    try:
        fetch_text(url, paced=False, retries=1)
    except RuntimeError:
        pass
    assert urllib_hits == []
    assert len(httpx_hits) == 1
    try:
        fetch_text(url, paced=False, retries=1)
        raise AssertionError("debía abortar el listado bloqueado")
    except RuntimeError as exc:
        assert "pausa" in str(exc).lower()
    assert len(httpx_hits) == 1


def test_fetch_text_properati_list_still_uses_proxy_after_401(monkeypatch):
    reset_fetch_state()
    url = "https://www.properati.com.ar/s/puerto-madryn/venta"
    html = (
        "mapData: { adLocationData: { coordinates: { latitude: '-42.75', longitude: '-65.03' } } }"
        + ("." * 4000)
    )
    routes = {
        "https://www.properati.com.ar/": _Resp(401, "Access Denied", url),
        "https://translate.yandex.com/translate": _Resp(200, html, "https://translated.turbopages.org/x"),
    }
    monkeypatch.setattr("app.http_client.httpx.Client", lambda **kw: _Client(routes, **kw))
    monkeypatch.setattr("app.http_client.crawl.wait", lambda **kw: None)
    monkeypatch.setattr("app.http_client.crawl.aborted", lambda: False)
    monkeypatch.setattr("app.http_client.crawl.note_http", lambda *a, **k: None)
    monkeypatch.setattr(
        "app.http_client._fetch_urllib",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("urllib fail")),
    )
    text = fetch_text(url, paced=False, retries=1)
    assert "mapData" in text
    text2 = fetch_text(url, paced=False, retries=1)
    assert "mapData" in text2


def test_fetch_text_marks_gone_on_410(monkeypatch):
    from app.http_client import PageGone

    reset_fetch_state()
    url = "https://www.argenprop.com/casa-en-venta-en-trelew--19704628"
    monkeypatch.setattr(
        "app.http_client.httpx.Client",
        lambda **kw: _Client({"https://www.argenprop.com/": _Resp(410, "Gone", url)}, **kw),
    )
    monkeypatch.setattr("app.http_client.crawl.wait", lambda **kw: None)
    monkeypatch.setattr("app.http_client.crawl.aborted", lambda: False)
    monkeypatch.setattr("app.http_client.crawl.note_http", lambda *a, **k: None)
    try:
        fetch_text(url, paced=False, retries=1)
        raise AssertionError("un 410 tiene que cortar la cascada")
    except PageGone as exc:
        assert "410" in str(exc)
    reset_fetch_state()


def test_fetch_text_skips_tor_on_zonaprop_listing(monkeypatch):
    from app import crawl, egress

    reset_fetch_state()
    crawl.reset()
    monkeypatch.setenv("PROPMAP_TOR_TEST", "1")
    monkeypatch.setenv("TOR_ENABLED", "1")
    monkeypatch.setenv("TOR_SOCKS", "socks5h://127.0.0.1:19050")
    monkeypatch.setenv("TOR_CIRCUITS", "8")
    monkeypatch.setenv("SCRAPE_USE_LOCAL", "1")
    monkeypatch.setenv("SCRAPE_LOCAL_GAP_SEC", "0")
    monkeypatch.delenv("SCRAPE_PROXIES", raising=False)
    monkeypatch.setattr(egress, "_socks_supported", lambda _url: True)
    egress.reset()
    url = "https://www.zonaprop.com.ar/propiedades/clasificado/vive.html"
    html = "<html>ficha zonaprop" + ("." * 9000) + "</html>"
    proxies = []

    class _Client:
        def __init__(self, **kw):
            self.proxy = kw.get("proxy")

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def get(self, href: str):
            proxies.append(self.proxy)
            if self.proxy:
                raise AssertionError("ZonaProp no debe salir por Tor")
            return _Resp(200, html, href)

    monkeypatch.setattr("app.http_client.httpx.Client", lambda **kw: _Client(**kw))
    monkeypatch.setattr("app.http_client.crawl.wait", lambda **kw: None)
    monkeypatch.setattr("app.http_client.crawl.aborted", lambda: False)
    monkeypatch.setattr("app.http_client._try_blocked_fallback", lambda *_a, **_k: None)
    monkeypatch.setattr("app.http_client._try_stealth_fetch", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "app.http_client._fetch_urllib",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("no")),
    )
    text = fetch_text(url, paced=False, retries=1)
    assert "ficha zonaprop" in text
    assert proxies == [None]
    assert egress.local_used_today() == 1
    crawl.reset()
    reset_fetch_state()
    egress.reset()


def test_fetch_text_drops_gone_from_local(monkeypatch):
    from app import crawl, egress
    from app.http_client import PageGone

    reset_fetch_state()
    crawl.reset()
    monkeypatch.setenv("PROPMAP_TOR_TEST", "1")
    monkeypatch.setenv("TOR_ENABLED", "1")
    monkeypatch.setenv("TOR_SOCKS", "socks5h://127.0.0.1:19050")
    monkeypatch.setenv("TOR_CIRCUITS", "2")
    monkeypatch.setenv("SCRAPE_USE_LOCAL", "1")
    monkeypatch.setenv("SCRAPE_LOCAL_GAP_SEC", "0")
    monkeypatch.delenv("SCRAPE_PROXIES", raising=False)
    monkeypatch.setattr(egress, "_socks_supported", lambda _url: True)
    egress.reset()
    url = "https://www.argenprop.com/casa-en-venta-en-trelew--19704628"

    class _Client:
        def __init__(self, **kw):
            self.proxy = kw.get("proxy")

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def get(self, href: str):
            if self.proxy:
                raise AssertionError("Argenprop no debe salir por Tor")
            return _Resp(410, "Gone", href)

    monkeypatch.setattr("app.http_client.httpx.Client", lambda **kw: _Client(**kw))
    monkeypatch.setattr("app.http_client.crawl.wait", lambda **kw: None)
    monkeypatch.setattr("app.http_client.crawl.aborted", lambda: False)
    monkeypatch.setattr("app.http_client._try_blocked_fallback", lambda *_a, **_k: None)
    monkeypatch.setattr("app.http_client._try_stealth_fetch", lambda *_a, **_k: None)
    try:
        fetch_text(url, paced=False, retries=1)
        raise AssertionError("un 410 local tiene que cortar")
    except PageGone as exc:
        assert "410" in str(exc)
    crawl.reset()
    reset_fetch_state()
    egress.reset()


def test_enrich_details_drops_listing_that_is_gone(monkeypatch):
    from app.http_client import PageGone
    from app.models import Listing
    from app.scrapers import details

    item = Listing(
        source="argenprop",
        source_id="19704628",
        url="https://www.argenprop.com/casa-en-venta-en-trelew--19704628",
        title="Casa",
        property_type="casa",
        city="trelew",
    )
    dropped = []
    monkeypatch.setattr("app.store.drop_listings", lambda ids: dropped.extend(ids))
    monkeypatch.setattr(details, "fetch_text", lambda *_a, **_k: (_ for _ in ()).throw(PageGone("HTTP 410")))
    details.enrich_details(item)
    assert dropped == [item.id]
    assert item.extra.get("gone") is True
    assert item.details_scraped is False
