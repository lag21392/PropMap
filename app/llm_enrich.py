from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from collections import deque
from typing import Any

from . import store
from .geo import CITIES, can_place_on_map, location_incomplete, pin_listing_city
from .geo_tools import city_label
from .llm_fields import KNOWN_TYPES as TYPE_IDS, classify_credit, normalize_type
from .models import Listing
from .text_quality import looks_like_intersection

KNOWN_TYPES = set(TYPE_IDS)

LLM_SCHEMA = 7
_RUN_META = f"llm_run:{LLM_SCHEMA}"
_urgent: deque[str] = deque()
_queue: deque[str] = deque()
_seen: set[str] = set()
_id_city: dict[str, str] = {}
_last_prompt_city = ""
_lock = threading.Lock()
_ready: deque[tuple[str, Listing, dict[str, Any]]] = deque()
_ready_cv = threading.Condition()
_prep_workers = 0
PREP_WORKERS = 2
READY_CAP = 3
# Sin tope: el único slot de GPU no puede quedarse esperando a que SQLite
# termine de guardar. El hilo llm-apply drena la cola en orden.
_out_q: queue.Queue[Any] = queue.Queue(maxsize=64)
_out_lock = threading.Lock()
_out_worker: threading.Thread | None = None
_prov_lock = threading.Lock()
_prov_q: deque[tuple[str, str, str]] = deque()
_prov_ids: set[str] = set()
_prov_workers = 0
MAX_PROV_WORKERS = 2
_workers = 0
MAX_TRIES = 2
QUEUE_CAP = 48
MAX_TOKENS = 96
CHAT_TIMEOUT_SEC = 20.0
CHAT_CONNECT_SEC = 3.0
STUCK_SEC = 15.0
SKIP_FAIL_SEC = 90.0
ENRICH_BUDGET_SEC = 55.0
MAX_INNER = 4
_busy: dict[str, float] = {}
_skip_until: dict[str, float] = {}
_inner_live = 0
DEFAULT_CTX = 2048


class _GpuSlot:
    """Un slot robable: si el HTTP se cuelga, el watchdog libera a los demás."""

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._held = False
        self._owner = 0

    def acquire(self, timeout: float | None = None) -> bool:
        return self.acquire_ticket(timeout) is not None

    def acquire_ticket(self, timeout: float | None = None) -> int | None:
        deadline = None if timeout is None else time.time() + max(0.0, float(timeout))
        with self._cv:
            while self._held:
                if deadline is None:
                    self._cv.wait()
                    continue
                left = deadline - time.time()
                if left <= 0:
                    return None
                self._cv.wait(left)
            self._held = True
            self._owner += 1
            return self._owner

    def release(self, ticket: int | None = None) -> None:
        with self._cv:
            if ticket is not None and ticket != self._owner:
                return
            self._held = False
            self._cv.notify_all()

    def held(self) -> bool:
        with self._cv:
            return self._held

    def steal(self) -> None:
        with self._cv:
            self._owner += 1
            self._held = False
            self._cv.notify_all()


