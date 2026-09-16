from app.geo import STREETS, locate
from app.models import Listing
from app.scrapers import _keep_portal_map_pin, detect_type, locate_item


def test_lote_title_is_terreno_even_if_description_says_casa():
    text = (
        "Lote en Ventallao Llao E/ Verbena y Cortaderasprecio U$d33.000 "
        "https://www.zonaprop.com.ar/propiedades/clasificado/vecltrin-lote-en-venta-58917239.html "
        "Excelente lote. Ideal para construir una casa. A metros de la Plaza Principal."
    )
    assert detect_type(text, "casa") == "terreno"


def test_casa_sobre_lote_stays_casa():
    text = "Casa en Venta en El Doradillo Puerto Madryn Sobre Lote de 2 Hectáreas"
    assert detect_type(text, "") == "casa"


def test_lote_with_proyecto_de_casa_is_terreno():
    text = "Lote (con proyecto de casa a terminar de 90m2 cub que consta de 2 dormitorios)"
    assert detect_type(text, "casa") == "terreno"


def test_portal_exact_beats_street_name_without_number():
    STREETS.append(("llao llao", -42.77855, -65.02582, "Villa del Parque"))
    try:
        item = Listing(
            source="zonaprop",
            source_id="58917239",
            url="https://www.zonaprop.com.ar/propiedades/clasificado/vecltrin-lote-en-ventallao-llao-e-verbena-y-cortaderasprecio-58917239.html",
            title="Lote en Ventallao Llao E/ Verbena y Cortaderasprecio U$d33.000",
            property_type="casa",
            address="Llao Llao",
            description="Excelente lote ubicado en calle llao llao entre Verbena y Cortaderas del barrio Solana.",
            city="puerto-madryn",
            lat=-42.77855,
            lon=-65.02582,
            has_exact_location=True,
            extra={
                "location_kind": "exact",
                "pin_kind": "address",
                "street": "Llao Llao",
                "portal_lat": -42.8196147,
                "portal_lon": -65.03254857,
                "portal_exact": True,
                "map_visibility": "exact",
                "search_city": "puerto-madryn",
            },
        )
        locate_item(item)
        _keep_portal_map_pin(item)
        assert detect_type(f"{item.title} {item.url} {item.description}", item.property_type) == "terreno"
        assert abs(item.lat - -42.8196147) < 0.002
        assert abs(item.lon - -65.03254857) < 0.002
        assert item.has_exact_location is True
        assert (item.extra or {}).get("pin_kind") == "saved"
    finally:
        STREETS[:] = [row for row in STREETS if row[0] != "llao llao"]


def test_locate_street_name_only_is_not_exact_without_portal():
    STREETS.append(("llao llao", -42.77855, -65.02582, "Villa del Parque"))
    try:
        _barrio, _zona, _lat, _lon, exact, kind = locate(
            "zonaprop:x",
            None,
            None,
            "Lote en Llao Llao",
            "Llao Llao",
            "",
            city="puerto-madryn",
        )
        assert exact is False
        assert kind == "approx"
    finally:
        STREETS[:] = [row for row in STREETS if row[0] != "llao llao"]
