from app.geo import remember_city_polygons
from app.llm_enrich import _run_tool, apply_analysis
from app.llm_fields import classify_credit, classify_field, field_catalog, normalize_type
from app.models import Listing


DESEMBARCO_RING = [
    [-42.7875, -65.0265],
    [-42.7875, -65.0140],
    [-42.7820, -65.0125],
    [-42.7795, -65.0160],
    [-42.7795, -65.0265],
    [-42.7875, -65.0265],
]


def _inject_desembarco() -> None:
    remember_city_polygons(
        "puerto-madryn",
        [
            {
                "name": "Desembarco",
                "zona": "Zona Sur",
                "lat": -42.7835,
                "lon": -65.0195,
                "ring": DESEMBARCO_RING,
            }
        ],
    )


def test_type_catalog_and_aliases():
    catalog = field_catalog("tipo")
    ids = {row["id"] for row in catalog["valores"]}
    assert ids == {"casa", "departamento", "ph", "terreno", "local", "oficina", "galpon"}
    assert normalize_type("apartamento") == "departamento"
    assert normalize_type("lote en venta") == "terreno"
    assert normalize_type("dúplex") == "ph"
    assert classify_field("tipo", "Terreno 300 m2 lote")["valor"] == "terreno"


def test_credit_catalog_rejects_not_eligible():
    assert classify_credit("Depto apto crédito hipotecario UVA") is True
    assert classify_credit("No apto crédito") is False
    assert classify_credit("Living comedor") is None
    item = Listing(
        source="zonaprop",
        source_id="no-credit",
        url="https://example.com",
        title="Casa no apto crédito",
        property_type="casa",
        description="No apto crédito hipotecario",
    )
    apply_analysis(item, {"mortgage_credit": True, "property_type": "casa"})
    assert item.extra.get("mortgage_credit") is False


def test_prompt_is_city_specific_and_clamp_drops_invented_barrio():
    _inject_desembarco()
    from app.llm_fields import EXTRACT_SYSTEM, barrios_for_prompt, build_extract_prompt, clamp_extracted

    prompt = build_extract_prompt(
        city="puerto-madryn",
        city_label="Puerto Madryn",
        title="Depto 2 amb Desembarco apto crédito",
        address="Chiquichan 1250",
        portal_type="departamento",
        description="Living comedor. No llama a herramientas.",
    )
    assert "Desembarco" in prompt
    assert "catalogo_campo" not in prompt
    assert "validar_esquina" not in prompt
    assert "casa|departamento|ph" in EXTRACT_SYSTEM
    assert "rooms=N+1" in EXTRACT_SYSTEM
    assert "2 habitaciones" in EXTRACT_SYSTEM
    assert "uncovered_m2" in EXTRACT_SYSTEM
    assert "city_label" in EXTRACT_SYSTEM
    assert "Sin province" in EXTRACT_SYSTEM
    assert "covered_m2 + uncovered_m2" in EXTRACT_SYSTEM
    assert len(EXTRACT_SYSTEM) > 600
    names = {row["nombre"] for row in barrios_for_prompt("puerto-madryn", "Desembarco")}
    assert "Desembarco" in names
    clamped = clamp_extracted(
        {
            "barrio": "Barrio Inventado",
            "property_type": "depto",
            "mortgage_credit": True,
            "orientation": "frente",
        },
        city="puerto-madryn",
        blob="Depto Desembarco no apto crédito",
    )
    assert clamped["barrio"] == "Desembarco"
    assert clamped["property_type"] == "departamento"
    assert clamped["mortgage_credit"] is False
    assert clamped["orientation"] == "frente"


