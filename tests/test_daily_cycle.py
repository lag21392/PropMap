from app import pipeline, schedule


def test_next_daily_delegates_to_scheduler(monkeypatch):
    monkeypatch.setattr(schedule, "next_jobs", lambda running, slots=1: [("trelew", False)])
    assert pipeline.next_daily_city() == "trelew"


def test_next_daily_none_when_queue_empty(monkeypatch):
    monkeypatch.setattr(schedule, "next_jobs", lambda running, slots=1: [])
    assert pipeline.next_daily_city() is None


def test_scheduler_fills_multiple_slots(monkeypatch):
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
    assert ("a", True, False) in seen
    assert ("b", False, False) in seen
