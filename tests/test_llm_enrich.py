import threading

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
    assert item.barrio == "Sin clasificar"


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
    assert item.barrio == "Sin clasificar"


def test_apply_analysis_stores_province_from_place_api():
    item = Listing(
        source="zonaprop",
        source_id="llm-prov",
        url="https://example.com",
        title="Casa en Trelew",
        property_type="casa",
        city="trelew",
        extra={"search_city": "trelew"},
    )
    apply_analysis(item, {"city_label": "Trelew", "property_type": "casa", "notes": ""})
    place = item.extra.get("llm_place") or {}
    llm = item.extra.get("llm") or {}
    assert "chubut" in str(place.get("province") or "").lower()
    assert "chubut" in str(llm.get("province") or "").lower()


def test_apply_analysis_does_not_keep_a_pin_in_the_river():
    from app.geo import remember_water

    remember_water(-34.63057747, -58.35346204, True)
    remember_water(-34.6358, -58.3622, False)
    item = Listing(
        source="properati",
        source_id="llm-water",
        url="https://www.properati.com.ar/detalle/x",
        title="Terreno en Venta en Boca",
        property_type="terreno",
        city="caba",
        address="Carlos F. Melo 600",
        lat=-34.63057747,
        lon=-58.35346204,
        extra={
            "search_city": "caba",
            "portal_lat": -34.6358,
            "portal_lon": -58.3622,
            "portal_approx": True,
        },
    )
    apply_analysis(
        item,
        {
            "street": "Carlos F. Melo",
            "street_number": 600,
            "address_text": "Carlos F. Melo 600, Buenos Aires",
            "foreign": False,
            "notes": "",
            "geo_tools": {
                "geo": {
                    "ok": True,
                    "lat": -34.63057747,
                    "lon": -58.35346204,
                    "label": "F. Melo 600",
                    "approx": True,
                }
            },
        },
    )
    assert abs(item.lat + 34.6358) < 1e-5
    assert item.has_exact_location is False


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


def test_apply_analysis_fills_rooms_from_bedrooms():
    item = Listing(
        source="properati",
        source_id="llm-beds",
        url="https://example.com",
        title="Casa 2 dormitorios",
        property_type="casa",
        bedrooms=2,
        city="puerto-madryn",
        extra={"search_city": "puerto-madryn"},
    )
    apply_analysis(item, {"property_type": "casa", "bedrooms": 2, "foreign": False})
    assert item.bedrooms == 2
    assert item.rooms == 3


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


def test_apply_analysis_pins_crossing_when_address_is_lote(monkeypatch):
    from app import place_api
    from app.place_api import reset_cache

    def fake_georef(path, params):
        if path == "/direcciones":
            return {"direcciones": []}
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
        source_id="lote-12",
        url="https://example.com/lote",
        title="Terreno lote 12 entrecalles Roca y Apeleg",
        property_type="terreno",
        city="puerto-madryn",
        address="Lote 12",
        lat=-42.767277,
        lon=-65.037932,
        extra={"search_city": "puerto-madryn"},
    )
    apply_analysis(
        item,
        {
            "address_text": "Lote 12",
            "street": "Lote",
            "street_number": 12,
            "corner_a": "roca",
            "corner_b": "apeleg",
            "foreign": False,
            "notes": "",
        },
    )
    assert abs(item.lat + 42.7680) < 1e-4
    assert abs(item.lon + 65.0380) < 1e-4
    assert item.extra.get("location_kind") == "intersection"
    assert "apeleg" in (item.extra.get("intersection") or "")
    public = item.to_public_dict()
    assert public.get("street_number") in {None, "", 0}
    assert public.get("location_kind") == "intersection"


def test_apply_analysis_keeps_saved_pin_and_treats_address_as_exact(monkeypatch):
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
    assert item.has_exact_location is True
    assert item.extra.get("location_kind") == "exact"
    public = item.to_public_dict()
    assert public["location_approx"] is False
    assert public["location_real"] is True


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


