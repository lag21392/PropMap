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


def test_priority_cities_start_with_madryn_then_caba(monkeypatch):
    from app import listings_cache

    monkeypatch.setattr("app.listings_cache.start_warmup", lambda: None)
    monkeypatch.setattr(
        "app.places.priority_place_ids",
        lambda: ["puerto-madryn", "caba"],
    )
    reset()
    ids = listings_cache._priority_city_ids()
    assert ids[:2] == ["puerto-madryn", "caba"]
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
