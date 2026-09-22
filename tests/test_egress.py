from app import crawl, egress
from app.http_client import fetch_text, reset_fetch_state


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


def test_test_mode_has_only_direct_lane(monkeypatch):
    monkeypatch.delenv("SCRAPE_PROXIES", raising=False)
    monkeypatch.delenv("PROPMAP_TOR_TEST", raising=False)
    egress.reset()
    rows = egress.lanes()
    assert [lane.id for lane in rows] == ["direct"]
    assert egress.lane_count() == 1


def test_proxies_and_tor_become_extra_lanes(monkeypatch):
    monkeypatch.setenv("SCRAPE_PROXIES", "http://127.0.0.1:18080, http://127.0.0.1:18081")
    monkeypatch.setenv("PROPMAP_TOR_TEST", "1")
    monkeypatch.setenv("TOR_ENABLED", "1")
    monkeypatch.setenv("TOR_SOCKS", "socks5h://127.0.0.1:19050")
    monkeypatch.setenv("TOR_CIRCUITS", "2")
    monkeypatch.delenv("SCRAPE_USE_LOCAL", raising=False)
    monkeypatch.setattr(egress, "_socks_supported", lambda _url: True)
    egress.reset()
    kinds = [lane.kind for lane in egress.lanes()]
    assert "direct" not in kinds
    assert kinds.count("proxy") == 2
    assert kinds.count("tor") == 2
    snap = egress.snapshot()
    assert snap["lanes"] == 4
    assert snap["use_local"] is False
    assert snap["tor_circuits"] == 2
    assert "down" in snap
    assert [row["kind"] for row in snap["tracks"]] == kinds
    tor_proxies = [lane.proxy for lane in egress.lanes() if lane.kind == "tor"]
    assert len(set(tor_proxies)) == 2
    assert all("@" in (url or "") for url in tor_proxies)


def test_pick_skips_cooling_lane_not_short_wait():
    crawl.reset()
    egress.reset()
    host = "www.zonaprop.com.ar"
    crawl.backoff(0.4, host, lane="direct")
    assert egress.pick(host) is not None
    assert egress.pick(host).id == "direct"
    crawl.note_http(403, host, lane="direct")
    assert egress.pick(host) is None
    crawl.reset()
    egress.reset()


def test_403_on_direct_still_picks_proxy(monkeypatch):
    monkeypatch.setenv("SCRAPE_PROXIES", "http://127.0.0.1:18080")
    egress.reset()
    crawl.reset()
    host = "www.argenprop.com"
    crawl.note_http(403, host, lane="direct")
    lane = egress.pick(host)
    assert lane is not None
    assert lane.kind == "proxy"
    assert egress.can_fetch(host)
    crawl.reset()
    egress.reset()


def test_dead_lane_is_skipped_then_recovers(monkeypatch):
    monkeypatch.setenv("SCRAPE_PROXIES", "http://127.0.0.1:18080")
    egress.reset()
    crawl.reset()
    egress.note_error("direct", seconds=30)
    lane = egress.pick("www.mercadolibre.com.ar")
    assert lane is not None and lane.id == "proxy-0"
    egress.reset()
    assert egress.pick("www.mercadolibre.com.ar").id == "proxy-0"


def test_fetch_text_uses_next_lane_after_403(monkeypatch):
    reset_fetch_state()
    crawl.reset()
    monkeypatch.setenv("SCRAPE_PROXIES", "http://proxy.test:8080")
    monkeypatch.setenv("SCRAPE_USE_LOCAL", "1")
    egress.reset()
    url = "https://www.zonaprop.com.ar/departamentos-venta-capital-federal.html"
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
                return _Resp(403, "Forbidden", href)
            return _Resp(200, "<html>listado</html>" + ("." * 200), href)

    monkeypatch.setattr("app.http_client.httpx.Client", lambda **kw: _Client(**kw))
    monkeypatch.setattr("app.http_client.crawl.wait", lambda **kw: None)
    monkeypatch.setattr("app.http_client.crawl.aborted", lambda: False)
    monkeypatch.setattr(
        "app.http_client._fetch_urllib",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("no")),
    )
    text = fetch_text(url, paced=False, retries=1)
    assert "listado" in text
    assert None in proxies
    assert "http://proxy.test:8080" in proxies
    crawl.reset()
    reset_fetch_state()
    egress.reset()


def test_pick_prefers_tor_over_local(monkeypatch):
    monkeypatch.setenv("PROPMAP_TOR_TEST", "1")
    monkeypatch.setenv("TOR_ENABLED", "1")
    monkeypatch.setenv("TOR_SOCKS", "socks5h://127.0.0.1:19050")
    monkeypatch.setenv("TOR_CIRCUITS", "3")
    monkeypatch.setenv("SCRAPE_USE_LOCAL", "0")
    monkeypatch.delenv("SCRAPE_PROXIES", raising=False)
    monkeypatch.setattr(egress, "_socks_supported", lambda _url: True)
    egress.reset()
    crawl.reset()
    rows = egress.lanes()
    assert [lane.kind for lane in rows] == ["tor", "tor", "tor"]
    lane = egress.pick("www.zonaprop.com.ar")
    assert lane is not None and lane.kind == "tor"
    crawl.note_http(403, "www.zonaprop.com.ar", lane="tor-0")
    nxt = egress.pick("www.zonaprop.com.ar")
    assert nxt is not None and nxt.id != "tor-0"
    crawl.reset()
    egress.reset()


