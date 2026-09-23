from app.features import analyze
from app.laya_client import parse_answers
from app.listing_signals import (
    SIGNALS_VER,
    apply_laya_decisions,
    apply_signals,
    needs_signals,
    refill,
    signals_for,
)
from app.models import Listing
from app.scrapers import collect_feature_labels, publisher_bits, structured_credit


def _item(**kw) -> Listing:
    extra = dict(kw.pop("extra", None) or {})
    return Listing(
        source="zonaprop",
        source_id=kw.pop("source_id", "sig-1"),
        url=kw.pop("url", "https://example.com/sig"),
        title=kw.pop("title", "Departamento 2 ambientes"),
        property_type=kw.pop("property_type", "departamento"),
        city=kw.pop("city", "puerto-madryn"),
        description=kw.pop("description", ""),
        extra=extra,
        **kw,
    )


def test_parse_noul_gates_uncertain_answers():
    questions = {
        "is_owner_direct": {"type": "noul"},
        "is_mortgage_eligible": {"type": "noul"},
        "has_low_expenses": {"type": "noul"},
    }
    decisions = parse_answers(
        {
            "answers": {
                "is_owner_direct": {"noul": 0.91},
                "is_mortgage_eligible": {"noul": 0.12},
                "has_low_expenses": {"noul": 0.51},
            }
        },
        questions,
    )
    by_key = {d.question_key: d for d in decisions}
    assert by_key["is_owner_direct"].answer is True
    assert by_key["is_mortgage_eligible"].answer is False
    assert by_key["has_low_expenses"].answer is None


def test_text_signals_dueño_credito_urgencia_and_quiet():
    item = _item(
        description=(
            "Dueño directo vende. Apto crédito hipotecario. Contrafrente silencioso. "
            "Vendo urgente, me voy del país. PH sin expensas."
        )
    )
    sig = signals_for(item)
    assert sig["owner_direct"] is True
    assert sig["mortgage_credit"] is True
    assert sig["urgent_sale"] is True
    assert sig["environment"] == "quiet"
    assert sig["low_expenses"] is True


def test_does_not_invent_quiet_urgent_or_noise_from_weak_phrases():
    item = _item(
        title="Depto sobre Avenida Colón",
        description="Contrafrente. Precio de oportunidad. Zona comercial. Por herencia.",
        address="Avenida Colón 1200",
    )
    sig = signals_for(item)
    assert sig["environment"] == ""
    assert sig["urgent_sale"] is None


def test_stale_extra_does_not_keep_invented_quiet_or_urgent():
    item = _item(
        description="Depto 3 ambientes al frente.",
        extra={"environment": "noisy", "urgent_sale": True},
    )
    sig = signals_for(item)
    assert sig["environment"] == ""
    assert sig["urgent_sale"] is None


def test_explicit_no_credit_beats_laya_yes():
    item = _item(description="Departamento no apto crédito.")
    item.laya_is_mortgage_eligible = True
    item.laya_mortgage_confidence = 0.99
    sig = signals_for(item)
    assert sig["mortgage_credit"] is False


def test_laya_fills_owner_and_mortgage_without_swapping_fields():
    item = _item(description="Depto 3 ambientes 70 m2")
    from app.laya_client import LayaDecision

    apply_laya_decisions(
        item,
        [
            LayaDecision("is_owner_direct", "noul", True, 0.88),
            LayaDecision("is_mortgage_eligible", "noul", True, 0.8),
            LayaDecision("has_low_expenses", "noul", False, 0.7),
            LayaDecision("shows_urgency", "noul", None, 0.5),
            LayaDecision("environment_noise", "choice", "quiet", 0.7, {"quiet": 0.7}),
            LayaDecision("property_condition", "choice", "brand_new", 0.8, {"brand_new": 0.8}),
        ],
    )
    assert item.laya_is_owner_direct is True
    assert item.laya_owner_confidence == 0.88
    assert item.laya_is_mortgage_eligible is True
    assert item.laya_mortgage_confidence == 0.8
    sig = signals_for(item)
    assert sig["owner_direct"] is True
    assert sig["mortgage_credit"] is True
    assert sig["environment"] == "quiet"
    assert sig["condition"] == "a estrenar"