def test_large_barrio_catalog_keeps_only_names_in_the_listing():
    from app.geo import remember_city_polygons
    from app.llm_fields import BARRIO_PROMPT_MAX, barrios_for_prompt

    rows = [{"name": f"Barrio {i:03d}", "zona": "Zona Norte", "lat": -34.0, "lon": -58.0} for i in range(BARRIO_PROMPT_MAX + 10)]
    rows.append({"name": "Palermo Soho", "zona": "Zona Norte", "lat": -34.58, "lon": -58.42})
    remember_city_polygons("caba", rows)
    picked = barrios_for_prompt("caba", "Depto en Palermo Soho 3 amb")
    names = {row["nombre"] for row in picked}
    assert "Palermo Soho" in names
    assert len(picked) < BARRIO_PROMPT_MAX


def test_barrio_catalog_uses_osm_of_the_city():
    _inject_desembarco()
    catalog = field_catalog("barrio", "puerto-madryn")
    names = {row["nombre"] for row in catalog["valores"]}
    assert "Desembarco" in names
    hit = classify_field("barrio", "Desembarco", "puerto-madryn")
    assert hit["ok"] is True
    assert hit["valor"] == "Desembarco"
    assert classify_field("barrio", "Barrio Inventado", "puerto-madryn")["ok"] is False


def test_llm_tools_expose_field_catalogs():
    _inject_desembarco()
    tipo = _run_tool("catalogo_campo", {"campo": "tipo"}, "puerto-madryn")
    assert tipo["ok"] is True
    credit = _run_tool("clasificar_campo", {"campo": "credito", "texto": "apto bancario"}, "puerto-madryn")
    assert credit["valor"] is True
    barrio = _run_tool(
        "clasificar_campo",
        {"campo": "barrio", "texto": "Desembarco"},
        "puerto-madryn",
    )
    assert barrio["valor"] == "Desembarco"


def test_apply_analysis_snaps_portal_type_aliases():
    item = Listing(
        source="zonaprop",
        source_id="lote-alias",
        url="https://example.com",
        title="Lote 300 m2",
        property_type="casa",
    )
    apply_analysis(item, {"property_type": "lote", "foreign": False})
    assert item.property_type == "terreno"
    depto = Listing(
        source="zonaprop",
        source_id="apto-alias",
        url="https://example.com",
        title="Apartamento 2 amb",
        property_type="casa",
    )
    apply_analysis(depto, {"property_type": "apartamento", "foreign": False})
    assert depto.property_type == "departamento"


def test_clamp_and_apply_use_lot_from_text_not_covered():
    from app.llm_fields import clamp_extracted

    blob = (
        "Casa 90 m² cubiertos. Patio: Gran terreno libre de más de 200 m². "
        "Ideal para parquizar o pileta."
    )
    data = clamp_extracted(
        {"property_type": "casa", "covered_m2": 90, "total_m2": 90},
        city="puerto-madryn",
        blob=blob,
    )
    assert data["covered_m2"] == 90
    assert data["total_m2"] == 200
    item = Listing(
        source="argenprop",
        source_id="lot-200",
        url="https://www.argenprop.com/x--12345678",
        title="Casa",
        property_type="casa",
        description=blob,
        covered_m2=90,
        total_m2=90,
        city="puerto-madryn",
    )
    apply_analysis(item, data)
    assert item.covered_m2 == 90
    assert item.total_m2 == 200


def test_prompt_unknown_city_asks_for_locality():
    from app.llm_fields import build_extract_prompt

    prompt = build_extract_prompt(
        city="fuera",
        city_label="fuera",
        title="Casa en Trelew 3 dorm",
        address="",
        portal_type="casa",
        description="80 m² cubiertos y 20 m² descubiertos en Trelew",
        known_places=["Trelew"],
    )
    assert "desconocido" in prompt
    assert "Trelew" in prompt
    assert "catalogo_campo" not in prompt


def test_prompt_stays_compact_and_keeps_listing_text():
    from app.llm_fields import DESC_PROMPT_MAX, build_extract_prompt

    marker = "Pileta climatizada y quincho cubierto"
    desc = marker + ". " + ("Living comedor. " * 90)
    assert len(desc) > 900
    prompt = build_extract_prompt(
        city="puerto-madryn",
        city_label="Puerto Madryn",
        title="Casa 3 amb",
        address="Mitre 100",
        portal_type="casa",
        description=desc,
    )
    assert marker in prompt
    assert "gimnasio" not in prompt
    assert len(prompt) < 3600
    assert DESC_PROMPT_MAX >= 1600


