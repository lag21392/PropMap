import gc
import weakref
from contextlib import nullcontext

from app import pipeline, schedule


def test_scraped_rows_are_released_before_catalog_fetch(monkeypatch):
    from app.models import Listing
    from app import llm_enrich

    row_ref = None

    def scraper(_report, **_kwargs):
        nonlocal row_ref
        row = Listing(
            source="fixture",
            source_id="memory-release",
            url="https://example.com/memory-release",
            title="Casa en venta",
            property_type="casa",
            price=100,
            currency="USD",
            price_usd=100,
            city="caba",
        )
        row_ref = weakref.ref(row)
        return [row]

    def fetch_catalog(_cities, *, source=None):
        gc.collect()
        assert row_ref is not None
        assert row_ref() is None
        assert source == "fixture"
        return []

    monkeypatch.setattr(pipeline, "usd_rate", lambda: 1.0)
    monkeypatch.setattr(pipeline, "is_country_place", lambda _city: False)
    monkeypatch.setattr(pipeline.crawl, "is_slow", lambda: False)
    monkeypatch.setattr(pipeline.crawl, "job_context", lambda **_kwargs: nullcontext())
    monkeypatch.setattr(pipeline.freshness, "reset_known", lambda _ids: None)
    monkeypatch.setattr(pipeline.freshness, "is_known", lambda _listing_id: True)
    monkeypatch.setattr(pipeline.freshness, "has_usable_listing_text", lambda _row: True)
    monkeypatch.setattr(pipeline.store, "listing_ids", lambda: [])
    monkeypatch.setattr(pipeline.store, "upsert_many", lambda _items: None)
    monkeypatch.setattr(pipeline.store, "fetch_by_cities", fetch_catalog)
    monkeypatch.setattr(pipeline, "pin_listing_city", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "to_usd", lambda price, _currency, _rate: price)
    monkeypatch.setattr(pipeline, "apply_unit_price", lambda *_args: None)
    monkeypatch.setattr(pipeline, "apply_signals", lambda *_args: None)
    monkeypatch.setattr(pipeline, "_retire_unseen", lambda *_args: 0)
    monkeypatch.setattr(pipeline, "_enrich_details", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(llm_enrich, "enqueue", lambda *_args, **_kwargs: None)

    pipeline._scrape_one_city(
        "caba",
        {"label": "CABA"},
        [("fixture", scraper)],
        lambda _message: None,
    )


def test_job_limits_leave_a_slot_for_an_interactive_search():
    """Los trabajos de fondo nunca ocupan todos los slots: siempre queda uno libre."""
    assert pipeline.MAX_BACKGROUND < pipeline.MAX_JOBS
    assert pipeline.PORTAL_WORKERS >= 4


def test_next_daily_delegates_to_scheduler(monkeypatch):
    monkeypatch.setattr(schedule, "next_jobs", lambda running, slots=1: [("trelew", False)])
    assert pipeline.next_daily_city() == "trelew"


def test_next_daily_none_when_queue_empty(monkeypatch):
    monkeypatch.setattr(schedule, "next_jobs", lambda running, slots=1: [])
    assert pipeline.next_daily_city() is None


def test_scheduler_fills_every_free_background_slot(monkeypatch):
    seen = []

    def fake_refresh(city, fast=False, interactive=False):
        seen.append((city, fast, interactive))
        return {}

    monkeypatch.setattr(pipeline, "_running_ids", lambda: set())
    monkeypatch.setattr(schedule, "next_jobs", lambda running, slots: [("a", True), ("b", False)][:slots])
    monkeypatch.setattr(pipeline, "refresh", fake_refresh)
    monkeypatch.setattr(pipeline, "set_listing_counts", lambda _c: None)
    monkeypatch.setattr("app.listings_cache.city_counts", lambda: {})
    pipeline.maybe_daily_refresh()
    assert seen == [("a", True, False), ("b", False, False)][: pipeline.MAX_BACKGROUND]


def test_maybe_daily_preempts_slow_job_for_home(monkeypatch):
    """Sin lugar libre, la ciudad prioritaria le saca el turno a una lenta."""
    called = []
    busy = {f"ocupada-{i}" for i in range(pipeline.MAX_JOBS)}
    monkeypatch.setattr(pipeline, "_running_ids", lambda: set(busy))
    monkeypatch.setattr(pipeline, "_jobs", {cid: {"running": True, "mode": "slow"} for cid in busy})
    monkeypatch.setattr(schedule, "home_scrape_id", lambda: "puerto-madryn")
    monkeypatch.setattr(schedule, "next_jobs", lambda running, slots=1: [("puerto-madryn", False)])
    monkeypatch.setattr(pipeline, "_preempt_one_slow_locked", lambda: called.append("preempt"))
    monkeypatch.setattr(pipeline, "set_listing_counts", lambda _c: None)
    monkeypatch.setattr(pipeline, "_tick_background_upkeep", lambda _r: None)
    monkeypatch.setattr(pipeline, "_kick_status_aux", lambda _r: None)
    monkeypatch.setattr(pipeline, "refresh", lambda *a, **k: called.append("refresh"))
    pipeline.maybe_daily_refresh()
    assert called == ["preempt"]
