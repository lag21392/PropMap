import sqlite3
import threading
import time

from fastapi.testclient import TestClient

from app import listings_cache, main, market
from app.http_timing import note, reset, snapshot


def test_timing_snapshot_has_percentiles():
    reset()
    note("listings", 10, {"city": "caba"})
    note("listings", 20)
    note("listings", 40)
    row = snapshot()["listings"]
    assert row["n"] == 3
    assert row["last_ms"] == 40
    assert row["max_ms"] == 40
    assert row["p50_ms"] >= 10
    assert row["extra"]["city"] == "caba"


def test_admin_stats_includes_perf(monkeypatch):
    monkeypatch.setenv("SEARCH_PASSWORD", "test-secret")
    reset()
    note("market", 85.2, {"city": "alta-gracia"})
    with TestClient(main.app) as client:
        res = client.post(
            "/api/admin/stats",
            json={"password": "test-secret", "days": 7, "client": {"listings": {"n": 1, "last_ms": 30}}},
        )
    assert res.status_code == 200
    body = res.json()
    assert body["perf"]["market"]["last_ms"] == 85.2
    assert body["client_perf"]["listings"]["n"] == 1


def test_listings_response_has_server_timing():
    with TestClient(main.app) as client:
        res = client.get("/api/listings?city=caba&pins=1")
    assert res.status_code == 200
    assert "listings" in (res.headers.get("Server-Timing") or "")


def test_price_history_filters_ids():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE price_history (
            id INTEGER PRIMARY KEY,
            listing_id TEXT,
            seen_at TEXT,
            price_usd REAL,
            price_m2 REAL,
            deal_label TEXT,
            deal_score REAL
        )
        """
    )
    conn.executemany(
        "INSERT INTO price_history (listing_id, seen_at, price_usd, price_m2, deal_label, deal_score) VALUES (?,?,?,?,?,?)",
        [
            ("a", "2026-01-01", 1, 1, "", 0),
            ("b", "2026-01-01", 2, 2, "", 0),
        ],
    )
    rows = market._price_history_rows(conn, ["a"])
    assert [row["listing_id"] for row in rows] == ["a"]
    assert market._price_history_rows(conn, []) == []


def test_market_payload_reuses_cache(monkeypatch):
    market.reset_cache()
    calls = {"n": 0}
    payload = {"city": "x", "property_type": "all"}

    def fake(city, kind):
        calls["n"] += 1
        return payload

    monkeypatch.setattr(market, "_build_market", fake)
    first = market.market_payload("x", "")
    second = market.market_payload("x", "")
    assert first is second
    assert calls["n"] == 1
    market.reset_cache()
    market.market_payload("x", "")
    assert calls["n"] == 2


def test_request_city_bytes_place_warm_is_async(monkeypatch):
    listings_cache.reset()
    monkeypatch.setenv("PROPMAP_TEST", "0")
    started = threading.Event()
    proceed = threading.Event()

    def slow(cid):
        started.set()
        proceed.wait(2)

    monkeypatch.setattr("app.places.ensure_view_city", slow)
    monkeypatch.setattr("app.places.ensure_city_outline", lambda *a, **k: None)
    monkeypatch.setattr("app.osm_poi.ensure_city_pois", lambda *a, **k: None)
    monkeypatch.setattr("app.access.kick_access_later", lambda *a, **k: None)
    monkeypatch.setattr(listings_cache, "_disk_http_ready", lambda cid: True)
    t0 = time.time()
    listings_cache.request_city_bytes("alta-gracia")
    assert time.time() - t0 < 0.4
    assert started.wait(1)
    proceed.set()


def test_listing_from_public_keeps_deal():
    item = listings_cache._listing_from_public(
        {
            "id": "zonaprop:1",
            "source": "zonaprop",
            "title": "Depto",
            "property_type": "departamento",
            "price_usd": 100000,
            "price_m2": 2000,
            "deal_label": "oportunidad",
            "city": "caba",
            "exclude_from_comps": True,
        }
    )
    assert item.id == "zonaprop:1"
    assert item.deal_label == "oportunidad"
    assert item.extra["exclude_from_comps"] is True


def test_build_market_prefers_snap_over_sql(monkeypatch):
    from app.models import Listing

    item = Listing(
        source="zonaprop",
        source_id="snap-1",
        url="",
        title="Depto",
        property_type="departamento",
        price_usd=90000,
        price_m2=1800,
        deal_label="mercado",
        city="test-city",
    )
    monkeypatch.setattr(listings_cache, "market_items", lambda city: [item])
    fetched = []
    monkeypatch.setattr("app.store.fetch_for_city", lambda city: fetched.append("fits") or [])
    monkeypatch.setattr("app.store.fetch_by_cities", lambda cities: fetched.append("sql") or [])
    monkeypatch.setattr(market, "ensure_ready", lambda *a, **k: None)
    monkeypatch.setattr(market, "public_place", lambda city: {"label": city})
    monkeypatch.setattr(market, "_yield_block", lambda items, city: {})

    class _Conn:
        def execute(self, *a, **k):
            return self

        def fetchall(self):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("app.store.connect", lambda: _Conn())
    monkeypatch.setattr(market, "ensure_tables", lambda conn: None)
    data = market._build_market("test-city", "all")
    assert fetched == []
    assert data["city"] == "test-city"
    assert data["tracked"] == 1
