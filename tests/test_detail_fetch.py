import time

from app.detail_fetch import enqueue, needs_llm
from app.llm_enrich import LLM_SCHEMA
from app.models import Listing


def _item(source_id: str, *, await_llm: bool = False, detailed: bool = False) -> Listing:
    extra = {"search_city": "caba"}
    if await_llm:
        extra["await_llm"] = True
    if detailed:
        extra["details_at"] = "2026-08-26T03:00:00+00:00"
    return Listing(
        source="zonaprop",
        source_id=source_id,
        url="https://example.com/" + source_id,
        title="Depto",
        property_type="departamento",
        city="caba",
        extra=extra,
        details_scraped=detailed,
    )


def test_enqueue_downloads_before_llm(monkeypatch):
    from app import detail_fetch

    monkeypatch.setattr("app.detail_fetch.enabled", lambda: True)
    monkeypatch.setattr("app.detail_fetch._ensure_workers_locked", lambda: None)
    detail_fetch._urgent.clear()
    detail_fetch._queue.clear()
    detail_fetch._seen.clear()
    sent = []
    monkeypatch.setattr("app.llm_enrich.enqueue", lambda rows, **kw: sent.extend(rows))
    raw = _item("need-details", await_llm=True)
    enqueue([raw])
    assert raw.id in detail_fetch._urgent
    assert sent == []


def test_enqueue_skips_unknown_city_after_first_llm_pass(monkeypatch):
    from app import detail_fetch

    monkeypatch.setattr("app.detail_fetch.enabled", lambda: True)
    monkeypatch.setattr("app.detail_fetch._ensure_workers_locked", lambda: None)
    detail_fetch._urgent.clear()
    detail_fetch._queue.clear()
    detail_fetch._seen.clear()
    sent = []
    monkeypatch.setattr("app.llm_enrich.enqueue", lambda rows, **kw: sent.extend(rows))
    lost = _item("no-city")
    lost.city = "fuera"
    lost.extra["llm_city_ok"] = True
    enqueue([lost])
    assert not detail_fetch._urgent and not detail_fetch._queue


def test_enqueue_sends_detailed_listings_to_llm(monkeypatch):
    from app import detail_fetch

    monkeypatch.setattr("app.detail_fetch.enabled", lambda: True)
    monkeypatch.setattr("app.detail_fetch._ensure_workers_locked", lambda: None)
    detail_fetch._urgent.clear()
    detail_fetch._queue.clear()
    detail_fetch._seen.clear()
    sent = []
    monkeypatch.setattr("app.llm_enrich.enqueue", lambda rows, **kw: sent.extend(rows))
    ready = _item("has-details", detailed=True)
    enqueue([ready])
    assert sent and sent[0].id == ready.id
    assert not detail_fetch._urgent and not detail_fetch._queue


def test_fetch_id_calls_llm_after_details(monkeypatch):
    from app import detail_fetch

    item = _item("fetch-then-llm", await_llm=True)
    fetched = []

    def fake_enrich(row, should_stop=lambda: False):
        fetched.append(row.id)
        row.details_scraped = True
        extra = dict(row.extra or {})
        extra["details_at"] = "2026-08-26T03:00:00+00:00"
        row.extra = extra
        return row

    sent = []
    monkeypatch.setattr("app.store.get_listing", lambda listing_id: item)
    monkeypatch.setattr("app.store.upsert_many", lambda rows: None)
    monkeypatch.setattr("app.scrapers.details.enrich_details", fake_enrich)
    monkeypatch.setattr("app.llm_enrich.enqueue", lambda rows, **kw: sent.extend(rows))
    detail_fetch._fetch_id(item.id)
    assert fetched == [item.id]
    assert sent and sent[0].id == item.id


def test_failed_fetch_waits_before_retrying(monkeypatch):
    from app import detail_fetch

    item = _item("blocked-by-portal")
    saved = []

    def blocked(row, should_stop=lambda: False):
        return row

    monkeypatch.setattr("app.detail_fetch.enabled", lambda: True)
    monkeypatch.setattr("app.detail_fetch._ensure_workers_locked", lambda: None)
    monkeypatch.setattr("app.store.get_listing", lambda listing_id: item)
    monkeypatch.setattr("app.store.upsert_many", lambda rows: saved.extend(rows))
    monkeypatch.setattr("app.scrapers.details.enrich_details", blocked)
    monkeypatch.setattr("app.llm_enrich.enqueue", lambda rows, **kw: None)
    detail_fetch._urgent.clear()
    detail_fetch._queue.clear()
    detail_fetch._seen.clear()
    detail_fetch._skip_until.clear()

    detail_fetch._fetch_id(item.id)
    assert item.extra["detail_tries"] == 1
    assert detail_fetch._skip_until[item.id] > time.time()
    assert detail_fetch.queue_stats()["cooling"] == 1

    enqueue([item])
    assert not detail_fetch._urgent and not detail_fetch._queue

    detail_fetch._skip_until.clear()
    detail_fetch._seen.clear()
    detail_fetch._fetch_id(item.id)
    assert item.extra["detail_tries"] == 2
    espera = detail_fetch._skip_until[item.id] - time.time()
    assert espera > detail_fetch.SKIP_FAIL_SEC
    detail_fetch._skip_until.clear()
    detail_fetch._seen.clear()


