import re

import pytest

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


def test_llao_llao_street_in_madryn_is_not_bariloche():
    from app.place_api import listing_places, remember

    remember(
        "llao llao",
        {"name": "Llao Llao", "kind": "localidad", "province": "Río Negro", "lat": -41.05, "lon": -71.53},
    )
    remember(
        "verbena",
        {"name": "La Verbena", "kind": "municipio", "province": "Entre Ríos", "lat": -30.46, "lon": -58.60},
    )
    item = Listing(
        source="zonaprop",
        source_id="llao-st",
        url="https://example.com",
        title="Lote en Solana - Llao Llao E/ Verbena y Cortaderas",
        property_type="terreno",
        city="puerto-madryn",
        address="Llao llao 4300",
        barrio="Estilo Solana",
        lat=-42.8144348,
        lon=-65.031671,
        extra={"search_city": "puerto-madryn"},
    )
    names = {str(p.get("name") or "").lower() for p in listing_places(item)}
    assert not any("llao" in n for n in names)
    assert not any("verbena" in n for n in names)
    assert listing_fits_city(item, "puerto-madryn") is True
    assert listing_fits_city(item, "cordoba") is False


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


def test_entrecalles_are_parsed_as_between():
    data = parse_plain_locations("Terreno lote 12 entrecalles Roca y Apeleg Puerto Madryn")
    assert data["between"]
    left, right = data["between"][0]
    assert "roca" in left
    assert "apeleg" in right


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


def test_locate_uses_entrecalles_when_address_is_lote(monkeypatch):
    from app import place_api
    from app.place_api import reset_cache
    from app.scrapers import locate_item

    monkeypatch.setattr(place_api, "_georef", lambda *a, **k: {})
    reset_cache()
    item = Listing(
        source="zonaprop",
        source_id="lote-entre",
        url="https://example.com/lote-entre",
        title="Lote 12 Los Robles",
        address="lote 12",
        property_type="terreno",
        city="puerto-madryn",
        lat=-42.7785,
        lon=-65.0257,
        extra={
            "street": "Lote",
            "street_number": 12,
            "between": "mutista y tomillo",
            "llm_geo": {"lat": -42.7785, "lon": -65.0257, "label": "J PARRY LOVE 12", "approx": False},
            "location_kind": "exact",
            "search_city": "puerto-madryn",
        },
        has_exact_location=True,
    )
    locate_item(item)
    assert item.has_exact_location is False
    assert item.extra.get("location_kind") == "intersection"
    assert "tomillo" in (item.extra.get("intersection") or item.extra.get("between") or "")
    public = item.to_public_dict()
    assert public.get("street_number") in {None, "", 0}
    assert public.get("location_kind") == "intersection"


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


def test_place_from_suggestion_accepts_known_city():
    from app.places import place_from_suggestion

    assert place_from_suggestion("puerto-madryn") == "puerto-madryn"


def test_place_from_suggestion_accepts_coords():
    from app.place_api import remember
    from app.places import place_from_suggestion

    remember(
        "playa union",
        {"name": "Playa Unión", "kind": "localidad", "province": "Chubut", "lat": -43.317, "lon": -65.045},
    )
    cid = place_from_suggestion(
        "playa-union",
        query="Playa Unión",
        label="Playa Unión",
        lat=-43.317,
        lon=-65.045,
        province="chubut",
    )
    assert cid == "playa-union"


def test_place_from_suggestion_rejects_cache_artifact():
    from app.places import place_from_suggestion

    with pytest.raises(ValueError, match="sugerencias"):
        place_from_suggestion(
            "cordoba-pins",
            query="Córdoba",
            label="Córdoba",
            lat=-31.416,
            lon=-64.183,
        )


