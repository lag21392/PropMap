from app.dedupe import collapse_duplicates, photo_key
from app.geo import fingerprint
from app.llm_enrich import should_publish
from app.models import Listing


def _listing(**kwargs) -> Listing:
    data = dict(
        source="zonaprop",
        source_id="a",
        url="https://www.zonaprop.com.ar/propiedades/clasificado/a.html",
        title="Depto 2 amb",
        property_type="departamento",
        price=90000,
        currency="USD",
        price_usd=90000,
        city="caba",
        address="Florida 600",
        barrio="San Nicolás",
        covered_m2=42,
        bedrooms=1,
        has_exact_location=True,
        lat=-34.6018,
        lon=-58.3782,
    )
    data.update(kwargs)
    item = Listing(**data)
    item.fingerprint = fingerprint(item.title, item.address, item.price_usd, item.covered_m2, item.property_type)
    return item


def test_photo_key_ignores_size_and_stock():
    assert photo_key("https://img.zonapropcdn.com/avisos/1/00/12/foo-730x532.jpg") == photo_key(
        "https://img.zonapropcdn.com/avisos/1/00/12/foo-360x266.webp"
    )
    assert photo_key("https://images.unsplash.com/photo-x") == ""


def test_cross_portal_same_address_merges_and_keeps_both_links():
    zp = _listing()
    zp.extra = {"photos": ["https://cdn.example/abc123456.jpg"]}
    zp.description = "Corto"
    pr = _listing(
        source="properati",
        source_id="p1",
        url="https://www.properati.com.ar/detalle/p1",
        title="Departamento en venta Florida 600",
        description="Descripción larga de la unidad en el microcentro con amenities y luz.",
        image="https://img.properati.com/abc123456-360x266.jpg",
        extra={"photos": ["https://img.properati.com/abc123456-360x266.jpg"], "amenities": ["Balcón"]},
    )
    changed = collapse_duplicates([zp, pr])
    assert {item.id for item in changed} == {zp.id, pr.id}
    winner = next(item for item in changed if not item.extra.get("duplicate_of"))
    loser = next(item for item in changed if item.extra.get("duplicate_of"))
    assert loser.extra["duplicate_of"] == winner.id
    assert should_publish(loser) is False
    assert should_publish(winner) is True
    labels = {row["source"] for row in winner.extra["sources"]}
    assert labels == {"zonaprop", "properati"}
    assert "Balcón" in winner.extra["amenities"]
    assert len(winner.description) > len("Corto")


def test_same_building_different_size_is_not_a_duplicate():
    a = _listing(source_id="big", covered_m2=80, bedrooms=3, title="3 amb Florida 600")
    b = _listing(
        source="argenprop",
        source_id="small",
        url="https://www.argenprop.com/x",
        covered_m2=40,
        bedrooms=1,
        title="1 amb Florida 600",
    )
    assert collapse_duplicates([a, b]) == []
    assert not a.extra.get("duplicate_of")
    assert not b.extra.get("duplicate_of")


def test_reused_cover_photo_does_not_merge_other_streets():
    photo = "https://cdn.agency.net/catalogo-portada-999.jpg"
    a = _listing(address="Florida 600", extra={"photos": [photo]})
    b = _listing(
        source="mercadolibre",
        source_id="m1",
        url="https://www.mercadolibre.com.ar/MLA-1",
        address="Humboldt 1800",
        extra={"photos": [photo]},
        lat=-34.588,
        lon=-58.430,
    )
    assert collapse_duplicates([a, b]) == []


def test_similar_photos_nearby_approx_pins_are_same_unit():
    photo = "https://cdn.agency.net/unidad-living-aabb9911.jpg"
    a = _listing(
        has_exact_location=False,
        extra={"photos": [photo]},
    )
    b = _listing(
        source="properati",
        source_id="p-near",
        url="https://www.properati.com.ar/detalle/p-near",
        address="Florida 610",
        has_exact_location=False,
        lat=-34.6018,
        lon=-58.3769,
        extra={"photos": [photo]},
    )
    changed = collapse_duplicates([a, b])
    assert {item.id for item in changed} == {a.id, b.id}
    loser = next(item for item in changed if item.extra.get("duplicate_of"))
    assert should_publish(loser) is False


def test_nearby_photos_different_size_not_merged():
    photo = "https://cdn.agency.net/unidad-living-aabb9911.jpg"
    a = _listing(covered_m2=80, bedrooms=3, extra={"photos": [photo]}, has_exact_location=False)
    b = _listing(
        source="argenprop",
        source_id="otro",
        url="https://www.argenprop.com/otro",
        covered_m2=40,
        bedrooms=1,
        extra={"photos": [photo]},
        has_exact_location=False,
        lat=-34.6018,
        lon=-58.3769,
    )
    assert collapse_duplicates([a, b]) == []