def test_analyze_writes_product_fields_not_ai_labels():
    item = _item(description="Propietario vende. Apto crédito. A estrenar.")
    analyze(item)
    assert item.extra.get("mortgage_credit") is True
    assert item.extra.get("owner_direct") is True
    assert item.extra.get("condition") == "a estrenar"
    public = item.to_public_dict()
    assert public["mortgage_credit"] is True
    assert public["owner_direct"] is True
    assert "laya_is_owner_direct" not in public
    assert "Filtros IA" not in str(public)


def test_collect_nested_general_features_and_credit_no():
    general = {
        "Grupo": {
            "features": [
                {"label": "Apto crédito", "value": "Sí"},
                {"label": "Apto profesional", "value": "No"},
                {"label": "Pileta", "value": "Sí"},
            ]
        }
    }
    labels = collect_feature_labels(general)
    assert "Apto crédito" in labels
    assert "Pileta" in labels
    assert "Apto profesional" not in labels
    assert structured_credit(general) is True
    assert structured_credit({"x": {"label": "Apto crédito", "value": "No"}}) is False


def test_publisher_particular_is_direct():
    bits = publisher_bits({"publisher": {"name": "Ana Pérez", "publisherType": "PARTICULAR"}})
    assert bits["publisher_direct"] is True
    agency = publisher_bits(
        {"publisher": {"name": "Inmobiliaria Sur", "url": "/inmobiliarias/sur-1.html"}}
    )
    assert agency["publisher_direct"] is False


def test_apply_signals_does_not_invent_condition_from_buenos_aires():
    item = _item(
        title="Departamento en Buenos Aires",
        description="Living comedor luminoso en Capital Federal.",
    )
    apply_signals(item)
    assert not item.extra.get("condition")
    assert item.extra.get("bright") is True


def test_text_signals_balcony_light_growing_view_patio():
    item = _item(
        description=(
            "Depto con balcón al frente, mucha luz y vista abierta. "
            "Barrio en crecimiento. Patio propio."
        )
    )
    sig = signals_for(item)
    assert sig["has_balcony"] is True
    assert sig["bright"] is True
    assert sig["growing_area"] is True
    assert sig["open_view"] is True
    assert sig["has_patio"] is True


def test_does_not_invent_balcony_light_growing_from_weak_phrases():
    item = _item(
        title="Depto cerca del parque",
        description=(
            "Terraza del edificio. Luz y agua. Plusvalía. "
            "Zona inundable. Jardín de infantes a una cuadra. Vista a la calle."
        ),
        extra={"amenities": ["Terraza", "Luz y gas"]},
    )
    sig = signals_for(item)
    assert sig["has_balcony"] is not True
    assert sig["bright"] is not True
    assert sig["growing_area"] is not True
    assert sig["open_view"] is not True
    assert sig["has_patio"] is not True
    assert sig["has_terrace"] is not True


def test_sin_balcon_beats_the_word_elsewhere():
    item = _item(description="Departamento sin balcón, interno.")
    sig = signals_for(item)
    assert sig["has_balcony"] is False


def test_portal_balcony_amenity_counts():
    item = _item(description="Departamento 2 ambientes al frente.", extra={"amenities": ["Balcón"]})
    sig = signals_for(item)
    assert sig["has_balcony"] is True


def test_extracted_lowercase_patio_or_balcony_is_ignored():
    item = _item(
        title="Lote en Parque Ecológico",
        description="Terreno en el Parque Ecológico El Doradillo, con terraza de uso común.",
        extra={"amenities": ["patio", "balcón", "terraza"]},
    )
    sig = signals_for(item)
    assert sig["has_patio"] is not True
    assert sig["has_balcony"] is not True
    assert sig["has_terrace"] is not True


def test_text_signals_garage_and_own_terrace():
    item = _item(description="PH con cochera cubierta y terraza propia.")
    sig = signals_for(item)
    assert sig["has_garage"] is True
    assert sig["has_terrace"] is True


def test_parking_field_counts_as_garage():
    item = _item(description="Departamento 2 ambientes.", parking=1)
    sig = signals_for(item)
    assert sig["has_garage"] is True


def test_does_not_invent_garage_or_shared_terrace():
    item = _item(
        description="A 2 cuadras de un garage comercial. Terraza de uso común del edificio.",
        extra={"amenities": ["Terraza"]},
    )
    sig = signals_for(item)
    assert sig["has_garage"] is not True
    assert sig["has_terrace"] is not True


