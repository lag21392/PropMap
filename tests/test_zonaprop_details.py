from app.models import Listing
from app.scrapers import locate_item
from app.scrapers.details import _from_zonaprop_state


CHIQUI_LAT = -42.7838439
CHIQUI_LON = -65.020025

# ZonaProp cifra el pin del mapa: base64("-42.783843900000000") / base64("-65.020025000000003")
EXACT_HTML = """
<script>
    const mapLatOf =  "LTQyLjc4Mzg0MzkwMDAwMDAwMA==";
    const mapLngOf =  "LTY1LjAyMDAyNTAwMDAwMDAwMw==";
    const avisoInfo = {
        'address': {"name":"Chiquichan","visibility":"EXACT"},
        'mapLat': mapLatOf,
        'mapLng': mapLngOf,
        'visibility' :'EXACT',
    };
</script>
"""

APPROX_HTML = """
<script>
    const mapLatOf =  "LTQyLjc4Mzg0MzkwMDAwMDAwMA==";
    const mapLngOf =  "LTY1LjAyMDAyNTAwMDAwMDAwMw==";
    const avisoInfo = {
        'address': {"name":"Puerto Madryn","visibility":"APPROXIMATE"},
        'visibility' :'APPROXIMATE',
    };
</script>
"""


def _item(**kwargs) -> Listing:
    data = {
        "source": "zonaprop",
        "source_id": "59769173",
        "url": "https://www.zonaprop.com.ar/propiedades/clasificado/veclapin-departamento-en-venta-puerto-madryn-59769173.html",
        "title": "Departamento en Venta Puerto Madryn",
        "property_type": "departamento",
        "address": "Chiquichan",
        "description": "Excelente monoambiente de 63 m² aprox., ubicado en segundo piso. Saldo en 24 cuotas de USD 1925",
        "city": "puerto-madryn",
        "lat": -42.784292554796984,
        "lon": -65.01848250363811,
        "has_exact_location": False,
    }
    data.update(kwargs)
    return Listing(**data)


def test_zonaprop_ficha_reads_exact_map_without_preloaded_state():
    item = _item()
    _from_zonaprop_state(item, EXACT_HTML)
    assert abs(item.lat - CHIQUI_LAT) < 1e-7
    assert abs(item.lon - CHIQUI_LON) < 1e-7
    assert item.extra.get("portal_exact") is True
    assert item.extra.get("portal_approx") is False
    assert item.extra.get("map_visibility") == "exact"
    assert "Chiquichan" in (item.address or "")


def test_zonaprop_exact_pin_stays_on_the_ficha_map():
    item = _item()
    _from_zonaprop_state(item, EXACT_HTML)
    locate_item(item)
    assert item.has_exact_location is True
    assert item.extra.get("location_kind") == "exact"
    assert abs(item.lat - CHIQUI_LAT) < 1e-7
    assert abs(item.lon - CHIQUI_LON) < 1e-7


def test_zonaprop_approximate_map_is_not_marked_exact():
    item = _item(address="Puerto Madryn")
    _from_zonaprop_state(item, APPROX_HTML)
    locate_item(item)
    assert item.extra.get("portal_approx") is True
    assert item.has_exact_location is False


def test_zonaprop_list_keeps_visibility_and_full_address():
    from app.scrapers.zonaprop import _parse

    item = _parse(
        {
            "postingId": "600",
            "url": "/propiedades/florida-600.html",
            "title": "Departamento en venta",
            "descriptionNormalized": "Entre Tucumán y Lavalle. Esquina Florida y Tucumán.",
            "postingLocation": {
                "address": {"name": "Florida 600", "visibility": "EXACT"},
                "neighborhood": {"name": "San Nicolás"},
                "postingGeolocation": {
                    "geolocation": {"latitude": -34.6018, "longitude": -58.3753}
                },
            },
            "priceOperationTypes": [{"prices": [{"amount": 120000, "currency": "USD"}]}],
        },
        "departamento",
        "caba",
    )
    assert item is not None
    assert "Florida 600" in item.address
    assert item.extra.get("portal_exact") is True
    assert item.extra.get("map_visibility") == "exact"
    from app.scrapers import attach_location_facts

    attach_location_facts(item)
    assert item.extra.get("street_number") == 600
    assert item.extra.get("between") or item.extra.get("intersection")
