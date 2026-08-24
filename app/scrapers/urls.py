from __future__ import annotations

from ..geo import CITIES

TYPES = (
    ("casa", "casas"),
    ("departamento", "departamentos"),
    ("ph", "ph"),
    ("terreno", "terrenos"),
)


def city_slug(city: str) -> str:
    return (CITIES.get(city) or {}).get("slug") or city


def city_province(city: str) -> str:
    return (CITIES.get(city) or {}).get("province") or "chubut"


def zonaprop_paths(city: str, predefined: dict[str, list[tuple[str, str]]]) -> list[tuple[str, str]]:
    if city in predefined:
        return predefined[city]
    slug = city_slug(city)
    return [
        (f"casas-venta-{slug}", "casa"),
        (f"departamentos-venta-{slug}", "departamento"),
        (f"ph-venta-{slug}", "ph"),
        (f"terrenos-venta-{slug}", "terreno"),
        (f"inmuebles-venta-{slug}", ""),
    ]


def properati_urls(city: str, predefined: dict[str, list[tuple[str, str]]]) -> list[tuple[str, str]]:
    if city in predefined:
        return predefined[city]
    slug = city_slug(city)
    return [(f"https://www.properati.com.ar/s/{slug}/{ptype}/venta", ptype) for ptype, _ in TYPES]


def mercadolibre_urls(city: str, predefined: dict[str, list[tuple[str, str]]]) -> list[tuple[str, str]]:
    if city in predefined:
        return predefined[city]
    slug = city_slug(city)
    province = city_province(city)
    return [
        (f"https://inmuebles.mercadolibre.com.ar/{folder}/venta/{province}/{slug}/", ptype)
        for ptype, folder in TYPES
    ]


def argenprop_urls(city: str, predefined: dict[str, list[tuple[str, str]]]) -> list[tuple[str, str]]:
    if city in predefined:
        return predefined[city]
    slug = city_slug(city)
    return [(f"https://www.argenprop.com/{folder}/venta/{slug}", ptype) for ptype, folder in TYPES]
