from app.geo_tools import parse_plain_locations
from app.llm_enrich import apply_analysis, mark_await_llm, needs_improve, should_publish
from app.models import Listing


def test_apply_analysis_moves_foreign_listing_out_of_search_city():
    item = Listing(
        source="properati",
        source_id="llm-1",
        url="https://example.com",
        title="Casa en Canning",
        property_type="casa",
        city="puerto-madryn",
        lat=-42.769,
        lon=-65.038,
        extra={"search_city": "puerto-madryn"},
    )
    apply_analysis(
        item,
        {
            "city_label": "CABA",
            "barrio": "Canning",
            "foreign": True,
            "property_type": "casa",
            "notes": "no es Madryn",
        },
    )
    assert item.city != "puerto-madryn"
    assert item.lat is None
    assert item.extra["llm"]["foreign"] is True
    assert item.barrio == "Canning"


def test_apply_analysis_ignores_foreign_when_city_label_is_the_search_city():
    item = Listing(
        source="properati",
        source_id="llm-local-foreign",
        url="https://example.com",
        title="Apartamento en Venta en Puerto Madryn",
        property_type="departamento",
        city="puerto-madryn",
        lat=-42.76841,
        lon=-65.03592,
        extra={"search_city": "puerto-madryn", "portal_approx": True},
    )
    apply_analysis(
        item,
        {
            "city_label": "Puerto Madryn",
            "barrio": "Gobernador Fontana",
            "foreign": True,
            "property_type": "departamento",
            "notes": "",
        },
    )
    assert item.city == "puerto-madryn"
    assert item.lat == -42.76841
    assert item.lon == -65.03592
    assert item.extra["llm"]["foreign"] is False


def test_apply_analysis_keeps_portal_map_pin_when_barrio_name_collides():
    item = Listing(
        source="properati",
        source_id="llm-portal-map",
        url="https://www.properati.com.ar/detalle/x",
        title="Apartamento en Venta en Puerto Madryn",
        property_type="departamento",
        city="puerto-madryn",
        lat=-42.756297,
        lon=-65.037425,
        extra={
            "search_city": "puerto-madryn",
            "portal_lat": -42.756297,
            "portal_lon": -65.037425,
            "portal_approx": True,
            "portal_map": "ver_mapa",
        },
    )
    apply_analysis(
        item,
        {
            "city_label": "Gobernador Fontana",
            "barrio": "Gobernador Fontana",
            "foreign": True,
            "property_type": "departamento",
            "notes": "",
        },
    )
    assert item.city == "puerto-madryn"
    assert abs(item.lat + 42.756297) < 1e-6
    assert abs(item.lon + 65.037425) < 1e-6
    assert item.extra["llm"]["foreign"] is False


def test_apply_analysis_keeps_local_listing():
    item = Listing(
        source="zonaprop",
        source_id="llm-2",
        url="https://example.com",
        title="Depto Centro",
        property_type="departamento",
        city="puerto-madryn",
        lat=-42.769,
        lon=-65.038,
        extra={"search_city": "puerto-madryn"},
    )
    apply_analysis(
        item,
        {
            "city_label": "Puerto Madryn",
            "barrio": "Centro",
            "foreign": False,
            "property_type": "departamento",
            "notes": "",
        },
    )
    assert item.city == "puerto-madryn"
    assert item.lat == -42.769
    assert item.barrio == "Centro"


def test_apply_analysis_fills_structure_and_drops_contact():
    item = Listing(
        source="zonaprop",
        source_id="llm-3",
        url="https://example.com",
        title="PH 3 ambientes",
        property_type="",
        city="puerto-madryn",
        extra={"search_city": "puerto-madryn"},
    )
    apply_analysis(
        item,
        {
            "property_type": "ph",
            "rooms": 3,
            "covered_m2": 72,
            "amenities": ["patio", "parrilla"],
            "notes": "llamar al 280 456-7890",
            "phone": "2804567890",
            "publisher": "Inmobiliaria X",
            "foreign": False,
        },
    )
    assert item.property_type == "ph"
    assert item.rooms == 3
    assert item.covered_m2 == 72
    assert "patio" in (item.extra.get("amenities") or [])
    assert "280" not in (item.extra.get("llm") or {}).get("notes", "")
    assert "phone" not in (item.extra.get("llm") or {})
    assert "publisher" not in (item.extra.get("llm") or {})


