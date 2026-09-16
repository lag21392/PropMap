from app.features import combine_areas, extract_areas, fill_areas
from app.models import Listing


def test_extract_areas_sums_uncovered_when_there_is_no_lot():
    covered, total = extract_areas("Casa 80 m² cubiertos y 20 m² descubiertos. Reciclada.")
    assert covered == 80
    assert total == 100


def test_extract_areas_keeps_lot_when_text_has_terreno():
    covered, total = extract_areas(
        "Casa 90 m² cubiertos y 20 m² descubiertos. Terreno 300 m²."
    )
    assert covered == 90
    assert total == 300


def test_fill_areas_depto_copies_covered_to_total():
    item = Listing(
        source="zonaprop",
        source_id="depto-m2",
        url="https://example.com",
        title="Depto 45 m² cubiertos",
        property_type="departamento",
        description="45 m² cubiertos al frente",
        covered_m2=45,
    )
    fill_areas(item)
    assert item.covered_m2 == 45
    assert item.total_m2 == 45


def test_combine_areas_prefers_explicit_lot():
    covered, total = combine_areas(80, 300, 20, property_type="casa")
    assert covered == 80
    assert total == 300
