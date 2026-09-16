import inspect
import threading
import time

from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from app import main
from app.main import _stats_is_asset


def test_stats_assets_are_not_forced_nostore():
    assert _stats_is_asset("/stats/index.php", "module=Proxy&action=getCoreJs")
    assert not _stats_is_asset("/stats/", "")
    assert not _stats_is_asset("/stats/index.php", "module=Login")


def test_secure_headers_does_not_buffer_bodies():
    assert not issubclass(main.SecureHeaders, BaseHTTPMiddleware)


def test_html_routes_are_async():
    assert inspect.iscoroutinefunction(main.index)
    assert inspect.iscoroutinefunction(main.legal_page)
    assert inspect.iscoroutinefunction(main.alive)


def test_home_serves_leaflet_from_same_origin():
    html = (main.STATIC / "index.html").read_text(encoding="utf-8")
    assert "/static/vendor/leaflet/leaflet.js" in html
    assert "/static/vendor/leaflet.markercluster/leaflet.markercluster.js" in html
    assert "unpkg.com/leaflet" not in html


def test_index_and_alive_are_fast():
    with TestClient(main.app) as client:
        t0 = time.time()
        home = client.get("/")
        alive = client.get("/api/alive")
        legal = client.get("/legal")
        elapsed = time.time() - t0
    assert home.status_code == 200
    assert b"PropMap" in home.content
    assert home.headers.get("x-content-type-options") == "nosniff"
    assert alive.status_code == 200
    assert alive.json()["ok"] is True
    assert legal.status_code == 200
    assert elapsed < 3


def test_kick_llm_later_does_not_block_caller(monkeypatch):
    from app import pipeline

    started = threading.Event()
    proceed = threading.Event()
    ran = []

    def slow(cid):
        started.set()
        proceed.wait(1)
        ran.append(cid)

    monkeypatch.setattr(pipeline, "_kick_llm_enrich", slow)
    t0 = time.time()
    pipeline.kick_llm_later("caba")
    assert time.time() - t0 < 0.25
    assert started.wait(1)
    assert ran == []
    proceed.set()
    deadline = time.time() + 1
    while time.time() < deadline and not ran:
        time.sleep(0.01)
    assert ran == ["caba"]


def test_listings_get_does_not_kick_city_llm(monkeypatch):
    called = []
    monkeypatch.setattr("app.pipeline.kick_llm_later", lambda cid: called.append(cid))
    monkeypatch.setattr("app.pipeline._kick_llm_enrich", lambda cid: called.append(cid))
    with TestClient(main.app) as client:
        client.get("/api/listings?city=caba")
    assert called == []


def test_eta_minutes_grows_with_queue():
    from app.pipeline import eta_minutes

    assert eta_minutes("x", running=["x"], mode="fast", live=True) == 4
    assert eta_minutes("x", running=["a", "b", "x"], mode="fast", live=True) == 16
    assert eta_minutes("x", running=["x"], mode="slow", live=True) == 18


def test_drop_empty_listed_place_forgets_city(monkeypatch):
    from app import pipeline

    forgot = []
    monkeypatch.setattr("app.store.city_listing_counts", lambda: {})
    monkeypatch.setattr("app.places.forget_place", lambda cid: forgot.append(cid))
    monkeypatch.setattr("app.listings_cache.refresh_city_catalog", lambda: None)
    pipeline._drop_empty_listed_place("alta-gracia")
    assert forgot == ["alta-gracia"]
    forgot.clear()
    monkeypatch.setattr("app.store.city_listing_counts", lambda: {"alta-gracia": 3})
    pipeline._drop_empty_listed_place("alta-gracia")
    assert forgot == ["alta-gracia"]
    forgot.clear()
    monkeypatch.setattr("app.store.city_listing_counts", lambda: {"alta-gracia": 8})
    pipeline._drop_empty_listed_place("alta-gracia")
    assert forgot == []
    pipeline._drop_empty_listed_place("caba")
    assert forgot == []


def test_listings_http_cache_control(monkeypatch):
    monkeypatch.setattr("app.listings_cache.request_city_bytes", lambda *a, **k: None)
    monkeypatch.setattr("app.listings_cache.unchanged_listings", lambda *a, **k: None)
    monkeypatch.setattr(
        "app.listings_cache.listings_body",
        lambda *a, **k: (b'{"rev":1,"listings":[]}', None),
    )
    with TestClient(main.app) as client:
        res = client.get("/api/listings?city=caba")
    assert res.status_code == 200
    cc = res.headers.get("Cache-Control") or ""
    assert "max-age=45" in cc
    assert "stale-while-revalidate=120" in cc
