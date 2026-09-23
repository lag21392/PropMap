from app.access import compute_access, pin_grade
from app.models import Listing
from app.osm_poi import city_pois, origin_for_city, remember, reset
from app.profile import compute_profile


def _depto(**kwargs) -> Listing:
    data = {
        "source": "zonaprop",
        "source_id": "1",
        "url": "",
        "title": "Depto 2 amb",
        "property_type": "departamento",
        "price_usd": 68000,
        "city": "test-city",
        "barrio": "Centro",
        "covered_m2": 48,
        "rooms": 2,
        "bedrooms": 1,
        "lat": -42.77,
        "lon": -65.04,
        "has_exact_location": True,
        "extra": {"location_kind": "exact", "peer_count": 8, "comp_scope": "deptos en Centro"},
        "vs_barrio_pct": 12.0,
    }
    data.update(kwargs)
    return Listing(**data)


def test_pin_grade_exact_vs_approx():
    exact = _depto()
    approx = _depto(has_exact_location=False, extra={"location_kind": "approx"})
    unknown = _depto(lat=None, lon=None, has_exact_location=False, extra={"location_kind": "unknown"})
    assert pin_grade(exact) == "exact"
    assert pin_grade(approx) == "approx"
    assert pin_grade(unknown) == "unknown"


def test_access_requires_exact_pin():
    reset()
    remember(
        "test-city",
        {
            "categories": {
                "health": [{"lat": -42.7702, "lon": -65.0402, "name": "Clinica"}],
                "police": [{"lat": -42.771, "lon": -65.041, "name": "Comisaria"}],
                "transport": [{"lat": -42.7701, "lon": -65.0401, "name": "Parada"}],
                "beach": [{"lat": -42.7703, "lon": -65.0403, "name": "Playa"}],
                "plaza": [{"lat": -42.77015, "lon": -65.0401, "name": "Plaza"}],
            }
        },
    )
    exact = _depto()
    approx = _depto(has_exact_location=False, extra={"location_kind": "approx"})
    hit = compute_access(exact)
    miss = compute_access(approx)
    assert hit["precise"] is True
    assert hit["score"] is not None
    assert hit["categories"]["health"]["km"] < 0.5
    assert miss["precise"] is False
    assert miss["score"] is None
    assert hit["nearby"]
    cats = {row["category"] for row in hit["nearby"]}
    assert {"health", "transport"} <= cats
    assert "plaza" in cats or "beach" in cats
    assert all(row["km"] <= 0.6 for row in hit["nearby"])
    assert hit["nearby"] == sorted(hit["nearby"], key=lambda row: row["km"])
    assert "aproximado" in (miss["reason"] or "")
    assert "600" in (hit["reason"] or "")


def test_nearby_picks_one_open_space_and_ignores_far_pois():
    reset()
    remember(
        "test-city",
        {
            "categories": {
                "health": [
                    {"lat": -42.7702, "lon": -65.0402, "name": "Clinica"},
                    {"lat": -42.79, "lon": -65.06, "name": "Hospital lejos"},
                ],
                "police": [{"lat": -42.7704, "lon": -65.0404, "name": "Comisaria"}],
                "transport": [
                    {"lat": -42.7701, "lon": -65.0401, "name": "Parada 1"},
                    {"lat": -42.77012, "lon": -65.04012, "name": "Parada 2"},
                    {"lat": -42.77014, "lon": -65.04014, "name": "Parada 3"},
                ],
                "plaza": [{"lat": -42.77015, "lon": -65.0401, "name": "Plaza cerca"}],
                "beach": [{"lat": -42.775, "lon": -65.045, "name": "Playa mas lejos"}],
                "shop": [{"lat": -42.7703, "lon": -65.0403, "name": "Super"}],
                "school": [{"lat": -42.77025, "lon": -65.04025, "name": "Escuela"}],
            }
        },
    )
    hit = compute_access(_depto())
    names = [row["name"] for row in hit["nearby"]]
    assert "Hospital lejos" not in names
    assert "Parada 3" in names
    assert sum(1 for row in hit["nearby"] if row["category"] == "transport") == 3
    assert sum(1 for row in hit["nearby"] if row["category"] == "shop") == 1
    assert sum(1 for row in hit["nearby"] if row["category"] == "school") == 1
    assert [row["km"] for row in hit["nearby"]] == sorted(row["km"] for row in hit["nearby"])
    open_cats = [row["category"] for row in hit["nearby"] if row["category"] in {"plaza", "beach"}]
    assert "plaza" in open_cats


def test_cheaper_m2_makes_pentagon_price_axis_better():
    cheap = compute_profile(_depto(vs_barrio_pct=22.0))
    fair = compute_profile(_depto(vs_barrio_pct=0.0))
    dear = compute_profile(_depto(vs_barrio_pct=-18.0))
    assert cheap["axes"]["price_m2"]["score"] > fair["axes"]["price_m2"]["score"]
    assert fair["axes"]["price_m2"]["score"] > dear["axes"]["price_m2"]["score"]
    assert cheap["axes"]["price_m2"]["score"] >= 80
    assert "barato" in (cheap["axes"]["price_m2"]["note"] or "")


