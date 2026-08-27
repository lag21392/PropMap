from app.places import listed_cities
from app.geo import (
    apply_city_extent,
    city_slug as geo_city_slug,
    in_city_radius,
    barrio_from_address,
    infer_barrio,
    listing_fits_city,
    listing_mentions_city,
    remember_barrio,
    resolve_city,
    same_place_ids,
)
from app.models import Listing
from app.scrapers.urls import city_slug, mercadolibre_urls, zonaprop_paths


def test_caba_query_resolves_to_existing_city():
    assert resolve_city("ciudad autonoma de buenos aires") == "caba"
    assert resolve_city("Capital Federal") == "caba"
    assert resolve_city("CABA") == "caba"


def test_caba_aliases_share_place_ids():
    ids = same_place_ids("microcentro-caba")
    assert "caba" in ids
    assert "microcentro-caba" in ids
    assert "ciudad-autonoma-de-buenos-aires" in ids
    assert "capital-federal" in ids
    assert "buenos-aires" not in ids


def test_tiny_microcentro_bbox_does_not_hide_palermo():
    apply_city_extent(
        "caba",
        bbox=(-34.6117, -58.3859, -34.5917, -58.3659),
        radius_km=4.2,
        slug="capital-federal",
    )
    assert geo_city_slug("caba") == "capital-federal"
    assert in_city_radius(-34.588, -58.430, "caba") is True


def test_listed_cities_only_shows_used_or_searched_places():
    rows = listed_cities([])
    ids = {row["id"] for row in rows}
    assert "caba" in ids
    assert "trelew" not in ids
    assert "gaiman" not in ids


def test_listed_cities_keeps_a_searched_place():
    from app.schedule import note_search, reset

    reset()
    note_search("puerto-madryn")
    ids = {row["id"] for row in listed_cities([])}
    assert "puerto-madryn" in ids
    assert "caba" in ids
    reset()


def test_listed_cities_merges_caba_aliases():
    item = Listing(
        source="zonaprop",
        source_id="alias",
        url="https://example.com",
        title="Depto",
        property_type="departamento",
        city="ciudad-autonoma-de-buenos-aires",
        address="Palermo",
        lat=-34.588,
        lon=-58.430,
    )
    ids = {row["id"] for row in listed_cities([item])}
    assert "caba" in ids
    assert "ciudad-autonoma-de-buenos-aires" not in ids
    assert "buenos-aires" not in ids


def test_caba_portal_slug_is_city_wide():
    assert city_slug("microcentro-caba") == "capital-federal"
    paths = zonaprop_paths("microcentro-caba")
    assert any("capital-federal" in slug for slug, _ in paths)
    assert not any("microcentro" in slug for slug, _ in paths)
    ml = mercadolibre_urls("microcentro-caba")
    assert any(url.endswith("/capital-federal/") for url, _ in ml)
    assert not any("microcentro" in url for url, _ in ml)


def test_caba_listing_with_buenos_aires_stays():
    item = Listing(
        source="zonaprop",
        source_id="pal",
        url="https://example.com",
        title="Departamento en Palermo",
        property_type="departamento",
        city="microcentro-caba",
        address="Humboldt 1800, Palermo, Buenos Aires",
        lat=-34.588,
        lon=-58.430,
    )
    assert listing_fits_city(item, "microcentro-caba") is True


def test_barrio_comes_from_address_not_catalog():
    assert barrio_from_address("Humboldt 1800, Palermo Hollywood, Capital Federal", "microcentro-caba") == "Palermo Hollywood"
    assert barrio_from_address("Palermo, Buenos Aires", "microcentro-caba") == "Palermo"
    barrio, _zona, _lat, _lon = infer_barrio(
        "Departamento en venta",
        "Av. Santa Fe 3200, Palermo, Capital Federal",
        city="microcentro-caba",
        barrio_hint="Palermo Hollywood",
    )
    assert barrio in {"Palermo", "Palermo Hollywood"}


def test_madryn_listing_mentioning_buenos_aires_is_still_foreign():
    item = Listing(
        source="zonaprop",
        source_id="ba",
        url="https://example.com",
        title="Departamento en Palermo, Buenos Aires",
        property_type="departamento",
        city="puerto-madryn",
        address="Palermo, Buenos Aires",
        lat=-42.769,
        lon=-65.038,
    )
    assert listing_fits_city(item, "puerto-madryn") is False