def test_loaded_city_is_not_forgotten_when_lookup_fails(monkeypatch):
    from app.places import _should_forget_unofficial

    monkeypatch.setenv("PROPMAP_TEST", "0")
    monkeypatch.setattr("app.places.official_place", lambda *_a, **_k: None)
    monkeypatch.setattr("app.places.search_city_places", lambda *_a, **_k: [])
    city = {"id": "cordoba", "label": "Córdoba", "province": "cordoba", "lat": -31.4, "lon": -64.18}
    barrio = {"id": "paraje-x", "label": "Paraje X", "categoria": "paraje", "lat": -31.4, "lon": -64.18}
    assert _should_forget_unofficial("cordoba", city, {"cordoba": 40}) is False
    assert _should_forget_unofficial("cordoba", city, {"cordoba": 2}) is True
    assert _should_forget_unofficial("paraje-x", barrio, {"paraje-x": 40}) is True


def test_barrio_with_listings_is_forgotten_when_georef_says_so(monkeypatch):
    from app.places import _should_forget_unofficial

    monkeypatch.setenv("PROPMAP_TEST", "0")
    monkeypatch.setattr(
        "app.places.search_city_places",
        lambda *_a, **_k: [
            {
                "name": "Florida",
                "kind": "localidad",
                "province": "Buenos Aires",
                "lat": -34.53,
                "lon": -58.49,
                "municipio": "Vicente López",
                "localidad_censal": "Vicente López",
            }
        ],
    )
    florida = {"id": "florida", "label": "Florida", "province": "buenos-aires", "lat": -34.53, "lon": -58.49}
    assert _should_forget_unofficial("florida", florida, {"florida": 27}) is True


def test_restore_hit_uses_georef_near_the_listings(monkeypatch):
    from app.places import _restore_hit

    def search(query, limit=6):
        if "villa" in query:
            return [
                {
                    "name": "Villa del Parque",
                    "kind": "localidad",
                    "province": "Ciudad Autónoma de Buenos Aires",
                    "lat": -34.61,
                    "lon": -58.49,
                    "municipio": "Comuna 11",
                    "localidad_censal": "Ciudad Autónoma de Buenos Aires",
                },
                {
                    "name": "Villa del Parque",
                    "kind": "localidad",
                    "province": "Río Negro",
                    "lat": -39.1,
                    "lon": -66.1,
                    "municipio": "Chichinales",
                    "localidad_censal": "Villa del Parque",
                },
            ]
        return [
            {
                "name": "Córdoba",
                "kind": "localidad",
                "province": "Córdoba",
                "lat": -31.42,
                "lon": -64.18,
                "municipio": "Córdoba",
                "localidad_censal": "Córdoba",
            }
        ]

    monkeypatch.setattr("app.places.search_city_places", search)
    cordoba = _restore_hit("cordoba", lat=-31.4, lon=-64.2)
    assert cordoba and cordoba["label"] == "Córdoba"
    assert _restore_hit("villa-del-parque", lat=-34.60, lon=-58.49) is None


def test_official_place_requires_real_name():
    from app.places import is_cache_artifact_id, official_place

    assert is_cache_artifact_id("cordoba-pins")
    assert is_cache_artifact_id("bahia-blanca-pins-pins")
    assert not is_cache_artifact_id("cordoba")
    assert official_place("cordoba")
    assert official_place("cordoba-pins") is None
    assert official_place("departamento-anelo") is None


def test_official_place_is_only_a_city():
    from app.place_api import remember
    from app.places import official_place

    remember(
        "quilmes",
        {"name": "Quilmes", "kind": "localidad", "province": "Buenos Aires", "lat": -34.72, "lon": -58.25},
    )
    remember(
        "quilmes y los sueldos",
        {
            "name": "Quilmes y Los Sueldos",
            "kind": "municipio",
            "province": "Tucumán",
            "lat": -26.87,
            "lon": -65.23,
        },
    )
    remember(
        "carpinchori",
        {"name": "Carpinchorí", "kind": "asentamiento", "province": "Entre Ríos", "lat": -30.95, "lon": -58.78},
    )
    assert official_place("quilmes")
    assert official_place("quilmes y los sueldos") is None
    assert official_place("carpinchori") is None


