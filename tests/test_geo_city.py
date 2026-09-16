from app.geo import (
    CITIES,
    can_place_on_map,
    city_for_point,
    in_city_radius,
    listing_fits_city,
    listing_mentions_city,
    location_incomplete,
    parse_street,
    pin_listing_city,
    register_city,
    resolve_city,
)
from app.models import Listing


def test_parse_street_skips_listing_jargon_before_the_address():
    assert parse_street("Pasaje Bruno 50") == ("pasaje bruno", 50)
    assert parse_street("Depto Bartolomé Mitre 644") == ("bartolome mitre", 644)
    blob = "Departamento Moderno en Ubicación Espectacular. Apto Credito Bartolomé Mitre 644"
    assert parse_street(blob) == ("bartolome mitre", 644)
    assert parse_street("Lote 12 entre Roca y Mitre") == ("", None)
    assert parse_street("Lote 12") == ("", None)
    assert parse_street("Soldado de la Independencia 1000")[0] == "soldado de la independencia"


def test_caba_coords_are_not_madryn():
    assert in_city_radius(-34.6037, -58.3816, "puerto-madryn") is False
    assert city_for_point(-34.6037, -58.3816) == "caba"
    assert in_city_radius(-34.588, -58.430, "caba") is True
    assert resolve_city("ciudad autonoma de buenos aires") == "caba"


def test_doradillo_is_still_madryn():
    assert in_city_radius(-42.6440, -65.0641, "puerto-madryn") is True
    assert city_for_point(-42.6440, -65.0641) == "puerto-madryn"


def test_trelew_is_not_madryn():
    assert in_city_radius(-43.2489, -65.3051, "puerto-madryn") is False
    assert city_for_point(-43.2489, -65.3051) == "trelew"


def test_pin_moves_foreign_listing_out_of_madryn():
    item = Listing(
        source="zonaprop",
        source_id="x",
        url="https://example.com",
        title="Depto Microcentro",
        property_type="departamento",
        city="puerto-madryn",
        lat=-34.6066,
        lon=-58.3828,
    )
    assert pin_listing_city(item) is True
    assert item.city != "puerto-madryn"


def test_pin_keeps_ver_mapa_when_barrio_name_collides():
    item = Listing(
        source="properati",
        source_id="ayacucho-map",
        url="https://www.properati.com.ar/detalle/x",
        title="Apartamento en Venta en Puerto Madryn",
        property_type="departamento",
        city="fuera",
        address="Calle Ayacucho 597, Puerto Madryn",
        barrio="Gobernador Fontana",
        lat=None,
        lon=None,
        extra={
            "search_city": "puerto-madryn",
            "portal_lat": -42.756297,
            "portal_lon": -65.037425,
            "portal_approx": True,
            "portal_map": "ver_mapa",
        },
    )
    assert pin_listing_city(item) is True
    assert item.city == "puerto-madryn"
    assert abs(item.lat + 42.756297) < 1e-6
    assert abs(item.lon + 65.037425) < 1e-6
    assert item.has_exact_location is False
    assert listing_fits_city(item, "puerto-madryn") is True


def test_public_row_keeps_ver_mapa_pin_despite_fontana_barrio_name():
    from app.geo import public_row_fits_city

    row = {
        "city": "puerto-madryn",
        "title": "Apartamento en Venta en Puerto Madryn",
        "address": "Calle Ayacucho 597, Puerto Madryn",
        "description": "Barrio Gobernador Fontana",
        "barrio": "Gobernador Fontana",
        "lat": -42.756297,
        "lon": -65.037425,
        "portal_lat": -42.756297,
        "portal_lon": -65.037425,
    }
    assert public_row_fits_city(row, "puerto-madryn") is True


