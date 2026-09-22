import pytest

from app.analytics import record, summary


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    from app import analytics, store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "listings.sqlite")
    store.init()
    analytics._recent.clear()
    analytics._prune_at = 0.0


def test_analytics_keeps_pageviews_and_places():
    record({"n": "pageview", "p": "/", "vid": "t-visitor", "city": ""}, ua="Mozilla")
    record({"n": "place", "p": "/", "vid": "t-visitor", "city": "microcentro-caba"}, ua="iPhone")
    data = summary(14)
    assert data["events"] >= 2
    assert data["visitors"] >= 1
    names = {row["name"] for row in data["events_by_name"]}
    assert "pageview" in names or "place" in names


def test_analytics_counts_where_they_came_from():
    from app import analytics

    analytics._recent.clear()
    record({"n": "pageview", "p": "/from-google", "vid": "g1", "r": "https://www.google.com/search"}, ua="Mozilla")
    record({"n": "pageview", "p": "/direct", "vid": "d1"}, ua="Mozilla")
    record({"n": "pageview", "p": "/utm", "vid": "i1", "utm": "instagram"}, ua="Mozilla")
    data = summary(14)
    refs = {row["name"]: row["count"] for row in data["referrers"]}
    assert refs.get("www.google.com") >= 1
    assert refs.get("directo") >= 1
    assert refs.get("instagram") >= 1
    assert data["visitors"] >= 1
    assert data["pageviews"] >= 1


def test_index_does_not_wait_for_visit_write(monkeypatch):
    import time

    from fastapi.testclient import TestClient

    from app import analytics, main

    def stuck(*_a, **_k):
        time.sleep(20)

    monkeypatch.setattr(analytics, "record", stuck)
    with TestClient(main.app) as client:
        t0 = time.time()
        resp = client.get("/")
        elapsed = time.time() - t0
    assert resp.status_code == 200
    assert b"PropMap" in resp.content
    assert elapsed < 3


def test_analytics_skips_crawlers():
    from app import analytics

    analytics._recent.clear()
    record(
        {"n": "pageview", "p": "/bot", "vid": "google-bot"},
        ua="Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    )
    data = summary(14)
    pages = {row["name"]: row["count"] for row in data["pages"]}
    assert pages.get("/bot", 0) == 0


def test_record_skips_when_write_lock_is_busy():
    from app import analytics, store

    analytics._recent.clear()
    held = store._write.acquire()
    assert held
    try:
        record({"n": "pageview", "p": "/locked", "vid": "lock-visitor"}, ua="Mozilla")
    finally:
        store._write.release()
    data = summary(14)
    pages = {row["name"]: row["count"] for row in data["pages"]}
    assert pages.get("/locked", 0) == 0
