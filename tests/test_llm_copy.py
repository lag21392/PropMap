from app.llm_copy import (
    COPY_SCHEMA,
    apply_copy,
    build_copy_prompt,
    display_description,
    facts_for_copy,
    needs_copy,
    parse_copy_text,
)
from app.llm_enrich import LLM_SCHEMA
from app.models import Listing


def _item(**kw) -> Listing:
    extra = dict(kw.pop("extra", None) or {})
    return Listing(
        source="zonaprop",
        source_id=kw.pop("source_id", "1"),
        url=kw.pop("url", "https://example.com/1"),
        title=kw.pop("title", "Depto en Palermo"),
        property_type=kw.pop("property_type", "departamento"),
        city=kw.pop("city", "caba"),
        address=kw.pop("address", "Niceto Vega 1200"),
        rooms=kw.pop("rooms", 3),
        bedrooms=kw.pop("bedrooms", 2),
        bathrooms=kw.pop("bathrooms", 1),
        covered_m2=kw.pop("covered_m2", 72),
        details_scraped=kw.pop("details_scraped", True),
        description=kw.pop(
            "description",
            "Departamento de 3 ambientes con living comedor, cocina independiente y balcón al frente.",
        ),
        extra=extra,
        **kw,
    )


def _ready(**kw) -> Listing:
    extra = dict(kw.pop("extra", None) or {})
    extra.setdefault("llm_ready", True)
    extra.setdefault("llm_ver", LLM_SCHEMA)
    extra.setdefault("llm_partial", False)
    extra.setdefault("mortgage_credit", True)
    extra.setdefault("amenities", ["balcón", "parrilla"])
    return _item(extra=extra, **kw)


def test_needs_copy_waits_for_llm_then_accepts_ready():
    pending = _item()
    assert needs_copy(pending) is False
    ready = _ready()
    assert needs_copy(ready) is True
    apply_copy(ready, "Departamento de 3 ambientes en Palermo, con 72 m² cubiertos, balcón y parrilla. Apto crédito.")
    assert needs_copy(ready) is False
    assert "72" in (ready.extra["copy"]["text"] or "")


def test_apply_copy_keeps_original_description():
    item = _ready()
    original = item.description
    apply_copy(
        item,
        "Depto de 3 ambientes en Palermo con living comedor, 72 m² cubiertos y balcón al frente. Apto crédito.",
    )
    assert item.description == original
    assert display_description(item) != original
    assert "Palermo" in display_description(item)


def test_display_description_prefers_user_edit():
    item = _ready(
        extra={"user_edits": {"description": "Texto que cargó el dueño a mano."}},
    )
    apply_copy(item, "Departamento de 3 ambientes en Palermo con balcón y parrilla. Apto crédito hipotecario.")
    assert display_description(item) == "Texto que cargó el dueño a mano."
    assert needs_copy(item) is False


def test_public_listing_shows_generated_copy():
    item = _ready()
    apply_copy(
        item,
        "Departamento de 3 ambientes en Palermo, 72 m² cubiertos, con balcón y parrilla. Apto crédito.",
    )
    public = item.to_public_dict()
    assert public["description"] == item.extra["copy"]["text"]
    assert item.description.startswith("Departamento de 3 ambientes con living")


def test_display_description_hides_the_original_and_old_copy():
    item = _ready()
    assert display_description(item) == ""
    item.extra["copy"] = {"text": "Texto viejo con teléfono 11 5555-5555.", "ver": 1}
    assert display_description(item) == ""


def test_copy_prompt_uses_original_facts_without_the_address_or_agency():
    from app.llm_copy import entorno_for_copy

    item = _ready()
    item.description = (
        "Departamento de 3 ambientes con living comedor, cocina independiente y balcón al frente. "
        "Niceto Vega 1200. Llamar a Inmobiliaria López al 11 5555-5555."
    )
    item.extra["access"] = {
        "categories": {
            "subway": {"present": True, "kind": "Subte"},
            "transport": {"present": True, "kind": "Parada"},
            "school": {"present": True, "kind": "Universidad"},
            "shop": {"present": True, "name": "Inmobiliaria López"},
        }
    }
    prompt = build_copy_prompt(item)
    assert "DATOS:" in prompt
    assert "ENTORNO:" in prompt
    assert "TEXTO:" in prompt
    assert "living comedor" in prompt
    assert "Niceto Vega" not in prompt
    assert "5555" not in prompt
    assert "López" not in prompt
    assert "universidad" in prompt
    assert any("subte" in part for part in entorno_for_copy(item))
    facts = " ".join(facts_for_copy(item))
    assert "3" in facts
    assert "balcón" in facts