def test_portal_in_pilar_is_not_pinned_to_madryn_chubut():
    item = Listing(
        source="zonaprop",
        source_id="59692183",
        url="https://www.zonaprop.com.ar/propiedades/clasificado/veclcain-x-59692183.html",
        title="Casa en Venta - 5 Ambientes - Mayling Club de Campo",
        property_type="casa",
        city="puerto-madryn",
        address="Chubut al 400",
        lat=-42.7712382,
        lon=-65.0511287,
        has_exact_location=True,
        extra={
            "search_city": "puerto-madryn",
            "portal_lat": -34.4239852,
            "portal_lon": -58.8748175,
            "portal_exact": True,
            "portal_approx": False,
            "street": "Campo Chubut",
            "street_number": 400,
            "location_kind": "exact",
            "pin_kind": "address",
        },
    )
    assert listing_fits_city(item, "puerto-madryn") is False
    pin_listing_city(item)
    assert item.city != "puerto-madryn"
    assert abs(item.lat + 34.4239852) < 1e-5
    assert abs(item.lon + 58.8748175) < 1e-5


def test_public_row_drops_foreign_portal_even_if_geocoded_locally():
    from app.geo import public_row_fits_city

    row = {
        "city": "puerto-madryn",
        "title": "Casa en Venta - 5 Ambientes - Mayling Club de Campo",
        "address": "Chubut al 400",
        "lat": -42.7712382,
        "lon": -65.0511287,
        "portal_lat": -34.4239852,
        "portal_lon": -58.8748175,
        "search_city": "puerto-madryn",
    }
    assert public_row_fits_city(row, "puerto-madryn") is False
    pilar = Listing(
        source="zonaprop",
        source_id="pilar",
        url="https://example.com",
        title="Terreno Lote en Venta. Barrio Privado Pilar Chico.",
        property_type="terreno",
        city="puerto-madryn",
        address="SOR TERESA 730",
        lat=-42.75,
        lon=-65.04,
    )
    escobar = Listing(
        source="zonaprop",
        source_id="esc",
        url="https://example.com",
        title="Barrio Orillas, Puertos / Escobar",
        property_type="casa",
        city="puerto-madryn",
        address="PUERTOS / ESCOBAR",
        lat=-42.76,
        lon=-65.04,
    )
    assert listing_fits_city(pilar, "puerto-madryn") is False
    assert listing_fits_city(escobar, "puerto-madryn") is False
    pin_listing_city(pilar)
    assert pilar.city != "puerto-madryn"
    assert pilar.lat is None
    pin_listing_city(escobar)
    assert escobar.lat is None


def test_trelew_title_is_not_madryn():
    item = Listing(
        source="argenprop",
        source_id="tw",
        url="https://example.com",
        title="Departamento en Venta en Trelew, Rawson",
        property_type="departamento",
        city="puerto-madryn",
        address="Cabot 100",
        lat=-42.772,
        lon=-65.036,
    )
    assert listing_fits_city(item, "puerto-madryn") is False


def test_neuquen_street_in_madryn_stays():
    item = Listing(
        source="zonaprop",
        source_id="nq",
        url="https://example.com",
        title="Departamento en Puerto Madryn",
        property_type="departamento",
        city="puerto-madryn",
        address="Neuquen al 800 Chubut, Argentina",
        lat=-42.7758,
        lon=-65.0412,
    )
    assert listing_fits_city(item, "puerto-madryn") is True


def test_docta_and_canning_stay_out_of_madryn_even_if_title_mentions_it():
    docta = Listing(
        source="properati",
        source_id="docta",
        url="https://example.com",
        title="Lote en Docta, Córdoba en Puerto Madryn",
        property_type="terreno",
        city="puerto-madryn",
        address="Docta, Córdoba",
        barrio="Docta",
        lat=-42.769,
        lon=-65.038,
    )
    canning = Listing(
        source="zonaprop",
        source_id="canning",
        url="https://example.com",
        title="Casa en Canning, Ezeiza. Búsqueda Puerto Madryn",
        property_type="casa",
        city="puerto-madryn",
        address="Canning, Esteban Echeverría",
        lat=-42.772,
        lon=-65.041,
    )
    assert listing_fits_city(docta, "puerto-madryn") is False
    assert listing_fits_city(canning, "puerto-madryn") is False
    pin_listing_city(docta)
    pin_listing_city(canning)
    assert docta.city != "puerto-madryn"
    assert canning.city != "puerto-madryn"
    assert docta.lat is None
    assert canning.lat is None


