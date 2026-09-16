import time

from app.pipeline import ping_lock, status
from app.watchdog import STALE_SEC, alive, heartbeat_path, snapshot


def test_ping_lock_is_quick():
    assert ping_lock(0.2) is True


def test_status_returns_cached_when_lock_is_busy():
    from app import pipeline

    pipeline._last_public = {
        "running": False,
        "running_cities": [],
        "message": "cacheado",
        "jobs": {},
        "city_catalog": [],
    }
    pipeline._lock.acquire()
    try:
        data = status()
        assert data["busy"] is True
        assert data["message"] == "cacheado"
    finally:
        pipeline._lock.release()
    pipeline._last_public = {}


def test_watchdog_snapshot_without_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr("app.store.DATA_DIR", tmp_path)
    beat = snapshot()
    assert beat["ok"] is False
    assert alive() is False


def test_watchdog_snapshot_fresh_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr("app.store.DATA_DIR", tmp_path)
    path = heartbeat_path()
    path.write_text("1 ok\n", encoding="ascii")
    assert snapshot()["ok"] is True
    path.touch()
    time.sleep(0.01)
    assert snapshot()["age_sec"] is not None
    assert snapshot()["age_sec"] < STALE_SEC


def test_watchdog_http_ok_and_fail(monkeypatch):
    from app import watchdog

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(watchdog.urllib.request, "urlopen", lambda *a, **k: FakeResp())
    assert watchdog._http_ok() is True

    def boom(*_a, **_k):
        raise TimeoutError("slow")

    monkeypatch.setattr(watchdog.urllib.request, "urlopen", boom)
    assert watchdog._http_ok() is False
    from app import watchdog

    watchdog.start()
    assert watchdog._thread is None or not watchdog._thread.is_alive()


def test_watchdog_stays_ok_while_cache_builds(monkeypatch):
    from app import listings_cache, watchdog

    monkeypatch.setattr(watchdog, "_ping_locks", lambda: False)
    monkeypatch.setattr(watchdog, "_http_ok", lambda: False)
    watchdog._started = 0.0
    with listings_cache._Building():
        assert watchdog._healthy() is True
    assert watchdog._healthy() is False


def test_cities_loading_does_not_block_when_lock_is_held():
    import threading

    from app.listings_cache import _lock, cities_loading

    _lock.acquire()
    try:
        result: dict = {}

        def worker() -> None:
            t0 = time.time()
            result["v"] = cities_loading()
            result["dt"] = time.time() - t0

        th = threading.Thread(target=worker)
        th.start()
        th.join(1.0)
        assert not th.is_alive()
        assert result["v"] is False
        assert result["dt"] < 0.4
    finally:
        _lock.release()


def test_status_lock_busy_without_cache():
    from app import pipeline

    pipeline._last_public = {}
    pipeline._lock.acquire()
    try:
        data = status()
        assert data["busy"] is True
        assert "ocupado" in (data.get("message") or "")
    finally:
        pipeline._lock.release()


def test_status_request_path_skips_sqlite(monkeypatch):
    from app import pipeline, store

    hits = []
    real = store.get_meta

    def wrapped(*args, **kwargs):
        hits.append(args[0] if args else kwargs.get("key"))
        return real(*args, **kwargs)

    monkeypatch.setattr("app.store.get_meta", wrapped)
    monkeypatch.setattr("app.pipeline.store.get_meta", wrapped)
    pipeline._aux["at"] = time.time()
    pipeline._aux_inflight = False
    data = status()
    assert data["busy"] is False
    assert hits == []
