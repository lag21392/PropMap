from app.laya_client import LayaDecision
from app.free_text_search import _place_phrase, filters_for_query, merge_search_decisions


def test_laya_sets_each_discrete_filter_and_keeps_price():
    decisions = [
        LayaDecision("property_kind", "choice", "casa", 0.9),
        LayaDecision("min_beds", "choice", "3", 0.8),
        LayaDecision("min_rooms", "choice", "ninguno", 0.7),
        LayaDecision("trait_patio", "noul", True, 0.91),
        LayaDecision("trait_garage", "noul", False, 0.8),
        LayaDecision("deal_bar", "choice", "oportunidad", 0.77),
    ]
    out = merge_search_decisions({"maxPrice": 200000, "minBeds": 2}, decisions)
    assert out["typeFilter"] == "casa"
    assert out["minBeds"] == 3
    assert out["maxPrice"] == 200000
    assert out["traits"] == ["patio"]
    assert out["dealBar"] == 70


def test_low_confidence_does_not_override_rules():
    decisions = [LayaDecision("property_kind", "choice", "terreno", 0.2)]
    out = merge_search_decisions({"typeFilter": "casa"}, decisions)
    assert out["typeFilter"] == "casa"


def test_filters_for_query_merges_laya_place_and_price(monkeypatch):
    monkeypatch.setattr(
        "app.laya_client.decide_questions",
        lambda state, questions: [
            LayaDecision("property_kind", "choice", "casa", 0.9),
            LayaDecision("min_beds", "choice", "3", 0.8),
            LayaDecision("trait_patio", "noul", True, 0.9),
        ],
    )
    monkeypatch.setattr(
        "app.free_text_search.resolve_place",
        lambda query: {
            "id": "caba",
            "label": "CABA",
            "lat": -34.6,
            "lon": -58.4,
            "province": "caba",
            "zoom": 12,
            "barrio": "Palermo",
        },
    )
    filters, place, source = filters_for_query("casa 3 dormitorios con patio en Palermo hasta 200k USD")
    assert source == "laya"
    assert filters["typeFilter"] == "casa"
    assert filters["minBeds"] == 3
    assert filters["maxPrice"] == 200000
    assert "patio" in filters["traits"]
    assert filters["cityFilter"] == "caba"
    assert filters["barrioFilter"] == "Palermo"
    assert place["id"] == "caba"


def test_pick_barrio_prefers_the_place_after_de():
    from app.free_text_search import pick_barrio, pick_barrios

    names = ["Barracas", "Belgrano", "Palermo", "Parque de los Patricios"]
    assert pick_barrio("monohambiente luminoso barracas de belgrano", names) == "Belgrano"
    assert pick_barrios("monohambiente luminoso barracas de belgrano", names) == ["Belgrano"]
    assert pick_barrio("departamento en barracas", names) == "Barracas"
    assert pick_barrios("casas y departamentos en belgrano y palermo", names) == ["Belgrano", "Palermo"]
    assert pick_barrio("casa en parque de los patricios", names) == "Parque de los Patricios"


def test_monoambiente_sets_one_room_and_keeps_the_current_barrio(monkeypatch):
    monkeypatch.setattr("app.laya_client.decide_questions", lambda state, questions: [])
    monkeypatch.setattr(
        "app.free_text_search.barrio_in_city",
        lambda query, city: "Belgrano" if city == "caba" else "",
    )
    monkeypatch.setattr("app.free_text_search.resolve_place", lambda query: None)
    filters, place, source = filters_for_query(
        "monohambiente luminoso barracas de belgrano",
        city="caba",
    )
    assert source == "rules"
    assert place is None
    assert filters["typeFilter"] == "departamento"
    assert filters["minRooms"] == 1
    assert filters["maxRooms"] == 1
    assert filters["traits"] == ["bright"]
    assert filters["cityFilter"] == "caba"
    assert filters["barrioFilter"] == "Belgrano"