def test_official_place_rejects_barrio_inside_another_city():
    from app.place_api import remember
    from app.places import official_place

    remember(
        "florida",
        {
            "name": "Florida",
            "kind": "localidad",
            "province": "Buenos Aires",
            "lat": -34.532,
            "lon": -58.491,
            "municipio": "Vicente López",
            "localidad_censal": "Vicente López",
            "categoria": "Entidad",
        },
    )
    remember(
        "palermo",
        {
            "name": "Palermo",
            "kind": "localidad",
            "province": "Ciudad Autónoma de Buenos Aires",
            "lat": -34.588,
            "lon": -58.43,
            "municipio": "Comuna 14",
            "localidad_censal": "Ciudad Autónoma de Buenos Aires",
            "categoria": "Entidad",
        },
    )
    remember(
        "mar del plata",
        {
            "name": "Mar del Plata",
            "kind": "localidad",
            "province": "Buenos Aires",
            "lat": -38.0055,
            "lon": -57.5426,
            "municipio": "General Pueyrredón",
            "localidad_censal": "Mar del Plata",
            "categoria": "Localidad simple",
        },
    )
    remember(
        "vicente lopez",
        {
            "name": "Vicente López",
            "kind": "localidad",
            "province": "Buenos Aires",
            "lat": -34.526,
            "lon": -58.48,
            "municipio": "Vicente López",
            "localidad_censal": "Vicente López",
        },
    )
    assert official_place("florida") is None
    assert official_place("palermo") is None
    assert official_place("mar del plata")
    assert official_place("vicente lopez")


def test_official_place_rejects_caba_barrio_without_censal():
    from app.place_api import remember
    from app.places import official_place

    remember(
        "villa del parque",
        {
            "name": "Villa del Parque",
            "kind": "localidad",
            "province": "Ciudad Autónoma de Buenos Aires",
            "lat": -34.61,
            "lon": -58.49,
        },
    )
    assert official_place("villa del parque") is None


def test_listed_cities_hides_caba_barrio(monkeypatch):
    from app.geo import CITY_ALIASES, CITIES, register_city
    from app.place_api import remember
    from app.places import forget_place, listed_cities, reset_listed_places

    remember(
        "villa del parque",
        {
            "name": "Villa del Parque",
            "kind": "localidad",
            "province": "Ciudad Autónoma de Buenos Aires",
            "lat": -34.61,
            "lon": -58.49,
        },
    )
    reset_listed_places()
    register_city(
        "villa-del-parque",
        label="Villa del Parque",
        lat=-34.61,
        lon=-58.49,
        province="capital-federal",
        builtin=False,
    )
    try:
        from app import places as places_mod

        with places_mod._listed_lock:
            places_mod._listed_mem = ["villa-del-parque"]
        monkeypatch.setattr("app.places._ids_with_saved_listings", lambda: {"villa-del-parque"})
        ids = {row["id"] for row in listed_cities([])}
        assert "villa-del-parque" not in ids
        assert "caba" in ids
    finally:
        forget_place("villa-del-parque")
        CITIES.pop("villa-del-parque", None)
        for token, owner in list(CITY_ALIASES.items()):
            if owner == "villa-del-parque" or token == "villa-del-parque":
                CITY_ALIASES.pop(token, None)
        reset_listed_places()


def test_search_places_skips_caba_barrio(monkeypatch):
    from app.places import search_places

    monkeypatch.setattr("app.places._nominatim", lambda *a, **k: [])
    rows = search_places("Palermo")
    assert not any("palermo" in str(row.get("id") or "") for row in rows)


def test_search_places_keeps_provincial_city(monkeypatch):
    from app.places import search_places

    monkeypatch.setattr("app.places._nominatim", lambda *a, **k: [])
    rows = search_places("Puerto Madryn")
    assert any(row.get("id") == "puerto-madryn" for row in rows)


