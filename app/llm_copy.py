"""Cola de última prioridad: ficha redactada a partir del aviso original y los datos extraídos."""
from __future__ import annotations

import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

from .freshness import LIST_TEXT_MIN
from .models import Listing

COPY_SCHEMA = 3
# Desde este esquema el texto ya sale sin teléfonos ni inmobiliarias.
_SHOW_COPY_FROM = 2
QUEUE_CAP = 32
READY_CAP = 2
MAX_TRIES = 2
SKIP_FAIL_SEC = 120.0
MAX_TOKENS = 240
MAX_CHARS = 700
MIN_CHARS = 40
DESC_PROMPT_MAX = 900

COPY_SYSTEM = (
    "Redactás la ficha pública en español rioplatense. Solo estas líneas, en este orden, y nada más:\n"
    "Qué es: una oración con el tipo, el tamaño y el barrio o la ciudad.\n"
    "Detalles: una oración natural, como la diría una persona. No hagas una lista de cifras "
    "('3 ambientes, 2 dormitorios, 1 baño, 75 m²'). "
    "Los enteros van sin decimal y en singular si es uno: baño, 75 m². "
    "Si no hay rasgos, no escribas esta línea.\n"
    "Si TEXTO trae datos que DATOS no tiene (plantas, distribución, quincho, terraza, "
    "orientación, lavadero, patio), incluilos en Detalles.\n"
    "Cerca: una oración solo si ENTORNO tiene ítems. Empezá con \"Cerca hay\" y usá solo esas palabras "
    "(transporte, subte, tren, colectivo, universidad, escuela, salud, comercios, plaza). "
    "Si ENTORNO está vacío, no escribas esta línea.\n"
    "Prohibido: teléfonos, mails, WhatsApp, nombres de personas, inmobiliarias, martilleros, "
    "corredores, marcas de inmobiliaria, códigos de aviso, altura de la calle, y frases de venta "
    "(imperdible, consultar, llamar). No inventes datos. No pegues el aviso: reescribí solo lo útil. /no_think"
)

_FORBIDDEN_RE = re.compile(
    r"inmobiliari|martiller|corredor|whats?app|re/?max|century\s*21|\[oculto\]|@",
    re.I,
)

_THINK_RE = re.compile(r"<think>[\s\S]*?</think>", re.I)
_MD_RE = re.compile(r"^[\s*`#>-]+|[\s*`]+$", re.M)

_queue: deque[str] = deque()
_seen: set[str] = set()
_lock = threading.Lock()
_ready: deque[tuple[str, Listing, list[dict[str, str]]]] = deque()
_ready_cv = threading.Condition()
_prep_workers = 0
_busy: dict[str, float] = {}
_skip_until: dict[str, float] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def enabled() -> bool:
    from .llm_enrich import enabled as llm_on

    return llm_on()


_fichas_at = 0.0
_fichas_n = 0


def fichas_pending() -> bool:
    """Hay fichas sin bajar: la GPU no redacta descripciones mientras tanto."""
    global _fichas_at, _fichas_n
    if os.environ.get("PROPMAP_TEST") == "1":
        return False
    now = time.time()
    if now - _fichas_at < 20:
        return _fichas_n > 0
    from .store import count_detail_backlog

    try:
        _fichas_n = count_detail_backlog()
    except Exception:
        _fichas_n = 0
    _fichas_at = now
    return _fichas_n > 0


def copy_payload(item: Listing) -> dict[str, Any]:
    extra = item.extra or {}
    raw = extra.get("copy")
    return raw if isinstance(raw, dict) else {}


def copy_is_current(item: Listing) -> bool:
    payload = copy_payload(item)
    text = str(payload.get("text") or "").strip()
    if not text:
        return False
    try:
        ver = int(payload.get("ver") or 0)
    except (TypeError, ValueError):
        ver = 0
    if ver != COPY_SCHEMA:
        return False
    extra = item.extra or {}
    try:
        llm_ver = int(extra.get("llm_ver") or 0)
        stamped = int(payload.get("llm_ver") or 0)
    except (TypeError, ValueError):
        return True
    return stamped == llm_ver


def needs_copy(item: Listing) -> bool:
    """Ficha lista para redactar: ya no falta extraer, hay texto o ficha, y no hay párrafo actual."""
    from .llm_enrich import needs_improve

    extra = item.extra or {}
    if extra.get("duplicate_of") or extra.get("dedupe_hidden"):
        return False
    if str((extra.get("user_edits") or {}).get("description") or "").strip():
        return False
    if needs_improve(item):
        return False
    if copy_is_current(item):
        return False
    if item.details_scraped or extra.get("details_at"):
        return True
    return len((item.description or "").strip()) >= LIST_TEXT_MIN