_gpu = _GpuSlot()
PHONE_RE = re.compile(
    r"(?:\+?54[\s-]?)?(?:9[\s-]?)?\d{2,4}[\s-]?\d{3,4}[\s-]?\d{4}|\bwhats?app\b[:\s]*\d[\d\s-]{6,}",
    re.I,
)
EMAIL_RE = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", re.I)
PII_KEYS = {
    "phone", "telefono", "tel", "whatsapp", "celular", "email", "mail",
    "publisher", "vendedor", "inmobiliaria", "empresa", "agencia", "seller",
    "contacto", "asesor",
}
KEEP_KEYS = (
    "city_label", "province", "barrio", "zona", "foreign", "property_type", "rooms",
    "bedrooms", "bathrooms", "parking", "covered_m2", "uncovered_m2", "total_m2", "age_years",
    "amenities", "expenses", "floor", "orientation", "condition", "street",
    "street_number", "corner_a", "corner_b", "address_text", "notes",
    "mortgage_credit", "location_kind", "tags",
)
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "validar_esquina",
            "description": "Valida una esquina escrita en el aviso y devuelve coordenadas si existe en la ciudad.",
            "parameters": {
                "type": "object",
                "properties": {
                    "calle_a": {"type": "string"},
                    "calle_b": {"type": "string"},
                    "ciudad": {"type": "string"},
                },
                "required": ["calle_a", "calle_b"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validar_direccion",
            "description": "Valida calle y altura en texto plano. No sirve para teléfonos ni inmobiliarias.",
            "parameters": {
                "type": "object",
                "properties": {
                    "street": {"type": "string"},
                    "number": {"type": "integer"},
                    "ciudad": {"type": "string"},
                },
                "required": ["street", "number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validar_calle",
            "description": "Comprueba si una calle existe en la ciudad buscada.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "ciudad": {"type": "string"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validar_lugar",
            "description": "Valida una ciudad, localidad o barrio contra Georef/Nominatim. Usala si el aviso nombra un lugar o no tiene ciudad asignada.",
            "parameters": {
                "type": "object",
                "properties": {
                    "nombre": {"type": "string"},
                    "provincia": {"type": "string"},
                    "ciudad": {"type": "string"},
                },
                "required": ["nombre"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "catalogo_campo",
            "description": (
                "Lista los valores permitidos de un campo: tipo, credito, barrio, zona, tags, "
                "orientacion, estado, location_kind o ciudad. ciudad = lugares ya cargados (filtrá con q). "
                "Para barrio/zona usa los OSM de la ciudad buscada. Si el aviso nombra otra localidad, validar_lugar."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "campo": {
                        "type": "string",
                        "enum": ["tipo", "credito", "barrio", "zona", "tags", "orientacion", "estado", "location_kind", "ciudad"],
                    },
                    "q": {"type": "string", "description": "Filtro opcional, sobre todo para barrio o tags"},
                    "ciudad": {"type": "string"},
                },
                "required": ["campo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clasificar_campo",
            "description": (
                "Clasifica un dato del aviso contra el catálogo: tipo de propiedad, apto crédito, "
                "barrio OSM, tag, orientación o estado. No inventa barrios."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "campo": {
                        "type": "string",
                        "enum": ["tipo", "credito", "barrio", "tags", "orientacion", "estado", "ciudad"],
                    },
                    "texto": {"type": "string"},
                    "ciudad": {"type": "string"},
                },
                "required": ["campo", "texto"],
            },
        },
    },
]


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def mark_schema_run() -> str:
    """Marca el inicio de esta pasada de schema, una sola vez."""
    store.init()
    started = store.get_meta(_RUN_META)
    if started:
        return started
    stamp = _now_iso()
    store.set_meta(_RUN_META, stamp)
    return stamp


def schema_run_started() -> str:
    store.init()
    return store.get_meta(_RUN_META)


def llm_url() -> str:
    return (os.environ.get("LLM_URL") or os.environ.get("OLLAMA_URL") or "").rstrip("/")


def llm_provider() -> str:
    raw = (os.environ.get("LLM_PROVIDER") or "").strip().lower()
    if raw:
        return raw
    from .llm_gemini import api_key

    if api_key():
        return "gemini"
    return "local"


def llm_model() -> str:
    if llm_provider() == "gemini":
        from .llm_gemini import DEFAULT_MODEL

        raw = (os.environ.get("GEMINI_MODEL") or os.environ.get("LLM_MODEL") or "").strip()
        if not raw or raw == "qwen35-9b-iq2xxs":
            return DEFAULT_MODEL
        return raw
    return (os.environ.get("LLM_MODEL") or os.environ.get("OLLAMA_MODEL") or "qwen35-9b-iq2xxs").strip()


def enabled() -> bool:
    if os.environ.get("PROPMAP_TEST") == "1":
        return False
    if llm_provider() in {"off", "none", "0", "disabled"}:
        return False
    if llm_provider() == "gemini":
        from .llm_gemini import api_key

        return bool(api_key())
    return bool(llm_url())


def llm_parallel() -> int:
    raw = os.environ.get("LLM_PARALLEL") or "1"
    try:
        return max(1, min(8, int(raw)))
    except ValueError:
        return 1


def llm_workers() -> int:
    """En local, un hilo por slot de GPU. Pascal tiene 1: dos workers se pisan y se truncan."""
    if llm_provider() == "gemini":
        raw = os.environ.get("LLM_WORKERS") or "6"
        try:
            return max(1, min(12, int(raw)))
        except ValueError:
            return 6
    return llm_parallel()


def llm_ctx() -> int:
    raw = os.environ.get("LLM_CTX") or str(DEFAULT_CTX)
    try:
        return max(1024, min(16384, int(raw)))
    except ValueError:
        return DEFAULT_CTX


def should_publish(item: Listing) -> bool:
    extra = item.extra or {}
    if extra.get("duplicate_of") or extra.get("dedupe_hidden"):
        return False
    if not enabled():
        return True
    if extra.get("llm_ready") or extra.get("llm_ver") == LLM_SCHEMA:
        return True
    return can_place_on_map(item)


def _city_unassigned(item: Listing) -> bool:
    from .llm_fields import city_is_unassigned

    return city_is_unassigned(item.city)


def _has_data_fixes(item: Listing) -> bool:
    fixes = (item.extra or {}).get("data_fixes") or []
    return bool(fixes)


def needs_improve(item: Listing) -> bool:
    """Falta enriquecer: schema, sin ciudad, errores puntuales o ubicación incompleta."""
    extra = item.extra or {}
    if extra.get("duplicate_of") or extra.get("dedupe_hidden"):
        return False
    if extra.get("llm_thin") and (item.details_scraped or extra.get("details_at")):
        return True
    if _city_unassigned(item) and not extra.get("llm_city_ok"):
        return True
    if _has_data_fixes(item) and not extra.get("llm_repair"):
        return True
    if extra.get("llm_ver") == LLM_SCHEMA and extra.get("llm_ready") and not extra.get("llm_partial"):
        return False
    if extra.get("llm_partial") and extra.get("llm_ver") == LLM_SCHEMA:
        return False
    return True


def can_run_now(item: Listing) -> bool:
    """Hay con qué enriquecer ahora: título, texto, ficha, ciudad por resolver, o la ficha ya falló."""
    from .detail_fetch import COLD_TRIES
    from .freshness import has_usable_listing_text, needs_detail_fetch

    if has_usable_listing_text(item):
        return True
    if (item.description or "").strip() or (item.title or "").strip():
        return True
    if not needs_detail_fetch(item):
        return True
    extra = item.extra or {}
    if extra.get("llm_thin") and (item.details_scraped or extra.get("details_at")):
        return True
    if _city_unassigned(item) and not extra.get("llm_city_ok"):
        return True
    if int(extra.get("detail_tries") or 0) >= COLD_TRIES:
        return True
    return False


def mark_await_llm(item: Listing) -> Listing:
    if not enabled():
        return item
    extra = dict(item.extra or {})
    if extra.get("llm_ready") or extra.get("llm_ver") == LLM_SCHEMA:
        return item
    if can_place_on_map(item):
        extra.pop("await_llm", None)
        item.extra = extra
        return item
    extra["await_llm"] = True
    item.extra = extra
    return item


def queue_stats() -> dict[str, Any]:
    now = time.time()
    with _lock:
        pending = len(_urgent) + len(_queue)
        busy = [
            {"id": lid, "s": round(max(0.0, now - started), 1)}
            for lid, started in _busy.items()
        ]
        stats = {
            "pending": pending,
            "urgent": len(_urgent),
            "rest": len(_queue),
            "ready": len(_ready),
            "saving": _out_q.qsize(),
            "cleaning": len(_busy),
            "workers": llm_workers(),
            "cap": QUEUE_CAP,
            "enabled": enabled(),
            "schema": LLM_SCHEMA,
            "provider": llm_provider(),
            "model": llm_model(),
            "busy": busy,
            "busy_s": max((row["s"] for row in busy), default=0),
            "slots": llm_parallel() if llm_provider() != "gemini" else llm_workers(),
        }
        live = llama_status()
    stats["gpu"] = bool(live.get("gpu")) or gpu_held()
    stats["llama_ok"] = bool(live.get("ok"))
    stats["n_ctx"] = int(live.get("n_ctx") or 0)
    return stats


def llama_status() -> dict[str, Any]:
    if llm_provider() != "local" or not llm_url():
        return {"ok": False, "gpu": False, "n_ctx": 0}
    try:
        import httpx

        with httpx.Client(timeout=2.0, trust_env=False) as client:
            health = client.get(f"{llm_url()}/health")
            slots = client.get(f"{llm_url()}/slots")
        rows = slots.json() if slots.status_code == 200 else []
        if not isinstance(rows, list):
            rows = []
        gpu = any(isinstance(row, dict) and row.get("is_processing") for row in rows)
        n_ctx = 0
        for row in rows:
            if isinstance(row, dict):
                try:
                    n_ctx = max(n_ctx, int(row.get("n_ctx") or 0))
                except (TypeError, ValueError):
                    pass
        return {"ok": health.status_code == 200, "gpu": gpu, "n_ctx": n_ctx}
    except Exception:
        return {"ok": True, "gpu": gpu_held(), "n_ctx": 0}


def _ops_note(metric: str, **labels: Any) -> None:
    try:
        from .ops import note

        note(metric, **labels)
    except Exception:
        return


def _queue_copy(item: Listing) -> None:
    try:
        from .llm_copy import enqueue as enqueue_copy

        enqueue_copy([item])
    except Exception:
        return


def enqueue(listings: list[Listing] | None, *, urgent: bool = False) -> None:
    if not enabled() or not listings:
        return
    now = time.time()
    with _lock:
        for item in listings:
            extra = item.extra or {}
            if not needs_improve(item):
                continue
            cooling = (_skip_until.get(item.id) or 0) > now
            if cooling and not urgent:
                continue
            if urgent:
                _skip_until.pop(item.id, None)
            if item.id in _seen:
                if (
                    location_incomplete(item)
                    or extra.get("await_llm")
                    or _city_unassigned(item)
                    or _has_data_fixes(item)
                ):
                    _promote(item.id)
                continue
            urgent_item = (
                location_incomplete(item)
                or extra.get("await_llm")
                or _city_unassigned(item)
                or _has_data_fixes(item)
                or urgent
            )
            pending = len(_urgent) + len(_queue)
            if pending >= QUEUE_CAP:
                if not urgent_item:
                    continue
                if _queue:
                    dropped = _queue.pop()
                    _seen.discard(dropped)
                    _id_city.pop(dropped, None)
                else:
                    continue
            _seen.add(item.id)
            _id_city[item.id] = _prompt_group(item)
            if (
                location_incomplete(item)
                or extra.get("await_llm")
                or _city_unassigned(item)
                or _has_data_fixes(item)
            ):
                _urgent.appendleft(item.id)
            elif urgent:
                _urgent.append(item.id)
            else:
                _queue.append(item.id)
        _ensure_workers_locked()


def refill(prefer_city: str = "") -> int:
    """Toma avisos de toda la base y llena la cola si hay hueco."""
    if not enabled():
        return 0
    with _lock:
        pending = len(_urgent) + len(_queue)
        room = max(0, QUEUE_CAP - pending)
        now = time.time()
        skip = set(_seen)
        skip.update(lid for lid, until in _skip_until.items() if until > now)
        blocked = sum(1 for until in _skip_until.values() if until > now)
        if len(_skip_until) > 400:
            for lid in [lid for lid, until in _skip_until.items() if until <= now]:
                _skip_until.pop(lid, None)
    if room <= 0:
        with _lock:
            _ensure_workers_locked()
        return 0
    from . import store

    items = store.fetch_llm_backlog(
        room + 24 + min(blocked, 80),
        prefer_city=prefer_city,
        schema=LLM_SCHEMA,
        skip_ids=skip,
    )

    take: list = []
    for item in items:
        if item.id in skip:
            continue
        if not can_run_now(item):
            continue
        take.append(item)
        if len(take) >= room:
            break
    take.sort(key=lambda item: (_prompt_group(item), item.id))
    if take:
        enqueue(take)
    elif os.environ.get("PROPMAP_TEST") != "1":
        with _lock:
            _ensure_workers_locked()
    return len(take)


def _promote(listing_id: str) -> None:
    if listing_id in _urgent:
        _urgent.remove(listing_id)
        _urgent.appendleft(listing_id)
        return
    if listing_id in _queue:
        _queue.remove(listing_id)
        _urgent.appendleft(listing_id)


def _prompt_group(item: Listing) -> str:
    from .llm_fields import city_is_unassigned

    extra = item.extra or {}
    cid = str(extra.get("search_city") or item.city or "").strip()
    if city_is_unassigned(cid):
        return ""
    return cid


def _pop_from(q: deque[str], now: float, prefer: str) -> str | None:
    skipped: list[str] = []
    other: list[str] = []
    found = None
    while q:
        lid = q.popleft()
        if lid in _busy or (_skip_until.get(lid) or 0) > now:
            skipped.append(lid)
            continue
        if prefer and _id_city.get(lid, "") != prefer:
            other.append(lid)
            continue
        found = lid
        break
    if found is None and other:
        found = other.pop(0)
    for lid in reversed(other):
        q.appendleft(lid)
    if skipped:
        q.extend(skipped)
    return found


def _pop() -> str | None:
    now = time.time()
    with _lock:
        prefer = _last_prompt_city
        for q in (_urgent, _queue):
            found = _pop_from(q, now, prefer)
            if found:
                return found
    return None


def _has_work() -> bool:
    return bool(_urgent or _queue)


_extract_on_gpu = False


def gpu_held() -> bool:
    return _gpu.held()


def poke_gpu() -> None:
    """Despierta al hilo de GPU: hay un prompt de extract o de copy listo."""
    with _ready_cv:
        _ready_cv.notify_all()


def extract_busy() -> bool:
    """Hay extracción lista para la GPU o ya generando. El apply a SQLite no cuenta."""
    if _extract_on_gpu:
        return True
    with _ready_cv:
        return bool(_ready)


def ensure_running() -> None:
    if not enabled():
        return
    with _lock:
        _ensure_workers_locked()


def _wanted_workers() -> int:
    """Un hilo: Pascal tiene 1 slot. El segundo se pisa y alarga cada aviso."""
    if llm_provider() != "local":
        return llm_workers()
    return 1


def _ensure_workers_locked() -> None:
    global _workers, _prep_workers
    testing = os.environ.get("PROPMAP_TEST") == "1"
    want = _wanted_workers()
    while _workers < want and (not testing or _has_work()):
        _workers += 1
        threading.Thread(target=_drain, daemon=True, name=f"llm-enrich-{_workers}").start()
    if testing:
        return
    while _prep_workers < PREP_WORKERS:
        _prep_workers += 1
        threading.Thread(target=_drain_prep, daemon=True, name=f"llm-prep-{_prep_workers}").start()
    try:
        from .llm_copy import ensure_running as ensure_copy

        ensure_copy()
    except Exception:
        pass


def _take_copy_job() -> tuple | None:
    try:
        from .llm_copy import fichas_pending, take_ready

        if fichas_pending():
            return None
        return take_ready()
    except Exception:
        return None


def _next_gpu_job(wait: float) -> tuple[str | None, tuple | None]:
    """Un solo consumidor de GPU: extracción si hay prompt, si no descripción."""
    job = _take_prepared(0)
    if job is not None:
        return "extract", job
    copy_job = _take_copy_job()
    if copy_job is not None:
        return "copy", copy_job
    job = _take_prepared(max(0.0, wait))
    if job is not None:
        return "extract", job
    copy_job = _take_copy_job()
    if copy_job is not None:
        return "copy", copy_job
    return None, None


def _run_extract_gpu(job: tuple) -> None:
    global _extract_on_gpu
    listing_id, item, prep = job
    _extract_on_gpu = True
    try:
        raw = _chat_json(str(prep.get("prompt") or ""))
    except Exception:
        raw = ""
    finally:
        _extract_on_gpu = False
    _finish_async(
        lambda lid=listing_id, row=item, pre=prep, txt=raw: _finish_job(lid, row, pre, txt)
    )


def _run_copy_gpu(job: tuple) -> None:
    from . import llm_copy

    listing_id, item, messages = job
    llm_copy.mark_gpu(listing_id)
    raw = ""
    try:
        payload = _chat(messages, use_tools=False, max_tokens=llm_copy.MAX_TOKENS)
        if payload:
            message = payload.get("message") or (payload.get("choices") or [{}])[0].get("message") or {}
            raw = str(message.get("content") or "")
    except Exception:
        raw = ""
    _finish_async(lambda lid=listing_id, row=item, txt=raw: llm_copy.complete(lid, row, txt))


def _drain() -> None:
    """Turno de GPU: extract primero, copy si el slot quedaría libre. No toca SQLite."""
    global _workers
    testing = os.environ.get("PROPMAP_TEST") == "1"
    try:
        while True:
            if testing:
                listing_id = _pop()
                if not listing_id:
                    return
                try:
                    _enrich_id(listing_id)
                except Exception:
                    pass
                continue
            kind, job = _next_gpu_job(0.12)
            if kind == "extract" and job is not None:
                _run_extract_gpu(job)
            elif kind == "copy" and job is not None:
                _run_copy_gpu(job)
    finally:
        with _lock:
            _workers = max(0, _workers - 1)
            _ensure_workers_locked()


def _take_prepared(timeout: float) -> tuple[str, Listing, dict[str, Any]] | None:
    with _ready_cv:
        if not _ready:
            if timeout <= 0:
                return None
            _ready_cv.wait(timeout)
        if _ready:
            job = _ready.popleft()
            _ready_cv.notify_all()
            return job
    return None


def _drain_prep() -> None:
    """Etapa de entrada: SQLite y armado del prompt mientras la GPU trabaja en el anterior."""
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
                time.sleep(0.3)
                continue
            try:
                job = _prepare_id(listing_id)
            except Exception:
                job = None
                with _lock:
                    _busy.pop(listing_id, None)
                    _seen.discard(listing_id)
            if job is None:
                continue
            with _ready_cv:
                _ready.append(job)
                _ready_cv.notify_all()
    finally:
        with _lock:
            _prep_workers = max(0, _prep_workers - 1)
            _ensure_workers_locked()


def _prepare_id(listing_id: str) -> tuple[str, Listing, dict[str, Any]] | None:
    item = store.get_listing(listing_id)
    if not item or not needs_improve(item):
        with _lock:
            _seen.discard(listing_id)
        return None
    if _needs_details_first(listing_id, item):
        return None
    prep = _build_job(item)
    if not prep:
        with _lock:
            _seen.discard(listing_id)
        return None
    with _lock:
        _busy[listing_id] = time.time()
    return listing_id, item, prep


def _finish_job(listing_id: str, item: Listing, prep: dict[str, Any], raw: str) -> None:
    """Etapa de salida: parseo, apply y SQLite ya con la GPU en el aviso siguiente."""
    try:
        data = _finish_analysis(item, prep, raw)
        if not data:
            _commit_llm_fail(listing_id, item)
            return
        with _lock:
            _skip_until.pop(listing_id, None)
            if listing_id not in _urgent and listing_id not in _queue:
                _seen.discard(listing_id)
        _commit_llm_ok(item, data)
    finally:
        with _lock:
            _busy.pop(listing_id, None)


def _enrich_id(listing_id: str) -> None:
    global _inner_live
    with _lock:
        if _inner_live >= MAX_INNER:
            _seen.discard(listing_id)
            _skip_until[listing_id] = time.time() + SKIP_FAIL_SEC
            return
        _busy[listing_id] = time.time()
        _inner_live += 1
    done = threading.Event()

    def run() -> None:
        global _inner_live
        try:
            _enrich_id_inner(listing_id)
        except Exception:
            pass
        finally:
            done.set()
            with _lock:
                _inner_live = max(0, _inner_live - 1)

    threading.Thread(target=run, daemon=True, name="llm-one").start()
    if not done.wait(ENRICH_BUDGET_SEC):
        _gpu.steal()
        _ops_note("llm", outcome="stuck")
        with _lock:
            _seen.discard(listing_id)
            _skip_until[listing_id] = time.time() + SKIP_FAIL_SEC
    with _lock:
        _busy.pop(listing_id, None)


def _save_llm_item(item: Listing) -> None:
    """SQLite only: rebuild del mapa en este hilo traba la GPU."""
    store.upsert_listings([item], notify=False)
    _maybe_queue_province(item)


def _out_loop() -> None:
    while True:
        fn = _out_q.get()
        try:
            fn()
        except Exception:
            pass
        finally:
            _out_q.task_done()


def _finish_async(fn) -> None:
    """Cola de salida: un solo hilo aplica y escribe, así no pelea el write lock de SQLite."""
    global _out_worker
    if os.environ.get("PROPMAP_TEST") == "1":
        fn()
        return
    with _out_lock:
        if _out_worker is None or not _out_worker.is_alive():
            _out_worker = threading.Thread(target=_out_loop, daemon=True, name="llm-apply")
            _out_worker.start()
    _out_q.put(fn)


def _mark_no_city_skip_details(item: Listing) -> None:
    """Si la pasada de ciudad no dio localidad, no reintentar la ficha."""
    from .llm_fields import city_is_unassigned

    extra = dict(item.extra or {})
    if city_is_unassigned(item.city):
        extra["skip_details"] = True
        item.extra = extra


_laya_q: queue.Queue[str] = queue.Queue()
_laya_worker: threading.Thread | None = None
_laya_lock = threading.Lock()


def _enrich_with_laya(item: Listing) -> None:
    """Señales de producto: portal + regex, y Laya para los huecos."""
    from .listing_signals import enrich_with_laya

    enrich_with_laya(item)


def _laya_loop() -> None:
    while True:
        listing_id = _laya_q.get()
        try:
            item = store.get_listing(listing_id)
            if item:
                _enrich_with_laya(item)
                _save_llm_item(item)
        except Exception:
            pass
        finally:
            _laya_q.task_done()


def _ensure_laya_worker() -> None:
    global _laya_worker
    with _laya_lock:
        if _laya_worker is not None and _laya_worker.is_alive():
            return
        _laya_worker = threading.Thread(target=_laya_loop, daemon=True, name="laya-signals")
        _laya_worker.start()


def _schedule_laya(item: Listing) -> None:
    """Laya es un ModernBERT en CPU. No puede frenar el guardado del aviso."""
    if os.environ.get("PROPMAP_TEST") == "1":
        _enrich_with_laya(item)
        return
    _laya_q.put(item.id)
    _ensure_laya_worker()

def _commit_llm_ok(item: Listing, data: dict[str, Any]) -> None:
    apply_analysis(item, data)
    if os.environ.get("PROPMAP_TEST") == "1":
        _enrich_with_laya(item)
    extra = dict(item.extra or {})
    extra["llm_ready"] = True
    extra["await_llm"] = False
    extra["llm_partial"] = False
    extra["llm_ver"] = LLM_SCHEMA
    extra["llm_city_ok"] = True
    extra["llm_repair"] = True
    extra["llm_at"] = _now_iso()
    if item.details_scraped or extra.get("details_at"):
        extra.pop("llm_thin", None)
    else:
        extra["llm_thin"] = True
    item.extra = extra
    _mark_no_city_skip_details(item)
    _save_llm_item(item)
    _ops_note("llm", outcome="ok")
    _queue_copy(item)
    if os.environ.get("PROPMAP_TEST") != "1":
        _schedule_laya(item)


def _needs_details_first(listing_id: str, item: Listing) -> bool:
    """True si el aviso todavía no tiene texto para analizar: primero baja la ficha."""
    from . import detail_fetch
    from .freshness import needs_detail_fetch

    extra = item.extra or {}
    if not needs_detail_fetch(item):
        return False
    if _city_unassigned(item) and not extra.get("llm_city_ok"):
        return False
    with detail_fetch._lock:
        already = listing_id in detail_fetch._seen
    detail_fetch.enqueue([item])
    if can_run_now(item) or already:
        return False
    with _lock:
        _seen.discard(listing_id)
    return True


def _commit_llm_fail(listing_id: str, item: Listing) -> None:
    extra = dict(item.extra or {})
    extra["llm_tries"] = int(extra.get("llm_tries") or 0) + 1
    item.extra = extra
    if extra["llm_tries"] >= MAX_TRIES:
        if os.environ.get("PROPMAP_TEST") == "1":
            _enrich_with_laya(item)
        extra["llm_ready"] = True
        extra["llm_partial"] = True
        extra["llm_ver"] = LLM_SCHEMA
        extra["await_llm"] = False
        extra["llm_city_ok"] = True
        extra["llm_repair"] = True
        extra["llm_at"] = _now_iso()
        item.extra = extra
        _mark_no_city_skip_details(item)
        _save_llm_item(item)
        _ops_note("llm", outcome="partial")
        if os.environ.get("PROPMAP_TEST") != "1":
            _schedule_laya(item)
        with _lock:
            _seen.discard(listing_id)
            _skip_until.pop(listing_id, None)
        _queue_copy(item)
        return
    _save_llm_item(item)
    with _lock:
        _seen.discard(listing_id)
        _skip_until[listing_id] = time.time() + SKIP_FAIL_SEC
    _ops_note("llm", outcome="retry")


def _enrich_id_inner(listing_id: str) -> None:
    item = store.get_listing(listing_id)
    if not item:
        with _lock:
            _seen.discard(listing_id)
        return
    if not needs_improve(item):
        with _lock:
            _seen.discard(listing_id)
        return
    if _needs_details_first(listing_id, item):
        return
    time.sleep(0)
    data = analyze_listing(item)
    if not data:
        _commit_llm_fail(listing_id, item)
        return
    with _lock:
        _skip_until.pop(listing_id, None)
        if listing_id not in _urgent and listing_id not in _queue:
            _seen.discard(listing_id)
    _finish_async(lambda: _commit_llm_ok(item, data))


def _scrub(text: str) -> str:
    cleaned = PHONE_RE.sub("[oculto]", text or "")
    return EMAIL_RE.sub("[oculto]", cleaned)


def _looks_like_intersection(text: str) -> bool:
    return looks_like_intersection(text)


def _build_job(item: Listing) -> dict[str, Any] | None:
    """Todo el CPU previo al pedido: Georef cacheado, barrios y prompt."""
    if not enabled():
        return None
    from .llm_fields import build_extract_prompt, city_is_unassigned
    from .place_api import listing_places

    search = str((item.extra or {}).get("search_city") or item.city or "")
    global _last_prompt_city
    _last_prompt_city = _prompt_group(item)
    title = _scrub(item.title or "")
    address = _scrub(item.address or "")
    description = _scrub(item.description or "")
    places = listing_places(item, remote=False)
    known = []
    for place in places:
        name = str(place.get("name") or "").strip()
        if not name:
            continue
        prov = str(place.get("province") or "").strip()
        known.append(f"{name}, {prov}" if prov else name)
    prompt = build_extract_prompt(
        city=search,
        city_label=city_label(search) or search,
        title=title,
        address=address,
        portal_type=item.property_type or "",
        description=description,
        known_places=known,
        fixes=list((item.extra or {}).get("data_fixes") or []),
    )
    return {
        "prompt": prompt,
        "search": search,
        "blob": " ".join(p for p in (title, address, description) if p),
        "places": places,
        "need_geo": city_is_unassigned(search),
    }


def _finish_analysis(item: Listing, prep: dict[str, Any], raw: str) -> dict[str, Any] | None:
    from .llm_fields import clamp_extracted, extract_json_obj
    from .place_api import place_conflicts_city

    data = extract_json_obj(raw)
    if not data:
        return None
    search = str(prep.get("search") or "")
    places = list(prep.get("places") or [])
    need_geo = bool(prep.get("need_geo"))
    data = clamp_extracted(data, city=search, blob=str(prep.get("blob") or ""))
    for place in places:
        if search and place_conflicts_city(place, search):
            data["foreign"] = True
            data["city_label"] = place.get("name") or data.get("city_label") or ""
            break
        if need_geo and place.get("name") and not str(data.get("city_label") or "").strip():
            data["city_label"] = place.get("name")
    if not str(data.get("province") or data.get("provincia") or "").strip():
        for place in places:
            prov = str(place.get("province") or "").strip()
            if prov:
                data["province"] = prov
                break
    data["geo_tools"] = {"found": {}, "checked": [], "geo": None}
    return data


def analyze_listing(item: Listing) -> dict[str, Any] | None:
    prep = _build_job(item)
    if not prep:
        return None
    return _finish_analysis(item, prep, _chat_json(str(prep.get("prompt") or "")))


def _chat_json(prompt: str) -> str:
    from .llm_fields import EXTRACT_SYSTEM

    payload = _chat(
        [
            {
                "role": "system",
                "content": EXTRACT_SYSTEM,
            },
            {"role": "user", "content": prompt},
        ],
        use_tools=False,
    )
    if not payload:
        return ""
    message = payload.get("message") or payload.get("choices", [{}])[0].get("message") or {}
    return str(message.get("content") or "")


def _chat_with_tools(prompt: str, city: str) -> str:
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "Sos un extractor. No inventes valores fuera de los catálogos. "
                "No copies teléfonos ni vendedores. "
                "Para tipo, crédito, barrio, zona o tags, llamá catalogo_campo o clasificar_campo. "
                "Si hay esquina, calle o una ciudad/localidad, llamá la herramienta de validación. "
                "El barrio del aviso no se copia: si no está en OSM, dejalo vacío. "
                "Cuando termines, devolvé solo JSON. /no_think"
            ),
        },
        {"role": "user", "content": prompt},
    ]
    last = ""
    for _ in range(4):
        payload = _chat(messages, use_tools=True)
        if not payload:
            return last
        message = payload.get("message") or payload.get("choices", [{}])[0].get("message") or {}
        last = str(message.get("content") or "")
        calls = message.get("tool_calls") or []
        if not calls:
            return last
        messages.append({"role": "assistant", "content": last, "tool_calls": calls})
        for call in calls:
            fn = (call.get("function") or {}) if isinstance(call, dict) else {}
            name = str(fn.get("name") or "")
            args = fn.get("arguments") or "{}"
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            result = _run_tool(name, args if isinstance(args, dict) else {}, city)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(call.get("id") or name),
                    "name": name,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )
    return last