def test_profile_hides_zona_and_servicios_without_exact_pin():
    reset()
    remember(
        "test-city",
        {"categories": {"health": [{"lat": -42.7702, "lon": -65.0402, "name": "Clinica"}]}},
    )
    approx = _depto(has_exact_location=False, extra={"location_kind": "approx", "peer_count": 8})
    profile = compute_profile(approx)
    assert profile["pin_grade"] == "approx"
    assert profile["axes"]["zona"]["score"] is None
    assert profile["axes"]["servicios"]["score"] is None
    assert profile["axes"]["price_m2"]["score"] is not None


def test_street_address_gets_zona_and_servicios_even_if_kind_approx():
    reset()
    remember(
        "test-city",
        {"categories": {"health": [{"lat": -42.7702, "lon": -65.0402, "name": "Clinica"}]}},
    )
    item = _depto(
        source="properati",
        has_exact_location=False,
        address="Roca 240",
        extra={
            "location_kind": "approx",
            "portal_approx": True,
            "street": "Roca",
            "street_number": 240,
            "peer_count": 8,
            "comp_scope": "deptos en Centro",
        },
    )
    assert pin_grade(item) == "exact"
    hit = compute_access(item)
    assert hit["precise"] is True
    assert hit["score"] is not None
    profile = compute_profile(item)
    assert profile["pin_grade"] == "exact"
    assert profile["axes"]["zona"]["score"] is not None
    assert profile["axes"]["servicios"]["score"] is not None
    assert profile["axes"]["price_m2"]["score"] is not None
    public = item.to_public_dict()
    assert public["location_approx"] is False
    assert public["pin_grade"] == "exact"
    assert public["profile"]["pin_grade"] == "exact"
    assert public["profile"]["axes"]["zona"]["score"] is not None
    assert public["profile"]["total"] is not None
    assert public["profile"]["labels"]["servicios"] == "POIs cercanos"


def test_origin_for_madryn_is_not_caba():
    lat, lon = origin_for_city("puerto-madryn")
    assert lat < -40
    assert lon < -64


def test_origin_for_unknown_city_is_not_caba():
    reset()
    origin = origin_for_city("ciudad-que-no-existe")
    assert origin is None


def test_city_pois_drops_caba_cache_for_other_city():
    reset()
    remember(
        "test-city",
        {
            "version": "2",
            "origin": {"lat": -34.6037, "lon": -58.3816},
            "categories": {"health": [{"lat": -34.6037, "lon": -58.3816, "name": "Hospital CABA"}]},
        },
    )
    assert not (city_pois("test-city").get("health") or [])


def test_compute_profile_keeps_stored_access_without_pois():
    from app.access import access_fingerprint

    reset()
    item = _depto()
    stored = {
        "precise": True,
        "score": 80.0,
        "fp": access_fingerprint(item),
        "reason": "8 lugares a menos de 600 m",
        "categories": {},
        "nearby": [{"category": "shop", "km": 0.1, "name": "Súper"}],
    }
    extra = dict(item.extra or {})
    extra["access"] = stored
    item.extra = extra
    profile = compute_profile(item, {})
    assert profile["axes"]["servicios"]["score"] == 80.0
    assert profile["access"]["nearby"]
    assert profile["total"] is not None
    assert profile["labels"]["servicios"] == "POIs cercanos"


def test_profile_total_is_mean_of_present_axes():
    reset()
    remember(
        "test-city",
        {
            "categories": {
                "health": [{"lat": -42.7702, "lon": -65.0402, "name": "Clinica"}],
                "transport": [{"lat": -42.7701, "lon": -65.0401, "name": "Parada"}],
                "shop": [{"lat": -42.7703, "lon": -65.0403, "name": "Super"}],
            }
        },
    )
    profile = compute_profile(_depto(vs_barrio_pct=22.0))
    scores = [axis["score"] for axis in profile["axes"].values() if axis.get("score") is not None]
    assert scores
    assert profile["total"] == round(sum(scores) / len(scores), 1)
    assert profile["axes"]["servicios"]["score"] is not None
    assert not any(row["key"] == "servicios" for row in profile["pending"])


def test_profile_reports_pending_axes_instead_of_fake_scores():
    reset()
    approx = _depto(has_exact_location=False, extra={"location_kind": "approx", "peer_count": 8})
    profile = compute_profile(approx)
    keys = {row["key"] for row in profile["pending"]}
    assert "zona" in keys
    assert "servicios" in keys
    assert profile["axes"]["servicios"]["score"] is None
    assert profile["axes"]["zona"]["score"] is None
    assert profile["total"] is not None
    assert all(row["state"] in {"pending", "blocked"} for row in profile["pending"])


def test_terreno_profile_uses_three_axes():
    lot = _depto(
        property_type="terreno",
        title="Lote 300 m2",
        rooms=None,
        bedrooms=None,
        covered_m2=None,
        total_m2=300,
    )
    profile = compute_profile(lot)
    assert profile["order"] == ["price_m2", "zona", "servicios"]
    assert set(profile["axes"]) == {"price_m2", "zona", "servicios"}
    assert "ambientes" not in profile["labels"]
    assert "alquiler" not in profile["labels"]
    assert all(row["key"] not in {"ambientes", "alquiler"} for row in profile["pending"])
    house = compute_profile(_depto())
    assert house["order"] == ["price_m2", "zona", "ambientes", "alquiler", "servicios"]


def test_ambientes_axis_scores_when_only_bedrooms_are_known():
    item = _depto(rooms=None, bedrooms=2, covered_m2=70)
    profile = compute_profile(item)
    axis = profile["axes"]["ambientes"]
    assert axis["score"] is not None
    assert "3 amb" in axis["note"]