def test_apply_analysis_marks_mortgage_and_intersection():
    item = Listing(
        source="zonaprop",
        source_id="llm-4",
        url="https://example.com",
        title="Casa apto crédito hipotecario",
        property_type="casa",
        city="puerto-madryn",
        extra={"search_city": "puerto-madryn"},
    )
    apply_analysis(
        item,
        {
            "property_type": "casa",
            "mortgage_credit": True,
            "corner_a": "roca",
            "corner_b": "apeleg",
            "foreign": False,
            "notes": "",
        },
    )
    assert item.extra["mortgage_credit"] is True
    assert "apeleg" in (item.extra.get("intersection") or "")
    assert item.extra.get("location_kind") == "intersection"


def test_apply_analysis_keeps_address_apart_from_intersection():
    item = Listing(
        source="zonaprop",
        source_id="llm-addr",
        url="https://example.com",
        title="Depto",
        property_type="departamento",
        city="caba",
        address="Roca 240",
        extra={"search_city": "caba"},
    )
    apply_analysis(
        item,
        {
            "address_text": "Roca y Apeleg",
            "corner_a": "roca",
            "corner_b": "apeleg",
            "foreign": False,
            "notes": "",
        },
    )
    assert item.address == "Roca 240"
    assert "apeleg" in (item.extra.get("intersection") or "")


def test_apply_analysis_relocates_pin_when_address_improves(monkeypatch):
    from app import place_api
    from app.place_api import reset_cache

    def fake_georef(path, params):
        if path != "/direcciones":
            return {}
        return {
            "direcciones": [
                {
                    "nomenclatura": "BARTOLOME MITRE 644, Biedma, Chubut",
                    "calle": {"nombre": "BARTOLOME MITRE"},
                    "ubicacion": {"lat": -42.7710437, "lon": -65.0334884},
                }
            ]
        }

    monkeypatch.setattr(place_api, "_georef", fake_georef)
    reset_cache()
    item = Listing(
        source="zonaprop",
        source_id="llm-mitre",
        url="https://example.com",
        title="Depto",
        property_type="departamento",
        city="puerto-madryn",
        address="Puerto Madryn",
        lat=-42.767277,
        lon=-65.037932,
        extra={"search_city": "puerto-madryn", "location_kind": "approx"},
    )
    apply_analysis(
        item,
        {
            "address_text": "Bartolomé Mitre 644",
            "street": "Bartolomé Mitre",
            "street_number": 644,
            "foreign": False,
            "notes": "",
        },
    )
    assert item.address == "Bartolomé Mitre 644"
    assert abs(item.lat + 42.7710437) < 1e-5
    assert item.has_exact_location is True
    assert item.extra.get("location_kind") == "exact"


def test_apply_analysis_prefers_address_over_intersection_pin(monkeypatch):
    from app import place_api
    from app.place_api import reset_cache

    def fake_georef(path, params):
        if path == "/direcciones":
            return {
                "direcciones": [
                    {
                        "nomenclatura": "MITRE 644",
                        "calle": {"nombre": "MITRE"},
                        "ubicacion": {"lat": -42.7710437, "lon": -65.0334884},
                    }
                ]
            }
        if path == "/intersecciones":
            return {
                "intersecciones": [
                    {
                        "nomenclatura": "ROCA Y APELEG",
                        "ubicacion": {"lat": -42.7680, "lon": -65.0380},
                    }
                ]
            }
        return {}

    monkeypatch.setattr(place_api, "_georef", fake_georef)
    reset_cache()
    item = Listing(
        source="zonaprop",
        source_id="llm-priority",
        url="https://example.com",
        title="Depto",
        property_type="departamento",
        city="puerto-madryn",
        address="Puerto Madryn",
        lat=-42.767277,
        lon=-65.037932,
        extra={"search_city": "puerto-madryn"},
    )
    apply_analysis(
        item,
        {
            "address_text": "Bartolomé Mitre 644",
            "street": "Bartolomé Mitre",
            "street_number": 644,
            "corner_a": "roca",
            "corner_b": "apeleg",
            "foreign": False,
            "notes": "",
        },
    )
    assert abs(item.lat + 42.7710437) < 1e-5
    assert item.extra.get("location_kind") == "exact"
    assert "apeleg" in (item.extra.get("intersection") or "")


