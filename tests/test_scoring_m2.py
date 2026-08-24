from app.models import Listing
from app.scoring import apply_unit_price, useful_m2


def _depto(**kwargs) -> Listing:
    data = {
        "source": "zonaprop",
        "source_id": "1",
        "url": "https://example.com/1",
        "title": "Dos Ambientes en Venta Zona Sur",
        "property_type": "departamento",
        "price": 66500,
        "currency": "USD",
        "covered_m2": 40,
        "total_m2": 45,
    }
    data.update(kwargs)
    return Listing(**data)


def test_depto_with_covered_m2_gets_unit_price():
    item = apply_unit_price(_depto(), 1350)
    assert item.price_usd == 66500
    assert useful_m2(item) == 40
    assert round(item.price_m2) == 1662


def test_small_studio_covered_m2_counts():
    item = apply_unit_price(_depto(covered_m2=16, total_m2=None, price=65000), 1350)
    assert useful_m2(item) == 16
    assert round(item.price_m2) == 4062


def test_depto_does_not_use_building_footprint_as_m2():
    item = _depto(covered_m2=None, total_m2=945, title="Venta de Terreno apto construcción")
    assert useful_m2(item) is None
    apply_unit_price(item, 1350)
    assert item.price_m2 is None
