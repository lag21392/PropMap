import os

import pytest

os.environ["PROPMAP_TEST"] = "1"

from app import place_api
from app.geo import CITIES, register_city
from app.http_client import reset_fetch_state
from app.places import reset_listed_places


def _place(name, province, lat, lon, kind="localidad"):
    return {"name": name, "kind": kind, "province": province, "lat": lat, "lon": lon}


@pytest.fixture(autouse=True)
def georef_fixtures(monkeypatch):
    reset_fetch_state()
    reset_listed_places()
    place_api.reset_cache()
    from app.places import reset_search_cache

    reset_search_cache()
    place_api._provinces = [
        {"id": "06", "nombre": "Buenos Aires", "slug": "buenos-aires"},
        {"id": "02", "nombre": "Ciudad Autónoma de Buenos Aires", "slug": "ciudad-autonoma-de-buenos-aires"},
        {"id": "14", "nombre": "Córdoba", "slug": "cordoba"},
        {"id": "26", "nombre": "Chubut", "slug": "chubut"},
        {"id": "10", "nombre": "Catamarca", "slug": "catamarca"},
        {"id": "82", "nombre": "Santa Fe", "slug": "santa-fe"},
    ]
    fixtures = {
        "puerto madryn": _place("Puerto Madryn", "Chubut", -42.7692, -65.0385),
        "madryn": _place("Puerto Madryn", "Chubut", -42.7692, -65.0385),
        "chubut": {"name": "Chubut", "kind": "provincia", "province": "Chubut", "lat": None, "lon": None},
        "trelew": _place("Trelew", "Chubut", -43.2489, -65.3051),
        "gobernador fontana": _place("Gobernador Fontana", "Chubut", -44.74, -68.23),
        "rawson": _place("Rawson", "Chubut", -43.3002, -65.1023),
        "pilar": _place("Pilar", "Buenos Aires", -34.4587, -58.9138, "municipio"),
        "pilar chico": _place("Pilar Chico", "Buenos Aires", -34.47, -58.90),
        "escobar": _place("Escobar", "Buenos Aires", -34.349, -58.796, "municipio"),
        "puertos": _place("Puertos", "Buenos Aires", -34.39, -58.76),
        "canning": _place("Canning", "Buenos Aires", -34.877, -58.511),
        "ezeiza": _place("Ezeiza", "Buenos Aires", -34.853, -58.522, "municipio"),
        "esteban echeverria": _place("Esteban Echeverría", "Buenos Aires", -34.82, -58.47, "municipio"),
        "docta": _place("Docta", "Córdoba", -31.42, -64.19),
        "cordoba": _place("Córdoba", "Córdoba", -31.416, -64.183),
        "la falda": _place("La Falda", "Córdoba", -31.093, -64.483),
        "la costa": _place("La Costa", "Catamarca", -28.47, -65.78),
        "catamarca": _place("Catamarca", "Catamarca", -28.469, -65.779),
        "el pato": _place("El Pato", "Buenos Aires", -34.90, -58.18),
        "berazategui": _place("Berazategui", "Buenos Aires", -34.75, -58.21, "municipio"),
        "parque bonito": _place("Parque Bonito", "Buenos Aires", -34.90, -58.18),
        "palermo": _place("Palermo", "Ciudad Autónoma de Buenos Aires", -34.588, -58.430),
        "mar del plata": _place("Mar del Plata", "Buenos Aires", -38.0055, -57.5426),
    }
    for name, place in fixtures.items():
        place_api.remember(name, place)
    if "caba" not in CITIES:
        register_city(
            "caba",
            label="CABA",
            lat=-34.6037,
            lon=-58.3816,
            province="capital-federal",
            slug="capital-federal",
            aliases=["caba", "capital federal", "ciudad autonoma de buenos aires", "ciudad de buenos aires", "microcentro-caba", "microcentro"],
            builtin=True,
            radius_km=16,
        )
    if "puerto-madryn" not in CITIES:
        register_city(
            "puerto-madryn",
            label="Puerto Madryn",
            lat=-42.7692,
            lon=-65.0385,
            province="chubut",
            aliases=["madryn", "puerto madryn"],
            radius_km=38,
            builtin=True,
        )
    if "trelew" not in CITIES:
        register_city(
            "trelew",
            label="Trelew",
            lat=-43.2489,
            lon=-65.3051,
            province="chubut",
            radius_km=18,
            builtin=True,
        )
    monkeypatch.setattr(place_api, "_georef", lambda *args, **kwargs: {})
    monkeypatch.setattr(place_api, "_nominatim_place", lambda *args, **kwargs: None)
    yield
    place_api.reset_cache()
