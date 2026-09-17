from app.llm_enrich import LLM_SCHEMA
from app.models import Listing


def _item(source_id: str, **kw) -> Listing:
    extra = dict(kw.pop("extra", None) or {})
    return Listing(
        source="zonaprop",
        source_id=source_id,
        url=kw.pop("url", f"https://example.com/{source_id}"),
        title="Depto",
        property_type="departamento",
        city=kw.pop("city", "caba"),
        extra=extra,
        details_scraped=kw.pop("details_scraped", False),
        **kw,
    )


def test_llm_backlog_skips_ready_and_prefers_await(tmp_path, monkeypatch):
    from app import store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "listings.sqlite")
    store.init()
    ready = _item(
        "ready",
        details_scraped=True,
        extra={"llm_ready": True, "llm_ver": LLM_SCHEMA, "llm_partial": False},
    )
    wait = _item("wait", city="trelew", extra={"await_llm": True}, details_scraped=True)
    other = _item("other", city="caba", details_scraped=True)
    dup = _item("dup", extra={"duplicate_of": "zonaprop:ready"})
    store.upsert_many([ready, wait, other, dup])
    rows = store.fetch_llm_backlog(8, prefer_city="caba", schema=LLM_SCHEMA)
    ids = [row.id for row in rows]
    assert "zonaprop:ready" not in ids
    assert "zonaprop:dup" not in ids
    assert ids[0] == "zonaprop:other"
    assert "zonaprop:wait" in ids


def test_llm_backlog_prioritizes_new_errors_and_unassigned(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app import store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "listings.sqlite")
    store.init()
    ready = _item(
        "ready",
        details_scraped=True,
        extra={"llm_ready": True, "llm_ver": LLM_SCHEMA, "llm_partial": False},
    )
    broken = _item(
        "broken",
        details_scraped=True,
        extra={
            "llm_ready": True,
            "llm_ver": LLM_SCHEMA,
            "llm_partial": False,
            "data_fixes": ["m² cubiertos irreales para una casa"],
        },
    )
    lost = _item(
        "lost",
        city="fuera",
        details_scraped=True,
        extra={"llm_ready": True, "llm_ver": LLM_SCHEMA, "llm_partial": False},
    )
    newbie = _item("new", details_scraped=True)
    old = _item("old", details_scraped=True)
    store.upsert_many([ready, broken, lost, newbie, old])
    now = datetime.now(timezone.utc)
    with store.connect() as conn:
        conn.execute(
            "UPDATE listings SET scraped_at = ? WHERE id = ?",
            ((now - timedelta(days=10)).isoformat(), "zonaprop:broken"),
        )
        conn.execute(
            "UPDATE listings SET scraped_at = ? WHERE id = ?",
            ((now - timedelta(days=10)).isoformat(), "zonaprop:lost"),
        )
        conn.execute(
            "UPDATE listings SET scraped_at = ? WHERE id = ?",
            ((now - timedelta(days=10)).isoformat(), "zonaprop:old"),
        )
        conn.execute(
            "UPDATE listings SET scraped_at = ? WHERE id = ?",
            (now.isoformat(), "zonaprop:new"),
        )
        conn.commit()
    ids = [row.id for row in store.fetch_llm_backlog(8, prefer_city="caba", schema=LLM_SCHEMA)]
    assert "zonaprop:ready" not in ids
    assert ids[0] == "zonaprop:new"
    assert ids.index("zonaprop:broken") < ids.index("zonaprop:old")
    assert ids.index("zonaprop:new") < ids.index("zonaprop:lost")


def test_llm_backlog_groups_same_city(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from app import store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "listings.sqlite")
    store.init()
    now = datetime.now(timezone.utc).isoformat()
    store.upsert_many(
        [
            _item("r1", city="rosario", details_scraped=True),
            _item("m1", city="mendoza", details_scraped=True),
            _item("r2", city="rosario", details_scraped=True),
            _item("m2", city="mendoza", details_scraped=True),
        ]
    )
    with store.connect() as conn:
        conn.execute("UPDATE listings SET scraped_at = ?", (now,))
        conn.commit()
    cities = [row.city for row in store.fetch_llm_backlog(8, prefer_city="", schema=LLM_SCHEMA)]
    assert cities.index("mendoza") < cities.index("rosario")
    assert cities == sorted(cities)


def test_detail_backlog_skips_downloaded(tmp_path, monkeypatch):
    from app import store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "listings.sqlite")
    store.init()
    done = _item("done", details_scraped=True)
    pending = _item("need", details_scraped=False)
    store.upsert_many([done, pending])
    rows = store.fetch_detail_backlog(8, prefer_city="caba")
    ids = [row.id for row in rows]
    assert ids == ["zonaprop:need"]


def test_detail_backlog_skips_unknown_city_after_llm_pass(tmp_path, monkeypatch):
    from app import store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "listings.sqlite")
    store.init()
    lost = _item("lost", city="fuera", extra={"llm_city_ok": True, "llm_at": "2026-09-01T12:00:00+00:00"})
    fresh = _item("fresh", city="")
    store.upsert_many([lost, fresh])
    ids = [row.id for row in store.fetch_detail_backlog(8)]
    assert "zonaprop:lost" not in ids
    assert "zonaprop:fresh" in ids