def test_apply_analysis_assigns_city_when_listing_is_unassigned():
    from app import place_api

    place_api.remember(
        "trelew",
        {"name": "Trelew", "kind": "localidad", "province": "Chubut", "lat": -43.25, "lon": -65.31},
    )
    item = Listing(
        source="zonaprop",
        source_id="sin-ciudad",
        url="https://example.com",
        title="Casa en Trelew",
        property_type="casa",
        city="fuera",
        extra={"search_city": "fuera"},
    )
    apply_analysis(
        item,
        {
            "city_label": "Trelew",
            "foreign": False,
            "property_type": "casa",
            "covered_m2": 80,
            "uncovered_m2": 20,
            "notes": "",
        },
    )
    assert item.city == "trelew"
    assert item.extra.get("resolved_city") == "trelew"


def test_apply_analysis_does_not_assign_a_barrio_as_city():
    from app import place_api

    place_api.remember(
        "palermo",
        {
            "name": "Palermo",
            "kind": "neighbourhood",
            "province": "Ciudad Autónoma de Buenos Aires",
            "lat": -34.588,
            "lon": -58.430,
        },
    )
    item = Listing(
        source="zonaprop",
        source_id="barrio-no-ciudad",
        url="https://example.com",
        title="Depto en Palermo",
        property_type="departamento",
        city="fuera",
        extra={"search_city": "fuera"},
    )
    apply_analysis(
        item,
        {"city_label": "Palermo", "foreign": False, "property_type": "departamento", "notes": ""},
    )
    assert item.city == "fuera"


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
    assert located.id in llm_enrich._queue
    assert located.id not in llm_enrich._urgent
    assert lost.id not in llm_enrich._queue


def test_llm_queue_caps_rest_and_keeps_room_for_urgent(monkeypatch):
    from app import llm_enrich
    from app.models import Listing

    monkeypatch.setattr(llm_enrich, "enabled", lambda: True)
    monkeypatch.setattr(llm_enrich, "_ensure_workers_locked", lambda: None)
    llm_enrich._urgent.clear()
    llm_enrich._queue.clear()
    llm_enrich._seen.clear()

    def row(n: int, *, lost: bool = False) -> Listing:
        return Listing(
            source="zonaprop",
            source_id=f"{n}",
            url=f"https://example.com/{n}",
            title="Depto",
            property_type="departamento",
            address="" if lost else f"Mitre {100 + n}",
            lat=-42.77,
            lon=-65.04,
            city="puerto-madryn",
        )

    rest = [row(i) for i in range(llm_enrich.QUEUE_CAP + 8)]
    llm_enrich.enqueue(rest)
    assert len(llm_enrich._queue) == llm_enrich.QUEUE_CAP
    assert len(llm_enrich._seen) == llm_enrich.QUEUE_CAP
    lost = row(99, lost=True)
    llm_enrich.enqueue([lost])
    assert lost.id in llm_enrich._urgent
    assert len(llm_enrich._urgent) + len(llm_enrich._queue) == llm_enrich.QUEUE_CAP


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
    assert item.id in llm_enrich._queue
    assert item.id not in llm_enrich._urgent


def test_needs_improve_prioritizes_missing_location_but_also_stale_schema():
    from app.llm_enrich import LLM_SCHEMA

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
    current = Listing(
        source="zonaprop",
        source_id="done",
        url="https://example.com/done",
        title="Depto",
        property_type="departamento",
        address="Mitre 100",
        city="caba",
        extra={"llm_ver": LLM_SCHEMA, "llm_ready": True},
    )
    assert needs_improve(complete) is True
    assert needs_improve(lost) is True
    assert needs_improve(current) is False
    stray = Listing(
        source="zonaprop",
        source_id="fuera",
        url="https://example.com/fuera",
        title="Casa",
        property_type="casa",
        city="fuera",
        extra={"llm_ver": LLM_SCHEMA, "llm_ready": True},
    )
    assert needs_improve(stray) is True
    stray.extra["llm_city_ok"] = True
    assert needs_improve(stray) is False
    broken = Listing(
        source="zonaprop",
        source_id="fix",
        url="https://example.com/fix",
        title="Casa",
        property_type="casa",
        city="caba",
        extra={
            "llm_ver": LLM_SCHEMA,
            "llm_ready": True,
            "data_fixes": ["m² cubiertos irreales para una casa"],
        },
    )
    assert needs_improve(broken) is True
    broken.extra["llm_repair"] = True
    assert needs_improve(broken) is False


