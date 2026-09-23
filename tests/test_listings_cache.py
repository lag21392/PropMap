import threading
import time

from app.listings_cache import ingest, payload, reset
from app.models import Listing


def _item(source_id: str, city: str = "microcentro-caba") -> Listing:
    return Listing(
        source="zonaprop",
        source_id=source_id,
        url="https://example.com/" + source_id,
        title="Depto en Palermo",
        property_type="departamento",
        price=90000,
        currency="USD",
        price_usd=90000,
        city=city,
        address="Palermo, CABA",
        barrio="Palermo",
        lat=-34.588,
        lon=-58.430,
    )


def test_cache_hides_foreign_ads_pinned_on_madryn():
    reset()
    foreign = Listing(
        source="properati",
        source_id="docta-mdq",
        url="https://example.com/docta",
        title="Lote en Docta, Córdoba en Puerto Madryn",
        property_type="terreno",
        price=40000,
        currency="USD",
        price_usd=40000,
        city="puerto-madryn",
        address="Docta",
        barrio="Docta",
        lat=-42.769,
        lon=-65.038,
    )
    local = Listing(
        source="zonaprop",
        source_id="local-mdq",
        url="https://example.com/local",
        title="Departamento en Puerto Madryn",
        property_type="departamento",
        price=90000,
        currency="USD",
        price_usd=90000,
        city="puerto-madryn",
        address="28 de Julio 200",
        barrio="Centro",
        lat=-42.769,
        lon=-65.038,
    )
    ingest([foreign, local])
    from app import listings_cache

    listings_cache._ready = True
    rows = payload("puerto-madryn")["listings"]
    assert len(rows) == 1
    assert rows[0]["id"] == local.id
    reset()


def test_cache_shows_placed_listings_before_llm(monkeypatch):
    from app import listings_cache
    from app.llm_enrich import mark_await_llm

    monkeypatch.setattr("app.llm_enrich.enabled", lambda: True)
    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    reset()
    listings_cache._ready = True
    placed = _item("await-llm", "caba")
    placed.address = "Vicente López 1900"
    mark_await_llm(placed)
    ingest([placed])
    assert len(payload("caba")["listings"]) == 1
    lost = Listing(
        source="zonaprop",
        source_id="no-place",
        url="https://example.com/no-place",
        title="Casa en venta",
        property_type="casa",
        city="caba",
        extra={"await_llm": True},
    )
    ingest([lost])
    ids = {row["id"] for row in payload("caba")["listings"]}
    assert placed.id in ids
    assert lost.id not in ids
    reset()


def test_cache_returns_listings_and_skips_when_unchanged():
    reset()
    ingest([_item("a"), _item("b", "ciudad-autonoma-de-buenos-aires")])
    from app import listings_cache

    listings_cache._ready = True
    first = payload("microcentro-caba")
    assert first["unchanged"] is False
    assert len(first["listings"]) == 2
    assert all(row["city"] == "caba" for row in first["listings"])
    second = payload("microcentro-caba", since=first["rev"])
    assert second["unchanged"] is True
    ingest([_item("c")])
    third = payload("microcentro-caba", since=first["rev"])
    assert third["unchanged"] is False
    assert len(third["listings"]) == 3
    reset()


def test_city_payload_is_reused_for_another_viewer(monkeypatch):
    from app import listings_cache

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    reset()
    ingest([_item("caba-1", "caba")])
    listings_cache._ready = True
    first = payload("caba")
    assert first["warming"] is False
    assert len(first["listings"]) == 1
    listings_cache._by_id.clear()
    listings_cache._ready = False
    second = payload("caba")
    assert second["warming"] is False
    assert len(second["listings"]) == 1
    assert second["listings"][0]["id"] == "zonaprop:caba-1"
    reset()


def test_city_disk_cache_survives_memory_reset(tmp_path, monkeypatch):
    from app import listings_cache

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    reset()
    listings_cache._force_cache_dir = tmp_path
    ingest([_item("disk-1", "caba")])
    listings_cache._ready = True
    first = payload("caba")
    assert (tmp_path / "caba.json").exists()
    assert len(first["listings"]) == 1
    listings_cache._by_id.clear()
    listings_cache._city_snaps.clear()
    listings_cache._ready = False
    listings_cache._disk_preloaded = True
    second = payload("caba")
    assert second["warming"] is False
    assert len(second["listings"]) == 1
    assert second["listings"][0]["id"] == "zonaprop:disk-1"
    reset()


def test_priority_cities_start_with_default_city(monkeypatch):
    from app import listings_cache

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    monkeypatch.setattr(
        "app.places.priority_place_ids",
        lambda: ["puerto-madryn", "caba"],
    )
    reset()
    ids = listings_cache._priority_city_ids()
    assert ids[0] == "caba"
    assert "puerto-madryn" in ids[:4]
    reset()


def test_city_snap_serves_listings_before_full_ready(monkeypatch):
    from app import listings_cache

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    reset()
    ingest([
        Listing(
            source="zonaprop",
            source_id="mdq-1",
            url="https://example.com/mdq-1",
            title="Departamento en Puerto Madryn",
            property_type="departamento",
            price=90000,
            currency="USD",
            price_usd=90000,
            city="puerto-madryn",
            address="28 de Julio 200",
            barrio="Centro",
            lat=-42.769,
            lon=-65.038,
        )
    ])
    listings_cache._ready = False
    listings_cache._warming = True
    data = payload("puerto-madryn")
    assert data["warming"] is False
    assert len(data["listings"]) == 1
    assert data["listings"][0]["id"] == "zonaprop:mdq-1"
    reset()


