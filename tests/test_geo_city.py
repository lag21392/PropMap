from app.geo import city_for_point, in_city_radius, listing_fits_city, pin_listing_city
from app.models import Listing


def test_caba_coords_are_not_madryn():
    assert in_city_radius(-34.6037, -58.3816, "puerto-madryn") is False
    assert city_for_point(-34.6037, -58.3816) == "microcentro-caba"


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