def test_listed_cities_hides_barrio_localidad(monkeypatch):
    from app.geo import CITY_ALIASES, CITIES, register_city
    from app.place_api import remember
    from app.places import forget_place, listed_cities, reset_listed_places

    remember(
        "florida",
        {
            "name": "Florida",
            "kind": "localidad",
            "province": "Buenos Aires",
            "lat": -34.532,
            "lon": -58.491,
            "municipio": "Vicente López",
            "localidad_censal": "Vicente López",
        },
    )
    reset_listed_places()
    register_city("florida", label="Florida", lat=-34.532, lon=-58.491, builtin=False)
    try:
        from app import places as places_mod

        with places_mod._listed_lock:
            places_mod._listed_mem = ["florida"]
        monkeypatch.setattr("app.places._ids_with_saved_listings", lambda: {"florida"})
        monkeypatch.setattr("app.pipeline.loading_city_ids", lambda: set())
        ids = {row["id"] for row in listed_cities([])}
        assert "florida" not in ids
    finally:
        forget_place("florida")
        CITIES.pop("florida", None)
        for token, owner in list(CITY_ALIASES.items()):
            if owner == "florida" or token == "florida":
                CITY_ALIASES.pop(token, None)
        reset_listed_places()


def test_listed_cities_dedupe_same_label(monkeypatch):
    from app.geo import CITY_ALIASES, CITIES, register_city
    from app.place_api import remember
    from app.places import forget_place, listed_cities, remember_listed_place, reset_listed_places

    remember(
        "quilmes",
        {"name": "Quilmes", "kind": "localidad", "province": "Buenos Aires", "lat": -34.72, "lon": -58.25},
    )
    reset_listed_places()
    register_city("quilmes", label="Quilmes", lat=-34.72, lon=-58.25, builtin=False)
    register_city("quilmes-partido", label="Quilmes", lat=-34.72, lon=-58.25, builtin=False)
    try:
        remember_listed_place("quilmes")
        remember_listed_place("quilmes-partido")
        monkeypatch.setattr("app.places._ids_with_saved_listings", lambda: {"quilmes", "quilmes-partido"})
        labels = [row["label"] for row in listed_cities([]) if row["label"] == "Quilmes"]
        assert labels == ["Quilmes"]
        ids = {row["id"] for row in listed_cities([]) if row["label"] == "Quilmes"}
        assert ids == {"quilmes"}
    finally:
        for cid in ("quilmes", "quilmes-partido"):
            forget_place(cid)
            CITIES.pop(cid, None)
            for token, owner in list(CITY_ALIASES.items()):
                if owner == cid or token == cid:
                    CITY_ALIASES.pop(token, None)
        reset_listed_places()


def test_search_city_places_keeps_same_name_in_two_provinces(monkeypatch):
    from app import place_api

    def fake_georef(path, params):
        if path == "/municipios":
            return {
                "municipios": [
                    {
                        "nombre": "Rawson",
                        "centroide": {"lat": -43.3002, "lon": -65.1023},
                        "provincia": {"nombre": "Chubut"},
                    },
                    {
                        "nombre": "Rawson",
                        "centroide": {"lat": -35.847, "lon": -60.735},
                        "provincia": {"nombre": "Buenos Aires"},
                    },
                ]
            }
        return {}

    monkeypatch.setattr(place_api, "_georef", fake_georef)
    rows = place_api.search_city_places("Rawson")
    by_prov = {place_api.province_slug(r["province"]): r for r in rows}
    assert "chubut" in by_prov
    assert "buenos-aires" in by_prov


def test_search_places_disambiguates_homonyms(monkeypatch):
    from app.places import reset_search_cache, search_places

    reset_search_cache()
    monkeypatch.setattr("app.places._nominatim", lambda *a, **k: [])
    monkeypatch.setattr(
        "app.places.search_city_places",
        lambda q, limit=8, **k: [
            {
                "name": "Rawson",
                "kind": "municipio",
                "province": "Chubut",
                "lat": -43.3002,
                "lon": -65.1023,
                "municipio": "Rawson",
            },
            {
                "name": "Rawson",
                "kind": "municipio",
                "province": "Buenos Aires",
                "lat": -35.847,
                "lon": -60.735,
                "municipio": "Rawson",
            },
        ]
        if "rawson" in q.lower()
        else [],
    )
    rows = [row for row in search_places("Rawson") if row.get("label") == "Rawson"]
    assert {row["id"] for row in rows} == {"rawson-chubut", "rawson-buenos-aires"}
    hints = " ".join(str(row.get("hint") or "") for row in rows).lower()
    assert "chubut" in hints
    assert "buenos aires" in hints
    subs = {row.get("province_label") for row in rows}
    assert "Chubut" in subs
    assert "Buenos Aires" in subs


