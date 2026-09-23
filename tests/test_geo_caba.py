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


def _box_ring(lat: float, lon: float, d: float = 0.02) -> list[list[float]]:
    return [
        [lat - d, lon - d],
        [lat - d, lon + d],
        [lat + d, lon + d],
        [lat + d, lon - d],
        [lat - d, lon - d],
    ]


def _caba_area_polygons() -> list[dict]:
    centers = [
        (-34.6037, -58.3816, "San Nicolás"),
        (-34.588, -58.430, "Palermo"),
        (-34.562, -58.456, "Belgrano"),
        (-34.620, -58.440, "Caballito"),
        (-34.635, -58.365, "La Boca"),
        (-34.650, -58.500, "Mataderos"),
        (-34.580, -58.500, "Villa Devoto"),
        (-34.615, -58.500, "Flores"),
    ]
    rows = []
    for lat, lon, name in centers:
        rows.append(
            {
                "name": name,
                "zona": name,
                "lat": lat,
                "lon": lon,
                "ring": _box_ring(lat, lon),
            }
        )
    return rows


def _caba_city_outline() -> list[list[list[float]]]:
    # Caja que cubre CABA (Palermo, Obelisco, Lugano) y deja afuera Vicente López y Avellaneda.
    return [
        [
            [-34.705, -58.531],
            [-34.705, -58.335],
            [-34.535, -58.335],
            [-34.535, -58.531],
            [-34.705, -58.531],
        ]
    ]


def test_rings_from_geojson_swaps_lon_lat():
    from app.places import rings_from_geojson

    rings = rings_from_geojson(
        {
            "type": "Polygon",
            "coordinates": [[[-58.4, -34.6], [-58.3, -34.6], [-58.3, -34.5], [-58.4, -34.5], [-58.4, -34.6]]],
        }
    )
    assert rings
    assert rings[0][0] == [-34.6, -58.4]


def test_gba_point_is_not_caba_when_barrio_polygons_exist():
    from app.geo import clear_city_polygons, remember_city_outline, remember_city_polygons

    remember_city_polygons("caba", _caba_area_polygons())
    remember_city_outline("caba", _caba_city_outline())
    try:
        assert in_city_radius(-34.588, -58.430, "caba") is True
        assert in_city_radius(-34.6037, -58.3816, "caba") is True
        assert in_city_radius(-34.526, -58.475, "caba") is False
        assert in_city_radius(-34.507, -58.487, "caba") is False
        vl = Listing(
            source="zonaprop",
            source_id="vl",
            url="https://example.com/vl",
            title="Depto Vicente López",
            property_type="departamento",
            city="caba",
            address="Maipú 600, Vicente López",
            lat=-34.526,
            lon=-58.475,
            extra={"search_city": "caba"},
        )
        assert listing_fits_city(vl, "caba") is False
        palermo = Listing(
            source="zonaprop",
            source_id="pal",
            url="https://example.com/pal",
            title="Depto Palermo",
            property_type="departamento",
            city="caba",
            address="Honduras 3800",
            lat=-34.588,
            lon=-58.430,
            extra={"search_city": "caba"},
        )
        assert listing_fits_city(palermo, "caba") is True
    finally:
        clear_city_polygons("caba")


def test_caba_pin_stays_on_map_if_title_names_another_locality():
    from app.geo import clear_city_polygons, remember_city_outline, remember_city_polygons
    from app.place_api import remember

    remember_city_polygons("caba", _caba_area_polygons())
    remember_city_outline("caba", _caba_city_outline())
    remember(
        "hudson",
        {"name": "Hudson", "kind": "localidad", "province": "buenos-aires", "lat": -34.79, "lon": -58.16},
    )
    try:
        item = Listing(
            source="mercadolibre",
            source_id="hudson-caba",
            url="https://example.com/hudson",
            title="Lagoon Hudson - Viví En Contacto Con El Agua",
            property_type="departamento",
            city="caba",
            address="Balvanera",
            barrio="Balvanera",
            lat=-34.6138,
            lon=-58.3952,
            extra={"search_city": "caba"},
        )
        assert listing_fits_city(item, "caba", remote=False) is True
    finally:
        clear_city_polygons("caba")


def test_listed_cities_only_shows_used_or_searched_places():
    from app.places import reset_listed_places
    from app.schedule import reset

    reset()
    reset_listed_places()
    rows = listed_cities([])
    ids = {row["id"] for row in rows}
    assert "caba" in ids
    assert "trelew" not in ids
    assert "gaiman" not in ids
    assert "canning" not in ids


def test_listing_count_for_catalog_skips_tiny_places():
    from app.places import listing_count_for_catalog

    assert listing_count_for_catalog(0) is False
    assert listing_count_for_catalog(7) is False
    assert listing_count_for_catalog(8) is True
    assert listing_count_for_catalog(80) is True


def test_listed_cities_shows_a_loaded_city_that_nobody_searched(monkeypatch):
    from app.geo import CITY_ALIASES, CITIES, register_city
    from app.places import forget_place, listed_cities, reset_listed_places

    reset_listed_places()
    register_city(
        "rosario",
        label="Rosario",
        lat=-32.95,
        lon=-60.64,
        province="santa-fe",
        builtin=False,
    )
    monkeypatch.setattr("app.places._ids_with_saved_listings", lambda: {"rosario"})
    try:
        ids = {row["id"] for row in listed_cities([])}
        assert "rosario" in ids
        assert "caba" in ids
    finally:
        forget_place("rosario")
        CITIES.pop("rosario", None)
        for token, owner in list(CITY_ALIASES.items()):
            if owner == "rosario" or token == "rosario":
                CITY_ALIASES.pop(token, None)
        reset_listed_places()


def test_listed_cities_includes_places_that_have_listings(monkeypatch):
    from app.places import reset_listed_places
    from app.schedule import reset

    reset()
    reset_listed_places()
    monkeypatch.setattr("app.places._ids_with_saved_listings", lambda: {"quilmes", "fuera"})
    ids = {row["id"] for row in listed_cities([])}
    assert "caba" in ids
    assert "quilmes" not in ids
    assert "fuera" not in ids


def test_listed_cities_hides_empty_searched_place(monkeypatch):
    from app.places import remember_listed_place, reset_listed_places
    from app.schedule import reset

    monkeypatch.setattr("app.pipeline.loading_city_ids", lambda: set())
    reset()
    reset_listed_places()
    remember_listed_place("puerto-madryn")
    ids = {row["id"] for row in listed_cities([])}
    assert "puerto-madryn" not in ids
    assert "caba" in ids
    reset_listed_places()
    reset()


def test_listed_cities_hides_a_loading_empty_place(monkeypatch):
    from app.places import remember_listed_place, reset_listed_places
    from app.schedule import reset

    reset()
    reset_listed_places()
    remember_listed_place("puerto-madryn")
    monkeypatch.setattr("app.pipeline.loading_city_ids", lambda: {"puerto-madryn"})
    ids = {row["id"] for row in listed_cities([])}
    assert "puerto-madryn" not in ids
    assert "caba" in ids
    reset_listed_places()
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
    from app.geo import CITIES, remember_barrio
    from app.place_api import remember

    remember_barrio("caba", "San Nicolás", -34.6037, -58.3816)
    cfg = CITIES["caba"]
    barrios = list(cfg.get("barrios") or [])
    barrios.append({"name": "San Nicolás", "lat": -34.6037, "lon": -58.3816, "aliases": ["san nicolas"]})
    cfg["barrios"] = barrios
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
    assert listing_fits_city(item, "caba") is True  # se buscó en CABA aunque el tag haya quedado en fuera
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

