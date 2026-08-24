from __future__ import annotations

from app.models import Listing
from app.scrapers.details import _from_mercadolibre
from app.scrapers.mercadolibre import _from_cards
from app.text_quality import address_quality, clean_portal_address, title_quality

CARD_HTML = """
<div class="poly-card__content">
  <a href="https://casa.mercadolibre.com.ar/MLA-123-propiedad-multifamiliar-_JM">ficha</a>
  <h2 class="poly-component__title">Propiedad Multifamiliar En Venta</h2>
  <span class="poly-component__location">Martin Rivadavia 33, Rawson, Chubut, Argentina, Rawson</span>
  <span class="poly-component__price">US$120.000</span>
  <span class="andes-money-amount__currency-symbol">US$</span>
</div>
"""

DETAIL_HTML = """
<h1 class="ui-pdp-title">Propiedad Multifamiliar En Venta</h1>
<div class="ui-pdp-subtitle">Casa en Venta  |  Publicado hace 6 meses</div>
<div class="ui-vip-location" id="location_and_points">
  <div class="ui-pdp-media ui-vip-location__subtitle">
    <svg></svg><svg></svg><svg></svg>
    <p class="ui-pdp-media__title"><span>Martin Rivadavia 33, Rawson, Chubut, Argentina</span></p>
  </div>
</div>
<script>{"latitude":"-43.3381","longitude":"-65.0472"}</script>
"""


def test_cards_keep_street_address():
    items = _from_cards(CARD_HTML, "casa", "rawson")
    assert len(items) == 1
    item = items[0]
    assert item.source_id == "MLA123"
    assert "Rivadavia" in item.address
    assert "33" in item.address
    assert "Argentina" not in item.address
    assert item.title.startswith("Propiedad Multifamiliar")
    assert item.price == 120000
    assert item.currency == "USD"


def test_details_prefer_street_and_real_title():
    item = Listing(
        source="mercadolibre",
        source_id="MLA123",
        url="https://casa.mercadolibre.com.ar/MLA-123-_JM",
        title="Casa en Venta  |  Publicado hace 6 meses",
        property_type="casa",
        address="Rawson",
        city="rawson",
    )
    _from_mercadolibre(item, DETAIL_HTML)
    assert "Rivadavia" in item.address
    assert "33" in item.address
    assert item.title == "Propiedad Multifamiliar En Venta"
    assert item.lat and abs(item.lat + 43.3381) < 0.001
    assert item.published_at.lower().startswith("casa en venta")


def test_quality_helpers():
    assert address_quality("Martin Rivadavia 33, Rawson") > address_quality("Rawson")
    assert title_quality("Propiedad Multifamiliar En Venta") > title_quality("Casa en Venta | Publicado hace 6 meses")
    assert "Argentina" not in clean_portal_address("Martin Rivadavia 33, U9100 Rawson, Argentina, Rawson")