def test_search_places_skips_nominatim_when_georef_hits(monkeypatch):
    from app.places import reset_search_cache, search_places

    reset_search_cache()
    called = {"n": 0}

    def boom(*a, **k):
        called["n"] += 1
        return []

    monkeypatch.setattr("app.places._nominatim", boom)
    monkeypatch.setattr(
        "app.places.search_city_places",
        lambda q, limit=8, **k: [
            {
                "name": "Rawson",
                "kind": "municipio",
                "province": "Chubut",
                "lat": -43.3002,
                "lon": -65.1023,
                "municipio": "Rawson",
            }
        ]
        if "rawson" in q.lower()
        else [],
    )
    rows = search_places("Rawson")
    assert called["n"] == 0
    assert any(row.get("id") == "rawson-chubut" for row in rows)


def test_search_places_dedupes_same_city_province(monkeypatch):
    from app.geo import CITY_ALIASES, CITIES, fold, register_city
    from app.places import forget_place, reset_listed_places, reset_search_cache, search_places

    reset_listed_places()
    reset_search_cache()
    monkeypatch.setattr("app.places._nominatim", lambda *a, **k: [])
    monkeypatch.setattr(
        "app.places.search_city_places",
        lambda q, limit=8, **k: [
            {
                "name": "Rawson",
                "kind": "municipio",
                "province": "San Juan",
                "lat": -31.5503,
                "lon": -68.5364,
                "municipio": "Rawson",
            },
            {
                "name": "Rawson",
                "kind": "municipio",
                "province": "Chubut",
                "lat": -43.3002,
                "lon": -65.1023,
                "municipio": "Rawson",
            },
        ]
        if "raw" in q.lower()
        else [],
    )
    register_city(
        "rawson",
        label="Rawson",
        lat=-31.5503,
        lon=-68.5364,
        province="san-juan",
        builtin=False,
    )
    try:
        rows = [row for row in search_places("Rawson") if fold(str(row.get("label") or "")) == "rawson"]
        assert len(rows) == 2
        ids = {row["id"] for row in rows}
        assert "rawson-chubut" in ids
        assert "rawson" not in ids or "rawson-san-juan" not in ids
        provs = " ".join(
            f"{row.get('province_label') or ''} {row.get('province') or ''}" for row in rows
        ).lower()
        assert "chubut" in provs
        assert "san juan" in provs or "san-juan" in provs
    finally:
        forget_place("rawson")
        CITIES.pop("rawson", None)
        CITIES.pop("rawson-san-juan", None)
        CITIES.pop("rawson-chubut", None)
        for token, owner in list(CITY_ALIASES.items()):
            if owner in {"rawson", "rawson-san-juan", "rawson-chubut"} or token in {
                "rawson",
                "rawson-san-juan",
                "rawson-chubut",
            }:
                CITY_ALIASES.pop(token, None)
        reset_listed_places()


def test_search_places_ignores_fuzzy_georef_name(monkeypatch):
    from app.places import reset_search_cache, search_places

    reset_search_cache()
    monkeypatch.setattr("app.places._nominatim", lambda *a, **k: [])
    monkeypatch.setattr(
        "app.places.search_city_places",
        lambda q, limit=8, **k: [
            {
                "name": "Esteban Rams",
                "kind": "localidad",
                "province": "Santa Fe",
                "lat": -29.77,
                "lon": -61.49,
                "municipio": "Esteban Rams",
            },
            {
                "name": "Rawson",
                "kind": "municipio",
                "province": "Chubut",
                "lat": -43.3002,
                "lon": -65.1023,
                "municipio": "Rawson",
            },
        ]
        if q.lower().startswith("raw")
        else [],
    )
    labels = {str(row.get("label") or "") for row in search_places("Raws")}
    assert "Esteban Rams" not in labels
    assert "Rawson" in labels