def _run_tool(name: str, args: dict[str, Any], fallback_city: str) -> dict[str, Any]:
    from .geo_tools import validate_address, validate_corner, validate_street
    from .geo import resolve_city

    city = resolve_city(str(args.get("ciudad") or fallback_city) or "") or fallback_city
    if name == "validar_esquina":
        return validate_corner(str(args.get("calle_a") or ""), str(args.get("calle_b") or ""), city)
    if name == "validar_direccion":
        return validate_address(str(args.get("street") or args.get("calle") or ""), args.get("number") or 0, city)
    if name == "validar_calle":
        return validate_street(str(args.get("name") or args.get("calle") or ""), city)
    if name == "validar_lugar":
        return _validate_place(str(args.get("nombre") or args.get("name") or ""), city, args.get("provincia"))
    if name == "catalogo_campo":
        from .llm_fields import field_catalog

        return field_catalog(str(args.get("campo") or ""), city, str(args.get("q") or args.get("query") or ""))
    if name == "clasificar_campo":
        from .llm_fields import classify_field

        return classify_field(
            str(args.get("campo") or ""),
            str(args.get("texto") or args.get("text") or ""),
            city,
        )
    return {"ok": False, "reason": "herramienta desconocida"}


def _validate_place(name: str, city: str, province: str | None = None) -> dict[str, Any]:
    from .place_api import lookup_place, place_conflicts_city

    token = (name or "").strip()
    if len(token) < 3:
        return {"ok": False, "reason": "nombre corto"}
    place = lookup_place(token, province_hint=str(province or "") or None)
    if not place:
        return {"ok": False, "nombre": token, "reason": "no encontrado"}
    foreign = bool(city) and place_conflicts_city(place, city)
    return {
        "ok": True,
        "nombre": place.get("name") or token,
        "province": place.get("province") or "",
        "kind": place.get("kind") or "",
        "lat": place.get("lat"),
        "lon": place.get("lon"),
        "foreign": foreign,
    }