def test_good_fetch_clears_the_wait(monkeypatch):
    from app import detail_fetch

    item = _item("recovers")
    item.extra["detail_tries"] = 3

    def ok(row, should_stop=lambda: False):
        row.details_scraped = True
        return row

    monkeypatch.setattr("app.store.get_listing", lambda listing_id: item)
    monkeypatch.setattr("app.store.upsert_many", lambda rows: None)
    monkeypatch.setattr("app.scrapers.details.enrich_details", ok)
    monkeypatch.setattr("app.llm_enrich.enqueue", lambda rows, **kw: None)
    detail_fetch._skip_until.clear()
    detail_fetch._skip_until[item.id] = time.time() + 999
    detail_fetch._fetch_id(item.id)
    assert "detail_tries" not in item.extra
    assert item.id not in detail_fetch._skip_until


def test_enqueue_cools_listings_that_already_failed_a_lot(monkeypatch):
    from app import detail_fetch

    monkeypatch.setattr("app.detail_fetch.enabled", lambda: True)
    monkeypatch.setattr("app.detail_fetch._ensure_workers_locked", lambda: None)
    monkeypatch.setattr("app.llm_enrich.enqueue", lambda rows, **kw: None)
    detail_fetch._urgent.clear()
    detail_fetch._queue.clear()
    detail_fetch._seen.clear()
    detail_fetch._skip_until.clear()
    item = _item("many-fails")
    item.extra["detail_tries"] = detail_fetch.COLD_TRIES
    enqueue([item])
    assert not detail_fetch._urgent and not detail_fetch._queue
    assert detail_fetch._skip_until[item.id] > time.time()
    detail_fetch._skip_until.clear()


def test_needs_llm_skips_current_schema():
    item = _item("done", detailed=True)
    item.extra["llm_ready"] = True
    item.extra["llm_ver"] = LLM_SCHEMA
    assert needs_llm(item) is False
    item.extra["llm_ver"] = 2
    assert needs_llm(item) is True
    item.extra["llm_ver"] = LLM_SCHEMA
    item.extra["llm_partial"] = True
    assert needs_llm(item) is False


def test_workers_scale_with_egress_lanes(monkeypatch):
    from app import detail_fetch, egress

    monkeypatch.delenv("DETAIL_WORKERS", raising=False)
    monkeypatch.delenv("SCRAPE_PROXIES", raising=False)
    egress.reset()
    assert detail_fetch.workers() == detail_fetch.MAX_WORKERS
    monkeypatch.setenv("SCRAPE_PROXIES", "http://127.0.0.1:18080,http://127.0.0.1:18081")
    egress.reset()
    assert detail_fetch.workers() == 6
    monkeypatch.setenv("DETAIL_WORKERS", "2")
    assert detail_fetch.workers() == 2
    monkeypatch.delenv("DETAIL_WORKERS", raising=False)
    monkeypatch.delenv("SCRAPE_PROXIES", raising=False)
    egress.reset()


def test_detail_queue_prioritizes_missing_address(monkeypatch):
    from app import detail_fetch

    monkeypatch.setattr("app.detail_fetch.enabled", lambda: True)
    monkeypatch.setattr("app.detail_fetch._ensure_workers_locked", lambda: None)
    detail_fetch._urgent.clear()
    detail_fetch._queue.clear()
    detail_fetch._seen.clear()
    located = _item("has-street")
    located.address = "Vicente López 1900"
    located.lat = -34.59
    located.lon = -58.39
    lost = _item("no-street")
    lost.address = "Recoleta, Capital Federal"
    lost.lat = -34.59
    lost.lon = -58.39
    enqueue([located, lost])
    assert lost.id in detail_fetch._urgent
    assert located.id in detail_fetch._queue


def test_queue_runs_other_portals_while_local_waits(monkeypatch):
    from app import crawl, detail_fetch, egress

    monkeypatch.setenv("SCRAPE_USE_LOCAL", "1")
    monkeypatch.setenv("SCRAPE_LOCAL_GAP_SEC", "0")
    monkeypatch.delenv("SCRAPE_PROXIES", raising=False)
    monkeypatch.delenv("TOR_ENABLED", raising=False)
    egress.reset()
    crawl.reset()
    crawl.note_http(403, "www.zonaprop.com.ar", lane="direct")
    detail_fetch._urgent.clear()
    detail_fetch._queue.clear()
    detail_fetch._queue.extend(["zonaprop:a", "properati:b", "zonaprop:c"])
    with detail_fetch._lock:
        first = detail_fetch._pop_work()
    assert first == "properati:b"
    egress.reset()
    crawl.reset()
    detail_fetch._urgent.clear()
    detail_fetch._queue.clear()
    detail_fetch._seen.clear()


def test_queue_runs_argenprop_while_zonaprop_cools(monkeypatch):
    from app import crawl, detail_fetch, egress

    monkeypatch.setenv("SCRAPE_USE_LOCAL", "1")
    monkeypatch.setenv("SCRAPE_LOCAL_GAP_SEC", "0")
    monkeypatch.delenv("SCRAPE_PROXIES", raising=False)
    monkeypatch.delenv("TOR_ENABLED", raising=False)
    egress.reset()
    crawl.reset()
    crawl.note_http(403, "www.zonaprop.com.ar", lane="direct")
    detail_fetch._urgent.clear()
    detail_fetch._queue.clear()
    detail_fetch._queue.extend(["zonaprop:a", "argenprop:b", "zonaprop:c"])
    with detail_fetch._lock:
        first = detail_fetch._pop_work()
        second = detail_fetch._pop_work()
    assert first == "argenprop:b"
    assert second is None
    egress.reset()
    crawl.reset()
    detail_fetch._urgent.clear()
    detail_fetch._queue.clear()
    detail_fetch._seen.clear()