def test_search_places_prefix_shows_city_and_province(monkeypatch):
    from app.geo import CITY_ALIASES, CITIES, register_city
    from app.places import forget_place, reset_listed_places, reset_search_cache, search_places

    reset_listed_places()
    reset_search_cache()
    monkeypatch.setattr("app.places._nominatim", lambda *a, **k: [])
    monkeypatch.setattr("app.places.search_city_places", lambda *a, **k: [])
    register_city(
        "rawson-chubut",
        label="Rawson",
        lat=-43.3002,
        lon=-65.1023,
        province="chubut",
        builtin=False,
    )
    try:
        rows = [row for row in search_places("raw") if row.get("id") == "rawson-chubut"]
        assert rows
        assert rows[0]["label"] == "Rawson"
        assert "Chubut" in str(rows[0].get("province_label") or rows[0].get("hint") or "")
    finally:
        forget_place("rawson-chubut")
        CITIES.pop("rawson-chubut", None)
        for token, owner in list(CITY_ALIASES.items()):
            if owner == "rawson-chubut" or token == "rawson-chubut":
                CITY_ALIASES.pop(token, None)
        reset_listed_places()


def test_listed_cities_keeps_homonyms(monkeypatch):
    from app.geo import CITY_ALIASES, CITIES, register_city
    from app.place_api import remember
    from app.places import forget_place, listed_cities, remember_listed_place, reset_listed_places

    remember(
        "rawson",
        {
            "name": "Rawson",
            "kind": "municipio",
            "province": "Chubut",
            "lat": -43.3002,
            "lon": -65.1023,
            "municipio": "Rawson",
        },
    )
    reset_listed_places()
    register_city(
        "rawson-chubut",
        label="Rawson",
        lat=-43.3002,
        lon=-65.1023,
        province="chubut",
        builtin=False,
    )
    register_city(
        "rawson-buenos-aires",
        label="Rawson",
        lat=-35.847,
        lon=-60.735,
        province="buenos-aires",
        builtin=False,
    )
    try:
        remember_listed_place("rawson-chubut")
        remember_listed_place("rawson-buenos-aires")
        monkeypatch.setattr(
            "app.places._ids_with_saved_listings",
            lambda: {"rawson-chubut", "rawson-buenos-aires"},
        )
        rows = [row for row in listed_cities([]) if row["label"] == "Rawson"]
        assert {row["id"] for row in rows} == {"rawson-chubut", "rawson-buenos-aires"}
        captions = " ".join(str(row.get("hint") or "") for row in rows)
        assert "Chubut" in captions
        assert "Buenos Aires" in captions
    finally:
        for cid in ("rawson-chubut", "rawson-buenos-aires"):
            forget_place(cid)
            CITIES.pop(cid, None)
            for token, owner in list(CITY_ALIASES.items()):
                if owner == cid or token == cid:
                    CITY_ALIASES.pop(token, None)
        reset_listed_places()


def test_place_hint_keeps_province_when_name_matches():
    from app.places import _place_hint

    assert _place_hint({"id": "cordoba", "label": "Córdoba", "province": "cordoba"}) == "Córdoba, Córdoba"
    assert _place_hint({"id": "caba", "label": "CABA", "province": "capital-federal"}) == "CABA"


def test_listed_cities_hide_pin_artifacts():
    from app.geo import CITY_ALIASES, CITIES, register_city
    from app.places import forget_place, listed_cities, remember_listed_place, reset_listed_places

    reset_listed_places()
    register_city(
        "cordoba-pins",
        label="Córdoba",
        lat=-31.416,
        lon=-64.183,
        builtin=False,
    )
    try:
        remember_listed_place("cordoba-pins")
        ids = {row["id"] for row in listed_cities([])}
        assert "cordoba-pins" not in ids
    finally:
        forget_place("cordoba-pins")
        CITIES.pop("cordoba-pins", None)
        for token, owner in list(CITY_ALIASES.items()):
            if owner == "cordoba-pins" or token == "cordoba-pins":
                CITY_ALIASES.pop(token, None)
        reset_listed_places()


