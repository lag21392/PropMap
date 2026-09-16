from app.geo import (
    apply_recovered_location,
    barrio_from_pin,
    recovered_location_overlay,
    remember_city_polygons,
)
from app.llm_enrich import apply_analysis
from app.models import Listing


CHIQUI_LAT = -42.7838439
CHIQUI_LON = -65.020025

DESEMBARCO_RING = [
    [-42.7875, -65.0265],
    [-42.7875, -65.0140],
    [-42.7820, -65.0125],
    [-42.7795, -65.0160],
    [-42.7795, -65.0265],
    [-42.7875, -65.0265],
]


def _inject_desembarco() -> None:
    remember_city_polygons(
        "puerto-madryn",
        [
            {
                "name": "Desembarco",
                "zona": "Zona Sur",
                "lat": -42.7835,
                "lon": -65.0195,
                "ring": DESEMBARCO_RING,
            }
        ],
    )


def _depto_68k(**extra) -> Listing:
    payload = {
        "llm": {"street": "Chiquichan", "street_number": 0},
        "llm_geo": {"lat": CHIQUI_LAT, "lon": CHIQUI_LON, "label": "CHIQUICHAN 1250", "approx": False},
        "portal_exact": True,
        "location_kind": "exact",
        "search_city": "puerto-madryn",
    }
    payload.update(extra)
    return Listing(
        source="zonaprop",
        source_id="59769173",
        url="https://www.zonaprop.com.ar/propiedades/clasificado/veclapin-departamento-en-venta-puerto-madryn-59769173.html",
        title="Depto Apto crédito Ubicación real",
        property_type="departamento",
        price=68000,
        currency="USD",
        price_usd=68000,
        address="Chiquichan",
        barrio="Un barrio inventado",
        city="puerto-madryn",
        lat=CHIQUI_LAT,
        lon=CHIQUI_LON,
        covered_m2=63,
        bedrooms=1,
        has_exact_location=True,
        extra=payload,
    )


def test_recovers_street_number_from_llm_geo_not_square_meters():
    item = _depto_68k()
    loc = recovered_location_overlay(item)
    assert loc["street"].lower() == "chiquichan"
    assert loc["street_number"] == 1250
    assert "1250" in loc["address"]
    assert "63" not in loc["address"]
    apply_recovered_location(item)
    public = item.to_public_dict()
    assert public["street_number"] == 1250
    assert "1250" in public["address"]
    assert public["address"] != "—"


def test_rejects_installment_number_as_street_height():
    item = _depto_68k(
        llm_geo={"label": "CHIQUICHAN 24, Biedma, Chubut", "approx": False},
    )
    item.description = "Monoambiente de 63 m². Saldo en 24 cuotas de USD 1925"
    loc = recovered_location_overlay(item)
    assert loc["street_number"] != 24
    assert "24" not in (loc["address"] or "")


def test_lote_number_is_not_a_street_height_when_there_are_cross_streets():
    item = Listing(
        source="zonaprop",
        source_id="lote-12",
        url="https://example.com/lote",
        title="Terreno lote 12",
        property_type="terreno",
        address="Lote 12",
        city="puerto-madryn",
        extra={
            "street": "Lote",
            "street_number": 12,
            "intersection": "Roca y Apeleg",
            "llm": {"address_text": "Lote 12", "street": "Lote", "street_number": 12, "corner_a": "Roca", "corner_b": "Apeleg"},
            "search_city": "puerto-madryn",
        },
    )
    loc = recovered_location_overlay(item)
    assert loc["street_number"] in {None, ""}
    assert (loc["street"] or "").lower() != "lote"
    assert "apeleg" in (loc["intersection"] or "").lower()
    apply_recovered_location(item)
    assert (item.extra or {}).get("street_number") in {None, ""}
    public = item.to_public_dict()
    assert public.get("street_number") in {None, "", 0}
    assert "apeleg" in (public.get("intersection") or "").lower()


def test_plot_address_ignores_false_geocode_label():
    item = Listing(
        source="zonaprop",
        source_id="lote-geo",
        url="https://example.com/lote-geo",
        title="Lote 12 Los Robles",
        property_type="terreno",
        address="lote 12",
        city="puerto-madryn",
        extra={
            "street": "Lote",
            "street_number": 12,
            "between": "mutista y tomillo",
            "llm_geo": {"lat": -42.77, "lon": -65.02, "label": "J PARRY LOVE 12, Biedma, Chubut", "approx": False},
            "geo_label": "J PARRY LOVE 12",
            "search_city": "puerto-madryn",
        },
    )
    loc = recovered_location_overlay(item)
    assert "parry" not in (loc.get("street") or "").lower()
    assert "parry" not in (loc.get("address") or "").lower()
    assert loc["street_number"] in {None, ""}


def test_barrio_comes_from_pin_polygon_not_listing_text():
    _inject_desembarco()
    item = _depto_68k()
    name, zona = barrio_from_pin(item.lat, item.lon, item.city)
    assert name == "Desembarco"
    public = item.to_public_dict()
    assert public["barrio"] == "Desembarco"
    assert public["barrio"] != "Un barrio inventado"


def test_llm_barrio_is_not_copied_onto_the_listing():
    _inject_desembarco()
    item = Listing(
        source="zonaprop",
        source_id="llm-barrio",
        url="https://example.com",
        title="Depto",
        property_type="departamento",
        city="puerto-madryn",
        lat=CHIQUI_LAT,
        lon=CHIQUI_LON,
        extra={"search_city": "puerto-madryn"},
    )
    apply_analysis(item, {"barrio": "Barrio Inventado Por El Portal", "foreign": False, "notes": ""})
    assert item.barrio != "Barrio Inventado Por El Portal"
    assert item.to_public_dict()["barrio"] == "Desembarco"