def test_parse_copy_strips_pii_and_think():
    raw = (
        "<think>no</think> Qué es: Departamento de 3 ambientes en Palermo con balcón.\n"
        "Detalles: Consultar al 11 5555-5555 o a la inmobiliaria López.\n"
        "Cerca: Cerca hay subte y universidad."
    )
    text = parse_copy_text(raw)
    assert "Palermo" in text
    assert "universidad" in text
    assert "5555" not in text
    assert "inmobiliaria" not in text.lower()
    assert "think" not in text.lower()


def test_copy_take_ready_feeds_gpu_queue():
    from app import llm_copy

    item = _ready(source_id="gpu-copy")
    llm_copy._ready.clear()
    llm_copy._ready.append((item.id, item, [{"role": "user", "content": "x"}]))
    try:
        job = llm_copy.take_ready()
        assert job is not None
        assert job[0] == item.id
        assert llm_copy.take_ready() is None
    finally:
        llm_copy._ready.clear()


def test_copy_refill_fills_even_if_extract_has_waiting_queue(tmp_path, monkeypatch):
    from app import llm_copy, llm_enrich, store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "listings.sqlite")
    store.init()
    monkeypatch.setattr(llm_copy, "enabled", lambda: True)
    monkeypatch.setattr(llm_copy, "_ensure_workers_locked", lambda: None)
    llm_copy._queue.clear()
    llm_copy._seen.clear()
    llm_copy._skip_until.clear()
    llm_copy._ready.clear()
    store.upsert_many([_ready(source_id="ready-copy")])
    llm_enrich._queue.append("zonaprop:busy")
    try:
        n = llm_copy.refill("caba")
        queued = set(llm_copy._queue)
        assert n == 1
        assert queued == {"zonaprop:ready-copy"}
    finally:
        llm_enrich._queue.clear()


def test_copy_buffer_runs_when_extract_has_nothing_ready(tmp_path, monkeypatch):
    from app import llm_copy, llm_enrich, store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "listings.sqlite")
    store.init()
    monkeypatch.setattr(llm_copy, "enabled", lambda: True)
    monkeypatch.setattr(llm_copy, "_ensure_workers_locked", lambda: None)
    monkeypatch.setattr(llm_copy, "fichas_pending", lambda: True)
    llm_copy._queue.clear()
    llm_copy._seen.clear()
    llm_copy._ready.clear()
    llm_enrich._since_copy = 0
    store.upsert_many([_ready(source_id="ready-copy")])
    assert llm_copy.refill("caba") == 1
    llm_copy._ready.append(("copy:hold", None, []))
    llm_enrich._ready.clear()
    kind, job = llm_enrich._next_gpu_job(0)
    assert kind == "copy"
    assert job[0] == "copy:hold"
    llm_copy._ready.clear()
    llm_enrich._since_copy = 0


def test_copy_backlog_skips_until_llm_done(tmp_path, monkeypatch):
    from app import store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "listings.sqlite")
    store.init()
    pending = _item(source_id="pending")
    ready = _ready(source_id="ready")
    store.upsert_many([pending, ready])
    ids = [row.id for row in store.fetch_copy_backlog(8, prefer_city="caba")]
    assert ids == ["zonaprop:ready"]
    assert store.get_listing("zonaprop:ready").extra.get("copy") is None


def test_facts_and_display_drop_needless_decimals():
    item = _ready(bathrooms=1.0, covered_m2=75.0, rooms=3, bedrooms=2)
    item.extra["llm_ver"] = LLM_SCHEMA
    item.extra["copy"] = {
        "text": "Detalles: Tiene 3 ambientes, 2 dormitorios, 1.0 baños, 75.0 m² cubiertos y es luminoso.",
        "ver": COPY_SCHEMA,
        "llm_ver": LLM_SCHEMA,
    }
    facts = " ".join(facts_for_copy(item))
    assert "baños: 1" in facts
    assert "m² cubiertos: 75" in facts
    assert "1.0" not in facts
    assert "75.0" not in facts
    shown = display_description(item)
    assert "1 baño" in shown
    assert "75 m²" in shown
    assert "1.0" not in shown
    assert "75.0" not in shown