def test_grows_tor_circuits_when_all_blocked(monkeypatch):
    monkeypatch.setenv("PROPMAP_TOR_TEST", "1")
    monkeypatch.setenv("TOR_ENABLED", "1")
    monkeypatch.setenv("TOR_SOCKS", "socks5h://127.0.0.1:19050")
    monkeypatch.setenv("TOR_CIRCUITS", "2")
    monkeypatch.setenv("SCRAPE_USE_LOCAL", "0")
    monkeypatch.delenv("SCRAPE_PROXIES", raising=False)
    monkeypatch.setattr(egress, "_socks_supported", lambda _url: True)
    monkeypatch.setattr(egress, "GROW_EVERY_SEC", 0)
    egress.reset()
    crawl.reset()
    assert [lane.id for lane in egress.lanes()] == ["tor-0", "tor-1"]
    crawl.note_http(403, "www.zonaprop.com.ar", lane="tor-0")
    crawl.note_http(403, "www.zonaprop.com.ar", lane="tor-1")
    lane = egress.pick("www.zonaprop.com.ar")
    assert lane is not None
    assert lane.id in {"tor-2", "tor-3"}
    snap = egress.snapshot()
    assert snap["tor_circuits"] == 4
    assert snap["tor_extra"] == 2
    crawl.reset()
    egress.reset()


def test_local_pages_run_in_parallel(monkeypatch):
    monkeypatch.setenv("SCRAPE_USE_LOCAL", "1")
    monkeypatch.setenv("SCRAPE_LOCAL_GAP_SEC", "20")
    monkeypatch.setenv("SCRAPE_LOCAL_PARALLEL", "3")
    monkeypatch.setenv("SCRAPE_LOCAL_MAX_DAY", "600")
    monkeypatch.delenv("SCRAPE_PROXIES", raising=False)
    monkeypatch.delenv("TOR_ENABLED", raising=False)
    egress.reset()
    crawl.reset()
    zp = "www.zonaprop.com.ar"
    assert egress.acquire_local(zp) is True
    assert egress.acquire_local(zp) is True
    assert egress.acquire_local(zp) is True
    assert egress.acquire_local(zp, timeout=0.05) is False
    assert egress.local_ready("www.argenprop.com") is True
    egress.release_local(zp)
    assert egress.acquire_local(zp, timeout=0.05) is True
    egress.reset()
    crawl.reset()


def test_acquire_local_holds_the_page_slot(monkeypatch):
    monkeypatch.setenv("SCRAPE_USE_LOCAL", "1")
    monkeypatch.setenv("SCRAPE_LOCAL_GAP_SEC", "0")
    monkeypatch.setenv("SCRAPE_LOCAL_PARALLEL", "1")
    monkeypatch.setenv("SCRAPE_LOCAL_MAX_DAY", "600")
    monkeypatch.delenv("SCRAPE_PROXIES", raising=False)
    monkeypatch.delenv("TOR_ENABLED", raising=False)
    egress.reset()
    assert egress.acquire_local("www.zonaprop.com.ar", timeout=0.2) is True
    assert egress.local_used_today() == 1
    assert egress.local_has_room() is False
    assert egress.acquire_local("www.argenprop.com", timeout=0.05) is False
    egress.release_local("www.zonaprop.com.ar")
    assert egress.acquire_local("www.argenprop.com", timeout=0.05) is True
    egress.reset()


def test_local_ready_while_other_portal_cools(monkeypatch):
    monkeypatch.setenv("SCRAPE_USE_LOCAL", "1")
    monkeypatch.setenv("SCRAPE_LOCAL_GAP_SEC", "0")
    monkeypatch.delenv("SCRAPE_PROXIES", raising=False)
    monkeypatch.delenv("TOR_ENABLED", raising=False)
    egress.reset()
    crawl.reset()
    crawl.note_http(403, "www.zonaprop.com.ar", lane="direct")
    assert egress.local_ready("www.zonaprop.com.ar") is False
    assert egress.local_ready("www.argenprop.com") is True
    assert egress.acquire_local("www.argenprop.com") is True
    egress.release_local("www.argenprop.com")
    crawl.note_http(403, "inmueble.mercadolibre.com.ar", lane="direct")
    assert egress.local_ready("mercadolibre.com.ar") is False
    egress.reset()
    crawl.reset()


def test_local_lane_stops_at_daily_cap(monkeypatch):
    monkeypatch.setenv("SCRAPE_USE_LOCAL", "1")
    monkeypatch.setenv("SCRAPE_LOCAL_GAP_SEC", "0")
    monkeypatch.setenv("SCRAPE_LOCAL_MAX_DAY", "2")
    monkeypatch.delenv("SCRAPE_PROXIES", raising=False)
    monkeypatch.delenv("TOR_ENABLED", raising=False)
    egress.reset()
    crawl.reset()
    egress.note_local_use()
    assert egress.local_ready() is True
    egress.note_local_use()
    assert egress.local_ready() is False
    assert egress.pick("www.zonaprop.com.ar") is None
    egress.reset()
    crawl.reset()


def test_tor_goes_before_the_local_ip(monkeypatch):
    monkeypatch.setenv("PROPMAP_TOR_TEST", "1")
    monkeypatch.setenv("TOR_ENABLED", "1")
    monkeypatch.setenv("TOR_SOCKS", "socks5h://127.0.0.1:19050")
    monkeypatch.setenv("TOR_CIRCUITS", "1")
    monkeypatch.setenv("SCRAPE_USE_LOCAL", "1")
    monkeypatch.setenv("SCRAPE_LOCAL_GAP_SEC", "0")
    monkeypatch.delenv("SCRAPE_PROXIES", raising=False)
    monkeypatch.setattr(egress, "_socks_supported", lambda _url: True)
    egress.reset()
    crawl.reset()
    kinds = [lane.kind for lane in egress.lanes()]
    assert "tor" in kinds and "direct" in kinds
    lane = egress.pick("www.zonaprop.com.ar")
    assert lane is not None and lane.kind == "tor"
    egress.reset()
    crawl.reset()