def _chat(messages: list[dict[str, Any]], *, use_tools: bool = False, max_tokens: int | None = None) -> dict[str, Any]:
    tools = TOOLS if use_tools else []
    tokens = max(16, int(max_tokens or MAX_TOKENS))
    if llm_provider() == "gemini":
        from .llm_gemini import chat as gemini_chat

        return gemini_chat(messages, tools, max_output_tokens=tokens)

    ctx = llm_ctx()
    body: dict[str, Any] = {
        "model": llm_model(),
        "stream": False,
        "think": False,
        "chat_template_kwargs": {"enable_thinking": False},
        "cache_prompt": True,
        "options": {"temperature": 0.1, "num_predict": tokens, "num_ctx": ctx},
        "max_tokens": tokens,
        "temperature": 0.1,
        "stop": ["```", "<|im_end|>", "<end_of_turn>"],
        "messages": messages,
    }
    if tools:
        body["tools"] = tools
    url = f"{llm_url()}/v1/chat/completions"
    wait = CHAT_TIMEOUT_SEC + STUCK_SEC
    # Backoff exponencial con jitter simple antes de intentar
    import random
    backoff = 0.5 * (2 ** min(3, int(time.time() % 5)))
    time.sleep(backoff + random.uniform(0, 0.3))
    ticket = _gpu.acquire_ticket(timeout=wait)
    if ticket is None:
        return {}
    try:
        import httpx

        timeout = httpx.Timeout(
            connect=CHAT_CONNECT_SEC,
            read=CHAT_TIMEOUT_SEC,
            write=min(15.0, CHAT_TIMEOUT_SEC),
            pool=CHAT_CONNECT_SEC,
        )
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            killer = threading.Timer(CHAT_TIMEOUT_SEC + 2.0, client.close)
            killer.daemon = True
            killer.start()
            try:
                res = client.post(url, json=body)
                payload = res.json()
            finally:
                killer.cancel()
        if isinstance(payload, dict) and (payload.get("choices") or payload.get("message")):
            return payload
    except Exception:
        return {}
    finally:
        _gpu.release(ticket)
    return {}