def test_san_nicolas_caba_is_not_a_foreign_town():
    from app.geo import remember_barrio
    from app.place_api import remember

    remember_barrio("caba", "San Nicolás", -34.6037, -58.3816)
    remember(
        "san nicolas",
        {"name": "San Nicolás", "kind": "localidad", "province": "La Rioja", "lat": -29.115, "lon": -67.473},
    )
    item = Listing(
        source="zonaprop",
        source_id="micro-1",
        url="https://example.com",
        title="Monoambiente Venta Microcentro Apto Profesional",
        property_type="departamento",
        city="fuera",
        address="Florida 600",
        barrio="San Nicolás",
        extra={"search_city": "caba"},
    )
    assert listing_mentions_city(item, "caba") is True
    assert listing_fits_city(item, "caba") is False  # city field is fuera until rehomed
    from app.geo import foreign_locality

    assert foreign_locality(item, "caba") is False
    item.city = "caba"
    assert listing_fits_city(item, "caba") is True


def test_caba_in_address_counts_as_the_city():
    item = Listing(
        source="zonaprop",
        source_id="caba-word",
        url="https://example.com",
        title="Oficina o Departamento",
        property_type="departamento",
        city="fuera",
        address="Pasaje Rivarola 111, Caba",
        barrio="San Nicolás",
    )
    assert listing_mentions_city(item, "caba") is True


def test_microcentro_hint_snaps_to_san_nicolas(monkeypatch):
    from app.geo import infer_barrio, remember_barrio
    from app.place_api import remember

    remember_barrio("caba", "San Nicolás", -34.6037, -58.3816)
    remember(
        "microcentro",
        {
            "name": "Microcentro",
            "kind": "neighbourhood",
            "province": "capital-federal",
            "lat": -34.6037,
            "lon": -58.3816,
        },
    )
    barrio, _zona, lat, lon = infer_barrio(
        "Departamento en venta",
        "Florida 600, CABA",
        city="caba",
        barrio_hint="Microcentro",
    )
    assert barrio == "San Nicolás"
    assert abs(lat + 34.6037) < 0.01


def test_learned_barrio_matches_later_listings():
    remember_barrio("microcentro-caba", "Belgrano", -34.562, -58.456)
    barrio, _zona, _lat, _lon = infer_barrio(
        "Depto 3 ambientes",
        "Juramento 2100, Belgrano C, Capital Federal",
        city="microcentro-caba",
    )
    assert barrio == "Belgrano"


def test_microcentro_is_not_kept_as_barrio_name():
    remember_barrio("caba", "San Nicolás", -34.6037, -58.3816)
    barrio, *_ = infer_barrio(
        "Departamento en venta",
        "Florida 600",
        city="caba",
        barrio_hint="Microcentro",
    )
    assert barrio == "San Nicolás"


def test_downtown_microcentro_listing_rehomes_to_caba():
    from app.geo import listing_fits_city, pin_listing_city
    from app.place_api import remember

    remember_barrio("caba", "San Nicolás", -34.6037, -58.3816)
    remember(
        "florida",
        {"name": "Florida", "kind": "localidad", "province": "Buenos Aires", "lat": -34.532, "lon": -58.491},
    )
    item = Listing(
        source="zonaprop",
        source_id="micro-fuera",
        url="https://example.com",
        title="Monoambiente Venta Microcentro Apto Profesional",
        property_type="departamento",
        city="fuera",
        address="Florida",
        barrio="Microcentro",
        lat=-34.6037,
        lon=-58.3816,
        extra={"search_city": "caba"},
    )
    assert pin_listing_city(item) is True
    assert item.city == "caba"
    assert item.barrio == "San Nicolás"
    assert listing_fits_city(item, "caba") is True


def test_buenos_aires_tag_with_san_nicolas_becomes_caba():
    from app.geo import listing_fits_city, pin_listing_city

    remember_barrio("caba", "San Nicolás", -34.6037, -58.3816)
    item = Listing(
        source="zonaprop",
        source_id="ba-sn",
        url="https://example.com",
        title="Oficina en venta",
        property_type="oficina",
        city="buenos-aires",
        address="Esmeralda 500",
        barrio="San Nicolás",
        lat=-34.6018,
        lon=-58.3782,
        extra={"search_city": "caba"},
    )
    assert pin_listing_city(item) is True
    assert item.city == "caba"
    assert listing_fits_city(item, "caba") is True

