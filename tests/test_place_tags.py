from app.geo import CITY_ALIASES, CITIES, listing_fits_city, register_city
from app.models import Listing
from app.place_api import remember
from app.place_tags import (
    apply_place_tags,
    listing_matches_city,
    place_token,
    related_place_ids,
    tags_for_place,
)


def _cleanup(*ids: str) -> None:
    for cid in ids:
        CITIES.pop(cid, None)
        for token, owner in list(CITY_ALIASES.items()):
            if owner == cid or token == cid:
                CITY_ALIASES.pop(token, None)


def _listing(source_id: str, city: str, **kwargs) -> Listing:
    return Listing(
        source="zonaprop",
        source_id=source_id,
        url="https://example.com",
        title=kwargs.pop("title", "Casa en venta"),
        property_type="casa",
        city=city,
        **kwargs,
    )


def test_pilar_norte_tags_include_pilar():
    remember(
        "pilar norte",
        {"name": "Pilar Norte", "kind": "localidad", "province": "Buenos Aires", "lat": -34.42, "lon": -58.90},
    )
    tags = {place_token(tag) for tag in tags_for_place("Pilar Norte", province="Buenos Aires")}
    assert "pilar norte" in tags
    assert "pilar" in tags


def test_pilar_tags_do_not_include_pilar_norte():
    tags = {place_token(tag) for tag in tags_for_place("Pilar", province="Buenos Aires")}
    assert "pilar" in tags
    assert "pilar norte" not in tags


def test_search_pilar_finds_both_pilar_norte_only_one():
    remember(
        "pilar norte",
        {"name": "Pilar Norte", "kind": "localidad", "province": "Buenos Aires", "lat": -34.42, "lon": -58.90},
    )
    register_city("pilar", label="Pilar", lat=-34.4587, lon=-58.9138, province="buenos-aires", builtin=False)
    register_city(
        "pilar-norte",
        label="Pilar Norte",
        lat=-34.42,
        lon=-58.90,
        province="buenos-aires",
        builtin=False,
    )
    try:
        norte = _listing("pn", "pilar-norte", barrio="Pilar Norte")
        pilar = _listing("p", "pilar")
        apply_place_tags(norte, remote=False)
        apply_place_tags(pilar, remote=False)
        assert listing_matches_city(norte, "pilar")
        assert listing_matches_city(norte, "pilar-norte")
        assert listing_matches_city(pilar, "pilar")
        assert listing_matches_city(pilar, "pilar-norte") is False
        assert listing_fits_city(norte, "pilar", remote=False, require_radius=False) is True
        assert listing_fits_city(norte, "pilar-norte", remote=False, require_radius=False) is True
        assert listing_fits_city(pilar, "pilar", remote=False, require_radius=False) is True
        assert listing_fits_city(pilar, "pilar-norte", remote=False, require_radius=False) is False
        assert "pilar-norte" in related_place_ids("pilar")
        assert "pilar" not in related_place_ids("pilar-norte")
    finally:
        _cleanup("pilar", "pilar-norte")