def test_disk_response_bytes_skips_json_parse(tmp_path, monkeypatch):
    from app import listings_cache

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    reset()
    listings_cache._force_cache_dir = tmp_path
    ingest([_item("disk-2", "caba")])
    listings_cache._ready = True
    payload("caba")
    listings_cache._ready = False
    listings_cache._city_snaps.clear()
    raw = listings_cache.disk_response_bytes("caba")
    assert raw
    data = __import__("json").loads(raw)
    assert len(data["listings"]) == 1
    assert data["listings"][0]["id"] == "zonaprop:disk-2"
    reset()


def test_listings_body_does_not_reserialize_when_json_exists(tmp_path, monkeypatch):
    from app import listings_cache

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    reset()
    listings_cache._force_cache_dir = tmp_path
    ingest([_item("gz-1", "caba")])
    listings_cache._ready = True
    payload("caba")
    listings_cache._city_snaps.clear()

    def boom(*_a, **_k):
        raise AssertionError("no hay que serializar en el GET")

    monkeypatch.setattr("app.listings_cache.json.dumps", boom)
    monkeypatch.setattr("app.listings_cache.json_dumps_bytes", boom)
    monkeypatch.setattr("app.listings_cache.gzip_encode", boom)
    monkeypatch.setattr("app.listings_cache.gzip_mod.compress", boom)
    raw, encoding = listings_cache.listings_body("caba")
    assert raw
    assert encoding is None
    assert b"zonaprop:gz-1" in raw
    packed, gzip_encoding = listings_cache.listings_body("caba", gzip=True)
    assert gzip_encoding == "gzip"
    assert packed[:2] == b"\x1f\x8b"
    import gzip as gzip_stdlib

    assert b"zonaprop:gz-1" in gzip_stdlib.decompress(packed)
    reset()


def test_payload_without_cache_does_not_block_on_db(monkeypatch):
    from app import listings_cache

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)

    def boom(*_a, **_k):
        raise AssertionError("no hay que ir a sqlite en el pedido")

    monkeypatch.setattr("app.store.fetch_all", boom)
    monkeypatch.setattr("app.store.fetch_by_cities", boom)
    reset()
    data = payload("caba")
    assert data["warming"] is True
    assert data["listings"] == []
    reset()


def test_listings_body_ignores_tiny_ram_snap_when_sqlite_is_full(monkeypatch):
    import json
    from app import listings_cache

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    reset()
    item = _item("solo-uno", "caba")
    listings_cache.ingest([item])
    listings_cache._cities[:] = [{"id": "caba", "label": "CABA", "n": 6105}]
    raw, _enc = listings_cache.listings_body("caba")
    assert raw is None
    reset()


def test_warming_pins_are_served_before_full_cache(monkeypatch):
    import json
    from app import listings_cache

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    reset()
    item = _item("pin-1", "caba")
    pin = listings_cache._pin_row(item)
    listings_cache._commit_snap("caba", [pin], warming=True, persist=False)
    raw, _ = listings_cache.listings_body("caba")
    assert raw
    data = json.loads(raw)
    assert data["warming"] is True
    assert len(data["listings"]) == 1
    assert data["listings"][0]["id"] == item.id
    assert data["listings"][0]["lat"] is not None
    served = payload("caba")
    assert served["warming"] is True
    assert len(served["listings"]) == 1
    stale = json.loads(listings_cache.unchanged_listings("caba", served["rev"]))
    assert stale["unchanged"] is True
    assert stale["warming"] is True
    pins, _enc = listings_cache.listings_pins_body("caba")
    assert pins
    pin_data = json.loads(pins)
    assert pin_data.get("layer") == "pins"
    assert len(pin_data["listings"]) == 1
    reset()


def test_http_list_row_drops_nearby_and_extra_photos():
    from app import listings_cache

    row = {
        "id": "x",
        "nearby": [{"km": 0.1}] * 8,
        "access": {"precise": True, "nearby": [{"km": 0.1}]},
        "profile": {"axes": {"servicios": {"score": 70, "nearby": [1], "details": [2]}}},
        "photos": [f"p{i}" for i in range(8)],
    }
    out = listings_cache._http_list_row(row)
    assert "nearby" not in out
    assert "nearby" not in out["access"]
    assert out["profile"]["axes"]["servicios"]["score"] == 70
    assert "nearby" not in out["profile"]["axes"]["servicios"]
    assert "details" not in out["profile"]["axes"]["servicios"]
    assert out["photos"] == ["p0", "p1", "p2", "p3"]


def test_pin_row_is_lighter_than_public_dict():
    from app import listings_cache

    item = _item("slim-1", "caba")
    item.extra = {"profile": {"axes": {"zona": {"score": 80}}}, "access": {"precise": True, "nearby": [{"km": 0.1}] * 8}}
    pin = listings_cache._pin_row(item)
    public = item.to_public_dict()
    assert "lat" in pin and pin["id"] == item.id
    assert pin["profile"]["axes"]["zona"]["score"] == 80
    assert "nearby" not in (pin["profile"]["axes"].get("servicios") or {})
    assert "access" not in pin
    assert "profile" in public


def test_load_city_final_snap_keeps_description_and_rent(monkeypatch):
    import json
    from app import listings_cache

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    reset()
    item = _item("ficha-1", "caba")
    item.description = "Living comedor al frente con balcón y cocina independiente."
    item.extra = {
        "monthly_rent_usd": 450,
        "monthly_yield_pct": 5.2,
        "nightly_usd": 38,
        "temporal_yield_pct": 8.1,
    }
    monkeypatch.setattr("app.store.init", lambda: None)
    monkeypatch.setattr("app.store.fetch_by_cities", lambda _ids: [item])
    monkeypatch.setattr(listings_cache, "_sqlite_city_n", lambda _cid: 1)
    monkeypatch.setattr(listings_cache, "_city_fetch_ids", lambda cid: [cid])
    monkeypatch.setattr("app.listings_cache.listing_fits_city", lambda *a, **k: True)
    monkeypatch.setattr("app.llm_enrich.should_publish", lambda _item: True)
    listings_cache._load_city_body("caba")
    raw, _enc = listings_cache.listings_body("caba")
    assert raw
    data = json.loads(raw)
    assert data.get("warming") is not True
    row = data["listings"][0]
    assert "Living" in (row.get("description") or "")
    assert row.get("monthly_yield_pct") == 5.2
    assert row.get("monthly_rent_usd") == 450
    pins, _penc = listings_cache.listings_pins_body("caba")
    assert pins
    pin_row = json.loads(pins)["listings"][0]
    assert "description" not in pin_row
    assert "monthly_yield_pct" not in pin_row
    reset()


