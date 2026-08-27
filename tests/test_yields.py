from app.models import Listing
from app.yields import apply_yields, estimate_rent, infer_period, rent_to_usd, rental_score


def test_rent_to_usd_does_not_treat_monthly_ars_as_dollars():
    assert rent_to_usd(650_000, "ARS", 1300) == 500
    assert rent_to_usd(80, "USD", 1300) == 80
    assert rent_to_usd(80_000, "USD", 1300) == 80_000 / 1300


def test_infer_period_night_vs_month():
    assert infer_period(70, "Depto por noche", "", "nightly") == "nightly"
    assert infer_period(450, "Alquiler 2 dorm", "", "monthly") == "monthly"
    assert infer_period(90, "Casa temporaria", "ideal airbnb", "nightly") == "nightly"


def test_rental_score_prefers_tourist_one_bed():
    item = Listing(
        source="manual",
        source_id="1",
        url="",
        title="Depto 1 dorm centro",
        property_type="departamento",
        price_usd=70_000,
        barrio="Centro",
        city="puerto-madryn",
        bedrooms=1,
        covered_m2=42,
        has_exact_location=True,
    )
    score, label, reasons = rental_score(item, 8.2, 16.0, 6, 4, 0.5)
    assert score >= 62
    assert label in {"renta fuerte", "mejor temporal"}
    assert any("temporal" in r or "turistas" in r or "depto" in r for r in reasons)


def test_apply_yields_from_comps():
    item = Listing(
        source="manual",
        source_id="2",
        url="",
        title="Casa 3 dorm",
        property_type="casa",
        price_usd=100_000,
        barrio="Zona Norte",
        city="puerto-madryn",
        bedrooms=3,
        covered_m2=120,
    )
    comps = [
        {
            "city": "puerto-madryn",
            "period": "monthly",
            "property_type": "casa",
            "bedrooms": 3,
            "barrio": "Zona Norte",
            "price_usd": 700,
        }
        for _ in range(4)
    ]
    apply_yields([item], comps)
    assert item.extra["monthly_rent_usd"] == 700
    assert item.extra["monthly_yield_pct"] > 5
    assert "Zona Norte" in (item.extra.get("rental_month_scope") or "")


def test_estimate_rent_scales_with_size_and_zone():
    item = Listing(
        source="manual",
        source_id="3",
        url="",
        title="Casa grande",
        property_type="casa",
        price_usd=140_000,
        barrio="Zona Norte",
        city="puerto-madryn",
        bedrooms=3,
        covered_m2=160,
    )
    comps = []
    for _ in range(4):
        comps.append(
            {
                "city": "puerto-madryn",
                "period": "monthly",
                "property_type": "casa",
                "bedrooms": 3,
                "barrio": "Zona Norte",
                "covered_m2": 100,
                "price_usd": 600,
            }
        )
    for _ in range(4):
        comps.append(
            {
                "city": "puerto-madryn",
                "period": "monthly",
                "property_type": "casa",
                "bedrooms": 3,
                "barrio": "Zona Sur",
                "covered_m2": 100,
                "price_usd": 450,
            }
        )
    rent, scope, n = estimate_rent(comps, item, "monthly")
    assert rent is not None
    assert rent > 600
    assert n >= 3
    assert "Zona Norte" in scope


def test_estimate_rent_without_city_comps_or_size():
    item = Listing(
        source="manual",
        source_id="4",
        url="",
        title="Casa 3 dorm",
        property_type="casa",
        price_usd=80_000,
        barrio="Centro",
        city="trelew",
        rooms=4,
        price_m2=900,
    )
    comps = [
        {
            "city": "puerto-madryn",
            "period": "monthly",
            "property_type": "casa",
            "bedrooms": 3,
            "barrio": "Zona Norte",
            "covered_m2": 110,
            "price_usd": 700,
        }
        for _ in range(5)
    ]
    rent, scope, n = estimate_rent(comps, item, "monthly", sale_m2={"puerto-madryn": 1600, "trelew": 900})
    assert rent is not None
    assert 250 < rent < 700
    assert item.bedrooms is None
    apply_yields([item], comps, sale_m2={"puerto-madryn": 1600, "trelew": 900}, persist=False)
    assert item.extra["monthly_rent_usd"]
    assert item.extra["nightly_usd"]
    assert item.extra["rent_fp"]
    fp = item.extra["rent_fp"]
    apply_yields([item], comps, sale_m2={"puerto-madryn": 1600, "trelew": 900}, persist=False)
    assert item.extra["rent_fp"] == fp