def test_analyze_listing_one_shot_without_tools(monkeypatch):
    from app import llm_enrich
    from app.geo import remember_city_polygons
    from app.llm_enrich import analyze_listing

    remember_city_polygons(
        "puerto-madryn",
        [
            {
                "name": "Desembarco",
                "zona": "Zona Sur",
                "lat": -42.7835,
                "lon": -65.0195,
                "ring": [
                    [-42.7875, -65.0265],
                    [-42.7875, -65.0140],
                    [-42.7820, -65.0125],
                    [-42.7795, -65.0160],
                    [-42.7795, -65.0265],
                    [-42.7875, -65.0265],
                ],
            }
        ],
    )
    seen: list[bool] = []

    def fake_chat(messages, *, use_tools=False):
        seen.append(use_tools)
        return {
            "message": {
                "content": (
                    '{"property_type":"departamento","barrio":"Desembarco","street":"Chiquichan",'
                    '"street_number":1250,"mortgage_credit":true,"foreign":false,"orientation":"frente"}'
                )
            }
        }

    monkeypatch.setattr(llm_enrich, "enabled", lambda: True)
    monkeypatch.setattr(llm_enrich, "_chat", fake_chat)
    item = Listing(
        source="zonaprop",
        source_id="one-shot",
        url="https://example.com/x",
        title="Depto Desembarco no apto crédito",
        property_type="departamento",
        address="Chiquichan 1250",
        city="puerto-madryn",
        extra={"search_city": "puerto-madryn"},
    )
    data = analyze_listing(item)
    assert seen == [False]
    assert data is not None
    assert data["barrio"] == "Desembarco"
    assert data["property_type"] == "departamento"
    assert data["mortgage_credit"] is False
    assert data["geo_tools"]["geo"] is None


def test_analyze_listing_does_not_call_remote_places(monkeypatch):
    from app import llm_enrich
    from app.llm_enrich import analyze_listing

    remotes: list[bool] = []

    def fake_places(item, *, remote=True):
        remotes.append(remote)
        return []

    monkeypatch.setattr(llm_enrich, "enabled", lambda: True)
    monkeypatch.setattr(llm_enrich, "_chat", lambda *a, **k: {"message": {"content": '{"property_type":"casa"}'}})
    monkeypatch.setattr("app.place_api.listing_places", fake_places)
    item = Listing(
        source="properati",
        source_id="no-remote",
        url="https://example.com/x",
        title="Terreno en Las Heras",
        property_type="terreno",
        address="Las Heras, Mendoza",
        city="fuera",
        extra={"search_city": "mendoza", "data_fixes": ["tipo corregido a terreno"]},
    )
    analyze_listing(item)
    assert remotes == [False]


def test_enrich_id_does_not_scrape_ficha_itself(monkeypatch):
    from app import detail_fetch, llm_enrich
    from app.models import Listing

    item = Listing(
        source="zonaprop",
        source_id="llm-no-scrape",
        url="https://example.com/ficha",
        title="Depto",
        property_type="departamento",
        city="caba",
        extra={"search_city": "caba", "await_llm": True},
    )
    scraped = []
    analyzed = []
    monkeypatch.setattr("app.store.get_listing", lambda listing_id: item)
    monkeypatch.setattr(
        "app.scrapers.details.enrich_details",
        lambda *a, **k: scraped.append("hit"),
    )
    monkeypatch.setattr(llm_enrich, "analyze_listing", lambda row: analyzed.append(row.id) or {"ok": True})
    monkeypatch.setattr(detail_fetch, "enabled", lambda: True)
    monkeypatch.setattr(detail_fetch, "_ensure_workers_locked", lambda: None)
    detail_fetch._urgent.clear()
    detail_fetch._queue.clear()
    detail_fetch._seen.clear()
    llm_enrich._seen.add(item.id)
    llm_enrich._enrich_id(item.id)
    assert scraped == []
    assert analyzed == []
    assert item.id in detail_fetch._urgent
    assert item.id not in llm_enrich._seen