def test_several_types_and_places_stay_as_lists(monkeypatch):
    monkeypatch.setattr("app.laya_client.decide_questions", lambda state, questions: [])
    monkeypatch.setattr(
        "app.free_text_search.barrio_in_city",
        lambda query, city: ["Belgrano", "Palermo"] if city == "caba" else [],
    )
    monkeypatch.setattr(
        "app.free_text_search.zonas_in_city",
        lambda query, city: ["Zona Norte", "Zona Sur"] if city == "caba" else [],
    )
    monkeypatch.setattr("app.free_text_search.resolve_place", lambda query: None)
    filters, place, source = filters_for_query(
        "casas y departamentos en belgrano y palermo zona norte y zona sur",
        city="caba",
    )
    assert source == "rules"
    assert place is None
    assert filters["typeFilter"] == ["casa", "departamento"]
    assert filters["barrioFilter"] == ["Belgrano", "Palermo"]
    assert filters["zonaFilter"] == ["Zona Norte", "Zona Sur"]
    assert filters["cityFilter"] == "caba"


def test_place_phrase_keeps_the_location_words():
    assert _place_phrase("casa 3 dormitorios palermo hasta 200k usd") == "palermo"
    assert _place_phrase("casa en villa carlos paz hasta 200k") == "villa carlos paz"
    assert _place_phrase("casa luminosa madryn") == "madryn"
    assert _place_phrase("ciudad de rawson") == "rawson"
    assert _place_phrase("ph o casa en la  rosario a menos de 200000") == "rosario"
    assert _place_phrase("puerto piramides") == "puerto piramides"
    assert "puerto piramides" in _place_phrase("casa grande linda nueva cerca plaza en puerto piramides")