def test_catalog_dummy_pin_is_not_madryn():
    item = Listing(
        source="zonaprop",
        source_id="dummy",
        url="https://example.com",
        title="Terreno en venta",
        property_type="terreno",
        city="puerto-madryn",
        lat=-36.252246,
        lon=-61.027394,
    )
    assert listing_fits_city(item, "puerto-madryn") is False


def test_cordoba_and_costa_loteos_are_not_madryn():
    falda = Listing(
        source="argenprop",
        source_id="falda",
        url="https://example.com",
        title="Apartamento en Venta en La Falda",
        property_type="departamento",
        city="puerto-madryn",
        address="La Falda, Córdoba",
        lat=-42.769,
        lon=-65.038,
    )
    costa = Listing(
        source="argenprop",
        source_id="costa",
        url="https://example.com",
        title="Casa en Venta en La Costa",
        property_type="casa",
        city="puerto-madryn",
        address="La Costa, Catamarca",
        lat=-42.77,
        lon=-65.04,
    )
    pato = Listing(
        source="zonaprop",
        source_id="pato",
        url="https://example.com",
        title="Lotes en Venta en El Pato - Berazategui - Financiación",
        property_type="terreno",
        city="puerto-madryn",
        address="Parque Bonito - Loteo en El Pato",
        lat=-42.768,
        lon=-65.037,
    )
    assert listing_fits_city(falda, "puerto-madryn") is False
    assert listing_fits_city(costa, "puerto-madryn") is False
    assert listing_fits_city(pato, "puerto-madryn") is False
    pin_listing_city(falda)
    pin_listing_city(costa)
    pin_listing_city(pato)
    assert falda.lat is None
    assert costa.lat is None
    assert pato.lat is None


def test_fuera_keeps_invented_madryn_pin_off_the_map():
    item = Listing(
        source="zonaprop",
        source_id="ghost",
        url="https://example.com",
        title="Terreno Lote en Venta. Barrio Privado Pilar Chico. Pilar, Zona Norte",
        property_type="terreno",
        city="fuera",
        address="Pilar Chico",
        barrio="Pilar",
        lat=-42.769,
        lon=-65.038,
        has_exact_location=False,
    )
    assert pin_listing_city(item) is True
    assert item.city == "fuera"
    assert item.lat is None
    assert item.lon is None


def test_pin_clears_coords_when_listing_leaves_known_city():
    item = Listing(
        source="zonaprop",
        source_id="lost",
        url="https://example.com",
        title="Campo en la estepa",
        property_type="terreno",
        city="puerto-madryn",
        lat=-20.0,
        lon=-50.0,
    )
    assert pin_listing_city(item) is True
    assert item.city == "fuera"
    assert item.lat is None


def test_fuera_madryn_title_without_en_returns_to_madryn():
    item = Listing(
        source="zonaprop",
        source_id="duplex-madryn",
        url="https://example.com",
        title="Venta Dúplex Puerto Madryn. 2 Dorm.. Cochera. Equipado.",
        property_type="ph",
        city="fuera",
        address="Marcos A. Zar 1928",
    )
    assert pin_listing_city(item) is True
    assert item.city == "puerto-madryn"


def test_fuera_dash_location_returns_to_madryn():
    item = Listing(
        source="argenprop",
        source_id="lote-dash",
        url="https://example.com",
        title="Terrenos/fracciones/loteos - Terrenos - Puerto Madryn",
        property_type="terreno",
        city="fuera",
        address="Lote 12,5 x 37,37 Zona Comercial Solana",
    )
    assert pin_listing_city(item) is True
    assert item.city == "puerto-madryn"