def test_listed_cities_does_not_import_live_catalog():
    from app.geo import CITIES
    from app.places import listed_cities, reset_listed_places

    reset_listed_places()
    before = set(CITIES)
    listed_cities([])
    leaked = set(CITIES) - before
    assert not leaked


def test_place_from_suggestion_rejects_free_text(monkeypatch):
    from app import places

    monkeypatch.setattr(places, "search_places", lambda *a, **k: [])
    with pytest.raises(ValueError, match="sugerencias"):
        places.place_from_suggestion(query="asdfghciudadinventada")


def test_search_places_skips_geo_pinned_junk():
    from app.geo import register_city
    from app.places import reset_listed_places, search_places

    reset_listed_places()
    register_city(
        "departamento-anelo",
        label="Departamento Anelo",
        lat=-38.35,
        lon=-68.78,
        builtin=False,
    )
    rows = search_places("Departamento Anelo")
    assert not any(row.get("id") == "departamento-anelo" for row in rows)


def test_place_from_suggestion_rejects_geo_pinned_without_coords(monkeypatch):
    from app import places
    from app.geo import register_city

    places.reset_listed_places()
    register_city(
        "departamento-anelo",
        label="Departamento Anelo",
        lat=-38.35,
        lon=-68.78,
        builtin=False,
    )
    monkeypatch.setattr(places, "search_places", lambda *a, **k: [])
    with pytest.raises(ValueError, match="sugerencias"):
        places.place_from_suggestion("departamento-anelo")


def test_mar_del_plata_in_title_is_not_caba():
    from app.geo import pin_listing_city
    from app.place_api import (
        _candidates,
        _clean_name,
        _province_slug,
        listing_places,
        place_conflicts_city,
    )

    assert _province_slug("Buenos Aires") == "buenos-aires"

    assert _clean_name("Mar del Plata") == "Mar del Plata"
    item = Listing(
        source="zonaprop",
        source_id="mdp-1",
        url="https://www.zonaprop.com.ar/propiedades/clasificado/veclcain-casa-en-venta-3-dorm.-2-banos-cocheras-194-59581806.html",
        title="Casa en Venta - 3 Dorm. 2 Baños - Cocheras - 194 m2 - Mar del Plata",
        property_type="casa",
        city="caba",
        address="Canadá e/ Belgrano y Moreno",
        barrio="Oeste",
        extra={"search_city": "caba"},
    )
    names = {str(p.get("name") or "").lower() for p in listing_places(item, remote=False)}
    assert any("mar del plata" in n for n in names)
    assert any("mar del plata" in _clean_name(raw).lower() for raw, _hint in _candidates(item))
    mdp = next(p for p in listing_places(item, remote=False) if "mar del plata" in str(p.get("name") or "").lower())
    assert place_conflicts_city(mdp, "caba") is True
    assert listing_fits_city(item, "caba", remote=False) is False
    pin_listing_city(item)
    assert item.city != "caba"


def test_calle_mar_del_plata_in_floresta_stays_caba():
    item = Listing(
        source="properati",
        source_id="mdp-street",
        url="https://www.properati.com.ar/x",
        title="Apartamento en Venta en Floresta",
        property_type="departamento",
        city="caba",
        address="Calle Mar del Plata 901-999, Floresta",
        barrio="Floresta",
        lat=-34.62538,
        lon=-58.48658,
        extra={"search_city": "caba"},
    )
    assert listing_fits_city(item, "caba", remote=False) is True


def test_microcentro_in_corrientes_is_not_caba():
    item = Listing(
        source="zonaprop",
        source_id="ctes-micro",
        url="https://www.zonaprop.com.ar/x",
        title="Departamento 1 Dormitorio en Microcentro, Ideal Inversión!",
        property_type="departamento",
        city="caba",
        address="Colón 125",
        lat=-27.45567,
        lon=-58.98431,
        extra={"search_city": "caba"},
    )
    assert listing_fits_city(item, "caba", remote=False) is False