def apply_analysis(item: Listing, data: dict[str, Any]) -> None:
    extra = dict(item.extra or {})
    clean = {k: data.get(k) for k in KEEP_KEYS if k not in PII_KEYS}
    for key in list(clean):
        if key in PII_KEYS:
            clean.pop(key, None)
    notes = _scrub(str(clean.get("notes") or ""))
    if PHONE_RE.search(notes) or EMAIL_RE.search(notes):
        notes = ""
    clean["notes"] = notes[:160]
    extra["llm"] = clean
    extra["llm_ver"] = LLM_SCHEMA
    geo = (data.get("geo_tools") or {}).get("geo") if isinstance(data.get("geo_tools"), dict) else None
    if geo:
        extra["llm_geo"] = {
            "lat": geo.get("lat"),
            "lon": geo.get("lon"),
            "label": geo.get("label") or "",
            "approx": bool(geo.get("approx")),
        }
    item.extra = extra
    ptype = normalize_type(str(data.get("property_type") or ""), item.property_type or "")
    if ptype in KNOWN_TYPES:
        item.property_type = ptype
    credit = data.get("mortgage_credit")
    if credit in {True, "true", "si", "sí", 1, "1"}:
        extra["mortgage_credit"] = True
    elif credit in {False, "false", "no", 0, "0"}:
        extra["mortgage_credit"] = False
    blob = " ".join(p for p in (item.title, item.address, item.description) if p)
    guessed_credit = classify_credit(blob or "")
    if extra.get("mortgage_credit") is None and guessed_credit is not None:
        extra["mortgage_credit"] = guessed_credit
    elif guessed_credit is False:
        extra["mortgage_credit"] = False
    corner_a = str(data.get("corner_a") or "").strip()
    corner_b = str(data.get("corner_b") or "").strip()
    if corner_a and corner_b:
        from .geo import street_names_match

        if not street_names_match(corner_a, corner_b):
            extra["intersection"] = f"{corner_a} y {corner_b}"
    _fill_number(item, "rooms", data.get("rooms"))
    _fill_number(item, "bedrooms", data.get("bedrooms"))
    _fill_number(item, "bathrooms", data.get("bathrooms"), allow_float=True)
    from .layout import apply_layout_counts

    apply_layout_counts(item)
    _fill_number(item, "parking", data.get("parking"))
    _fill_number(item, "covered_m2", data.get("covered_m2"), allow_float=True)
    _fill_number(item, "total_m2", data.get("total_m2"), allow_float=True)
    from .features import fill_areas

    fill_areas(item, blob)
    _fill_number(item, "age_years", data.get("age_years"))
    expenses = data.get("expenses")
    if expenses not in {None, ""}:
        extra["expenses"] = expenses
        item.extra = extra
    from .listing_tags import apply_tags

    apply_tags(item, data)
    extra = dict(item.extra or {})
    address = str(data.get("address_text") or "").strip()
    from .text_quality import is_plot_label, is_plot_street_name

    if address and 6 <= len(address) <= 80 and not PHONE_RE.search(address) and not is_plot_label(address):
        if _looks_like_intersection(address):
            extra.setdefault("intersection", address)
            extra.setdefault("location_kind", "intersection")
        else:
            from .geo import parse_street, street_names_match

            cur_s, cur_n = parse_street(item.address or "")
            new_s, new_n = parse_street(address)
            conflict = bool(cur_s and cur_n and new_s and new_n and not street_names_match(cur_s, new_s))
            if not conflict and (not item.address or len(address) > len(item.address)):
                item.address = address
    street = str(data.get("street") or "").strip()
    number = data.get("street_number") or data.get("number")
    if (
        street
        and number
        and not is_plot_street_name(street)
        and not is_plot_label(street, str(number))
        and (not item.address or not re.search(r"\d", item.address or "") or is_plot_label(item.address or ""))
    ):
        try:
            item.address = f"{street} {int(number)}"
        except (TypeError, ValueError):
            pass
    item.extra = extra
    from .geo import apply_recovered_location

    apply_recovered_location(item)
    extra = dict(item.extra or {})
    _apply_place_api(item, data)
    extra = dict(item.extra or {})
    moved = _maybe_assign_city(item, data)
    _stamp_province_from_city(item, data)
    extra = dict(item.extra or {})
    if moved:
        return
    extra = dict(item.extra or {})
    llm_address = bool(address and not _looks_like_intersection(address)) or bool(street and number)
    llm_crossing = bool(str(data.get("corner_a") or "").strip() and str(data.get("corner_b") or "").strip())
    if address and _looks_like_intersection(address):
        llm_crossing = True
    if llm_address or llm_crossing:
        extra.setdefault("intersection", extra.get("intersection") or "")
        if llm_crossing and not extra.get("location_kind"):
            extra["location_kind"] = "intersection"
        item.extra = extra
        if os.environ.get("PROPMAP_TEST") == "1":
            from .scrapers import locate_item

            locate_item(item)
            apply_recovered_location(item)
            from .geo import drop_water_pin

            drop_water_pin(item)
            extra = dict(item.extra or {})
            item.extra = extra
    else:
        item.extra = extra


