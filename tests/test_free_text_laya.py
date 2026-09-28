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
