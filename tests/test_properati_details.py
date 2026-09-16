from app.models import Listing
from app.scrapers.details import _from_properati
from app.scrapers.properati import _usable_address

DETAIL_HTML = """
<div id="description-text" class="content">Hay propiedades que ofrecen metros cuadrados.
Ubicado sobre Vicente López al 1900, a pasos de Plaza Francia.
</div>
<div class="location-map__location-address-map">Recoleta, Capital Federal</div>
<div>El anunciante prefiere no mostrar la dirección exacta</div>
<script>
    let pageData = {
        mapData: {
            showMap: true,
            adLocationData: {
                coordinates: {
                    latitude: "-34.590238",
                    longitude: "-58.391935"
                },
                province: "Ciudad Autónoma de Buenos Aires",
                locality: "Buenos Aires",
                district: "Ciudad Autónoma de Buenos Aires",
                address: "Calle Vicente López 1900, Recoleta, Comuna 2, Buenos Aires, Ciudad Autónoma de Buenos Aires, C1128, Ciudad Autónoma de Buenos Aires, ARG",
                postcode: ""
            },
            visibility: "approximate",
            enableApproximateArea: true,
        },
    };
</script>
"""

SHUFFLED_HTML = """
<div id="description-text">Casa en Recoleta con terraza.</div>
<script>
mapData: {
    visibility: "accurate",
    address: "Av. Callao 1200, Recoleta, ARG",
    adLocationData: {
        coordinates: { longitude: "-58.392", latitude: "-34.599" }
    }
}
</script>
"""


def _item(**kwargs) -> Listing:
    data = {
        "source": "properati",
        "source_id": "recoleta-1",
        "url": "https://www.properati.com.ar/detalle/x",
        "title": "Casa en Venta en Recoleta",
        "property_type": "casa",
        "address": "Recoleta, Capital Federal",
        "description": "Casa en Venta en Recoleta USD 423.000 Recoleta, Capital Federal 2 dormitorios",
        "city": "caba",
    }
    data.update(kwargs)
    return Listing(**data)


def test_properati_ficha_keeps_street_even_if_map_is_approx():
    item = _item()
    _from_properati(item, DETAIL_HTML)
    assert "Vicente López 1900" in item.address
    assert "Recoleta" in item.address
    assert "Argentina" not in item.address
    assert "vicente" in (item.extra.get("street") or "").lower()
    assert int(item.extra.get("street_number") or 0) == 1900
    assert "Vicente López al 1900" in (item.description or "")
    assert item.lat and abs(item.lat + 34.590238) < 0.001
    assert item.lon and abs(item.lon + 58.391935) < 0.001
    assert item.extra.get("portal_approx") is True
    assert item.extra.get("map_visibility") == "approximate"
    assert item.has_exact_location is False


def test_properati_mapdata_fields_can_appear_in_any_order():
    item = _item(address="Recoleta")
    _from_properati(item, SHUFFLED_HTML)
    assert "Callao 1200" in item.address
    assert item.lat and abs(item.lat + 34.599) < 0.001
    assert item.lon and abs(item.lon + 58.392) < 0.001


def test_usable_address_needs_street_or_number():
    assert _usable_address("Calle Vicente López 1900, Recoleta") is True
    assert _usable_address("Recoleta, Capital Federal") is False
    assert _usable_address("Capital Federal") is False


HIDDEN_MAP_HTML = """
<div id="description-text">A una cuadra del mar, con semivista desde el balcón.</div>
<div>El anunciante prefiere no mostrar la dirección exacta</div>
<script>
mapData: {
    showMap: true,
    adLocationData: {
        coordinates: {
            latitude: "-42.76841",
            longitude: "-65.03592"
        },
        address: "Puerto Madryn, Chubut, ARG"
    },
    visibility: "approximate",
    enableApproximateArea: true,
}
</script>
"""


def test_properati_hidden_address_keeps_approx_map_center():
    from app.scrapers import locate_item

    item = _item(
        source_id="madryn-hidden",
        title="Apartamento en Venta en Puerto Madryn",
        address="Puerto Madryn, Chubut",
        city="puerto-madryn",
        description="A una cuadra del mar. APTO CRÉDITO",
    )
    _from_properati(item, HIDDEN_MAP_HTML)
    assert abs(item.lat + 42.76841) < 1e-5
    assert abs(item.lon + 65.03592) < 1e-5
    assert item.extra.get("portal_approx") is True
    locate_item(item)
    assert abs(item.lat + 42.76841) < 1e-5
    assert item.has_exact_location is False
    assert item.extra.get("location_kind") == "approx"


def test_properati_json_ld_does_not_replace_approx_map():
    from app.scrapers.details import _from_json_ld

    item = _item()
    _from_properati(item, DETAIL_HTML)
    _from_json_ld(
        item,
        """<script type="application/ld+json">
        {"@type":"Residence","geo":{"latitude":-34.6037,"longitude":-58.3816}}
        </script>""",
    )
    assert abs(item.lat + 34.590238) < 0.001
    assert abs(item.lon + 58.391935) < 0.001