def test_enrich_id_runs_llm_when_card_already_has_text(monkeypatch):
    from app import detail_fetch, llm_enrich
    from app.models import Listing

    item = Listing(
        source="zonaprop",
        source_id="llm-has-text",
        url="https://example.com/ficha3",
        title="Depto",
        property_type="departamento",
        city="caba",
        description="Departamento en venta con living comedor, cocina y dos dormitorios. " * 3,
        extra={"search_city": "caba", "await_llm": True},
    )
    analyzed = []
    monkeypatch.setattr("app.store.get_listing", lambda listing_id: item)
    monkeypatch.setattr(llm_enrich, "analyze_listing", lambda row: analyzed.append(row.id) or {"ok": True})
    monkeypatch.setattr("app.store.upsert_listings", lambda rows, **k: None)
    monkeypatch.setattr("app.store.upsert_many", lambda rows, **k: None)
    monkeypatch.setattr(llm_enrich, "apply_analysis", lambda *a, **k: None)
    monkeypatch.setattr(llm_enrich, "pin_listing_city", lambda row, **_k: None)
    monkeypatch.setattr(detail_fetch, "enabled", lambda: True)
    monkeypatch.setattr(detail_fetch, "_ensure_workers_locked", lambda: None)
    detail_fetch._urgent.clear()
    detail_fetch._queue.clear()
    detail_fetch._seen.clear()
    llm_enrich._enrich_id(item.id)
    assert analyzed == [item.id]
    assert item.id in detail_fetch._urgent


def test_enrich_id_runs_llm_after_details_already_tried(monkeypatch):
    from app import detail_fetch, llm_enrich
    from app.models import Listing

    item = Listing(
        source="zonaprop",
        source_id="llm-after-try",
        url="https://example.com/ficha2",
        title="Depto",
        property_type="departamento",
        city="caba",
        extra={"search_city": "caba", "await_llm": True},
    )
    analyzed = []
    monkeypatch.setattr("app.store.get_listing", lambda listing_id: item)
    monkeypatch.setattr(llm_enrich, "analyze_listing", lambda row: analyzed.append(row.id) or None)
    monkeypatch.setattr("app.store.upsert_listings", lambda rows, **k: None)
    monkeypatch.setattr("app.store.upsert_many", lambda rows, **k: None)
    monkeypatch.setattr(llm_enrich, "pin_listing_city", lambda row, **_k: None)
    detail_fetch._urgent.clear()
    detail_fetch._queue.clear()
    detail_fetch._seen.clear()
    detail_fetch._seen.add(item.id)
    llm_enrich._enrich_id(item.id)
    assert analyzed == [item.id]