def test_prompt_omits_barrios_not_named_in_the_listing():
    _inject_desembarco()
    from app.llm_fields import build_extract_prompt

    prompt = build_extract_prompt(
        city="puerto-madryn",
        city_label="Puerto Madryn",
        title="Casa 3 amb",
        address="Mitre 100",
        portal_type="casa",
        description="Living comedor.",
    )
    assert "Desembarco" not in prompt


def test_same_city_prompts_share_prefix_for_cache():
    from app.llm_fields import build_extract_prompt

    a = build_extract_prompt(
        city="cordoba",
        city_label="Córdoba",
        title="Depto 2 amb",
        address="Colón 100",
        portal_type="departamento",
        description="Living al frente.",
    )
    b = build_extract_prompt(
        city="cordoba",
        city_label="Córdoba",
        title="Casa 3 dorm",
        address="Vélez 50",
        portal_type="casa",
        description="Patio y parrilla.",
    )
    c = build_extract_prompt(
        city="rosario",
        city_label="Rosario",
        title="PH 2 amb",
        address="Pellegrini 10",
        portal_type="ph",
        description="Cocina independiente.",
    )
    head_a = a.split("titulo:", 1)[0]
    head_b = b.split("titulo:", 1)[0]
    head_c = c.split("titulo:", 1)[0]
    assert head_a == head_b
    assert head_a.startswith("lugar_buscado: Córdoba")
    assert head_c.startswith("lugar_buscado: Rosario")
    assert a.index("lugar_buscado:") < a.index("titulo:")
    assert a.index("titulo:") < a.index("Living al frente")


def test_extract_json_obj_repairs_truncated_qwen_output():
    from app.llm_fields import extract_json_obj

    complete = '{"property_type":"departamento","rooms":3,"bedrooms":2,"foreign":false}'
    assert extract_json_obj(complete)["rooms"] == 3
    assert extract_json_obj("```json\n" + complete + "\n```")["bedrooms"] == 2
    cut = (
        '{"property_type":"ph","rooms":3,"bedrooms":2,"street":"Mitre","street_number":100,'
        '"notes":"PH con patio y parrilla en Quilmes Oeste, luminoso'
    )
    repaired = extract_json_obj(cut)
    assert repaired is not None
    assert repaired["property_type"] == "ph"
    assert repaired["street"] == "Mitre"
    dangling = '{"property_type":"casa","rooms":4, "not'
    assert extract_json_obj(dangling)["rooms"] == 4
    think = "<think>voy a copiar todo el aviso</think>\n" + complete
    assert extract_json_obj(think)["foreign"] is False
    assert extract_json_obj("") is None
    assert extract_json_obj("no hay json") is None


def test_llm_ctx_reads_env(monkeypatch):
    monkeypatch.setenv("LLM_CTX", "2048")
    from app.llm_enrich import llm_ctx

    assert llm_ctx() == 2048
    monkeypatch.setenv("LLM_CTX", "8192")
    assert llm_ctx() == 8192


def test_local_llm_uses_one_gpu_worker(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "local")
    monkeypatch.setenv("LLM_WORKERS", "6")
    monkeypatch.setenv("LLM_PARALLEL", "1")
    from app.llm_enrich import llm_workers

    assert llm_workers() == 1