VER_MAPA_HTML = """
<div class="location-map">
  <p>El anunciante prefiere no mostrar la dirección exacta</p>
  <button type="button">Ver mapa</button>
  <div class="map-modal">
    <img class="static-map" src="https://maps.googleapis.com/maps/api/staticmap?center=-42.77120,-65.03680&amp;zoom=15&amp;size=640x360">
  </div>
</div>
"""

NAVENT_MAP_HTML = """
<button type="button">Ver mapa</button>
<script>
    const mapLatOf =  "LTQyLjc3MTIwMDAwMDAwMDAw";
    const mapLngOf =  "LTY1LjAzNjgwMDAwMDAwMDAw";
    const avisoInfo = {
        'address': {"name":"Puerto Madryn","visibility":"APPROXIMATE"},
        'visibility' :'APPROXIMATE',
    };
</script>
"""


def test_properati_ver_mapa_static_image_gives_approx_center():
    from app.scrapers import locate_item

    item = _item(
        source_id="35f7-ver-mapa",
        title="Apartamento en Venta en Puerto Madryn",
        address="Puerto Madryn, Chubut",
        city="puerto-madryn",
        url="https://www.properati.com.ar/detalle/14032-32-35f7-85bf7f22d1c8-19e955f-aae4-7024",
    )
    _from_properati(item, VER_MAPA_HTML)
    assert abs(item.lat + 42.77120) < 1e-5
    assert abs(item.lon + 65.03680) < 1e-5
    assert item.extra.get("portal_approx") is True
    assert item.extra.get("portal_map") == "ver_mapa"
    locate_item(item)
    assert item.has_exact_location is False


def test_properati_ver_mapa_reads_page_data_coordinates():
    html = """
    <script>
        let pageData = {
            mapData: {
                showMap: true,
                adLocationData: {
                    coordinates: {
                        latitude: "-42.756297",
                        longitude: "-65.037425"
                    },
                    address: "Calle Ayacucho 597, Puerto Madryn, Biedma, Chubut, ARG",
                    postcode: ""
                },
                visibility: "approximate",
                enableApproximateArea: true,
            },
        };
    </script>
    """
    item = _item(
        address="Puerto Madryn, Chubut",
        city="puerto-madryn",
        url="https://www.properati.com.ar/detalle/14032-32-35f7-85bf7f22d1c8-19e955f-aae4-7024",
    )
    _from_properati(item, html)
    assert abs(item.lat + 42.756297) < 1e-6
    assert abs(item.lon + 65.037425) < 1e-6
    assert "Ayacucho 597" in item.address
    assert item.extra.get("portal_approx") is True
    assert item.extra.get("portal_map") == "ver_mapa"


def test_properati_ver_mapa_reads_navent_encoded_pin():
    item = _item(
        address="Puerto Madryn, Chubut",
        city="puerto-madryn",
        url="https://www.properati.com.ar/detalle/14032-32-35f7-85bf7f22d1c8-19e955f-aae4-7024",
    )
    _from_properati(item, NAVENT_MAP_HTML)
    assert abs(item.lat + 42.7712) < 1e-4
    assert abs(item.lon + 65.0368) < 1e-4
    assert item.extra.get("map_visibility") == "approximate"
    assert item.extra.get("portal_approx") is True
    assert item.has_exact_location is False


ACCURATE_BUT_AREA_HTML = """
<script>
    let pageData = {
        mapData: {
            showMap: true,
            adLocationData: {
                coordinates: {
                    latitude: "-34.6436912",
                    longitude: "-58.36284"
                },
                province: "Ciudad Autónoma de Buenos Aires",
                locality: "Boca",
                address: "Carlos F. Melo 600, Buenos Aires, Argentina",
                postcode: ""
            },
            visibility: "accurate",
            enableApproximateArea: true,
        },
    };
</script>
"""


def test_properati_accurate_with_approx_area_keeps_street_and_is_not_a_door():
    item = _item(
        title="Terreno en Venta en Boca",
        address="Boca",
        source_id="melo-600",
    )
    _from_properati(item, ACCURATE_BUT_AREA_HTML)
    assert "Carlos F. Melo 600" in item.address
    street = (item.extra.get("street") or "").lower()
    assert "carlos" in street and "melo" in street
    assert "boca" not in street
    assert int(item.extra.get("street_number") or 0) == 600
    assert item.extra.get("portal_approx") is True
    assert item.extra.get("portal_approx_area") is True
    assert item.extra.get("portal_exact") is False
    assert item.has_exact_location is False
    assert item.extra.get("portal_lat") == -34.6436912
    assert item.extra.get("portal_lon") == -58.36284

