from app.access import (
    access_fingerprint,
    access_needs_refresh,
    compute_access,
    near_payload,
    stored_access_ok,
)
from app.models import Listing
from app.osm_poi import remember, reset


def _exact(**kwargs) -> Listing:
    data = {
        "source": "zonaprop",
        "source_id": "near-1",
        "url": "https://example.com/near-1",
        "title": "Depto",
        "property_type": "departamento",
        "lat": -34.6037,
        "lon": -58.3816,
        "city": "caba",
        "has_exact_location": True,
        "extra": {"location_kind": "exact"},
    }
    data.update(kwargs)
    return Listing(**data)


def test_stored_access_ok_needs_nearby_or_score():
    item = _exact()
    assert stored_access_ok(item) is False
    assert access_needs_refresh(item) is True
    extra = dict(item.extra or {})
    extra["access"] = {
        "fp": access_fingerprint(item),
        "precise": True,
        "score": 66.0,
        "nearby": [{"category": "shop", "km": 0.12, "name": "Súper"}],
    }
    item.extra = extra
    assert stored_access_ok(item) is True
    assert access_needs_refresh(item) is False


def test_near_payload_reads_stored_without_live_compute(monkeypatch):
    item = _exact()
    nearby = [{"category": "shop", "km": 0.12, "name": "Súper", "label": "Súper", "kind": "Súper"}]
    extra = dict(item.extra or {})
    extra["access"] = {
        "fp": access_fingerprint(item),
        "precise": True,
        "score": 80.0,
        "nearby": nearby,
        "reason": "ya estaba",
        "pin_grade": "exact",
    }
    item.extra = extra

    monkeypatch.setattr("app.store.init", lambda: None)
    monkeypatch.setattr("app.store.get_listing", lambda _lid: item)
    monkeypatch.setattr("app.access.compute_access", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no hay que calcular al abrir")))
    monkeypatch.setattr("app.access.kick_access_later", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no reencolar si ya está")))
    monkeypatch.setattr("app.access.city_pois", lambda *_a, **_k: {})

    out = near_payload(listing_id=item.id, city="caba", lat=item.lat, lon=item.lon)
    assert out["pending"] is False
    assert out["nearby"] == nearby
    assert out["score"] == 80.0
    assert out["city"] == "caba"


def test_near_payload_does_not_invent_a_score(monkeypatch):
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
    monkeypatch.setattr("app.store.init", lambda: None)
    monkeypatch.setattr("app.store.get_listing", lambda _lid: item)
    saved = []
    monkeypatch.setattr("app.access._save_access", lambda listing, live: saved.append((listing.id, live)))
    monkeypatch.setattr("app.access.kick_access_later", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("la lectura no dispara el cálculo")))

    out = near_payload(listing_id=item.id, city="caba", lat=item.lat, lon=item.lon)
    assert out["pending"] is True
    assert out["score"] is None
    assert not saved
    reset()


def test_refresh_city_access_waits_for_pois(monkeypatch):
    from app import access

    seen = {}

    def fake_ensure(cid, blocking=False):
        seen["blocking"] = blocking
        return {}

    monkeypatch.setattr("app.osm_poi.ensure_city_pois", fake_ensure)
    monkeypatch.setattr("app.store.init", lambda: None)
    n = access.refresh_city_access("caba")
    assert n == 0
    assert seen["blocking"] is True


def test_compute_access_still_works_live():
    reset()
    remember(
        "caba",
        {"categories": {"shop": [{"lat": -34.6038, "lon": -58.3817, "name": "Súper"}]}},
    )
    hit = compute_access(_exact())
    assert hit["nearby"]
    reset()


def test_poi_score_compares_within_the_city_and_is_not_maxed():
    reset()
    cats = {key: [] for key in ("health", "police", "transport", "subway", "train", "beach", "plaza", "shop", "school")}
    for row in range(6):
        for col in range(6):
            lat = -34.70 + row * 0.008
            lon = -58.50 + col * 0.008
            for key in ("health", "police", "transport", "plaza", "shop", "school"):
                cats[key].append({"lat": lat, "lon": lon, "name": key})
    remember("caba", {"categories": cats})
    center = compute_access(_exact(lat=-34.68, lon=-58.48))
    edge = compute_access(_exact(source_id="edge", lat=-34.74, lon=-58.56))
    assert center["score"] < 97
    assert edge["score"] < center["score"]
    assert "respecto de esta ciudad" in center["reason"]
    assert center["score_raw"] > edge["score_raw"]
    reset()


def test_walk_km_grows_when_amenities_are_spread():
    from app.osm_poi import walk_km_for_city

    reset()
    shops = [{"lat": -32.90 + i * 0.005, "lon": -64.20, "name": f"S{i}"} for i in range(10)]
    km = walk_km_for_city("ciudad-laxa", {"shop": shops})
    assert km > 0.9
    assert km <= 2.4
    reset()
    clustered = [{"lat": -32.90 + i * 0.00015, "lon": -64.20, "name": f"C{i}"} for i in range(10)]
    assert walk_km_for_city("ciudad-densa", {"shop": clustered}) == 0.6
    reset()
    grid = [
        {"lat": -32.90 + (i % 40) * 0.0007, "lon": -64.20 + (i // 40) * 0.0007, "name": f"G{i}"}
        for i in range(500)
    ]
    assert walk_km_for_city("ciudad-densa-grid", {"shop": grid}) == 0.6
    reset()


def test_walk_radius_grows_when_nothing_is_within_default():
    reset()
    remember(
        "test-city",
        {
            "categories": {
                "shop": [{"lat": -42.782, "lon": -65.04, "name": "Súper del pueblo"}],
            }
        },
    )
    hit = compute_access(_exact(city="test-city", lat=-42.77, lon=-65.04, extra={"location_kind": "exact"}))
    assert hit["walk_km"] > 0.6
    assert hit["nearby"]
    assert hit["nearby"][0]["name"] == "Súper del pueblo"
    reset()