def test_pin_keeps_chiquichan_in_madryn():
    item = Listing(
        source="zonaprop",
        source_id="y",
        url="https://example.com",
        title="Depto Chiquichan",
        property_type="departamento",
        city="puerto-madryn",
        lat=-42.7838439,
        lon=-65.020025,
    )
    pin_listing_city(item)
    assert item.city == "puerto-madryn"


def test_pin_does_not_exile_when_city_catalog_is_empty():
    item = Listing(
        source="zonaprop",
        source_id="catalog-miss",
        url="https://example.com",
        title="Departamento en Venta Puerto Madryn",
        property_type="departamento",
        city="puerto-madryn",
        address="Chiquichan",
        lat=-42.7838439,
        lon=-65.020025,
        has_exact_location=True,
    )
    saved = dict(CITIES)
    CITIES.clear()
    try:
        pin_listing_city(item)
        assert item.city == "puerto-madryn"
        assert item.lat == -42.7838439
    finally:
        CITIES.update(saved)


def test_location_incomplete_if_barrio_only_or_no_pin():
    barrio = Listing(
        source="properati",
        source_id="barrio",
        url="https://example.com",
        title="Casa en Recoleta",
        property_type="casa",
        address="Recoleta, Capital Federal",
        lat=-34.59,
        lon=-58.39,
        city="caba",
    )
    street = Listing(
        source="zonaprop",
        source_id="street",
        url="https://example.com",
        title="Casa",
        property_type="casa",
        address="Vicente López 1900",
        lat=-34.59,
        lon=-58.39,
        city="caba",
    )
    no_pin = Listing(
        source="argenprop",
        source_id="nopin",
        url="https://example.com",
        title="Casa",
        property_type="casa",
        address="Roca 2400",
        city="caba",
    )
    lost = Listing(
        source="properati",
        source_id="lost",
        url="https://example.com",
        title="Casa en venta",
        property_type="casa",
        city="caba",
    )
    corner = Listing(
        source="zonaprop",
        source_id="corner",
        url="https://example.com",
        title="Terreno",
        property_type="terreno",
        extra={"intersection": "Roca y Apeleg", "location_kind": "intersection"},
        lat=-42.77,
        lon=-65.04,
        city="puerto-madryn",
    )
    assert location_incomplete(barrio) is True
    assert can_place_on_map(barrio) is True
    assert location_incomplete(street) is False
    assert can_place_on_map(street) is True
    assert location_incomplete(no_pin) is False
    assert can_place_on_map(no_pin) is True
    assert location_incomplete(lost) is True
    assert can_place_on_map(lost) is False
    assert location_incomplete(corner) is False
    assert can_place_on_map(corner) is True
    exact_pin = Listing(
        source="zonaprop",
        source_id="gps",
        url="https://example.com",
        title="Casa en Recoleta",
        property_type="casa",
        address="Recoleta, Capital Federal",
        lat=-34.59,
        lon=-58.39,
        city="caba",
        has_exact_location=True,
        extra={"location_kind": "exact"},
    )
    assert can_place_on_map(exact_pin) is True


def test_avenida_cordoba_in_caba_is_not_the_city_cordoba():
    item = Listing(
        source="zonaprop",
        source_id="av-cba",
        url="https://example.com",
        title="Departamento 3 Amb C/ Dep Apto Profesional Retiro",
        property_type="departamento",
        city="cordoba",
        address="Avenida Córdoba 1300",
        barrio="Zona Norte",
        lat=-34.5991237,
        lon=-58.3860025,
        extra={"search_city": "cordoba"},
    )
    assert listing_mentions_city(item, "cordoba") is False
    assert listing_fits_city(item, "cordoba") is False