def display_description(item: Listing) -> str:
    """En la página solo entra la ficha nueva. El aviso original puede traer teléfonos y nombres."""
    extra = item.extra or {}
    edited = str((extra.get("user_edits") or {}).get("description") or "").strip()
    if edited:
        return edited
    payload = copy_payload(item)
    try:
        ver = int(payload.get("ver") or 0)
    except (TypeError, ValueError):
        ver = 0
    if ver < _SHOW_COPY_FROM:
        return ""
    return polish_copy_text(str(payload.get("text") or "").strip())


def _plain_number(value: Any) -> str:
    """1.0 y 75.0 se leen como 1 y 75. Un decimal de verdad se conserva."""
    if isinstance(value, bool) or value in {None, ""}:
        return ""
    if isinstance(value, (int, float)):
        number = float(value)
        if number.is_integer():
            return str(int(number))
        return f"{number:.1f}".rstrip("0").rstrip(".")
    text = str(value).strip()
    if re.fullmatch(r"-?\d+\.0+", text):
        return str(int(float(text)))
    return text


_NEEDLESS_DECIMAL = re.compile(r"\b(\d+)\.0+\b")
_ONE_NOUN = (
    (re.compile(r"\b1 baños\b", re.I), "1 baño"),
    (re.compile(r"\b1 dormitorios\b", re.I), "1 dormitorio"),
    (re.compile(r"\b1 ambientes\b", re.I), "1 ambiente"),
)


def polish_copy_text(text: str) -> str:
    """Saca decimales y plurales que la ficha no necesita."""
    cleaned = _NEEDLESS_DECIMAL.sub(r"\1", text or "")
    for pattern, repl in _ONE_NOUN:
        cleaned = pattern.sub(repl, cleaned)
    return cleaned


def facts_for_copy(item: Listing) -> list[str]:
    from .features import TYPE_LABEL
    from .geo_tools import city_label

    extra = item.extra or {}
    llm = extra.get("llm") if isinstance(extra.get("llm"), dict) else {}
    lines: list[str] = []

    def add(label: str, value: Any) -> None:
        text = _plain_number(value)
        if not text or text in {"Sin clasificar", "—"}:
            return
        lines.append(f"{label}: {text}")

    kind = TYPE_LABEL.get(item.property_type or "", item.property_type or "")
    add("tipo", kind)
    add("ciudad", city_label(item.city) or item.city)
    add("barrio", item.barrio)
    add("zona", item.zona)
    add("ambientes", item.rooms)
    add("dormitorios", item.bedrooms)
    add("baños", item.bathrooms)
    add("m² cubiertos", item.covered_m2)
    add("m² total", item.total_m2)
    add("antigüedad", f"{item.age_years} años" if item.age_years is not None else "")
    credit = extra.get("mortgage_credit")
    add("apto crédito", "sí" if credit is True else "no" if credit is False else "")
    add("dueño directo", "sí" if extra.get("owner_direct") is True else "")
    add("expensas bajas", "sí" if extra.get("low_expenses") is True else "")
    add("venta urgente", "sí" if extra.get("urgent_sale") is True else "")
    add("entorno", extra.get("environment") or "")
    add("balcón", "sí" if extra.get("has_balcony") is True else "")
    add("luminoso", "sí" if extra.get("bright") is True else "")
    add("barrio en crecimiento", "sí" if extra.get("growing_area") is True else "")
    add("vista abierta", "sí" if extra.get("open_view") is True else "")
    add("patio", "sí" if extra.get("has_patio") is True else "")
    add("cochera", "sí" if extra.get("has_garage") is True or item.parking else "")
    add("terraza", "sí" if extra.get("has_terrace") is True else "")
    add("piso", llm.get("floor"))
    add("orientación", llm.get("orientation"))
    add("estado", extra.get("condition") or llm.get("condition"))
    tags = extra.get("tags") or extra.get("amenities") or []
    if tags:
        add("amenities", ", ".join(str(tag) for tag in tags if tag)[:180])
    return lines


def entorno_for_copy(item: Listing) -> list[str]:
    """Tipos de lugar cerca, sin nombres de comercios ni personas."""
    extra = item.extra or {}
    access = extra.get("access") if isinstance(extra.get("access"), dict) else {}
    cats = access.get("categories") if isinstance(access.get("categories"), dict) else {}

    def present(key: str) -> bool:
        row = cats.get(key)
        return isinstance(row, dict) and bool(row.get("present"))

    out: list[str] = []
    transport: list[str] = []
    if present("subway"):
        transport.append("subte")
    if present("train"):
        transport.append("tren")
    if present("transport"):
        transport.append("colectivo")
    if transport:
        out.append("transporte (" + ", ".join(transport) + ")")
    school = cats.get("school") if isinstance(cats.get("school"), dict) else {}
    if school.get("present"):
        kind = str(school.get("kind") or "").lower()
        out.append("universidad" if "univers" in kind or "terci" in kind else "escuela")
    if present("health"):
        out.append("salud")
    if present("shop"):
        out.append("comercios")
    if present("plaza") or present("beach"):
        out.append("plaza")
    return out


