import re

from app.geo import listing_fits_city
from app.geo_tools import parse_plain_locations
from app.models import Listing
from app.place_api import listing_places
from app.places import _query_variants


def test_street_with_number_is_not_a_city():
    item = Listing(
        source="zonaprop",
        source_id="st",
        url="https://example.com",
        title="Departamento en Puerto Madryn",
        property_type="departamento",
        city="puerto-madryn",
        address="Neuquen al 800 Chubut, Argentina",
    )
    names = {str(p.get("name") or "").lower() for p in listing_places(item)}
    assert not any("neuquen" in n for n in names)
    assert listing_fits_city(item, "puerto-madryn") is True


def test_extracted_place_uses_cached_api_hit():
    item = Listing(
        source="argenprop",
        source_id="api",
        url="https://example.com",
        title="Apartamento en Venta en La Falda",
        property_type="departamento",
        city="puerto-madryn",
        address="La Falda, Córdoba",
    )
    places = listing_places(item)
    assert any("falda" in str(p.get("name") or "").lower() for p in places)
    assert listing_fits_city(item, "puerto-madryn") is False


def test_glued_place_query_splits_generic_prefix():
    variants = [v.lower() for v in _query_variants("puertomadryn")]
    assert any(v == "puerto madryn" for v in variants)


def test_roca_y_apeleg_is_an_intersection():
    data = parse_plain_locations("Avenida Julio Argentino Roca y Apeleg (Puerto Madryn, Chubut)")
    assert data["corners"]
    left, right = data["corners"][0]
    assert "roca" in left
    assert "apeleg" in right


def test_locate_item_keeps_intersection_when_geocode_misses():
    from app.scrapers import locate_item

    item = Listing(
        source="zonaprop",
        source_id="esquina-1",
        url="https://example.com/esquina",
        title="Terreno en Avenida Julio Argentino Roca y Apeleg (Puerto Madryn, Chubut)",
        address="Avenida Julio Argentino Roca y Apeleg (Puerto Madryn, Chubut)",
        property_type="terreno",
        city="puerto-madryn",
    )
    locate_item(item)
    assert "apeleg" in (item.extra.get("intersection") or "")
    assert item.extra.get("location_kind") == "intersection"
    assert item.has_exact_location is False


def test_locate_prefers_georef_street_over_portal_pin(monkeypatch):
    from app import place_api
    from app.place_api import reset_cache
    from app.scrapers import locate_item

    def fake_georef(path, params):
        if path != "/direcciones":
            return {}
        return {
            "direcciones": [
                {
                    "nomenclatura": "BARTOLOME MITRE 644, Biedma, Chubut",
                    "calle": {"nombre": "BARTOLOME MITRE"},
                    "ubicacion": {"lat": -42.7710437, "lon": -65.0334884},
                    "localidad_censal": {"nombre": "Puerto Madryn"},
                }
            ]
        }

    monkeypatch.setattr(place_api, "_georef", fake_georef)
    reset_cache()
    item = Listing(
        source="zonaprop",
        source_id="59147320",
        url="https://www.zonaprop.com.ar/propiedades/x.html",
        title="Departamento Moderno en Ubicación Espectacular. Apto Credito",
        property_type="departamento",
        address="Bartolomé Mitre 644",
        lat=-42.76727747394898,
        lon=-65.03793200311183,
        city="puerto-madryn",
        has_exact_location=True,
        extra={"location_kind": "exact"},
    )
    locate_item(item)
    assert abs(item.lat + 42.7710437) < 1e-5
    assert abs(item.lon + 65.0334884) < 1e-5
    assert item.has_exact_location is True
    assert item.extra.get("location_kind") == "exact"


def test_locate_item_pins_pasaje_even_if_portal_is_approx(monkeypatch):
    from app import place_api
    from app.place_api import reset_cache
    from app.scrapers import locate_item

    def fake_georef(path, params):
        direccion = str(params.get("direccion") or "").lower()
        nombre = str(params.get("nombre") or "").lower()
        if path == "/direcciones":
            if not params.get("localidad") and not params.get("provincia"):
                return {
                    "direcciones": [
                        {
                            "nomenclatura": "BRUNO CEBALLOS 50, Córdoba",
                            "calle": {"nombre": "BRUNO CEBALLOS"},
                            "ubicacion": {"lat": -32.41757, "lon": -63.25477},
                        }
                    ]
                }
            if "bruno" in direccion and re.search(r"\b50\b", direccion):
                return {"direcciones": []}
            if "bruno" in direccion:
                return {
                    "direcciones": [
                        {
                            "nomenclatura": "PJE BRUNO 201, Biedma, Chubut",
                            "calle": {"nombre": "PJE BRUNO"},
                            "ubicacion": {"lat": -42.7636537, "lon": -65.0382136},
                            "localidad_censal": {"nombre": "Puerto Madryn"},
                        }
                    ]
                }
            return {}
        if path == "/calles" and "bruno" in nombre:
            return {
                "calles": [
                    {
                        "nombre": "PJE BRUNO",
                        "categoria": "PJE",
                        "localidad_censal": {"nombre": "Puerto Madryn"},
                        "altura": {
                            "inicio": {"derecha": 201, "izquierda": 202},
                            "fin": {"derecha": 299, "izquierda": 300},
                        },
                    }
                ]
            }
        return {}

    monkeypatch.setattr(place_api, "_georef", fake_georef)
    reset_cache()
    item = Listing(
        source="zonaprop",
        source_id="57970200",
        url="https://www.zonaprop.com.ar/propiedades/x.html",
        title="Hermoso Departamento Estilo Loft",
        property_type="departamento",
        address="Pasaje Bruno 50",
        description="Departamento ideal para inversores a pasos del centro de Puerto Madryn",
        lat=-42.76756493352497,
        lon=-65.03754044686475,
        city="puerto-madryn",
        has_exact_location=False,
        extra={"location_kind": "approx", "search_city": "puerto-madryn"},
    )
    locate_item(item)
    assert abs(item.lat + 42.7636537) < 1e-5
    assert abs(item.lon + 65.0382136) < 1e-5
    assert item.has_exact_location is True
    assert item.extra.get("location_kind") == "exact"


def test_priority_place_ids_are_madryn_then_caba():
    from app.places import priority_place_ids

    ids = priority_place_ids()
    assert ids[0] == "puerto-madryn"
    assert "caba" in ids


def test_search_places_hits_cached_city_without_waiting_remote():
    from app.places import search_places

    rows = search_places("Puerto Madryn")
    assert any(row.get("id") == "puerto-madryn" for row in rows)