def test_madryn_llao_llao_lote_is_not_cordoba():
    item = Listing(
        source="zonaprop",
        source_id="llao",
        url="https://example.com",
        title="Lote en Solana - Llao Llao E/ Verbena y Cortaderas",
        property_type="terreno",
        city="puerto-madryn",
        address="Llao llao 4300",
        barrio="Estilo Solana",
        lat=-42.8144348,
        lon=-65.031671,
        extra={"search_city": "puerto-madryn"},
    )
    assert listing_fits_city(item, "cordoba") is False
    assert listing_fits_city(item, "puerto-madryn") is True


def test_barrio_named_cordoba_far_away_is_not_the_city():
    added = "cordoba" not in CITIES
    if added:
        register_city(
            "cordoba",
            label="Córdoba",
            lat=-31.4201,
            lon=-64.1888,
            province="cordoba",
            radius_km=28,
            builtin=True,
        )
    item = Listing(
        source="zonaprop",
        source_id="grun",
        url="https://example.com",
        title="Oport! Venta Casa 4 Dorm, Quincho, Garaje, Patio - Ing. Grun",
        property_type="casa",
        city="cordoba",
        address="CONSTITUCION 2140. Entre Jose hernandez y Lavalle",
        barrio="Córdoba",
        lat=-46.45387570155754,
        lon=-67.53304476721871,
        extra={"search_city": "cordoba"},
    )
    try:
        assert listing_fits_city(item, "cordoba") is False
    finally:
        if added:
            CITIES.pop("cordoba", None)


def test_foreign_outline_is_ignored_and_radius_still_works():
    from app.geo import city_outline_rings, clear_city_polygons, remember_city_outline, same_place_ids

    register_city(
        "ciudad-prueba",
        label="Ciudad Prueba",
        lat=-31.42,
        lon=-64.18,
        radius_km=25,
        province="otra-provincia",
        builtin=False,
    )
    register_city(
        "barrio-vecino",
        label="Barrio Vecino",
        lat=-31.43,
        lon=-64.19,
        radius_km=8,
        province="otra-provincia",
        builtin=False,
    )
    caba_ring = [
        [-34.70, -58.53],
        [-34.70, -58.33],
        [-34.52, -58.33],
        [-34.52, -58.53],
        [-34.70, -58.53],
    ]
    remember_city_outline("ciudad-prueba", [caba_ring])
    try:
        assert city_outline_rings("ciudad-prueba") == []
        assert in_city_radius(-31.42, -64.18, "ciudad-prueba") is True
        assert in_city_radius(-34.60, -58.40, "ciudad-prueba") is False
        local = Listing(
            source="zonaprop",
            source_id="local-1",
            url="https://example.com/local",
            title="Casa en el centro",
            property_type="casa",
            city="ciudad-prueba",
            address="San Martín 100",
            lat=-31.421,
            lon=-64.181,
            extra={"search_city": "ciudad-prueba"},
        )
        leak = Listing(
            source="zonaprop",
            source_id="leak-1",
            url="https://example.com/caba",
            title="Departamento 2 Ambientes en Barrio Norte",
            property_type="departamento",
            city="ciudad-prueba",
            address="Norte Anchorena 800",
            barrio="Balvanera",
            lat=-34.598,
            lon=-58.408,
            extra={"search_city": "ciudad-prueba"},
        )
        assert listing_fits_city(local, "ciudad-prueba", remote=False) is True
        assert listing_fits_city(leak, "ciudad-prueba", remote=False) is False
        assert "barrio-vecino" in same_place_ids("ciudad-prueba")
    finally:
        CITIES.pop("ciudad-prueba", None)
        CITIES.pop("barrio-vecino", None)
        clear_city_polygons("ciudad-prueba")