def test_init_marks_unknown_city_after_llm_pass(tmp_path, monkeypatch):
    from app import store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "listings.sqlite")
    store.init()
    lost = _item("lost", city="fuera", extra={"llm_city_ok": True})
    fresh = _item("fresh", city="")
    store.upsert_many([lost, fresh])
    with store.connect() as conn:
        store._stamp_skip_details_unknown_city(conn, force=True)
        conn.commit()
    marked = store.get_listing("zonaprop:lost")
    pending = store.get_listing("zonaprop:fresh")
    assert marked and marked.extra.get("skip_details") is True
    assert pending and not pending.extra.get("skip_details")


def test_pump_is_idle_in_tests():
    from app.backfill import pump

    assert pump(prefer_cities=["caba"]) == {"details": 0, "llm": 0}


def test_llm_refill_fills_from_store(tmp_path, monkeypatch):
    from app import llm_enrich, store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "listings.sqlite")
    store.init()
    monkeypatch.setattr(llm_enrich, "enabled", lambda: True)
    monkeypatch.setattr(llm_enrich, "_ensure_workers_locked", lambda: None)
    llm_enrich._urgent.clear()
    llm_enrich._queue.clear()
    llm_enrich._seen.clear()
    store.upsert_many(
        [
            _item("a", details_scraped=True),
            _item("b", details_scraped=True, extra={"await_llm": True}),
        ]
    )
    n = llm_enrich.refill("caba")
    assert n == 2
    queued = set(llm_enrich._urgent) | set(llm_enrich._queue)
    assert queued == {"zonaprop:a", "zonaprop:b"}


def test_llm_refill_skips_cards_without_text(tmp_path, monkeypatch):
    from app import llm_enrich, store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "listings.sqlite")
    store.init()
    monkeypatch.setattr(llm_enrich, "enabled", lambda: True)
    monkeypatch.setattr(llm_enrich, "_ensure_workers_locked", lambda: None)
    llm_enrich._urgent.clear()
    llm_enrich._queue.clear()
    llm_enrich._seen.clear()
    store.upsert_many(
        [
            _item("raw", details_scraped=False),
            _item("ready-text", details_scraped=True),
        ]
    )
    n = llm_enrich.refill("caba")
    queued = set(llm_enrich._urgent) | set(llm_enrich._queue)
    assert n == 1
    assert queued == {"zonaprop:ready-text"}


def test_llm_backlog_does_not_starve_detailed_behind_short_new_ads(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app import llm_enrich, store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "listings.sqlite")
    store.init()
    monkeypatch.setattr(llm_enrich, "enabled", lambda: True)
    monkeypatch.setattr(llm_enrich, "_ensure_workers_locked", lambda: None)
    llm_enrich._urgent.clear()
    llm_enrich._queue.clear()
    llm_enrich._seen.clear()
    now = datetime.now(timezone.utc)
    raw = [_item(f"raw-{i}", details_scraped=False, description="corto") for i in range(20)]
    ready = _item("ready-text", details_scraped=True, description="x" * 200)
    store.upsert_many(raw + [ready])
    with store.connect() as conn:
        conn.execute(
            "UPDATE listings SET scraped_at = ? WHERE id = ?",
            ((now - timedelta(days=10)).isoformat(), "zonaprop:ready-text"),
        )
        conn.execute(
            "UPDATE listings SET scraped_at = ? WHERE id LIKE 'zonaprop:raw-%'",
            (now.isoformat(),),
        )
        conn.commit()
    ids = [row.id for row in store.fetch_llm_backlog(8, prefer_city="caba", schema=LLM_SCHEMA)]
    assert "zonaprop:ready-text" in ids
    n = llm_enrich.refill("caba")
    queued = set(llm_enrich._urgent) | set(llm_enrich._queue)
    assert "zonaprop:ready-text" in queued
    assert n >= 1


def test_llm_backlog_prefers_live_city_over_fuera(tmp_path, monkeypatch):
    from app import store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "listings.sqlite")
    store.init()
    here = _item(
        "here",
        details_scraped=True,
        extra={"llm_place": {"name": "Córdoba", "province": "Córdoba"}},
    )
    lost = _item("lost", city="fuera", details_scraped=True)
    store.upsert_many([here, lost])
    ids = [row.id for row in store.fetch_llm_backlog(8, prefer_city="caba", schema=LLM_SCHEMA)]
    assert ids[0] == "zonaprop:here"
    assert ids.index("zonaprop:here") < ids.index("zonaprop:lost")


def test_llm_backlog_uses_needs_llm_index(tmp_path, monkeypatch):
    from app import store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "listings.sqlite")
    store.init()
    store.upsert_many([_item("a", details_scraped=True), _item("b", details_scraped=True)])
    with store.connect() as conn:
        plan = " ".join(
            str(row[-1])
            for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT id FROM listings WHERE needs_llm = 1 AND is_hidden = 0 LIMIT 8"
            )
        ).lower()
    assert "idx_listings_needs_llm" in plan
