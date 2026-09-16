from app.layout import classify, classify_listing
from app.models import Listing


def test_studio_title_stays_studio_even_with_bedrooms_one():
    layout = classify(
        title="Oportunidad de Monoambiente a un Precio Excepcional",
        rooms=1,
        bedrooms=1,
        m2=33,
        property_type="departamento",
    )
    assert layout.bucket == "0"
    assert layout.conflict is False


def test_large_open_plan_with_mixed_signals_is_conflict_not_studio():
    layout = classify(
        title="Departamento en Venta Puerto Madryn",
        description="1 dorm. A Estrenar Monoambiente amplio de 63 m2 aprox.",
        rooms=1,
        bedrooms=1,
        m2=63,
        property_type="departamento",
    )
    assert layout.bucket == "1"
    assert layout.conflict is True
    assert layout.confidence == "low"


def test_two_ambientes_are_one_bedroom_bucket():
    item = Listing(
        source="manual",
        source_id="2a",
        url="",
        title="Depto 2 ambientes",
        property_type="departamento",
        bedrooms=2,
        rooms=2,
        covered_m2=42,
    )
    layout = classify_listing(item)
    assert layout.bucket == "1"
    assert layout.label == "2 amb"


def test_two_bedrooms_without_rooms_are_three_ambientes():
    layout = classify(
        title="Casa en venta",
        bedrooms=2,
        m2=90,
        property_type="casa",
    )
    assert layout.rooms == 3
    assert layout.beds == 2
    assert layout.label == "3 amb"


def test_apply_layout_counts_fills_rooms_from_bedrooms():
    from app.layout import apply_layout_counts

    item = Listing(
        source="properati",
        source_id="beds-only",
        url="",
        title="Casa 2 dormitorios",
        property_type="casa",
        bedrooms=2,
    )
    apply_layout_counts(item)
    assert item.rooms == 3


def test_apply_layout_counts_keeps_explicit_ambientes():
    from app.layout import apply_layout_counts

    item = Listing(
        source="zonaprop",
        source_id="named-amb",
        url="",
        title="Depto 2 ambientes",
        property_type="departamento",
        bedrooms=2,
    )
    apply_layout_counts(item)
    assert item.rooms == 2


def test_apply_layout_counts_skips_lots():
    from app.layout import apply_layout_counts

    item = Listing(
        source="properati",
        source_id="lot",
        url="",
        title="Lote",
        property_type="terreno",
        bedrooms=2,
    )
    apply_layout_counts(item)
    assert item.rooms is None