def _maybe_assign_city(item: Listing, data: dict[str, Any]) -> bool:
    """True si hay que cortar apply_analysis (se movió a otra ciudad)."""
    extra = dict(item.extra or {})
    search = str(extra.get("search_city") or "")
    label = str(data.get("city_label") or (extra.get("llm_place") or {}).get("name") or "").strip()
    guessed = _ensure_assigned_city(label) if label else None
    foreign = data.get("foreign") in {True, "true", "si", "sí", 1, "1"}
    if _city_unassigned(item) and guessed:
        item.city = guessed
        extra["resolved_city"] = guessed
        extra["llm"] = dict(extra.get("llm") or {})
        extra["llm"]["foreign"] = False
        item.extra = extra
        return False
    if foreign:
        from .geo import in_city_radius, same_place_ids

        local = guessed and search and (
            guessed == search or guessed in same_place_ids(search) or search in same_place_ids(guessed)
        )
        portal_here = False
        try:
            plat, plon = float(extra["portal_lat"]), float(extra["portal_lon"])
            portal_here = bool(search) and in_city_radius(plat, plon, search)
        except (KeyError, TypeError, ValueError):
            portal_here = False
        if local or portal_here:
            extra["llm"] = dict(extra.get("llm") or {})
            extra["llm"]["foreign"] = False
            item.extra = extra
            return False
        item.city = guessed or "fuera"
        extra["resolved_city"] = item.city
        item.extra = extra
        if item.lat is not None and item.lon is not None:
            if not search or in_city_radius(item.lat, item.lon, search):
                item.lat = None
                item.lon = None
                item.has_exact_location = False
        return True
    item.extra = extra
    return False


