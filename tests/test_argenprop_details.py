from app.models import Listing
from app.scrapers import locate_item
from app.scrapers.details import _from_argenprop


MAP_LAT = -42.78182
MAP_LON = -65.03802

FICHA_HTML = """
<section class="section-map" data-map-container>
  <h2 class="section-title" id="location">Ubicación</h2>
  <div class="location-container">
    <p class="location-label">
      <span>CALLE FOURNIER , Piso 0, Puerto Madryn, Biedma, Chubut</span>
    </p>
  </div>
  <div class="map-container">
    <div class="leaflet-container"
         data-location-map
         data-url="https://static1.sosiva451.com/Mapas/v3/{z}/{x}/{y}"
         data-latitude="-42,78182"
         data-longitude="-65,03802"
         data-syst="Argenprop"
         data-location="Ficha"
         data-ad="19942803" />
  </div>
</section>
"""

DESC_HTML = """
<section>
  <h2>Descripción</h2>
  <p>Excelente oportunidad. 90 m² cubiertos. Patio: Gran terreno libre de más de 200 m².
  Un espacio ideal para parquizar o instalar una piscina.</p>
</section>
<section>
  <h2>Características</h2>
</section>
""" + FICHA_HTML


def _item(**kwargs) -> Listing:
    data = {
        "source": "argenprop",
        "source_id": "19942803",
        "url": "https://www.argenprop.com/casa-en-venta-en-puerto-madryn-5-ambientes--19942803",
        "title": "Casa en Venta en Puerto Madryn, Biedma",
        "property_type": "casa",
        "address": "CALLE FOURNIER , Piso 0",
        "description": "A una cuadra de Fournier, cercanía a Villarino. 90 m² cubiertos.",
        "city": "puerto-madryn",
        "lat": -42.76921,
        "lon": -65.03851,
        "has_exact_location": False,
        "covered_m2": 90,
        "extra": {"search_city": "puerto-madryn"},
    }
    data.update(kwargs)
    return Listing(**data)


def test_argenprop_ficha_reads_leaflet_map_with_comma_decimals():
    item = _item()
    _from_argenprop(item, FICHA_HTML)
    assert abs(item.lat - MAP_LAT) < 1e-5
    assert abs(item.lon - MAP_LON) < 1e-5
    assert item.extra.get("portal_lat") == item.lat
    assert item.extra.get("portal_lon") == item.lon
    assert item.extra.get("portal_approx") is True
    assert item.extra.get("portal_exact") is False
    assert item.has_exact_location is False
    assert "fournier" in (item.address or "").lower()


def test_argenprop_map_replaces_city_center_and_stays_approx():
    item = _item()
    _from_argenprop(item, FICHA_HTML)
    locate_item(item)
    assert abs(item.lat - MAP_LAT) < 0.002
    assert abs(item.lon - MAP_LON) < 0.002
    assert item.extra.get("location_kind") == "approx"
    assert item.has_exact_location is False
    assert item.extra.get("portal_approx") is True


def test_argenprop_parser_ignores_other_portals():
    item = _item(source="zonaprop", url="https://www.zonaprop.com.ar/x.html")
    _from_argenprop(item, FICHA_HTML)
    assert abs(item.lat + 42.76921) < 0.001
    assert not item.extra.get("portal_map")


def test_argenprop_ficha_keeps_lot_m2_from_description():
    from app.features import fill_areas

    item = _item(total_m2=90)
    _from_argenprop(item, DESC_HTML)
    fill_areas(item)
    assert "200" in (item.description or "")
    assert item.covered_m2 == 90
    assert item.total_m2 == 200
