"""Cliente Laya: decisiones tipadas en una sola pasada, como TypeSafe Jev.

No genera texto. Preguntas `choice` / `noul` / `score` sobre el aviso, con
probabilidad calibrada. Los avisos están en español: el checkpoint por defecto
es `multilingual`. Todas las preguntas van juntas; no una inferencia por campo.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

LAYA_MODEL = os.getenv("LAYA_MODEL", "multilingual")
LAYA_DEVICE = os.getenv("LAYA_DEVICE", "cpu")
LAYA_CACHE_DIR = os.getenv("LAYA_CACHE_DIR", "/app/data/laya_cache")
SYSTEMONE_URL = (os.getenv("SYSTEMONE_URL") or os.getenv("KEV_URL") or os.getenv("JEV_URL") or "").strip()

# noul es la P(afirmativo). Laya sale sobreconfiado: umbral por encima de 0.5.
NOUL_YES = float(os.getenv("LAYA_NOUL_YES", "0.62"))
CHOICE_MIN = float(os.getenv("LAYA_CHOICE_MIN", "0.45"))

LAYA_QUESTIONS: dict[str, dict[str, Any]] = {
    "quality_score": {
        "type": "score",
        "instructions": "¿Qué tan completo, detallado y confiable es este aviso inmobiliario?",
        "criteria": [
            "Muy vago o nulo: apenas una línea, sin m² ni detalles básicos",
            "Malo/Insuficiente: faltan muchos datos básicos importantes",
            "Regular: tiene m² y descripción básica, pero faltan expensas o estado claro",
            "Bueno/Completo: detalla m², dormitorios, estado general, expensas y ubicación aproximada",
            "Excelente/Profesional: máximo detalle, especifica expensas exactas, amenities, cochera, etc.",
        ],
    },
    "is_owner_direct": {
        "type": "noul",
        "instructions": (
            "¿El aviso indica explícitamente que es dueño directo, propietario vende, "
            "particular vende, o que no hay comisión inmobiliaria / no interviene inmobiliaria?"
        ),
    },
    "is_mortgage_eligible": {
        "type": "noul",
        "instructions": (
            "¿El texto confirma de manera explícita y afirmativa que la propiedad es apta "
            "para crédito hipotecario, préstamo bancario, UVA o Procrear? "
            "(No cuente 'consulte financiación' ni 'apto crédito en trámite')"
        ),
    },
    "has_low_expenses": {
        "type": "noul",
        "instructions": (
            "¿La descripción indica que no paga expensas, expensas muy bajas, o que es una "
            "propiedad sin expensas (ej: PH sin expensas, casa, terreno)?"
        ),
    },
    "shows_urgency": {
        "type": "noul",
        "instructions": (
            "¿El vendedor muestra urgencia real por vender? "
            "(Ej: 'vendo urgente', 'necesito vender ya', 'precio de oportunidad', "
            "'me voy del país', 'herencia', 'divorcio')"
        ),
    },
    "environment_noise": {
        "type": "choice",
        "instructions": "Según la descripción, ¿cómo es el nivel de ruido/entorno de la propiedad?",
        "criteria": {
            "quiet": "Muy silencioso, zona residencial tranquila, contrafrente silencioso, calle cortada",
            "normal": "Residencial normal, nivel de ruido urbano típico tolerable",
            "noisy": "Comercial, ruido de tráfico, zona muy transitada o comercial activa",
            "avenue": "Avenida, alto tránsito, ruidoso por colectivos/tránsito pesado",
        },
    },
    "property_condition": {
        "type": "choice",
        "instructions": "¿En qué estado general se encuentra la propiedad según el aviso?",
        "criteria": {
            "brand_new": "A estrenar, nuevo",
            "under_construction": "En pozo o en construcción",
            "recycled": "Reciclado a nuevo, impecable",
            "good": "Bueno, habitable sin necesidad de grandes reformas",
            "to_rebuild": "A reciclar, necesita obras o refacciones importantes",
        },
    },
    "has_balcony": {
        "type": "noul",
        "instructions": (
            "¿El aviso o las características dicen que la unidad tiene balcón propio? "
            "No cuente terraza común del edificio ni 'sin balcón'."
        ),
    },
    "is_bright": {
        "type": "noul",
        "instructions": (
            "¿El aviso dice que es luminoso, con mucha luz o luz natural? "
            "No cuente 'luz y agua' ni servicios."
        ),
    },
    "growing_area": {
        "type": "noul",
        "instructions": (
            "¿El aviso dice que el barrio o la zona está en crecimiento, expansión, desarrollo o auge?"
        ),
    },
    "open_view": {
        "type": "noul",
        "instructions": (
            "¿El aviso promete vista abierta, panorámica, despejada, al mar o al río? "
            "No cuente 'vista a la calle'."
        ),
    },
    "has_patio": {
        "type": "noul",
        "instructions": (
            "¿La propiedad tiene patio o jardín propio? No cuente un parque público cercano."
        ),
    },
    "has_garage": {
        "type": "noul",
        "instructions": (
            "¿El aviso indica cochera, garage o estacionamiento propio? No cuente 'a 2 cuadras de un garage'."
        ),
    },
    "has_terrace": {
        "type": "noul",
        "instructions": (
            "¿Tiene terraza propia o exclusiva de la unidad? No cuente terraza común del edificio."
        ),
    },
    "contains_dwelling": {
        "type": "noul",
        "instructions": (
            "¿Se vende una vivienda ya existente o a terminar (casa, depto, PH, dúplex), "
            "no un lote baldío para construir? "
            "'Ideal para construir una casa' o 'proyecto de casa' es lote, no vivienda."
        ),
    },
    "property_kind": {
        "type": "choice",
        "instructions": "¿Qué se está vendiendo realmente, más allá de la categoría del portal?",
        "criteria": {
            "terreno": "Lote o terreno vacío, o para construir. Sin vivienda habitable.",
            "casa": "Casa, chalet o vivienda sobre un lote, incluso a terminar.",
            "departamento": "Departamento o apartamento en edificio.",
            "ph": "PH, dúplex o triplex.",
            "local": "Local comercial.",
            "oficina": "Oficina.",
            "galpon": "Galpón o nave.",
        },
    },
}

_agent_lock = threading.Lock()
_agent: Any | None = None
_agent_kind = ""
_agent_model = ""
_agent_failed = False


@dataclass(slots=True)
class LayaDecision:
    question_key: str
    question_type: str
    answer: str | float | bool | None
    confidence: float
    all_probs: dict[str, float] | None = None


def _testing() -> bool:
    return os.environ.get("PROPMAP_TEST") == "1"


def _systemone_endpoint() -> str:
    url = SYSTEMONE_URL.rstrip("/")
    if not url:
        return ""
    if url.endswith("/systemone") or url.endswith("/decide"):
        return url
    return f"{url}/v1/systemone"


def _load_backend() -> tuple[str, Any, str] | None:
    endpoint = _systemone_endpoint()
    if endpoint:
        return "http", endpoint, "systemone"
    os.environ.setdefault("USE_TF", "0")
    if LAYA_CACHE_DIR:
        os.environ.setdefault("HF_HOME", LAYA_CACHE_DIR)
        os.environ.setdefault("HUGGINGFACE_HUB_CACHE", LAYA_CACHE_DIR)
    import laya

    wanted = (LAYA_MODEL or "multilingual").strip()
    alias = {
        "multilingual": "multilingual",
        "laya-multilingual": "multilingual",
        "english": "english",
        "laya": "english",
        "typed-decisions": "typed-decisions",
        "laya-typed-decisions": "typed-decisions",
    }
    route = alias.get(wanted.lower(), "")
    device = LAYA_DEVICE
    try:
        from laya import Router

        name = route or "multilingual"
        router = Router(max_loaded=1, device=device)
        if hasattr(router, "preload"):
            router.preload([name])
        return "router", router, name
    except Exception as exc:
        logger.info("Laya Router no disponible (%s); cargo un checkpoint", exc)

    repo = wanted
    subfolder = None
    if route == "multilingual":
        repo = "convaiinnovations/laya"
        subfolder = "multilingual"
    elif route == "typed-decisions":
        repo = "convaiinnovations/laya"
        subfolder = "typed-decisions"
    elif route == "english" or wanted in {"convaiinnovations/laya", "laya"}:
        repo = "convaiinnovations/laya"
        subfolder = None
    kwargs: dict[str, Any] = {"device": device}
    if subfolder:
        kwargs["subfolder"] = subfolder
    agent = laya.load(repo, **kwargs)
    return "agent", agent, route or wanted


def _ensure_agent() -> tuple[str, Any, str] | None:
    global _agent, _agent_kind, _agent_model, _agent_failed
    if _testing() or _agent_failed:
        return None
    if _agent is not None:
        return _agent_kind, _agent, _agent_model
    with _agent_lock:
        if _agent is not None:
            return _agent_kind, _agent, _agent_model
        if _agent_failed:
            return None
        try:
            logger.info("Cargando decisiones tipadas (%s)", LAYA_MODEL if not _systemone_endpoint() else "systemone")
            t0 = time.time()
            loaded = _load_backend()
            if not loaded:
                _agent_failed = True
                return None
            _agent_kind, _agent, _agent_model = loaded
            logger.info("Laya listo (%s/%s) en %.2fs", _agent_kind, _agent_model, time.time() - t0)
            return _agent_kind, _agent, _agent_model
        except Exception:
            logger.exception("No se pudo cargar Laya")
            _agent_failed = True
            return None


def parse_answers(result: dict[str, Any] | None, questions: dict[str, dict[str, Any]]) -> list[LayaDecision]:
    """Traduce el dict de Laya/Jev a decisiones tipadas. Sin I/O."""
    answers = (result or {}).get("answers") or {}
    out: list[LayaDecision] = []
    for key, spec in questions.items():
        resp = answers.get(key)
        if not isinstance(resp, dict):
            continue
        qtype = spec.get("type") or resp.get("type") or ""
        if qtype == "choice":
            answer = resp.get("choice") or ""
            probs = resp.get("probabilities") or {}
            conf = float(resp.get("confidence") or 0.0)
            if not conf and isinstance(probs, dict) and answer in probs:
                conf = float(probs.get(answer) or 0.0)
            out.append(
                LayaDecision(
                    question_key=key,
                    question_type="choice",
                    answer=str(answer) if answer else None,
                    confidence=conf,
                    all_probs={str(k): float(v) for k, v in dict(probs).items()},
                )
            )
        elif qtype == "noul":
            prob = float(resp.get("noul") if resp.get("noul") is not None else 0.0)
            if prob >= NOUL_YES:
                answer: bool | None = True
                conf = prob
            elif prob <= (1.0 - NOUL_YES):
                answer = False
                conf = 1.0 - prob
            else:
                answer = None
                conf = max(prob, 1.0 - prob)
            out.append(
                LayaDecision(
                    question_key=key,
                    question_type="noul",
                    answer=answer,
                    confidence=conf,
                )
            )
        elif qtype == "score":
            raw = resp.get("score")
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            probs = resp.get("probabilities") or {}
            conf = float(resp.get("confidence") or 0.0)
            out.append(
                LayaDecision(
                    question_key=key,
                    question_type="score",
                    answer=value,
                    confidence=conf,
                    all_probs={str(k): float(v) for k, v in dict(probs).items()},
                )
            )
    return out


def _run_predict(backend: tuple[str, Any, str], state: Any, questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    kind, agent, model = backend
    if kind == "http":
        import httpx

        payload = {"state": state, "questions": questions, "model": os.getenv("SYSTEMONE_MODEL") or "kev-latest"}
        resp = httpx.post(str(agent), json=payload, timeout=12.0)
        resp.raise_for_status()
        return resp.json() or {}
    if kind == "router":
        try:
            return agent.predict(state, questions, model=model) or {}
        except TypeError:
            return agent.predict(state, questions) or {}
    if hasattr(agent, "predict"):
        return agent.predict(state, questions) or {}
    if hasattr(agent, "system_one"):
        return agent.system_one(state, questions) or {}
    raise AttributeError("el agente Laya no expone predict ni system_one")


def decide(state: Any, question_keys: list[str] | None = None) -> list[LayaDecision]:
    """Una sola pasada para todas las preguntas pedidas."""
    if state in (None, "", {}, []):
        return []
    keys = question_keys or list(LAYA_QUESTIONS)
    questions = {key: LAYA_QUESTIONS[key] for key in keys if key in LAYA_QUESTIONS}
    if not questions:
        return []
    backend = _ensure_agent()
    if backend is None:
        return []
    try:
        t0 = time.time()
        result = _run_predict(backend, state, questions)
        elapsed = (time.time() - t0) * 1000
        logger.debug("Laya inferencia: %.1fms para %d preguntas", elapsed, len(questions))
        return parse_answers(result, questions)
    except Exception:
        logger.exception("Error en inferencia Laya")
        return []


def ask_laya(state_text: str, question_keys: list[str] | None = None) -> list[LayaDecision]:
    return decide(state_text, question_keys)


def ask_laya_single(state_text: str, question_key: str) -> LayaDecision | None:
    decisions = decide(state_text, [question_key])
    return decisions[0] if decisions else None


def get_quality_score(state_text: str) -> tuple[float, float]:
    d = ask_laya_single(state_text, "quality_score")
    if d and d.question_type == "score" and d.answer is not None:
        return (float(d.answer) / 4.0) * 100.0, d.confidence
    return 0.0, 0.0


def is_owner_direct(state_text: str) -> tuple[bool, float]:
    d = ask_laya_single(state_text, "is_owner_direct")
    if d and d.question_type == "noul" and d.answer is True:
        return True, d.confidence
    return False, d.confidence if d else 0.0


def is_mortgage_eligible(state_text: str) -> tuple[bool, float]:
    d = ask_laya_single(state_text, "is_mortgage_eligible")
    if d and d.question_type == "noul" and d.answer is True:
        return True, d.confidence
    return False, d.confidence if d else 0.0


def has_low_expenses(state_text: str) -> tuple[bool, float]:
    d = ask_laya_single(state_text, "has_low_expenses")
    if d and d.question_type == "noul" and d.answer is True:
        return True, d.confidence
    return False, d.confidence if d else 0.0


def shows_urgency(state_text: str) -> tuple[bool, float]:
    d = ask_laya_single(state_text, "shows_urgency")
    if d and d.question_type == "noul" and d.answer is True:
        return True, d.confidence
    return False, d.confidence if d else 0.0


def get_environment_noise(state_text: str) -> tuple[str, float, dict[str, float]]:
    d = ask_laya_single(state_text, "environment_noise")
    if d and d.question_type == "choice" and d.answer:
        return str(d.answer), d.confidence, d.all_probs or {}
    return "unknown", 0.0, {}


def get_property_condition(state_text: str) -> tuple[str, float, dict[str, float]]:
    d = ask_laya_single(state_text, "property_condition")
    if d and d.question_type == "choice" and d.answer:
        return str(d.answer), d.confidence, d.all_probs or {}
    return "unknown", 0.0, {}


def warmup_laya() -> bool:
    if _testing():
        return False
    try:
        _ = decide("Departamento 2 ambientes 50 m2", ["quality_score"])
        return _agent is not None
    except Exception as exc:
        logger.error("Laya warmup falló: %s", exc)
        return False


laya_ask = ask_laya
laya_ask_single = ask_laya_single