def _ensure_assigned_city(label: str) -> str | None:
    """Solo cache: el worker no puede pegarle a Georef/Nominatim o se traba la GPU."""
    token = (label or "").strip()
    if not token:
        return None
    from .llm_fields import city_is_unassigned
    from .place_api import lookup_place
    from .places import _from_georef_place, _is_city_place
    from .geo import resolve_city

    place = lookup_place(token, remote=False)
    if place:
        parsed = _from_georef_place(place)
        if parsed and not _is_city_place(parsed):
            return None
        if parsed and _is_city_place(parsed):
            cid = resolve_city(str(parsed.get("id") or parsed.get("label") or token))
            if cid and not city_is_unassigned(cid):
                return cid
    guessed = _city_from_label(token)
    if guessed and not city_is_unassigned(guessed):
        return guessed
    return None


def _fill_number(item: Listing, field: str, value: Any, allow_float: bool = False) -> None:
    if value in {None, ""}:
        return
    try:
        number = float(value)
    except (TypeError, ValueError):
        return
    if number <= 0 or number > 100_000:
        return
    if not allow_float:
        number = int(number)
    current = getattr(item, field, None)
    if current in {None, 0}:
        setattr(item, field, number)
        return
    if field == "total_m2" and number > float(current) + 9:
        setattr(item, field, number)