def test_calle_name_hit_matches_last_word_of_official_street():
    from app.place_api import _calle_name_hit

    row = {"nombre": "Abraham Mathews"}
    assert _calle_name_hit("mathews", row) is False
    assert _calle_name_hit("mathews", row, loose=True) is True
    assert _calle_name_hit("abraham mathews", row) is True
    assert _calle_name_hit("mayo", {"nombre": "25 de Mayo"}, loose=True) is False
    assert _calle_name_hit("castelli", {"nombre": "Juan José Castelli"}, loose=True) is True


def test_official_place_rejects_asentamiento_even_with_matching_municipio():
    from app.place_api import remember
    from app.places import official_place

    remember(
        "carpinchori",
        {
            "name": "Carpinchorí",
            "kind": "asentamiento",
            "province": "Entre Ríos",
            "lat": -30.95,
            "lon": -58.78,
            "municipio": "Carpinchorí",
            "localidad_censal": "Carpinchorí",
        },
    )
    assert official_place("carpinchori") is None


def test_official_place_rejects_nominatim_village():
    from app.place_api import remember
    from app.places import official_place

    remember(
        "el paraje",
        {"name": "El Paraje", "kind": "village", "province": "Córdoba", "lat": -31.1, "lon": -64.3},
    )
    assert official_place("el paraje") is None


def test_ensure_view_city_does_not_add_to_scrape_pool():
    from app.geo import CITY_ALIASES, CITIES, register_city
    from app.place_api import remember
    from app.places import ensure_view_city, listed_place_ids, reset_listed_places

    remember(
        "villa testina",
        {
            "name": "Villa Testina",
            "kind": "localidad",
            "province": "Córdoba",
            "lat": -31.2,
            "lon": -64.4,
            "municipio": "Villa Testina",
            "localidad_censal": "Villa Testina",
        },
    )
    reset_listed_places()
    register_city("villa-testina", label="Villa Testina", lat=-31.2, lon=-64.4, builtin=False)
    try:
        assert ensure_view_city("villa-testina") == "villa-testina"
        assert "villa-testina" not in listed_place_ids()
    finally:
        CITIES.pop("villa-testina", None)
        for token, owner in list(CITY_ALIASES.items()):
            if owner == "villa-testina" or token == "villa-testina":
                CITY_ALIASES.pop(token, None)
        reset_listed_places()


def test_scrape_pool_skips_unsearched_localidad(monkeypatch):
    from app.geo import CITY_ALIASES, CITIES, register_city
    from app.place_api import remember
    from app.places import remember_listed_place, reset_listed_places, scrape_place_ok
    from app.schedule import note_search, reset as reset_schedule

    remember(
        "salsipuedes",
        {
            "name": "Salsipuedes",
            "kind": "localidad",
            "province": "Córdoba",
            "lat": -31.14,
            "lon": -64.29,
            "municipio": "Salsipuedes",
            "localidad_censal": "Salsipuedes",
            "categoria": "Entidad",
        },
    )
    reset_listed_places()
    reset_schedule()
    register_city("salsipuedes", label="Salsipuedes", lat=-31.14, lon=-64.29, builtin=False)
    monkeypatch.setattr("app.places._ids_with_saved_listings", lambda: set())
    monkeypatch.setattr("app.places._ids_with_scrape_listings", lambda: set())
    try:
        remember_listed_place("salsipuedes")
        assert scrape_place_ok("salsipuedes") is False
        note_search("salsipuedes")
        assert scrape_place_ok("salsipuedes") is True
    finally:
        CITIES.pop("salsipuedes", None)
        for token, owner in list(CITY_ALIASES.items()):
            if owner == "salsipuedes" or token == "salsipuedes":
                CITY_ALIASES.pop(token, None)
        reset_listed_places()
        reset_schedule()