def test_unchanged_listings_skips_full_body(monkeypatch):
    import json
    from app import listings_cache

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    reset()
    ingest([_item("u1", "caba")])
    listings_cache._ready = True
    first = payload("caba")
    raw = listings_cache.unchanged_listings("caba", first["rev"])
    assert raw
    data = json.loads(raw)
    assert data["unchanged"] is True
    assert data["listings"] == []
    assert listings_cache.unchanged_listings("caba", int(first["rev"]) - 1) is None
    reset()


def test_llm_ingest_patches_city_snap_without_dropping_others(monkeypatch):
    from app import listings_cache

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    reset()
    first = Listing(
        source="zonaprop",
        source_id="keep-mdq",
        url="https://example.com/keep",
        title="Departamento en Puerto Madryn",
        property_type="departamento",
        price=90000,
        currency="USD",
        price_usd=90000,
        city="puerto-madryn",
        address="28 de Julio 200",
        barrio="Centro",
        lat=-42.769,
        lon=-65.038,
    )
    second = Listing(
        source="zonaprop",
        source_id="llm-mdq",
        url="https://example.com/llm",
        title="Casa en Puerto Madryn",
        property_type="casa",
        price=80000,
        currency="USD",
        price_usd=80000,
        city="puerto-madryn",
        address="Roca 100",
        barrio="Centro",
        lat=-42.77,
        lon=-65.04,
    )
    ingest([first, second])
    listings_cache._ready = False
    second.bedrooms = 3
    ingest([second])
    rows = payload("puerto-madryn")["listings"]
    ids = {row["id"] for row in rows}
    assert ids == {first.id, second.id}
    patched = next(row for row in rows if row["id"] == second.id)
    assert patched["bedrooms"] == 3
    reset()


def test_loose_listing_with_search_city_shows_in_that_city(monkeypatch):
    from app import listings_cache

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    reset()
    kept = Listing(
        source="zonaprop",
        source_id="mdq-loose",
        url="https://example.com/mdq-loose",
        title="Departamento en Puerto Madryn",
        property_type="departamento",
        price=88000,
        currency="USD",
        price_usd=88000,
        city="fuera",
        address="28 de Julio 200",
        barrio="Centro",
        lat=-42.769,
        lon=-65.038,
        extra={"search_city": "puerto-madryn"},
    )
    other = Listing(
        source="zonaprop",
        source_id="cba-loose",
        url="https://example.com/cba-loose",
        title="Casa en Córdoba",
        property_type="casa",
        price=120000,
        currency="USD",
        price_usd=120000,
        city="fuera",
        address="Chacabuco 100",
        barrio="Centro",
        lat=-31.42,
        lon=-64.18,
        extra={"search_city": "cordoba"},
    )
    ingest([kept, other])
    listings_cache._ready = True
    rows = payload("puerto-madryn")["listings"]
    ids = {row["id"] for row in rows}
    assert kept.id in ids
    assert other.id not in ids
    reset()


def test_search_city_does_not_pull_ads_from_another_city(monkeypatch):
    from app import listings_cache

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    reset()
    local = Listing(
        source="zonaprop",
        source_id="mdq-ok",
        url="https://example.com/mdq-ok",
        title="Departamento en Puerto Madryn",
        property_type="departamento",
        price=88000,
        currency="USD",
        price_usd=88000,
        city="puerto-madryn",
        address="28 de Julio 200",
        barrio="Centro",
        lat=-42.769,
        lon=-65.038,
        extra={"search_city": "puerto-madryn", "location_kind": "exact"},
    )
    stray = Listing(
        source="zonaprop",
        source_id="caba-in-mdq-search",
        url="https://example.com/caba-stray",
        title="Depto en Palermo",
        property_type="departamento",
        price=150000,
        currency="USD",
        price_usd=150000,
        city="fuera",
        address="Palermo, CABA",
        barrio="Palermo",
        lat=-34.588,
        lon=-58.430,
        extra={"search_city": "puerto-madryn", "location_kind": "exact"},
    )
    ingest([local, stray])
    listings_cache._ready = True
    ids = {row["id"] for row in payload("puerto-madryn")["listings"]}
    assert local.id in ids
    assert stray.id not in ids
    reset()


def test_updating_a_snap_filters_without_the_cache_lock(monkeypatch):
    from app import listings_cache
    from app.geo import public_row_fits_city as real_fits

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    reset()
    first = _item("snap-1", "caba")
    ingest([first])
    public = listings_cache._by_id[first.id]
    listings_cache._city_snaps["caba"] = {
        "listings": [dict(public)],
        "rev": 1,
        "loaded": True,
    }

    def unlocked(row, city):
        got = listings_cache._lock.acquire(blocking=False)
        assert got, "filtrar un snap ya armado no debe tomar el candado del cache"
        listings_cache._lock.release()
        return real_fits(row, city)

    monkeypatch.setattr("app.listings_cache.public_row_fits_city", unlocked)
    ingest([_item("snap-1", "caba")])
    rows = listings_cache._city_snaps["caba"]["listings"]
    assert any(row.get("id") == first.id for row in rows)
    reset()