def test_laya_fills_balcony_and_flips_false_lot():
    from app.laya_client import LayaDecision
    from app.listing_signals import apply_laya_decisions, apply_signals

    item = _item(
        property_type="terreno",
        description="Lote 20 x 30 con servicios.",
    )
    apply_laya_decisions(
        item,
        [
            LayaDecision("has_balcony", "noul", True, 0.8),
            LayaDecision("contains_dwelling", "noul", True, 0.9),
            LayaDecision("property_kind", "choice", "casa", 0.86, {"casa": 0.86, "terreno": 0.1}),
        ],
    )
    apply_signals(item)
    assert item.extra.get("has_balcony") is True
    assert item.property_type == "casa"
    assert "no es lote vacío" in " ".join(item.extra.get("data_fixes") or [])


def test_analyze_reclassifies_lote_con_casa():
    item = _item(
        property_type="terreno",
        title="Lote con casa en El Doradillo",
        description="Vivienda existente de 3 dormitorios, lista para habitar.",
    )
    analyze(item)
    assert item.property_type == "casa"


def test_analyze_keeps_proyecto_de_casa_as_terreno():
    item = _item(
        property_type="terreno",
        title="Lote en venta",
        description="Lote con proyecto de casa a terminar de 90 m2 que consta de 2 dormitorios.",
        bedrooms=2,
        bathrooms=1,
        covered_m2=90,
    )
    analyze(item)
    assert item.property_type == "terreno"


def test_analyze_reclassifies_terreno_with_standing_house_layout():
    item = _item(
        property_type="terreno",
        title="Oportunidad en Puerto Madryn",
        description="Construcción estilo Fonavi, living comedor, cocina y patio.",
        bedrooms=2,
        bathrooms=1,
        covered_m2=72,
    )
    analyze(item)
    assert item.property_type == "casa"


def test_apply_signals_stamps_version():
    item = _item(description="Depto 2 ambientes.")
    assert needs_signals(item) is True
    apply_signals(item)
    assert item.extra.get("signals_ver") == SIGNALS_VER
    assert needs_signals(item) is False


def test_signals_refill_persists_old_listings(tmp_path, monkeypatch):
    from app import listing_signals, store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "listings.sqlite")
    store.init()
    lot = _item(
        source_id="lot",
        property_type="terreno",
        title="Lote con casa en El Doradillo",
        description="Vivienda existente de 3 dormitorios, lista para habitar.",
        bedrooms=3,
        bathrooms=1,
        covered_m2=90,
    )
    garage = _item(source_id="gar", description="PH con cochera cubierta.")
    done = _item(source_id="ok", description="Depto al frente.", extra={"signals_ver": SIGNALS_VER})
    dup = _item(source_id="dup", description="PH con cochera.", extra={"duplicate_of": "zonaprop:gar"})
    store.upsert_many([lot, garage, done, dup])
    listing_signals._refill_announced = False
    n = refill("caba")
    assert n == 2
    stored_lot = store.get_listing("zonaprop:lot")
    stored_gar = store.get_listing("zonaprop:gar")
    stored_ok = store.get_listing("zonaprop:ok")
    stored_dup = store.get_listing("zonaprop:dup")
    assert stored_lot is not None and stored_lot.property_type == "casa"
    assert stored_lot.extra.get("signals_ver") == SIGNALS_VER
    assert stored_gar is not None and stored_gar.extra.get("has_garage") is True
    assert stored_ok is not None and stored_ok.property_type == "departamento"
    assert stored_dup is not None and stored_dup.extra.get("signals_ver") != SIGNALS_VER
    assert refill("caba") == 0


def test_public_dict_corrects_false_lot_type():
    item = _item(
        property_type="terreno",
        title="Lote con casa en El Doradillo",
        description="Vivienda existente de 3 dormitorios, lista para habitar.",
        bedrooms=3,
        bathrooms=1,
        covered_m2=90,
    )
    public = item.to_public_dict()
    assert public["property_type"] == "casa"
    assert item.property_type == "casa"
    assert "no es lote vacío" in " ".join(public.get("data_fixes") or [])
