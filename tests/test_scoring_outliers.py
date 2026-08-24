from app.models import Listing
from app.scoring import enrich, unrealistic_unit_price


def _lot(**kwargs) -> Listing:
    data = {
        "source": "properati",
        "source_id": "lot",
        "url": "https://example.com/lot",
        "title": "Terreno en Venta en Puerto Madryn",
        "property_type": "terreno",
        "price": 28000,
        "currency": "USD",
        "total_m2": 384,
        "city": "puerto-madryn",
        "barrio": "Fontana",
        "zona": "Oeste residencial",
    }
    data.update(kwargs)
    return Listing(**data)


def test_cheap_urban_lot_is_not_unrealistic():
    item = _lot()
    item.price_usd = 28000
    item.price_m2 = 28000 / 384
    assert unrealistic_unit_price(item) is False


def test_tiny_unit_price_is_unrealistic():
    item = _lot(price=500, total_m2=384)
    item.price_usd = 500
    item.price_m2 = 500 / 384
    assert unrealistic_unit_price(item) is True


def test_enrich_marks_cheap_lot_as_deal_not_outlier():
    peers = [
        _lot(source_id=str(i), price=price, total_m2=380)
        for i, price in enumerate([55000, 60000, 62000, 58000, 70000, 65000, 59000, 61000], start=2)
    ]
    cheap = _lot(source_id="cheap")
    enrich([cheap, *peers], 1350)
    assert cheap.deal_label != "revisar (outlier)"
    assert cheap.deal_label != "revisar (dato raro)"
    assert cheap.extra.get("is_outlier") is False
    assert cheap.deal_label in {"oportunidad", "bueno"}