def test_snap_filter_runs_without_cache_lock(monkeypatch):
    from app import listings_cache
    from app.geo import public_row_fits_city as real_fits

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    reset()

    def unlocked(row, city):
        got = listings_cache._lock.acquire(blocking=False)
        assert got, "filtrar avisos no debe tomar el candado del cache"
        listings_cache._lock.release()
        return real_fits(row, city)

    monkeypatch.setattr("app.listings_cache.public_row_fits_city", unlocked)
    ingest([_item("lock-free", "caba")])
    listings_cache._ready = True
    rows = payload("caba")["listings"]
    assert len(rows) == 1
    reset()


def test_ingest_keeps_disk_snap_when_ram_is_partial(tmp_path, monkeypatch):
    from app import listings_cache

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    reset()
    listings_cache._force_cache_dir = tmp_path
    ingest([_item("keep-a", "caba"), _item("keep-b", "caba")])
    listings_cache._ready = True
    payload("caba")
    on_disk = listings_cache._read_disk("caba")
    assert on_disk
    assert len(on_disk["listings"]) == 2
    listings_cache._city_snaps.clear()
    listings_cache._by_id.clear()
    ingest([_item("keep-c", "caba")])
    listings_cache._ready = True
    ids = {row["id"] for row in payload("caba")["listings"]}
    assert ids == {
        "zonaprop:keep-a",
        "zonaprop:keep-b",
        "zonaprop:keep-c",
    }
    reset()


def test_forget_ids_removes_listing_from_city_snap():
    from app import listings_cache

    reset()
    ingest([_item("stay", "caba"), _item("sold", "caba")])
    listings_cache._ready = True
    payload("caba")
    listings_cache.forget_ids(["zonaprop:sold"])
    ids = {row["id"] for row in payload("caba")["listings"]}
    assert "zonaprop:stay" in ids
    assert "zonaprop:sold" not in ids
    reset()


def test_retire_unseen_drops_listings_missing_from_a_full_scrape(monkeypatch):
    from app.models import Listing
    from app.pipeline import _retire_unseen

    kept = Listing(
        source="zonaprop",
        source_id="keep",
        url="https://example.com/keep",
        title="Sigue",
        property_type="departamento",
        city="caba",
    )
    gone = Listing(
        source="zonaprop",
        source_id="gone",
        url="https://example.com/gone",
        title="Vendida",
        property_type="departamento",
        city="caba",
    )
    dropped: list[str] = []
    monkeypatch.setattr("app.pipeline.store.fetch_by_cities", lambda _cities: [kept, gone] + [kept] * 40)
    monkeypatch.setattr("app.pipeline.store.drop_listings", lambda ids: dropped.extend(ids) or len(ids))
    seen = {kept.id, *[f"zonaprop:extra-{i}" for i in range(40)]}
    n = _retire_unseen("caba", "zonaprop", seen)
    assert n >= 1
    assert gone.id in dropped
    assert kept.id not in dropped


