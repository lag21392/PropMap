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
            "covered_m2": 120,
            "price_usd": 700,
        }
        for _ in range(4)
    ]
    apply_yields([item], comps)
    assert 650 <= item.extra["monthly_rent_usd"] <= 760
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
    assert 620 < rent < 820
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


def test_weekly_hint_with_monthly_price_is_monthly():
    assert infer_period(450, "Depto 2 amb", "", "weekly") == "monthly"
    assert infer_period(90, "Casa por semana", "", "weekly") == "weekly"


def test_two_ambientes_use_one_bedroom_comps():
    item = Listing(
        source="manual",
        source_id="amb-2",
        url="",
        title="Depto 2 ambientes",
        property_type="departamento",
        price_usd=85_000,
        barrio="Centro",
        city="puerto-madryn",
        bedrooms=2,
        rooms=2,
        covered_m2=42,
    )
    comps = []
    for _ in range(4):
        comps.append(
            {
                "city": "puerto-madryn",
                "period": "monthly",
                "property_type": "departamento",
                "bedrooms": 1,
                "barrio": "Centro",
                "covered_m2": 40,
                "price_usd": 400,
            }
        )
    for _ in range(4):
        comps.append(
            {
                "city": "puerto-madryn",
                "period": "monthly",
                "property_type": "departamento",
                "bedrooms": 2,
                "barrio": "Centro",
                "covered_m2": 60,
                "price_usd": 720,
            }
        )
    rent, scope, _n = estimate_rent(comps, item, "monthly")
    assert rent is not None
    assert rent < 520
    assert "2 amb" in scope


def test_large_house_does_not_triple_same_beds_rent():
    small = Listing(
        source="manual",
        source_id="casa-chica",
        url="",
        title="Casa 3 dorm",
        property_type="casa",
        price_usd=120_000,
        barrio="Centro",
        city="puerto-madryn",
        bedrooms=3,
        covered_m2=110,
    )
    big = Listing(
        source="manual",
        source_id="casa-grande",
        url="",
        title="Casa 3 dorm 7 amb",
        property_type="casa",
        price_usd=280_000,
        barrio="Centro",
        city="puerto-madryn",
        bedrooms=3,
        rooms=7,
        covered_m2=320,
    )
    comps = [
        {
            "city": "puerto-madryn",
            "period": "monthly",
            "property_type": "casa",
            "bedrooms": 3,
            "barrio": "Centro",
            "covered_m2": 100,
            "price_usd": 650,
        }
        for _ in range(6)
    ]
    r_small, _, _ = estimate_rent(comps, small, "monthly")
    r_big, scope, _ = estimate_rent(comps, big, "monthly")
    assert r_small is not None and r_big is not None
    assert r_big < r_small * 1.45
    assert r_big < 1100
    assert "7 amb" in scope


def test_monoambiente_uses_studio_comps_not_one_bedroom():
    item = Listing(
        source="manual",
        source_id="mono-1",
        url="",
        title="Oportunidad de Monoambiente a un Precio Excepcional",
        property_type="departamento",
        price_usd=53_000,
        barrio="Centro",
        city="puerto-madryn",
        bedrooms=1,
        rooms=1,
        covered_m2=33,
    )
    comps = []
    for _ in range(4):
        comps.append(
            {
                "city": "puerto-madryn",
                "period": "monthly",
                "property_type": "departamento",
                "bedrooms": 1,
                "barrio": "Centro",
                "covered_m2": 19,
                "title": "Apartamento en Alquiler en Puerto Madryn",
                "price_usd": 292,
            }
        )
    for _ in range(4):
        comps.append(
            {
                "city": "puerto-madryn",
                "period": "monthly",
                "property_type": "departamento",
                "bedrooms": 1,
                "barrio": "Centro",
                "covered_m2": 48,
                "title": "Departamento 1 dormitorio en Puerto Madryn",
                "price_usd": 520,
            }
        )
    rent, scope, _n = estimate_rent(comps, item, "monthly")
    assert rent is not None
    assert 250 <= rent <= 340
    assert "monoamb" in scope


def test_comp_title_without_en_place_is_kept():
    from app.yields import _period_rows

    rows = _period_rows(
        [
            {
                "city": "puerto-madryn",
                "period": "monthly",
                "property_type": "departamento",
                "price_usd": 575,
                "title": "En Alquiler Puerto Madryn Tipo Duplex Amoblado",
                "barrio": "Desembarco",
                "covered_m2": 70,
                "bedrooms": 1,
            }
        ],
        "monthly",
    )
    assert len(rows) == 1


def test_large_mixed_layout_uses_similar_size_not_barrio_mix():
    item = Listing(
        source="zonaprop",
        source_id="desembarco-63",
        url="",
        title="Departamento en Venta Puerto Madryn",
        description="1 dorm. A Estrenar Departamento Monoambiente amplio de 63 m2 aprox.",
        property_type="departamento",
        price_usd=68_000,
        barrio="Desembarco",
        city="puerto-madryn",
        bedrooms=1,
        rooms=1,
        covered_m2=63,
        has_exact_location=True,
        extra={"location_kind": "exact"},
    )
    comps = []
    for _ in range(2):
        for price, m2 in ((455, 50), (455, 65), (519, 62), (487, 54)):
            comps.append(
                {
                    "city": "puerto-madryn",
                    "period": "monthly",
                    "property_type": "departamento",
                    "bedrooms": 1,
                    "barrio": "Sin clasificar",
                    "covered_m2": m2,
                    "title": "Apartamento en Alquiler en Puerto Madryn",
                    "price_usd": price,
                }
            )
    for _ in range(8):
        comps.append(
            {
                "city": "puerto-madryn",
                "period": "monthly",
                "property_type": "departamento",
                "bedrooms": 1,
                "barrio": "Desembarco",
                "title": "Apartamento en Alquiler en Puerto Madryn",
                "price_usd": 700,
            }
        )
    apply_yields([item], comps, persist=False)
    assert item.extra["monthly_rent_usd"] is not None
    assert item.extra["monthly_rent_usd"] < 530
    assert item.extra["layout_conflict"] is True
    assert item.extra["rent_confidence"] == "low"
    assert item.extra["monthly_rent_lo"] <= item.extra["monthly_rent_usd"] <= item.extra["monthly_rent_hi"]
    assert "Desembarco" not in (item.extra.get("rental_month_scope") or "")
    assert "señales mixtas" in (item.extra.get("rental_month_scope") or "")

