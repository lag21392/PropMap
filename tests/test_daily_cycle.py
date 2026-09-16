from app import pipeline, schedule


def test_max_jobs_is_one():
    assert pipeline.MAX_JOBS == 1
    assert pipeline.MAX_BACKGROUND == 1
    assert pipeline.PORTAL_WORKERS >= 4


def test_next_daily_delegates_to_scheduler(monkeypatch):
    monkeypatch.setattr(schedule, "next_jobs", lambda running, slots=1: [("trelew", False)])
    assert pipeline.next_daily_city() == "trelew"


def test_next_daily_none_when_queue_empty(monkeypatch):
    monkeypatch.setattr(schedule, "next_jobs", lambda running, slots=1: [])
    assert pipeline.next_daily_city() is None


def test_scheduler_fills_one_slot(monkeypatch):
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
    assert seen == [("a", True, False)]


def test_maybe_daily_preempts_slow_job_for_home(monkeypatch):
    called = []
    monkeypatch.setattr(pipeline, "_running_ids", lambda: {"caba"})
    monkeypatch.setattr(pipeline, "_jobs", {"caba": {"running": True, "mode": "slow"}})
    monkeypatch.setattr(schedule, "home_scrape_id", lambda: "puerto-madryn")
    monkeypatch.setattr(schedule, "next_jobs", lambda running, slots=1: [("puerto-madryn", False)])
    monkeypatch.setattr(pipeline, "_preempt_one_slow_locked", lambda: called.append("preempt"))
    monkeypatch.setattr(pipeline, "set_listing_counts", lambda _c: None)
    monkeypatch.setattr(pipeline, "_tick_background_upkeep", lambda _r: None)
    monkeypatch.setattr(pipeline, "_kick_status_aux", lambda _r: None)
    monkeypatch.setattr(pipeline, "refresh", lambda *a, **k: called.append("refresh"))
    pipeline.maybe_daily_refresh()
    assert called == ["preempt"]