def test_apply_analysis_uses_intersection_if_no_address(monkeypatch):
    from app import place_api
    from app.place_api import reset_cache

    def fake_georef(path, params):
        if path == "/intersecciones":
            return {
                "intersecciones": [
                    {
                        "nomenclatura": "ROCA Y APELEG",
                        "ubicacion": {"lat": -42.7680, "lon": -65.0380},
                    }
                ]
            }
        return {}

    monkeypatch.setattr(place_api, "_georef", fake_georef)
    reset_cache()
    item = Listing(
        source="zonaprop",
        source_id="llm-corner",
        url="https://example.com",
        title="Terreno",
        property_type="terreno",
        city="puerto-madryn",
        lat=-42.767277,
        lon=-65.037932,
        extra={"search_city": "puerto-madryn"},
    )
    apply_analysis(
        item,
        {
            "corner_a": "roca",
            "corner_b": "apeleg",
            "foreign": False,
            "notes": "",
        },
    )
    assert abs(item.lat + 42.7680) < 1e-4
    assert item.extra.get("location_kind") == "intersection"


def test_apply_analysis_keeps_saved_approx_if_geocode_misses(monkeypatch):
    from app import place_api
    from app.place_api import reset_cache

    monkeypatch.setattr(place_api, "_georef", lambda *a, **k: {})
    reset_cache()
    item = Listing(
        source="zonaprop",
        source_id="llm-keep",
        url="https://example.com",
        title="Depto",
        property_type="departamento",
        city="puerto-madryn",
        address="Bartolomé Mitre 644",
        lat=-42.767277,
        lon=-65.037932,
        has_exact_location=True,
        extra={"search_city": "puerto-madryn", "location_kind": "exact"},
    )
    apply_analysis(item, {"address_text": "Bartolomé Mitre 644", "foreign": False, "notes": ""})
    assert abs(item.lat + 42.767277) < 1e-5
    assert item.has_exact_location is False
    assert item.extra.get("location_kind") == "approx"


def test_apply_analysis_uses_repeatable_tags(monkeypatch):
    from app.listing_tags import normalize_tag

    assert normalize_tag("Piscina") == "pileta"
    assert normalize_tag("Roca 240") is None
    item = Listing(
        source="zonaprop",
        source_id="llm-tags",
        url="https://example.com",
        title="PH con pileta y parrilla a estrenar",
        property_type="ph",
        city="caba",
        description="Patio, parrilla y pileta. Apto crédito hipotecario.",
        extra={"search_city": "caba"},
    )
    apply_analysis(
        item,
        {
            "tags": ["pileta", "a estrenar", "Roca y Apeleg"],
            "amenities": ["Piscina", "parrilla"],
            "foreign": False,
            "notes": "",
        },
    )
    tags = item.extra.get("tags") or []
    assert "pileta" in tags
    assert "parrilla" in tags
    assert "a estrenar" in tags
    assert "apto crédito" in tags
    assert item.to_public_dict()["tags"]
    assert not any("roca" in str(tag).lower() for tag in tags)


def test_analyze_marks_mortgage_from_text():
    from app.features import analyze

    item = Listing(
        source="zonaprop",
        source_id="llm-5",
        url="https://example.com",
        title="PH apto crédito hipotecario UVA",
        property_type="ph",
        city="caba",
    )
    analyze(item)
    assert item.extra.get("mortgage_credit") is True
    assert item.to_public_dict()["mortgage_credit"] is True


def test_should_publish_shows_placed_listings_before_llm(monkeypatch):
    monkeypatch.setattr("app.llm_enrich.enabled", lambda: True)
    placed = Listing(
        source="zonaprop",
        source_id="llm-6",
        url="https://example.com",
        title="Casa",
        property_type="casa",
        city="caba",
        address="Vicente López 1900",
        lat=-34.59,
        lon=-58.39,
    )
    mark_await_llm(placed)
    assert should_publish(placed) is True
    barrio_pin = Listing(
        source="properati",
        source_id="llm-6c",
        url="https://example.com/barrio",
        title="Casa en Recoleta",
        property_type="casa",
        city="caba",
        address="Recoleta, Capital Federal",
        lat=-34.59,
        lon=-58.39,
    )
    mark_await_llm(barrio_pin)
    assert should_publish(barrio_pin) is True
    lost = Listing(
        source="properati",
        source_id="llm-6b",
        url="https://example.com/x",
        title="Casa en Recoleta",
        property_type="casa",
        city="caba",
    )
    mark_await_llm(lost)
    assert lost.extra.get("await_llm") is True
    assert should_publish(lost) is False
    lost.extra["llm_ready"] = True
    lost.extra["llm_ver"] = 4
    assert should_publish(lost) is True


