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


def test_enrich_scores_deals_inside_each_city():
    def house(city, sid, price, lat, lon):
        return Listing(
            source="zonaprop",
            source_id=sid,
            url=f"https://example.com/{sid}",
            title=f"Casa en {city}",
            property_type="casa",
            price=price,
            currency="USD",
            price_usd=price,
            covered_m2=100,
            city=city,
            barrio="Centro",
            lat=lat,
            lon=lon,
        )

    caba = [house("caba", f"c{i}", 200000, -34.603, -58.381) for i in range(6)]
    peers = [house("bahia-blanca", f"b{i}", 90000, -38.719, -62.266) for i in range(5)]
    cheap = house("bahia-blanca", "cheap", 50000, -38.719, -62.266)
    enrich([*caba, *peers, cheap], 1350)
    assert cheap.deal_label == "oportunidad"
    assert cheap.extra.get("deal_score", 0) >= 60
    assert all(row.deal_label != "oportunidad" for row in caba)


def test_enrich_does_not_use_far_same_city_median():
    def house(sid, price, lat, lon, barrio):
        return Listing(
            source="zonaprop",
            source_id=sid,
            url=f"https://example.com/{sid}",
            title=f"Casa en {barrio}",
            property_type="casa",
            price=price,
            currency="USD",
            price_usd=price,
            covered_m2=100,
            city="ciudad-prueba",
            barrio=barrio,
            lat=lat,
            lon=lon,
        )

    dense = [house(f"d{i}", 200000, -34.60, -58.40, "Barrio Norte") for i in range(6)]
    cheap = house("cheap", 70000, -34.72, -58.52, "Barrio Sur")
    enrich([*dense, cheap], 1350)
    assert cheap.deal_label != "oportunidad"
    assert cheap.deal_label == "sin comparables"
    scope = str(cheap.extra.get("comp_scope") or "")
    assert "ciudad" not in scope
    assert cheap.extra.get("peer_count") == 0