def _extra_province(extra: dict | None) -> str:
    extra = extra or {}
    place = extra.get("llm_place") if isinstance(extra.get("llm_place"), dict) else {}
    llm = extra.get("llm") if isinstance(extra.get("llm"), dict) else {}
    return str(place.get("province") or llm.get("province") or "").strip()


def _write_province(item: Listing, data: dict[str, Any], province: str, *, name: str = "") -> None:
    raw = (province or "").strip()
    if not raw:
        return
    extra = dict(item.extra or {})
    llm = dict(extra.get("llm") or {})
    place = dict(extra.get("llm_place") or {})
    if not str(place.get("province") or "").strip():
        place["province"] = raw
        if name:
            place.setdefault("name", name)
        extra["llm_place"] = place
    if not str(llm.get("province") or "").strip():
        llm["province"] = raw
        extra["llm"] = llm
    if not str(data.get("province") or "").strip():
        data["province"] = raw
    item.extra = extra
    cid = str(item.city or "").strip()
    if cid:
        from .places import apply_city_province

        apply_city_province(cid, raw)


def _province_for_city_id(city_id: str) -> str:
    from .geo import same_place_ids
    from .llm_fields import city_is_unassigned
    from .places import _is_caba_province, _province_display

    if not city_id or city_is_unassigned(city_id):
        return ""
    seen: set[str] = set()
    for cid in (city_id, *(same_place_ids(city_id) or [])):
        if not cid or cid in seen:
            continue
        seen.add(cid)
        raw = str((CITIES.get(cid) or {}).get("province") or "").strip()
        if not raw:
            continue
        pretty = _province_display(raw)
        if pretty and not _is_caba_province(pretty):
            return pretty
        if not _is_caba_province(raw):
            return raw
    return ""


def _stamp_province_from_city(item: Listing, data: dict[str, Any]) -> None:
    extra = dict(item.extra or {})
    if _extra_province(extra):
        return
    cid = str(item.city or extra.get("search_city") or "").strip()
    pretty = _province_for_city_id(cid)
    if not pretty:
        from .place_api import lookup_place

        label = str((CITIES.get(cid) or {}).get("label") or cid)
        if label:
            place = lookup_place(label, remote=False)
            pretty = str((place or {}).get("province") or "").strip()
    if not pretty:
        return
    name = str((extra.get("llm_place") or {}).get("name") or (CITIES.get(cid) or {}).get("label") or "")
    _write_province(item, data, pretty, name=name)


def _maybe_queue_province(item: Listing) -> None:
    if os.environ.get("PROPMAP_TEST") == "1":
        return
    extra = item.extra or {}
    if _extra_province(extra):
        return
    llm = extra.get("llm") if isinstance(extra.get("llm"), dict) else {}
    place = extra.get("llm_place") if isinstance(extra.get("llm_place"), dict) else {}
    label = str(llm.get("city_label") or place.get("name") or "").strip()
    cid = str(item.city or extra.get("search_city") or "").strip()
    if not label:
        label = str((CITIES.get(cid) or {}).get("label") or "").strip()
    if not label and cid and cid not in {"fuera", "otros", "argentina"}:
        label = cid.replace("-", " ").strip()
    if not label:
        return
    lid = item.id
    with _prov_lock:
        if lid in _prov_ids:
            return
        _prov_ids.add(lid)
        _prov_q.append((lid, label, cid))
        _ensure_prov_workers_locked()


def _ensure_prov_workers_locked() -> None:
    global _prov_workers
    while _prov_workers < MAX_PROV_WORKERS and _prov_q:
        _prov_workers += 1
        threading.Thread(target=_drain_province, daemon=True, name="llm-province").start()


def _drain_province() -> None:
    global _prov_workers
    try:
        while True:
            with _prov_lock:
                if not _prov_q:
                    return
                listing_id, label, city = _prov_q.popleft()
            try:
                _fill_province_remote(listing_id, label, city)
            except Exception:
                pass
            finally:
                with _prov_lock:
                    _prov_ids.discard(listing_id)
    finally:
        with _prov_lock:
            _prov_workers = max(0, _prov_workers - 1)
            _ensure_prov_workers_locked()


def _fill_province_remote(listing_id: str, label: str, city: str) -> None:
    from .place_api import lookup_place

    place = lookup_place(label, remote=True)
    if not place:
        cfg = CITIES.get(city) or {}
        fallback = str(cfg.get("label") or "").strip()
        if fallback and fallback != label:
            place = lookup_place(fallback, remote=True)
    province = str((place or {}).get("province") or "").strip()
    if not province:
        return
    item = store.get_listing(listing_id)
    if not item or _extra_province(item.extra):
        return
    _write_province(item, {}, province, name=str((place or {}).get("name") or label))
    store.upsert_listings([item], notify=False)


def _apply_place_api(item: Listing, data: dict[str, Any]) -> None:
    from .place_api import lookup_place, place_conflicts_city
    from .places import apply_city_province

    extra = dict(item.extra or {})
    search = str(extra.get("search_city") or item.city or "")
    label = str(data.get("city_label") or "").strip()
    hint = str(data.get("province") or data.get("provincia") or "").strip()
    if not label:
        return
    place = lookup_place(label, province_hint=hint or None, remote=False)
    if not place:
        return
    province = str(place.get("province") or hint or "").strip()
    extra["llm_place"] = {
        "name": place.get("name") or label,
        "province": province,
        "lat": place.get("lat"),
        "lon": place.get("lon"),
    }
    llm = dict(extra.get("llm") or {})
    if province and not str(llm.get("province") or "").strip():
        llm["province"] = province
        extra["llm"] = llm
        data["province"] = province
    item.extra = extra
    if item.city and province:
        apply_city_province(item.city, province)
    from .llm_fields import city_is_unassigned

    if search and not city_is_unassigned(search) and place_conflicts_city(place, search):
        data["foreign"] = True
        guessed = _city_from_label(str(place.get("name") or label))
        if guessed:
            data["city_label"] = place.get("name") or label
            extra["resolved_city"] = guessed
            item.extra = extra


def _city_from_label(label: str) -> str | None:
    from .geo import CITY_ALIASES, fold, resolve_city, slug_place
    from .place_api import lookup_place

    token = fold(label)
    if not token:
        return None
    if token in CITY_ALIASES:
        return CITY_ALIASES[token]
    for city_id, cfg in list(CITIES.items()):
        if fold(cfg.get("label") or "") == token:
            return city_id
    place = lookup_place(label, remote=False)
    if place and place.get("name"):
        return resolve_city(str(place.get("name"))) or slug_place(str(place.get("name")))
    return resolve_city(label) or None