def test_nearby_loteo_does_not_swallow_the_city():
    from app.geo import same_place_ids
    from app.places import _adopt_existing_city, ensure_place

    register_city(
        "loteo-local",
        label="Loteo Local",
        lat=-31.42,
        lon=-64.19,
        radius_km=25,
        province="otra-provincia",
        builtin=False,
    )
    try:
        parsed = {
            "id": "ciudad-vista",
            "label": "Ciudad Vista",
            "lat": -31.416,
            "lon": -64.183,
            "addresstype": "localidad",
        }
        assert _adopt_existing_city(parsed) == "ciudad-vista"
        cid = ensure_place(
            city="ciudad-vista",
            query="ciudad vista",
            label="Ciudad Vista",
            lat=-31.416,
            lon=-64.183,
            province="otra-provincia",
        )
        assert cid == "ciudad-vista"
        assert "ciudad-vista" in CITIES
        item = Listing(
            source="zonaprop",
            source_id="loteo-1",
            url="https://example.com/loteo",
            title="Lote en el loteo",
            property_type="terreno",
            city="loteo-local",
            lat=-31.421,
            lon=-64.191,
            extra={"search_city": "loteo-local"},
        )
        assert "loteo-local" in same_place_ids("ciudad-vista")
        assert listing_fits_city(item, "ciudad-vista", remote=False) is True
    finally:
        CITIES.pop("ciudad-vista", None)
        CITIES.pop("loteo-local", None)


def test_offset_by_number_does_not_walk_into_remembered_water():
    from app.geo import offset_by_number, offset_meters, remember_water

    start = (-34.6276, -58.3535)
    south = offset_meters(start[0], start[1], south=600 * 0.55)
    remember_water(south[0], south[1], True)
    out = offset_by_number(start[0], start[1], 600, "caba")
    assert (round(out[0], 5), round(out[1], 5)) != (round(south[0], 5), round(south[1], 5))


def test_drop_water_pin_restores_land_portal():
    from app.geo import drop_water_pin, remember_water
    from app.models import Listing

    remember_water(-34.63057747, -58.35346204, True)
    remember_water(-34.6358, -58.3622, False)
    item = Listing(
        source="properati",
        source_id="melo-water",
        url="https://www.properati.com.ar/detalle/x",
        title="Terreno en Venta en Boca",
        property_type="terreno",
        city="caba",
        address="Carlos F. Melo 600",
        barrio="La Boca",
        lat=-34.63057747,
        lon=-58.35346204,
        extra={
            "search_city": "caba",
            "portal_lat": -34.6358,
            "portal_lon": -58.3622,
            "street": "Carlos F. Melo",
            "street_number": 600,
        },
    )
    assert drop_water_pin(item) is True
    assert abs(item.lat + 34.6358) < 1e-6
    assert item.has_exact_location is False
    assert item.extra.get("location_kind") == "approx"


def test_outline_rejects_point_inside_old_radius():
    from app.geo import clear_city_polygons, remember_city_outline

    register_city(
        "ciudad-limite",
        label="Ciudad Límite",
        lat=-31.42,
        lon=-64.18,
        radius_km=25,
        province="otra-provincia",
        builtin=False,
    )
    ring = [
        [-31.40, -64.20],
        [-31.40, -64.16],
        [-31.44, -64.16],
        [-31.44, -64.20],
        [-31.40, -64.20],
    ]
    remember_city_outline("ciudad-limite", [ring])
    try:
        assert in_city_radius(-31.42, -64.18, "ciudad-limite") is True
        assert in_city_radius(-31.32, -64.18, "ciudad-limite") is False
        leak = Listing(
            source="zonaprop",
            source_id="fuera-limite",
            url="https://example.com/fuera",
            title="Casa en las afueras",
            property_type="casa",
            city="ciudad-limite",
            lat=-31.32,
            lon=-64.18,
            extra={"search_city": "ciudad-limite"},
        )
        assert listing_fits_city(leak, "ciudad-limite", remote=False) is False
    finally:
        CITIES.pop("ciudad-limite", None)
        clear_city_polygons("ciudad-limite")