def test_apply_analysis_uses_place_api_for_foreign_city():
    from app import place_api

    place_api.remember(
        "rosario",
        {"name": "Rosario", "kind": "localidad", "province": "Santa Fe", "lat": -32.95, "lon": -60.64},
    )
    item = Listing(
        source="zonaprop",
        source_id="llm-7",
        url="https://example.com",
        title="Depto en Rosario",
        property_type="departamento",
        city="puerto-madryn",
        extra={"search_city": "puerto-madryn"},
    )
    apply_analysis(
        item,
        {
            "city_label": "Rosario",
            "foreign": False,
            "property_type": "departamento",
            "notes": "",
        },
    )
    assert item.extra.get("llm_place", {}).get("name") == "Rosario"
    assert item.city != "puerto-madryn"


def test_parse_plain_corner_and_street():
    data = parse_plain_locations("Departamento en esquina 25 de Mayo y Belgrano")
    assert data["corners"]
    assert data["corners"][0][0] == "25 de mayo"
    assert data["corners"][0][1] == "belgrano"
    numbered = parse_plain_locations("Belgrano 340")
    assert numbered["street"] == "belgrano"
    assert numbered["number"] == 340


def test_missing_address_goes_to_front_of_llm_queue(monkeypatch):
    from app import llm_enrich

    monkeypatch.setattr(llm_enrich, "enabled", lambda: True)
    monkeypatch.setattr(llm_enrich, "_ensure_workers_locked", lambda: None)
    llm_enrich._urgent.clear()
    llm_enrich._queue.clear()
    llm_enrich._seen.clear()
    located = Listing(
        source="zonaprop",
        source_id="with-street",
        url="https://example.com/ok",
        title="Depto",
        property_type="departamento",
        address="Vicente López 1900, Recoleta",
        lat=-34.59,
        lon=-58.39,
        city="caba",
    )
    lost = Listing(
        source="properati",
        source_id="no-street",
        url="https://example.com/no",
        title="Casa en Recoleta",
        property_type="casa",
        address="Recoleta, Capital Federal",
        lat=-34.59,
        lon=-58.39,
        city="caba",
    )
    llm_enrich.enqueue([located, lost])
    assert list(llm_enrich._urgent)[0] == lost.id
    assert located.id not in llm_enrich._queue
    assert located.id not in llm_enrich._urgent
    assert lost.id not in llm_enrich._queue


def test_missing_street_without_pin_is_not_llm_urgent_if_it_has_address(monkeypatch):
    from app import llm_enrich

    monkeypatch.setattr(llm_enrich, "enabled", lambda: True)
    monkeypatch.setattr(llm_enrich, "_ensure_workers_locked", lambda: None)
    llm_enrich._urgent.clear()
    llm_enrich._queue.clear()
    llm_enrich._seen.clear()
    item = Listing(
        source="argenprop",
        source_id="no-pin",
        url="https://example.com/pin",
        title="Casa",
        property_type="casa",
        address="Roca 2400",
        city="caba",
    )
    llm_enrich.enqueue([item])
    assert item.id not in llm_enrich._queue
    assert item.id not in llm_enrich._urgent


def test_needs_improve_only_when_location_is_incomplete():
    complete = Listing(
        source="zonaprop",
        source_id="ok",
        url="https://example.com/ok",
        title="Depto",
        property_type="departamento",
        address="Mitre 100",
        city="caba",
        lat=-34.6,
        lon=-58.4,
    )
    lost = Listing(
        source="properati",
        source_id="lost",
        url="https://example.com/lost",
        title="Casa",
        property_type="casa",
        address="Recoleta",
        city="caba",
        extra={"await_llm": True},
    )
    assert needs_improve(complete) is False
    assert needs_improve(lost) is True
