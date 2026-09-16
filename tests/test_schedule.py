from app.schedule import (
    COUNTRY_ID,
    STARVE_SEC,
    is_country_place,
    next_jobs,
    note_finished,
    note_search,
    reset,
    score,
)
from app.scrapers.urls import argenprop_urls, mercadolibre_urls, properati_urls, zonaprop_paths


def test_country_urls_are_national():
    assert is_country_place("todo el pais")
    zp = zonaprop_paths(COUNTRY_ID)
    assert any(path == "departamentos-venta" for path, _ in zp)
    assert not any("capital-federal" in path for path, _ in zp)
    assert all("/venta/" in url or url.endswith("/venta") for url, _ in mercadolibre_urls(COUNTRY_ID))
    assert all("/venta" in url for url, _ in properati_urls(COUNTRY_ID))
    assert all(url.endswith("/venta") for url, _ in argenprop_urls(COUNTRY_ID))


def test_lugar_search_outranks_idle_country(monkeypatch):
    reset()
    monkeypatch.setattr("app.schedule.pool_ids", lambda: ["microcentro-caba", COUNTRY_ID])
    note_search("microcentro-caba")
    note_finished(COUNTRY_ID, fast=False)
    jobs = next_jobs(set(), slots=1)
    assert jobs
    assert jobs[0][0] == "caba"
    assert jobs[0][1] is True


def test_starved_place_gets_a_reserved_slot(monkeypatch):
    reset()
    monkeypatch.setattr("app.schedule.pool_ids", lambda: ["microcentro-caba", "trelew"])
    note_search("microcentro-caba")
    note_finished("trelew", fast=False)
    from app import schedule

    with schedule._lock:
        row = schedule._row("trelew")
        row["due_since"] = "2000-01-01T00:00:00+00:00"
        row["last_complete"] = "2000-01-01T00:00:00+00:00"
        row["last_slow"] = "2000-01-01T00:00:00+00:00"
    jobs = next_jobs(set(), slots=2)
    ids = [cid for cid, _fast in jobs]
    assert "trelew" in ids
    assert "caba" in ids
    assert score("microcentro-caba") > 0
    assert STARVE_SEC > 0


def test_empty_searched_place_jumps_the_queue(monkeypatch):
    reset()
    monkeypatch.setattr("app.schedule.pool_ids", lambda: ["rio-gallegos", "microcentro-caba"])
    from app import schedule

    note_search("rio-gallegos")
    note_finished("microcentro-caba", fast=False)
    with schedule._lock:
        schedule._row("rio-gallegos")["listings"] = 0
        schedule._row("microcentro-caba")["listings"] = 2000
    jobs = next_jobs(set(), slots=1)
    assert jobs[0][0] == "rio-gallegos"


def test_home_place_jumps_queue_even_if_another_is_starved(monkeypatch):
    reset()
    monkeypatch.setattr("app.schedule.pool_ids", lambda: ["puerto-madryn", "trelew", "caba"])
    monkeypatch.setattr("app.schedule.home_scrape_id", lambda: "puerto-madryn")
    note_search("caba")
    from app import schedule

    with schedule._lock:
        for cid in ("puerto-madryn", "trelew"):
            row = schedule._row(cid)
            row["due_since"] = "2000-01-01T00:00:00+00:00"
            row["last_complete"] = "2000-01-01T00:00:00+00:00"
            row["last_slow"] = "2000-01-01T00:00:00+00:00"
    jobs = next_jobs(set(), slots=1)
    assert jobs[0][0] == "puerto-madryn"


def test_home_place_waits_if_fresh(monkeypatch):
    reset()
    monkeypatch.setattr("app.schedule.pool_ids", lambda: ["puerto-madryn", "caba"])
    monkeypatch.setattr("app.schedule.home_scrape_id", lambda: "puerto-madryn")
    note_finished("puerto-madryn", fast=False)
    note_search("caba")
    jobs = next_jobs(set(), slots=1)
    assert jobs[0][0] == "caba"


def test_keeps_scraping_when_everything_is_fresh(monkeypatch):
    reset()
    monkeypatch.setattr("app.schedule.pool_ids", lambda: ["puerto-madryn", "caba", COUNTRY_ID])
    monkeypatch.setattr("app.schedule.home_scrape_id", lambda: "puerto-madryn")
    note_finished("puerto-madryn", fast=False)
    note_finished("caba", fast=False)
    note_finished(COUNTRY_ID, fast=False)
    from datetime import datetime, timedelta, timezone

    from app import schedule

    older = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    with schedule._lock:
        schedule._row("caba")["last_complete"] = older
        schedule._row("caba")["last_slow"] = older
    jobs = next_jobs(set(), slots=1)
    assert jobs
    assert jobs[0][0] == "caba"
    nxt = next_jobs({"caba"}, slots=1)
    assert nxt[0][0] == "puerto-madryn"
    assert nxt[0][0] != COUNTRY_ID


def test_never_run_country_is_due(monkeypatch):
    reset()
    monkeypatch.setattr("app.schedule.pool_ids", lambda: [COUNTRY_ID, "puerto-madryn"])
    note_finished("puerto-madryn", fast=False)
    jobs = next_jobs(set(), slots=3)
    ids = [cid for cid, _fast in jobs]
    assert COUNTRY_ID in ids


def test_note_view_debounces_sqlite_writes(monkeypatch):
    from app import schedule
    from app.schedule import note_view

    reset()
    schedule._persist = True
    saves = {"n": 0}

    def fake_save():
        saves["n"] += 1

    monkeypatch.setattr("app.schedule._save_locked", fake_save)
    assert note_view("caba")
    note_view("caba")
    note_view("caba")
    assert saves["n"] == 1
