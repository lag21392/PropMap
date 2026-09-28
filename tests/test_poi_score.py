from app.access import access_fingerprint, score_listings, stored_access_ok
from app.models import Listing
from app.osm_poi import remember, reset


def _exact(source_id: str = "near-1", **extra) -> Listing:
    blob = {"location_kind": "exact"}
    blob.update(extra)
    return Listing(
        source="zonaprop",
        source_id=source_id,
        url=f"https://example.com/{source_id}",
        title="Depto",
        property_type="departamento",
        lat=-34.6037,
        lon=-58.3816,
        city="caba",
        has_exact_location=True,
        extra=blob,
    )


def test_apply_poi_overlay_fills_missing_score_only():
    from app.listings_cache import _poi_by_id, apply_poi_overlay, remember_poi_axes

    _poi_by_id.clear()
    remember_poi_axes({"zonaprop:a": {"score": 80, "confidence": "high", "note": "a pie"}})
    pending = {"id": "zonaprop:a", "profile": {"axes": {"servicios": {"score": None, "note": "todavía"}}}}
    ready = {"id": "zonaprop:b", "profile": {"axes": {"servicios": {"score": 10, "note": "ya"}}}}
    assert apply_poi_overlay([pending, ready]) == 1
    assert pending["profile"]["axes"]["servicios"]["score"] == 80
    assert ready["profile"]["axes"]["servicios"]["score"] == 10
    _poi_by_id.clear()


def test_poi_backlog_skips_scored_and_blocked(tmp_path, monkeypatch):
    from app import store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "listings.sqlite")
    store.init()
    pending = _exact("pend")
    done = _exact(
        "done",
        profile={"axes": {"servicios": {"score": 70, "confidence": "high", "note": "ok"}}},
    )
    blocked = _exact(
        "block",
        profile={"axes": {"servicios": {"score": None, "confidence": "none", "note": "pin aproximado"}}},
    )
    store.upsert_many([pending, done, blocked])
    ids = [item.id for item in store.fetch_poi_backlog(10, prefer_city="caba")]
    assert "zonaprop:pend" in ids
    assert "zonaprop:done" not in ids
    assert "zonaprop:block" not in ids
    axes = store.poi_axes_for_cities(["caba"])
    assert float(axes["zonaprop:done"]["score"]) == 70.0


def test_score_listings_saves_poi_axis(monkeypatch):
    reset()
    remember(
        "caba",
        {
            "categories": {
                "shop": [{"lat": -34.6038, "lon": -58.3817, "name": "Súper"}],
                "transport": [{"lat": -34.6039, "lon": -58.3818, "name": "Parada"}],
            }
        },
    )
    item = _exact()
    saved: list[Listing] = []
    monkeypatch.setattr("app.store.update_extras", lambda rows: saved.extend(rows))
    monkeypatch.setattr("app.listings_cache.ingest", lambda rows: None)
    assert score_listings([item]) == 1
    assert saved and saved[0].id == item.id
    score = item.extra["profile"]["axes"]["servicios"]["score"]
    assert score is not None
    assert stored_access_ok(item) is True
    assert item.extra["access"]["fp"] == access_fingerprint(item, item.extra["access"].get("walk_km"))
    from app.listings_cache import _poi_by_id

    _poi_by_id.clear()
    reset()