def test_failed_llm_retry_goes_to_back_of_queue(monkeypatch):
    from app import llm_enrich
    from app.models import Listing

    item = Listing(
        source="zonaprop",
        source_id="retry-back",
        url="https://example.com/retry",
        title="Casa en Vicente López 100",
        address="Vicente López 100",
        description="Casa de 3 ambientes con patio.",
        property_type="casa",
        city="fuera",
        details_scraped=True,
        extra={"await_llm": True, "data_fixes": ["geo"]},
    )
    monkeypatch.setattr("app.store.get_listing", lambda listing_id: item)
    monkeypatch.setattr("app.freshness.needs_detail_fetch", lambda _row: False)
    monkeypatch.setattr(llm_enrich, "analyze_listing", lambda _row: None)
    monkeypatch.setattr("app.store.upsert_listings", lambda rows, **k: None)
    monkeypatch.setattr("app.store.upsert_many", lambda rows, **k: None)
    monkeypatch.setattr(llm_enrich, "pin_listing_city", lambda row, **_k: None)
    monkeypatch.setattr(llm_enrich, "_ensure_workers_locked", lambda: None)
    llm_enrich._urgent.clear()
    llm_enrich._queue.clear()
    llm_enrich._seen.clear()
    llm_enrich._skip_until.clear()
    llm_enrich._seen.add(item.id)
    llm_enrich._enrich_id(item.id)
    assert item.id not in llm_enrich._urgent
    assert item.id not in llm_enrich._queue
    assert item.id not in llm_enrich._seen
    assert llm_enrich._skip_until[item.id] > 0


def test_failed_llm_skip_lets_other_listings_enter_queue(monkeypatch):
    from app import llm_enrich
    from app.models import Listing

    stuck = Listing(
        source="zonaprop",
        source_id="stuck-skip",
        url="https://example.com/stuck",
        title="Casa",
        property_type="casa",
        city="fuera",
        extra={"await_llm": True},
    )
    nxt = Listing(
        source="zonaprop",
        source_id="next-ok",
        url="https://example.com/next",
        title="Depto",
        property_type="departamento",
        city="fuera",
        extra={"await_llm": True},
    )
    monkeypatch.setattr(llm_enrich, "enabled", lambda: True)
    monkeypatch.setattr(llm_enrich, "_ensure_workers_locked", lambda: None)
    llm_enrich._urgent.clear()
    llm_enrich._queue.clear()
    llm_enrich._seen.clear()
    llm_enrich._skip_until.clear()
    llm_enrich._skip_until[stuck.id] = 10**12
    llm_enrich.enqueue([stuck, nxt])
    assert stuck.id not in llm_enrich._urgent
    assert stuck.id not in llm_enrich._queue
    assert nxt.id in llm_enrich._urgent or nxt.id in llm_enrich._queue


def test_enrich_gives_up_when_a_listing_hangs(monkeypatch):
    import time

    from app import llm_enrich
    from app.models import Listing

    hung = threading.Event()
    item = Listing(
        source="zonaprop",
        source_id="hang-budget",
        url="https://example.com/hang",
        title="Terreno en Funes",
        property_type="terreno",
        city="fuera",
        extra={"await_llm": True},
    )

    def hang(_row):
        hung.wait(8)
        return None

    monkeypatch.setattr("app.store.get_listing", lambda listing_id: item)
    monkeypatch.setattr("app.freshness.needs_detail_fetch", lambda _row: False)
    monkeypatch.setattr(llm_enrich, "analyze_listing", hang)
    monkeypatch.setattr("app.store.upsert_listings", lambda rows, **k: None)
    monkeypatch.setattr("app.store.upsert_many", lambda rows, **k: None)
    monkeypatch.setattr(llm_enrich, "pin_listing_city", lambda row, **_k: None)
    monkeypatch.setattr(llm_enrich, "ENRICH_BUDGET_SEC", 0.2)
    llm_enrich._skip_until.clear()
    llm_enrich._seen.add(item.id)
    started = time.time()
    llm_enrich._enrich_id(item.id)
    assert time.time() - started < 2.0
    assert item.id not in llm_enrich._busy
    assert llm_enrich._skip_until[item.id] > time.time()
    hung.set()


def test_assign_city_skips_remote_ensure_place(monkeypatch):
    from app import llm_enrich

    def boom(*_a, **_k):
        raise AssertionError("ensure_place no debe correr en el worker LLM")

    monkeypatch.setattr("app.places.ensure_place", boom)
    monkeypatch.setattr("app.place_api.lookup_place", lambda *_a, **_k: None)
    monkeypatch.setattr(llm_enrich, "_city_from_label", lambda _label: None)
    assert llm_enrich._ensure_assigned_city("Chacras del Lago") is None
