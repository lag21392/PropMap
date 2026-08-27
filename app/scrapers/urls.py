from __future__ import annotations

from ..geo import city_scrape_slugs, city_slug as _city_slug, CITIES
from ..schedule import is_country_place

TYPES = (
    ("casa", "casas"),
    ("departamento", "departamentos"),
    ("ph", "ph"),
    ("terreno", "terrenos"),
)


def city_slug(city: str) -> str:
    return _city_slug(city)


def city_province(city: str) -> str:
    return (CITIES.get(city) or {}).get("province") or ""


def zonaprop_paths(city: str, _predefined: dict | None = None) -> list[tuple[str, str]]:
    if is_country_place(city):
        return [
            ("casas-venta", "casa"),
            ("departamentos-venta", "departamento"),
            ("ph-venta", "ph"),
            ("terrenos-venta", "terreno"),
            ("inmuebles-venta", ""),
        ]
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for slug in city_scrape_slugs(city):
        for path in (
            (f"casas-venta-{slug}", "casa"),
            (f"departamentos-venta-{slug}", "departamento"),
            (f"ph-venta-{slug}", "ph"),
            (f"terrenos-venta-{slug}", "terreno"),
            (f"inmuebles-venta-{slug}", ""),
        ):
            if path[0] not in seen:
                seen.add(path[0])
                rows.append(path)
    return rows


def properati_urls(city: str, _predefined: dict | None = None) -> list[tuple[str, str]]:
    if is_country_place(city):
        return [
            (f"https://www.properati.com.ar/s/{ptype}/venta", ptype)
            for ptype, _folder in TYPES
        ]
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for slug in city_scrape_slugs(city):
        for ptype, _folder in TYPES:
            url = f"https://www.properati.com.ar/s/{slug}/{ptype}/venta"
            if url not in seen:
                seen.add(url)
                rows.append((url, ptype))
    return rows


def mercadolibre_urls(city: str, _predefined: dict | None = None) -> list[tuple[str, str]]:
    if is_country_place(city):
        return [
            (f"https://inmuebles.mercadolibre.com.ar/{folder}/venta/", ptype)
            for ptype, folder in TYPES
        ]
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    province = city_province(city)
    for slug in city_scrape_slugs(city):
        for ptype, folder in TYPES:
            if province and slug == province:
                url = f"https://inmuebles.mercadolibre.com.ar/{folder}/venta/{province}/"
            elif province:
                url = f"https://inmuebles.mercadolibre.com.ar/{folder}/venta/{province}/{slug}/"
            else:
                url = f"https://inmuebles.mercadolibre.com.ar/{folder}/venta/{slug}/"
            if url not in seen:
                seen.add(url)
                rows.append((url, ptype))
    return rows


def argenprop_urls(city: str, _predefined: dict | None = None) -> list[tuple[str, str]]:
    if is_country_place(city):
        return [
            (f"https://www.argenprop.com/{folder}/venta", ptype)
            for ptype, folder in TYPES
        ]
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for slug in city_scrape_slugs(city):
        for ptype, folder in TYPES:
            url = f"https://www.argenprop.com/{folder}/venta/{slug}"
            if url not in seen:
                seen.add(url)
                rows.append((url, ptype))
    return rows


def zonaprop_rental_paths(city: str) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for slug in city_scrape_slugs(city):
        for ptype, folder in TYPES:
            if ptype == "terreno":
                continue
            rows.append((f"{folder}-alquiler-{slug}", ptype, "monthly"))
            rows.append((f"{folder}-alquiler-temporario-{slug}", ptype, "nightly"))
            rows.append((f"alquiler-temporario-{folder}-{slug}", ptype, "nightly"))
    return rows


def properati_rental_urls(city: str) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for slug in city_scrape_slugs(city):
        for ptype, _folder in TYPES:
            if ptype == "terreno":
                continue
            rows.append((f"https://www.properati.com.ar/s/{slug}/{ptype}/alquiler", ptype, "monthly"))
            rows.append((f"https://www.properati.com.ar/s/{slug}/{ptype}/alquiler-temporario", ptype, "nightly"))
    return rows


def mercadolibre_rental_urls(city: str) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    province = city_province(city)
    for slug in city_scrape_slugs(city):
        place = province if province and (not slug or slug == province) else (
            f"{province}/{slug}" if province else slug
        )
        for ptype, folder in TYPES:
            if ptype == "terreno":
                continue
            rows.append((f"https://inmuebles.mercadolibre.com.ar/{folder}/alquiler/{place}/", ptype, "monthly"))
            rows.append(
                (f"https://inmuebles.mercadolibre.com.ar/{folder}/alquiler-temporario/{place}/", ptype, "nightly")
            )
        rows.append((f"https://inmuebles.mercadolibre.com.ar/alquiler-temporario/{place}/", "departamento", "nightly"))
    return rows


def argenprop_rental_urls(city: str) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    folders = {
        "casa": ("casas", "casa"),
        "departamento": ("departamento", "departamentos"),
        "ph": ("ph",),
    }
    for slug in city_scrape_slugs(city):
        for ptype, alts in folders.items():
            for folder in alts:
                rows.append((f"https://www.argenprop.com/{folder}/alquiler/{slug}", ptype, "monthly"))
                rows.append((f"https://www.argenprop.com/{folder}/alquiler-temporario/{slug}", ptype, "nightly"))
    return rows
