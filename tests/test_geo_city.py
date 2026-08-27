from app.geo import (
    CITIES,
    can_place_on_map,
    city_for_point,
    in_city_radius,
    listing_fits_city,
    location_incomplete,
    parse_street,
    pin_listing_city,
    resolve_city,
)
from app.models import Listing


def test_parse_street_skips_listing_jargon_before_the_address():
    assert parse_street("Pasaje Bruno 50") == ("pasaje bruno", 50)
    assert parse_street("Depto Bartolomé Mitre 644") == ("bartolome mitre", 644)
    blob = "Departamento Moderno en Ubicación Espectacular. Apto Credito Bartolomé Mitre 644"
    assert parse_street(blob) == ("bartolome mitre", 644)


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


def test_pilar_and_escobar_are_not_madryn():
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
    assert pin_listing_city(item) is False
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
        assert pin_listing_city(item) is False
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