def source_for_copy(item: Listing) -> str:
    """Datos del aviso original que DATOS no tiene. Sin teléfonos, inmobiliarias ni altura."""
    from .llm_enrich import _scrub

    raw = _scrub(item.description or "").replace("[oculto]", " ")
    extra = item.extra or {}
    for chunk in (item.address or "", str(extra.get("street") or "")):
        token = chunk.strip()
        if len(token) >= 4:
            raw = re.sub(re.escape(token), " ", raw, flags=re.I)
    raw = re.sub(r"\b(?:usd|u\$s|ars)\s*[\d.]+", " ", raw, flags=re.I)
    raw = re.sub(r"\b\d{2,5}\b(?!\s*(?:m²|m2|mts?|metros|años))", " ", raw, flags=re.I)
    parts: list[str] = []
    for bit in re.split(r"[\n.;]+", raw):
        line = re.sub(r"\s+", " ", bit).strip(" -_:")
        if len(line) < 12 or _FORBIDDEN_RE.search(line):
            continue
        if re.search(r"publicado por|c[oó]digo\b", line, re.I):
            continue
        parts.append(line)
    return re.sub(r"\s+", " ", ". ".join(parts)).strip()[:480]


def build_copy_prompt(item: Listing) -> str:
    from .llm_enrich import _scrub

    facts = facts_for_copy(item)
    fact_txt = "\n".join(facts) if facts else "(sin datos extraídos)"
    entorno = entorno_for_copy(item)
    entorno_txt = ", ".join(entorno) if entorno else "(nada medido cerca)"
    title = _FORBIDDEN_RE.sub("", _scrub((item.title or "").strip()))
    title = re.sub(r"\s+", " ", title).replace("[oculto]", "").strip()[:160]
    source = source_for_copy(item) or "(el aviso no trae más datos)"
    return (
        f"titulo: {title}\n"
        f"DATOS:\n{fact_txt}\n"
        f"ENTORNO:\n{entorno_txt}\n"
        f"TEXTO:\n{source}\n"
    )


def parse_copy_text(raw: str) -> str:
    from .llm_enrich import _scrub

    text = _THINK_RE.sub("", raw or "").strip()
    text = text.replace("```json", "").replace("```", "").strip()
    if text.startswith("{"):
        from .llm_fields import extract_json_obj

        data = extract_json_obj(text)
        if data:
            text = str(data.get("text") or data.get("descripcion") or data.get("description") or "").strip()
    text = _MD_RE.sub("", text).strip().strip('"').strip()
    text = _scrub(text).replace("[oculto]", "")
    kept: list[str] = []
    for line in re.split(r"\n+", text):
        line = re.sub(r"[ \t]+", " ", line).strip()
        if not line or _FORBIDDEN_RE.search(line):
            continue
        kept.append(line)
    text = "\n".join(kept).strip()
    if len(text) > MAX_CHARS:
        cut = text[:MAX_CHARS].rsplit(" ", 1)[0]
        text = cut or text[:MAX_CHARS]
    if len(text) < MIN_CHARS:
        return ""
    return polish_copy_text(text)


def apply_copy(item: Listing, text: str) -> None:
    clean = parse_copy_text(text)
    if not clean:
        return
    extra = dict(item.extra or {})
    try:
        llm_ver = int(extra.get("llm_ver") or 0)
    except (TypeError, ValueError):
        llm_ver = 0
    extra["copy"] = {
        "text": clean,
        "ver": COPY_SCHEMA,
        "llm_ver": llm_ver,
        "at": _now_iso(),
    }
    extra.pop("copy_tries", None)
    item.extra = extra


def queue_stats() -> dict[str, Any]:
    now = time.time()
    with _lock:
        pending = len(_queue)
        busy = dict(_busy)
    with _ready_cv:
        ready = len(_ready)
    return {
        "pending": pending,
        "ready": ready,
        "cleaning": len(busy),
        "workers": _prep_workers,
        "cap": QUEUE_CAP,
        "enabled": enabled(),
        "schema": COPY_SCHEMA,
        "busy_s": max((now - started for started in busy.values()), default=0),
    }


def enqueue(listings: list[Listing] | None) -> None:
    if not enabled() or not listings:
        return
    now = time.time()
    with _lock:
        for item in listings:
            if not item or not needs_copy(item):
                continue
            if (_skip_until.get(item.id) or 0) > now:
                continue
            if item.id in _seen:
                continue
            if len(_queue) >= QUEUE_CAP:
                break
            _seen.add(item.id)
            _queue.append(item.id)
        _ensure_workers_locked()