def test_city_nickname_selects_the_loaded_city(monkeypatch):
    from app import places

    monkeypatch.setattr("app.laya_client.decide_questions", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("app.free_text_search.barrio_in_city", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("app.free_text_search.zonas_in_city", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("app.listings_cache.cached_city_ids", lambda: ["puerto-madryn", "rawson-chubut"])
    monkeypatch.setattr("app.place_api.lookup_place", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("red")))
    monkeypatch.setitem(places.CITIES, "puerto-madryn", {
        "id": "puerto-madryn",
        "label": "Puerto Madryn",
        "lat": -42.77,
        "lon": -65.04,
        "province": "Chubut",
        "zoom": 13,
        "aliases": [],
    })
    monkeypatch.setitem(places.CITIES, "rawson", {
        "id": "rawson",
        "label": "Rawson",
        "lat": -31.57,
        "lon": -68.54,
        "province": "San Juan",
        "zoom": 13,
        "aliases": [],
    })
    monkeypatch.setitem(places.CITIES, "rawson-chubut", {
        "id": "rawson-chubut",
        "label": "Rawson",
        "lat": -43.3,
        "lon": -65.1,
        "province": "Chubut",
        "zoom": 13,
        "aliases": [],
    })

    filters, place, source = filters_for_query("casa luminosa madryn", city="caba")
    assert source == "rules"
    assert place["id"] == "puerto-madryn"
    assert filters["cityFilter"] == "puerto-madryn"
    assert filters["typeFilter"] == "casa"
    assert "bright" in filters["traits"]

    filters, place, _source = filters_for_query("ciudad de rawson", city="caba")
    assert place["id"] == "rawson-chubut"
    assert filters["cityFilter"] == "rawson-chubut"


def test_price_tail_does_not_hide_the_city_or_invent_traits(monkeypatch):
    from app import places
    from app.laya_client import LayaDecision

    monkeypatch.setattr(
        "app.laya_client.decide_questions",
        lambda *_args, **_kwargs: [
            LayaDecision("property_kind", "choice", "departamento", 0.95),
            LayaDecision("trait_balcony", "noul", True, 0.9),
            LayaDecision("trait_bright", "noul", True, 0.9),
            LayaDecision("trait_credit", "noul", True, 0.9),
        ],
    )
    monkeypatch.setattr("app.free_text_search.barrio_in_city", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("app.free_text_search.zonas_in_city", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("app.listings_cache.cached_city_ids", lambda: ["rosario"])
    monkeypatch.setattr("app.place_api.lookup_place", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("red")))
    monkeypatch.setitem(places.CITIES, "rosario", {
        "id": "rosario",
        "label": "Rosario",
        "lat": -32.95,
        "lon": -60.64,
        "province": "Santa Fe",
        "zoom": 13,
        "aliases": [],
    })

    assert _place_phrase("casa luminosa en la rosario a menos de 80 mil") == "rosario"
    filters, place, source = filters_for_query(
        "casa luminosa en la rosario a menos de 80 mil",
        city="caba",
    )
    assert source == "laya"
    assert place["id"] == "rosario"
    assert filters["cityFilter"] == "rosario"
    assert filters["typeFilter"] == "casa"
    assert filters["maxPrice"] == 80000
    assert filters["traits"] == ["bright"]

    filters, place, _source = filters_for_query("ciudad de rosario", city="caba")
    assert place["id"] == "rosario"
    assert "typeFilter" not in filters
    assert "traits" not in filters


def test_unloaded_city_in_the_sentence_becomes_the_search_place(monkeypatch):
    from app import place_api, places

    place_api.reset_cache()
    place_api.remember("rosario", {
        "name": "Rosario",
        "province": "Santa Fe",
        "lat": -32.95,
        "lon": -60.64,
        "kind": "localidad",
    })
    place_api.remember("puerto piramides", {
        "name": "Puerto Pirámides",
        "province": "Chubut",
        "lat": -42.57,
        "lon": -64.28,
        "kind": "localidad",
    })
    place_api.remember("palermo", {
        "name": "Palermo",
        "province": "Ciudad Autónoma de Buenos Aires",
        "lat": -34.58,
        "lon": -58.43,
        "kind": "asentamiento",
    })
    monkeypatch.setattr("app.laya_client.decide_questions", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("app.free_text_search.barrio_in_city", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("app.free_text_search.zonas_in_city", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("app.listings_cache.cached_city_ids", lambda: ["puerto-madryn"])
    real_lookup = place_api.lookup_place
    monkeypatch.setattr(
        "app.place_api.lookup_place",
        lambda name, province_hint=None, remote=True: real_lookup(name, province_hint=province_hint, remote=False),
    )
    monkeypatch.setattr(
        "app.place_api.provinces",
        lambda: [
            {"id": "82", "nombre": "Santa Fe", "slug": "santa-fe"},
            {"id": "26", "nombre": "Chubut", "slug": "chubut"},
            {"id": "02", "nombre": "Ciudad Autónoma de Buenos Aires", "slug": "capital-federal"},
        ],
    )
    monkeypatch.setitem(places.CITIES, "puerto-madryn", {
        "id": "puerto-madryn",
        "label": "Puerto Madryn",
        "lat": -42.77,
        "lon": -65.04,
        "province": "Chubut",
        "zoom": 13,
        "aliases": ["madryn"],
        "radius_km": 25,
    })
    monkeypatch.setitem(places.CITIES, "caba", {
        "id": "caba",
        "label": "CABA",
        "lat": -34.6,
        "lon": -58.4,
        "province": "capital-federal",
        "zoom": 12,
        "aliases": [],
        "radius_km": 16,
    })

    filters, place, _source = filters_for_query(
        "ph o casa en la  rosario a menos de 200000",
        city="puerto-madryn",
    )
    assert place["id"] == "rosario-santa-fe"
    assert place["label"] == "Rosario"
    assert filters["cityFilter"] == "rosario-santa-fe"
    assert filters["maxPrice"] == 200000
    assert set(filters["typeFilter"]) == {"casa", "ph"}
    assert "barrioFilter" not in filters

    filters, place, _source = filters_for_query("casa en puerto piramides", city="puerto-madryn")
    assert place["label"] == "Puerto Pirámides"
    assert filters["cityFilter"] == "puerto-piramides-chubut"
    assert filters["typeFilter"] == "casa"
    assert "barrioFilter" not in filters

    filters, place, _source = filters_for_query("departamento en palermo", city="puerto-madryn")
    assert place["id"] == "caba"
    assert filters["cityFilter"] == "caba"
    assert filters["barrioFilter"] == "Palermo"
    assert filters["typeFilter"] == "departamento"
    place_api.reset_cache()


def _caba_and_flores_cities(monkeypatch):
    from app import places

    monkeypatch.setattr("app.laya_client.decide_questions", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("app.place_api.lookup_place", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("red")))
    monkeypatch.setattr("app.listings_cache.cached_city_ids", lambda: ["caba", "flores"])
    monkeypatch.setattr(
        "app.geo.barrios_for",
        lambda city: [
            {"name": "Recoleta"},
            {"name": "Flores"},
            {"name": "Palermo"},
            {"name": "Belgrano"},
            {"name": "Villa Flores"},
        ] if city == "caba" else [],
    )
    monkeypatch.setitem(places.CITIES, "caba", {
        "id": "caba",
        "label": "CABA",
        "lat": -34.6,
        "lon": -58.4,
        "province": "capital-federal",
        "zoom": 12,
        "aliases": ["capital federal"],
    })
    monkeypatch.setitem(places.CITIES, "flores", {
        "id": "flores",
        "label": "Flores",
        "lat": -34.63,
        "lon": -58.46,
        "province": "capital-federal",
        "zoom": 14,
        "aliases": [],
    })


def test_or_between_barrios_keeps_each_name():
    from app.free_text_search import pick_barrios

    names = ["Recoleta", "Flores", "Palermo", "Villa Flores"]
    assert pick_barrios(
        "depto o casa luminosa en caba en el barrio recoleta o flores menos de 100000",
        names,
    ) == ["Recoleta", "Flores"]
    assert pick_barrios("casa en palermo y belgrano", ["Palermo", "Belgrano", "Recoleta"]) == ["Palermo", "Belgrano"]


def test_caba_sentence_sets_both_barrios_price_and_types(monkeypatch):
    _caba_and_flores_cities(monkeypatch)
    query = "depto o casa luminosa en caba en el barrio recoleta o flores menos de 100000"
    filters, place, _source = filters_for_query(query, city="puerto-madryn")
    assert place["id"] == "caba"
    assert filters["cityFilter"] == "caba"
    assert filters["barrioFilter"] == ["Recoleta", "Flores"]
    assert set(filters["typeFilter"]) == {"casa", "departamento"}
    assert filters["traits"] == ["bright"]
    assert filters["maxPrice"] == 100000


def test_same_barrios_when_the_search_already_is_in_caba(monkeypatch):
    _caba_and_flores_cities(monkeypatch)
    filters, place, _source = filters_for_query(
        "depto o casa luminosa en el barrio recoleta o flores menos de 100000",
        city="caba",
    )
    assert place is None
    assert filters["cityFilter"] == "caba"
    assert filters["barrioFilter"] == ["Recoleta", "Flores"]
    assert set(filters["typeFilter"]) == {"casa", "departamento"}
    assert filters["maxPrice"] == 100000


def test_one_barrio_and_mil_price_stay_in_the_current_city(monkeypatch):
    _caba_and_flores_cities(monkeypatch)
    filters, place, _source = filters_for_query(
        "depto luminoso en recoleta menos de 80 mil",
        city="caba",
    )
    assert place is None
    assert filters["cityFilter"] == "caba"
    assert filters["barrioFilter"] == "Recoleta"
    assert filters["typeFilter"] == "departamento"
    assert filters["traits"] == ["bright"]
    assert filters["maxPrice"] == 80000


def test_y_between_barrios_does_not_open_another_city(monkeypatch):
    _caba_and_flores_cities(monkeypatch)
    filters, place, _source = filters_for_query(
        "casa y depto en palermo y belgrano",
        city="caba",
    )
    assert place is None
    assert filters["cityFilter"] == "caba"
    assert filters["barrioFilter"] == ["Palermo", "Belgrano"]
    assert set(filters["typeFilter"]) == {"casa", "departamento"}


def test_search_uses_the_gpu_server_instead_of_loading_torch(monkeypatch):
    from app import laya_client

    monkeypatch.setenv("LAYA_SERVE_URL", "http://laya-gpu:8000")
    monkeypatch.setenv("LAYA_MODEL", "multilingual")
    monkeypatch.setattr(laya_client, "_load_local", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("local")))

    kind, endpoint, model = laya_client._load_backend()
    assert kind == "http"
    assert endpoint == "http://laya-gpu:8000/v1/systemone"
    assert model == "multilingual"