def _city_snap_files(folder, city_id, listings, deals, score_ver=None, db_loaded=False):
    import json
    import time

    from app.geo import GEO_VERSION
    from app.listings_cache import SNAP_VER

    body = {
        "rev": 1,
        "listings": listings,
        "stats": {"total": len(listings), "deals": deals},
        "snap_ver": SNAP_VER,
        "geo_ver": GEO_VERSION,
        "saved_at": time.time(),
    }
    if score_ver is not None:
        body["score_ver"] = score_ver
    meta = {
        "rev": 1,
        "n": len(listings),
        "deals": deals,
        "snap_ver": SNAP_VER,
        "geo_ver": GEO_VERSION,
        "saved_at": time.time(),
        "db_loaded": bool(db_loaded),
    }
    if score_ver is not None:
        meta["score_ver"] = score_ver
    (folder / f"{city_id}.json").write_text(
        json.dumps(body, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    (folder / f"{city_id}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def test_unscored_city_snap_is_not_http_ready(tmp_path, monkeypatch):
    from app import listings_cache

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    reset()
    listings_cache._force_cache_dir = tmp_path
    rows = [
        {"id": f"zonaprop:bb-{i}", "city": "bahia-blanca", "deal_label": "", "price_usd": 80000}
        for i in range(12)
    ]
    _city_snap_files(tmp_path, "bahia-blanca", rows, deals=0)
    assert listings_cache._disk_http_ready("bahia-blanca") is False
    assert listings_cache._read_disk("bahia-blanca") is None
    reset()


def test_legacy_city_snap_with_deals_stays_ready(tmp_path, monkeypatch):
    from app import listings_cache

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    reset()
    listings_cache._force_cache_dir = tmp_path
    rows = [
        {
            "id": f"zonaprop:caba-{i}",
            "city": "caba",
            "deal_label": "oportunidad" if i < 5 else "mercado",
            "price_usd": 120000,
        }
        for i in range(12)
    ]
    _city_snap_files(tmp_path, "caba", rows, deals=5, db_loaded=True)
    assert listings_cache._disk_http_ready("caba") is True
    loaded = listings_cache._read_disk("caba")
    assert loaded is not None
    assert loaded["stats"]["deals"] == 5
    reset()


def test_scored_city_snap_with_zero_deals_stays_ready(tmp_path, monkeypatch):
    from app import listings_cache
    from app.scoring import SCORE_VERSION

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    reset()
    listings_cache._force_cache_dir = tmp_path
    rows = [
        {"id": f"zonaprop:even-{i}", "city": "rosario", "deal_label": "mercado", "price_usd": 90000}
        for i in range(12)
    ]
    _city_snap_files(tmp_path, "rosario", rows, deals=0, score_ver=SCORE_VERSION, db_loaded=True)
    assert listings_cache._disk_http_ready("rosario") is True
    reset()


def test_tiny_incomplete_snap_is_not_http_ready(tmp_path, monkeypatch):
    from app import listings_cache

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    reset()
    listings_cache._force_cache_dir = tmp_path
    rows = [
        {"id": "zonaprop:caba-1", "city": "caba", "deal_label": "mercado", "price_usd": 120000},
        {"id": "zonaprop:caba-2", "city": "caba", "deal_label": "mercado", "price_usd": 90000},
    ]
    _city_snap_files(tmp_path, "caba", rows, deals=0, score_ver="13")
    assert listings_cache._disk_http_ready("caba") is False
    assert listings_cache._disk_cache_ok("caba") is False
    raw, _encoding = listings_cache.listings_body("caba")
    assert raw is None
    pins, _pin_encoding = listings_cache.listings_pins_body("caba")
    assert pins is None
    reset()


def test_stale_score_pins_are_not_served(tmp_path, monkeypatch):
    import json
    from app import listings_cache
    from app.geo import GEO_VERSION
    from app.listings_cache import SNAP_VER

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    reset()
    listings_cache._force_cache_dir = tmp_path
    rows = [
        {"id": f"zonaprop:pilar-{i}", "city": "pilar", "deal_label": "mercado", "price_usd": 90000}
        for i in range(12)
    ]
    _city_snap_files(tmp_path, "pilar", rows, deals=0, score_ver="13", db_loaded=True)
    pin_body = {
        "rev": 1,
        "listings": rows,
        "layer": "pins",
        "score_ver": "13",
        "snap_ver": SNAP_VER,
        "geo_ver": GEO_VERSION,
    }
    (tmp_path / "pilar.pins").write_text(json.dumps(pin_body, separators=(",", ":")), encoding="utf-8")
    pins, _enc = listings_cache.listings_pins_body("pilar")
    assert pins is None
    reset()


def test_tiny_db_loaded_snap_is_http_ready(tmp_path, monkeypatch):
    from app import listings_cache
    from app.scoring import SCORE_VERSION

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    reset()
    listings_cache._force_cache_dir = tmp_path
    rows = [
        {"id": "zonaprop:madryn-1", "city": "puerto-madryn", "deal_label": "mercado", "price_usd": 90000},
        {"id": "zonaprop:madryn-2", "city": "puerto-madryn", "deal_label": "mercado", "price_usd": 110000},
    ]
    _city_snap_files(tmp_path, "puerto-madryn", rows, deals=0, score_ver=SCORE_VERSION, db_loaded=True)
    assert listings_cache._disk_http_ready("puerto-madryn") is True
    reset()


def test_partial_city_snap_without_db_load_is_not_http_ready(tmp_path, monkeypatch):
    from app import listings_cache
    from app.scoring import SCORE_VERSION

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    reset()
    listings_cache._force_cache_dir = tmp_path
    rows = [
        {"id": f"zonaprop:q-{i}", "city": "quilmes", "deal_label": "mercado", "price_usd": 70000}
        for i in range(12)
    ]
    _city_snap_files(tmp_path, "quilmes", rows, deals=0, score_ver=SCORE_VERSION)
    assert listings_cache._disk_http_ready("quilmes") is False
    reset()


def test_ingest_refreshes_city_deal_stats(monkeypatch):
    from app import listings_cache

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    reset()
    listings_cache._ready = True
    first = _item("deal-a", "caba")
    ingest([first])
    payload("caba")
    scored = _item("deal-a", "caba")
    scored.deal_label = "oportunidad"
    scored.extra = {"deal_score": 81.0}
    ingest([scored])
    data = payload("caba")
    assert data["listings"][0]["deal_label"] == "oportunidad"
    assert data["listings"][0]["deal_score"] == 81.0
    assert data["stats"]["deals"] == 1
    reset()


def test_pin_row_includes_city_deal_score():
    from app import listings_cache

    item = _item("pin-deal", "caba")
    item.deal_label = "oportunidad"
    item.extra = {"deal_score": 72.5}
    pin = listings_cache._pin_row(item)
    assert pin["deal_label"] == "oportunidad"
    assert pin["deal_score"] == 72.5
    reset()


def _write_city_gzip(folder, city_id):
    import gzip

    raw = (folder / f"{city_id}.json").read_bytes()
    (folder / f"{city_id}.json.gz").write_bytes(gzip.compress(raw))


def test_listings_body_serves_expired_gzip(tmp_path, monkeypatch):
    import gzip
    import json
    import os
    import time

    from app import listings_cache
    from app.scoring import SCORE_VERSION

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    monkeypatch.setattr(listings_cache, "CITY_CACHE_TTL_SEC", 10)
    reset()
    listings_cache._force_cache_dir = tmp_path
    rows = [
        {"id": f"zonaprop:stale-{i}", "city": "caba", "deal_label": "mercado", "price_usd": 110000}
        for i in range(12)
    ]
    old = time.time() - 100
    _city_snap_files(tmp_path, "caba", rows, deals=0, score_ver=SCORE_VERSION, db_loaded=True)
    for name in ("caba.json", "caba.meta.json"):
        data = json.loads((tmp_path / name).read_text(encoding="utf-8"))
        data["saved_at"] = old
        (tmp_path / name).write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    _write_city_gzip(tmp_path, "caba")
    for name in ("caba.json", "caba.meta.json", "caba.json.gz"):
        os.utime(tmp_path / name, (old, old))
    assert listings_cache._disk_http_ready("caba") is False
    packed, encoding = listings_cache.listings_body("caba", gzip=True)
    assert encoding == "gzip"
    assert packed[:2] == b"\x1f\x8b"
    served = json.loads(gzip.decompress(packed))
    assert len(served["listings"]) == 12
    reset()


def test_listings_body_keeps_disk_gzip_while_refresh_warms(tmp_path, monkeypatch):
    import gzip
    import json

    from app import listings_cache
    from app.scoring import SCORE_VERSION

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    reset()
    listings_cache._force_cache_dir = tmp_path
    rows = [
        {"id": f"zonaprop:full-{i}", "city": "caba", "deal_label": "mercado", "price_usd": 100000}
        for i in range(12)
    ]
    _city_snap_files(tmp_path, "caba", rows, deals=0, score_ver=SCORE_VERSION, db_loaded=True)
    _write_city_gzip(tmp_path, "caba")
    pin = listings_cache._pin_row(_item("warm-only", "caba"))
    listings_cache._commit_snap("caba", [pin], warming=True, persist=False)
    packed, encoding = listings_cache.listings_body("caba", gzip=True)
    assert encoding == "gzip"
    served = json.loads(gzip.decompress(packed))
    assert len(served["listings"]) == 12
    assert served["listings"][0]["id"] == "zonaprop:full-0"
    reset()


def test_keep_catalog_loads_missing_city(monkeypatch):
    from app import listings_cache

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    reset()
    called = []
    monkeypatch.setattr(
        listings_cache,
        "request_city_bytes",
        lambda cid, refresh=False: called.append((cid, refresh)),
    )
    monkeypatch.setattr(listings_cache, "_catalog_keep_ids", lambda: ["puerto-madryn"])
    monkeypatch.setattr(listings_cache, "_snap_behind_db", lambda cid: False)
    monkeypatch.setattr(listings_cache, "_disk_http_ready", lambda cid, allow_stale=False: False)
    listings_cache.keep_catalog_cached()
    assert ("puerto-madryn", False) in called
    reset()


def test_keep_catalog_starts_one_missing_city_at_a_time(monkeypatch):
    from app import listings_cache

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    reset()
    called = []

    def keep(cid):
        called.append(cid)
        return True

    monkeypatch.setattr(listings_cache, "_keep_city", keep)
    monkeypatch.setattr(listings_cache, "_catalog_keep_ids", lambda: ["caba", "cordoba", "mendoza"])
    listings_cache.keep_catalog_cached()
    assert called == ["caba"]
    reset()


def test_keep_catalog_refreshes_when_sqlite_grew(tmp_path, monkeypatch):
    from app import listings_cache
    from app.scoring import SCORE_VERSION

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    reset()
    listings_cache._force_cache_dir = tmp_path
    rows = [
        {"id": f"zonaprop:keep-{i}", "city": "caba", "deal_label": "mercado", "price_usd": 90000}
        for i in range(100)
    ]
    _city_snap_files(tmp_path, "caba", rows, deals=0, score_ver=SCORE_VERSION, db_loaded=True)
    called = []
    monkeypatch.setattr(
        listings_cache,
        "request_city_bytes",
        lambda cid, refresh=False: called.append((cid, refresh)),
    )
    monkeypatch.setattr(listings_cache, "_catalog_keep_ids", lambda: ["caba"])
    monkeypatch.setattr(listings_cache, "_snap_behind_db", lambda cid: True)
    listings_cache.keep_catalog_cached()
    assert ("caba", True) in called
    reset()


def test_keep_catalog_skips_ready_city(tmp_path, monkeypatch):
    from app import listings_cache
    from app.scoring import SCORE_VERSION

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    reset()
    listings_cache._force_cache_dir = tmp_path
    rows = [
        {"id": f"zonaprop:ok-{i}", "city": "caba", "deal_label": "mercado", "price_usd": 90000}
        for i in range(12)
    ]
    _city_snap_files(tmp_path, "caba", rows, deals=0, score_ver=SCORE_VERSION, db_loaded=True)
    called = []
    monkeypatch.setattr(
        listings_cache,
        "request_city_bytes",
        lambda cid, refresh=False: called.append((cid, refresh)),
    )
    monkeypatch.setattr(listings_cache, "_catalog_keep_ids", lambda: ["caba"])
    monkeypatch.setattr("app.store.city_listing_counts", lambda: {"caba": 12})
    listings_cache.keep_catalog_cached()
    assert called == []
    reset()


def test_keep_catalog_rewrites_snaps_only_when_catalog_changes(monkeypatch):
    from app import listings_cache

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    reset()
    rewrites = []
    monkeypatch.setattr(listings_cache, "rewrite_snap_cities", lambda: rewrites.append(1))
    monkeypatch.setattr(listings_cache, "_keep_city", lambda cid: False)
    monkeypatch.setattr(listings_cache, "_catalog_keep_ids", lambda: ["caba"])
    listings_cache.keep_catalog_cached()
    assert rewrites == []
    monkeypatch.setattr(listings_cache, "_catalog_keep_ids", lambda: ["caba", "trelew"])
    listings_cache.keep_catalog_cached()
    assert rewrites == [1]
    reset()


def test_commit_snap_writes_json_before_gzip(tmp_path, monkeypatch):
    from app import listings_cache
    from app.scoring import SCORE_VERSION

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    reset()
    listings_cache._force_cache_dir = tmp_path
    rows = [
        {"id": f"zonaprop:json-{i}", "city": "caba", "deal_label": "mercado", "price_usd": 90000, "title": "Depto"}
        for i in range(12)
    ]

    def boom(_raw):
        path = tmp_path / "caba.json"
        assert path.is_file() and path.stat().st_size > 80
        raise RuntimeError("gzip falló a propósito")

    monkeypatch.setattr(listings_cache, "gzip_encode", boom)
    try:
        listings_cache._commit_snap("caba", rows, warming=False, persist=True)
    except RuntimeError:
        pass
    assert (tmp_path / "caba.json").is_file()
    assert (tmp_path / "caba.meta.json").is_file()
    meta = (tmp_path / "caba.meta.json").read_text(encoding="utf-8")
    assert SCORE_VERSION in meta
    reset()
    from app import listings_cache
    from app.scoring import SCORE_VERSION

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    reset()
    listings_cache._force_cache_dir = tmp_path
    rows = [
        {"id": f"zonaprop:gz-{i}", "city": "caba", "deal_label": "mercado", "price_usd": 90000}
        for i in range(12)
    ]
    _city_snap_files(tmp_path, "caba", rows, deals=0, score_ver=SCORE_VERSION, db_loaded=True)
    _write_city_gzip(tmp_path, "caba")
    (tmp_path / "caba.json").unlink()
    assert listings_cache._disk_http_ready("caba") is True
    reset()


def test_hydrate_ram_gzip_serves_from_memory(tmp_path, monkeypatch):
    import gzip
    import json

    from app import listings_cache
    from app.scoring import SCORE_VERSION

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    reset()
    listings_cache._force_cache_dir = tmp_path
    rows = [
        {"id": f"zonaprop:ram-{i}", "city": "caba", "deal_label": "mercado", "price_usd": 90000}
        for i in range(12)
    ]
    _city_snap_files(tmp_path, "caba", rows, deals=0, score_ver=SCORE_VERSION, db_loaded=True)
    _write_city_gzip(tmp_path, "caba")
    listings_cache._hydrate_ram_gzip("caba")
    (tmp_path / "caba.json").unlink()
    (tmp_path / "caba.json.gz").unlink()
    packed, encoding = listings_cache.listings_body("caba", gzip=True)
    assert encoding == "gzip"
    served = json.loads(gzip.decompress(packed))
    assert len(served["listings"]) == 12
    reset()


def test_warming_disk_is_not_complete_and_still_served(tmp_path, monkeypatch):
    import json

    from app import listings_cache
    from app.scoring import SCORE_VERSION

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    reset()
    listings_cache._force_cache_dir = tmp_path
    pins = [
        listings_cache._pin_row(_item(f"warm-{i}", "caba"))
        for i in range(listings_cache.PIN_FLUSH_FIRST)
    ]
    listings_cache._commit_snap("caba", pins, warming=True, persist=True)
    meta = json.loads((tmp_path / "caba.meta.json").read_text(encoding="utf-8"))
    assert meta.get("warming") is True
    assert meta.get("n") == listings_cache.PIN_FLUSH_FIRST
    assert listings_cache._disk_http_ready("caba") is False
    listings_cache._city_snaps.clear()
    raw, _enc = listings_cache.listings_body("caba")
    assert raw
    data = json.loads(raw)
    assert data["warming"] is True
    assert len(data["listings"]) == listings_cache.PIN_FLUSH_FIRST
    reset()


def test_ensure_snap_keeps_warming_city(monkeypatch):
    from app import listings_cache

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    reset()
    pin = listings_cache._pin_row(_item("keep-warm", "caba"))
    listings_cache._commit_snap("caba", [pin], warming=True, persist=False)
    listings_cache._ready = True
    listings_cache._loading.add("caba")
    served = listings_cache._ensure_snap("caba", None)
    assert served["warming"] is True
    assert served["listings"][0]["id"] == "zonaprop:keep-warm"
    reset()


def test_city_fetch_ids_skip_radius_neighbors(monkeypatch):
    from app import listings_cache

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    monkeypatch.setattr(listings_cache, "related_place_ids", lambda city: {city})
    monkeypatch.setattr(listings_cache, "same_place_ids", lambda city: {city, "vecino-25km"})
    ids = listings_cache._city_fetch_ids("rosario")
    assert "rosario" in ids
    assert "vecino-25km" not in ids
    reset()


def test_city_fetch_ids_caba_keeps_aliases(monkeypatch):
    from app import listings_cache

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    monkeypatch.setattr(listings_cache, "related_place_ids", lambda city: {"caba"})
    monkeypatch.setattr(listings_cache, "same_place_ids", lambda city: {"caba", "capital-federal"})
    ids = listings_cache._city_fetch_ids("caba")
    assert "caba" in ids
    assert "capital-federal" in ids
    reset()


def test_catalog_keep_skips_tiny_places(monkeypatch):
    from app import listings_cache

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    monkeypatch.setattr(
        "app.places.listed_cities",
        lambda _listings=None: [
            {"id": "caba", "n": 6105},
            {"id": "pinero", "n": 2},
            {"id": "cordoba", "n": 4472},
        ],
    )
    monkeypatch.setattr("app.places.priority_place_ids", lambda: ["puerto-madryn"])
    reset()
    ids = listings_cache._catalog_keep_ids()
    assert ids[0] == "caba"
    assert "pinero" not in ids
    assert "cordoba" not in ids
    assert "puerto-madryn" in ids
    reset()


def test_fitted_cache_is_not_behind_larger_sqlite_count(tmp_path, monkeypatch):
    from app import listings_cache
    from app.scoring import SCORE_VERSION

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    monkeypatch.setenv("PROPMAP_TEST", "0")
    reset()
    listings_cache._force_cache_dir = tmp_path
    rows = [
        {"id": f"zonaprop:fit-{i}", "city": "caba", "deal_label": "mercado", "price_usd": 90000}
        for i in range(80)
    ]
    _city_snap_files(tmp_path, "caba", rows, deals=0, score_ver=SCORE_VERSION, db_loaded=True)
    monkeypatch.setattr("app.store.city_listing_counts", lambda: {"caba": 6105})
    assert listings_cache._snap_behind_db("caba") is False
    reset()


def test_warming_cache_is_not_behind_larger_sqlite_count(tmp_path, monkeypatch):
    import json

    from app import listings_cache
    from app.scoring import SCORE_VERSION

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    monkeypatch.setenv("PROPMAP_TEST", "0")
    reset()
    listings_cache._force_cache_dir = tmp_path
    rows = [
        {"id": f"zonaprop:warm-{i}", "city": "cordoba", "deal_label": "mercado", "price_usd": 90000}
        for i in range(80)
    ]
    _city_snap_files(tmp_path, "cordoba", rows, deals=0, score_ver=SCORE_VERSION, db_loaded=False)
    meta_path = tmp_path / "cordoba.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["warming"] = True
    meta["db_n"] = 0
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    monkeypatch.setattr("app.store.city_listing_counts", lambda: {"cordoba": 4472})
    assert listings_cache._snap_behind_db("cordoba") is False
    reset()


def test_snap_behind_when_recorded_sqlite_grew(tmp_path, monkeypatch):
    import json

    from app import listings_cache
    from app.scoring import SCORE_VERSION

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    monkeypatch.setenv("PROPMAP_TEST", "0")
    reset()
    listings_cache._force_cache_dir = tmp_path
    rows = [
        {"id": f"zonaprop:grew-{i}", "city": "caba", "deal_label": "mercado", "price_usd": 90000}
        for i in range(80)
    ]
    _city_snap_files(tmp_path, "caba", rows, deals=0, score_ver=SCORE_VERSION, db_loaded=True)
    meta_path = tmp_path / "caba.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["db_n"] = 100
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    monkeypatch.setattr("app.store.city_listing_counts", lambda: {"caba": 6105})
    assert listings_cache._snap_behind_db("caba") is True
    reset()


def test_rewrite_snap_cities_does_not_touch_disk(tmp_path, monkeypatch):
    from app import listings_cache

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    reset()
    listings_cache._force_cache_dir = tmp_path
    raw = b'{"listings":[],"cities":[],"snap_ver":"15"}'
    json_path = tmp_path / "caba.json"
    gz_path = tmp_path / "caba.json.gz"
    json_path.write_bytes(raw)
    gz_path.write_bytes(raw)
    monkeypatch.setattr(
        "app.places.listed_cities",
        lambda _listings=None: [{"id": "caba", "label": "CABA", "n": 3}],
    )
    listings_cache._city_snaps["caba"] = {"cities": [], "encoded_gzip": b"keep"}
    listings_cache.rewrite_snap_cities()
    assert json_path.read_bytes() == raw
    assert gz_path.read_bytes() == raw
    assert listings_cache._city_snaps["caba"]["encoded_gzip"] == b"keep"
    assert listings_cache._cities[0]["id"] == "caba"
    reset()


def test_request_city_bytes_skips_reload_after_empty_db_load(monkeypatch):
    from app import listings_cache

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    reset()
    started = []
    monkeypatch.setattr(
        listings_cache,
        "_load_city",
        lambda cid: started.append(cid),
    )
    monkeypatch.setattr(listings_cache, "_schedule_city_warm", lambda cid: None)
    monkeypatch.setattr(listings_cache, "_disk_http_ready", lambda cid, allow_stale=False: False)
    listings_cache._db_loaded.add("villa-canto")
    listings_cache._city_snaps["villa-canto"] = {"listings": [], "warming": False}
    listings_cache.request_city_bytes("villa-canto")
    assert started == []
    assert "villa-canto" not in listings_cache._loading
    reset()


def test_kick_access_runs_one_city_at_a_time(monkeypatch):
    from app import access

    monkeypatch.setenv("PROPMAP_TEST", "0")
    started = threading.Event()
    hold = threading.Event()
    runs = []

    def slow_refresh(cid):
        runs.append(cid)
        started.set()
        hold.wait(2)
        return 0

    monkeypatch.setattr(access, "refresh_city_access", slow_refresh)
    monkeypatch.setattr(access, "ACCESS_TURN_SEC", 0.05)
    access._access_at.clear()
    access._access_inflight.clear()
    try:
        access.kick_access_later("caba")
        assert started.wait(2)
        access.kick_access_later("mendoza")
        time.sleep(0.3)
        assert runs == ["caba"]
    finally:
        hold.set()
        time.sleep(0.2)
        access._access_at.clear()
        access._access_inflight.clear()


def test_kick_access_waits_between_full_city_passes(monkeypatch):
    from app import access

    monkeypatch.setenv("PROPMAP_TEST", "0")
    runs = []
    done = threading.Event()

    def fake_refresh(cid):
        runs.append(cid)
        done.set()
        return 0

    monkeypatch.setattr(access, "refresh_city_access", fake_refresh)
    access._access_at.clear()
    access._access_inflight.clear()
    try:
        access.kick_access_later("caba")
        assert done.wait(2)
        access.kick_access_later("caba")
        assert runs == ["caba"]
        done.clear()
        access.kick_access_later("caba", now=True)
        assert done.wait(2)
        assert runs == ["caba", "caba"]
    finally:
        access._access_at.clear()
        access._access_inflight.clear()


def test_refresh_meta_is_throttled_in_production(monkeypatch):
    from app import listings_cache

    reset()
    monkeypatch.setenv("PROPMAP_TEST", "0")
    calls = []
    monkeypatch.setattr("app.places.listed_cities", lambda _ids: calls.append(1) or [])
    monkeypatch.setattr("app.store.get_meta", lambda *a, **k: "")
    monkeypatch.setattr(listings_cache, "_read_rate", lambda: 1.0)
    listings_cache._refresh_meta()
    listings_cache._refresh_meta()
    listings_cache._refresh_meta()
    assert len(calls) == 1
    listings_cache._refresh_meta(force=True)
    assert len(calls) == 2
    reset()


def test_schedule_city_warm_runs_once(monkeypatch):
    from app import listings_cache

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    reset()
    monkeypatch.setenv("PROPMAP_TEST", "0")
    calls = []
    done = threading.Event()

    def fake_warm(cid):
        calls.append(cid)
        with listings_cache._lock:
            listings_cache._warm_queued.discard(cid)
            listings_cache._warmed.add(cid)
        done.set()

    monkeypatch.setattr(listings_cache, "_warm_city_extras", fake_warm)
    listings_cache._schedule_city_warm("villa-canto")
    assert done.wait(1)
    listings_cache._schedule_city_warm("villa-canto")
    assert calls == ["villa-canto"]
    reset()