def refill(prefer_city: str = "") -> int:
    """Mantiene un buffer corto. La GPU las toma cuando la limpieza no ocupa el turno."""
    if not enabled():
        return 0
    with _lock:
        pending = len(_queue)
        room = max(0, QUEUE_CAP - pending)
        now = time.time()
        skip = set(_seen)
        skip.update(lid for lid, until in _skip_until.items() if until > now)
    if room <= 0:
        with _lock:
            _ensure_workers_locked()
        return 0
    from . import store

    items = store.fetch_copy_backlog(room + 8, prefer_city=prefer_city)
    take = [item for item in items if item.id not in skip and needs_copy(item)][:room]
    if take:
        enqueue(take)
    else:
        with _lock:
            _ensure_workers_locked()
    return len(take)


def ensure_running() -> None:
    if not enabled():
        return
    with _lock:
        _ensure_workers_locked()


def take_ready(timeout: float = 0.0) -> tuple[str, Listing, list[dict[str, str]]] | None:
    """El hilo de GPU pide un prompt de descripción ya armado."""
    with _ready_cv:
        if not _ready and timeout > 0:
            _ready_cv.wait(timeout)
        if not _ready:
            return None
        job = _ready.popleft()
        _ready_cv.notify_all()
        return job


def mark_gpu(listing_id: str | None) -> None:
    with _lock:
        _busy.clear()
        if listing_id:
            _busy[listing_id] = time.time()


def complete(listing_id: str, item: Listing, raw: str) -> None:
    from . import store
    from .llm_enrich import _ops_note, _scrub

    try:
        if not item or not needs_copy(item):
            with _lock:
                _seen.discard(listing_id)
            return
        text = parse_copy_text(_scrub(raw))
        extra = dict(item.extra or {})
        if not text:
            tries = int(extra.get("copy_tries") or 0) + 1
            extra["copy_tries"] = tries
            item.extra = extra
            store.upsert_listings([item], notify=False)
            with _lock:
                _seen.discard(listing_id)
                if tries < MAX_TRIES:
                    _skip_until[listing_id] = time.time() + SKIP_FAIL_SEC
                else:
                    _skip_until.pop(listing_id, None)
            _ops_note("copy", outcome="retry" if tries < MAX_TRIES else "fail")
            return
        extra.pop("copy_tries", None)
        item.extra = extra
        original = item.description
        apply_copy(item, text)
        item.description = original
        store.upsert_listings([item], notify=True)
        with _lock:
            _seen.discard(listing_id)
            _skip_until.pop(listing_id, None)
        _ops_note("copy", outcome="ok")
    except Exception:
        with _lock:
            _seen.discard(listing_id)
            _skip_until[listing_id] = time.time() + SKIP_FAIL_SEC
    finally:
        with _lock:
            _busy.pop(listing_id, None)


def _ensure_workers_locked() -> None:
    global _prep_workers
    if os.environ.get("PROPMAP_TEST") == "1":
        return
    if _prep_workers >= 1:
        return
    _prep_workers += 1
    threading.Thread(target=_drain_prep, daemon=True, name="llm-copy-prep").start()


def _pop() -> str | None:
    now = time.time()
    with _lock:
        skipped: list[str] = []
        found = None
        while _queue:
            lid = _queue.popleft()
            if lid in _busy or (_skip_until.get(lid) or 0) > now:
                skipped.append(lid)
                continue
            found = lid
            break
        if skipped:
            _queue.extend(skipped)
        return found


def _prepare_id(listing_id: str) -> tuple[str, Listing, list[dict[str, str]]] | None:
    from . import store

    item = store.get_listing(listing_id)
    if not item or not needs_copy(item):
        with _lock:
            _seen.discard(listing_id)
        return None
    prompt = build_copy_prompt(item)
    messages = [
        {"role": "system", "content": COPY_SYSTEM},
        {"role": "user", "content": prompt},
    ]
    return listing_id, item, messages


def _drain_prep() -> None:
    """Arma prompts de descripción en CPU. La GPU las toma desde llm_enrich."""
    global _prep_workers
    try:
        while True:
            with _ready_cv:
                while len(_ready) >= READY_CAP:
                    _ready_cv.wait(1.0)
            listing_id = _pop()
            if not listing_id:
                try:
                    if refill():
                        continue
                except Exception:
                    pass
                time.sleep(0.25)
                continue
            try:
                job = _prepare_id(listing_id)
            except Exception:
                job = None
                with _lock:
                    _seen.discard(listing_id)
            if job is None:
                continue
            with _ready_cv:
                _ready.append(job)
                _ready_cv.notify_all()
            try:
                from .llm_enrich import poke_gpu

                poke_gpu()
            except Exception:
                pass
    finally:
        with _lock:
            _prep_workers = max(0, _prep_workers - 1)
            _ensure_workers_locked()
