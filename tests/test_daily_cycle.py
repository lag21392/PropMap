from app import pipeline, schedule


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