def test_local_chat_does_not_stack_timeouts(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "local")
    monkeypatch.setenv("LLM_URL", "http://127.0.0.1:9")
    posts = {"n": 0}

    class Fake:
        def __init__(self, **_kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def post(self, _url, json=None):
            posts["n"] += 1
            raise TimeoutError("hang")

        def close(self):
            return None

    monkeypatch.setattr("httpx.Client", Fake)
    from app.llm_enrich import CHAT_TIMEOUT_SEC, _chat

    assert _chat([{"role": "user", "content": "x"}]) == {}
    assert posts["n"] == 1
    assert CHAT_TIMEOUT_SEC <= 45


def test_local_python_uses_one_worker_for_one_gpu_slot(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "local")
    monkeypatch.setenv("LLM_PARALLEL", "1")
    from app import llm_enrich

    llm_enrich._busy.clear()
    assert llm_enrich.llm_workers() == 1
    assert llm_enrich._wanted_workers() == 1
    assert llm_enrich.MAX_TOKENS <= 96


def test_local_chat_does_not_force_json_object(monkeypatch):
    """Con response_format, llama.cpp devuelve contenido vacío y quema todos los tokens."""
    seen = {}

    class Fake:
        def __init__(self, *a, **k):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, _url, json=None):
            seen.update(json or {})

            class Res:
                def json(self_inner):
                    return {"choices": [{"message": {"content": "{}"}}]}

            return Res()

        def close(self):
            return None

    monkeypatch.setenv("LLM_PROVIDER", "local")
    monkeypatch.setattr("httpx.Client", Fake)
    monkeypatch.setattr("app.llm_enrich._gpu.acquire_ticket", lambda timeout=None: 1)
    monkeypatch.setattr("app.llm_enrich._gpu.release", lambda ticket=None: None)
    from app.llm_enrich import _chat

    _chat([{"role": "user", "content": "x"}])
    assert "response_format" not in seen
    assert seen.get("cache_prompt") is True


def test_queue_stats_working_counts_busy_listings(monkeypatch):
    import time

    from app import llm_enrich

    monkeypatch.setattr(llm_enrich, "llama_status", lambda: {"ok": True, "gpu": False, "n_ctx": 0})
    monkeypatch.setattr(llm_enrich, "_workers", 2)
    with llm_enrich._lock:
        llm_enrich._busy.clear()
        llm_enrich._busy["lid-1"] = time.time() - 40
    try:
        stats = llm_enrich.queue_stats()
        assert stats["cleaning"] == 1
        assert stats["gpu"] is False
        assert stats["busy_s"] >= 39
    finally:
        with llm_enrich._lock:
            llm_enrich._busy.clear()


def test_local_chat_gives_up_if_gpu_lock_is_held(monkeypatch):
    import time

    monkeypatch.setenv("LLM_PROVIDER", "local")
    monkeypatch.setenv("LLM_URL", "http://127.0.0.1:9")
    from app import llm_enrich

    monkeypatch.setattr(llm_enrich, "CHAT_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(llm_enrich, "STUCK_SEC", 0.05)
    assert llm_enrich._gpu.acquire()
    try:
        started = time.time()
        assert llm_enrich._chat([{"role": "user", "content": "x"}]) == {}
        assert time.time() - started < 1.5
    finally:
        llm_enrich._gpu.release()


def test_city_catalog_is_queryable():
    from app.llm_fields import field_catalog
    from app.llm_enrich import _run_tool

    catalog = field_catalog("ciudad", query="madryn")
    labels = {row["label"] for row in catalog["valores"]}
    assert "Puerto Madryn" in labels
    hit = _run_tool("clasificar_campo", {"campo": "ciudad", "texto": "Puerto Madryn"}, "fuera")
    assert hit["ok"] is True
    assert hit["id"] == "puerto-madryn"


def test_clamp_sums_uncovered_when_no_lot():
    from app.llm_fields import clamp_extracted

    data = clamp_extracted(
        {"property_type": "casa", "covered_m2": 80, "total_m2": 80, "uncovered_m2": 20},
        city="puerto-madryn",
        blob="Casa 80 m² cubiertos y 20 m² descubiertos. Living comedor.",
    )
    assert data["covered_m2"] == 80
    assert data["uncovered_m2"] == 20
    assert data["total_m2"] == 100
