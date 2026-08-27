from __future__ import annotations

import json
import os
import re
import threading
from collections import deque
from typing import Any

from . import store
from .features import CREDIT_RE
from .geo import CITIES, can_place_on_map, location_incomplete, pin_listing_city
from .geo_tools import city_label, run_location_tools
from .models import Listing
from .text_quality import looks_like_intersection

KNOWN_TYPES = {"casa", "departamento", "ph", "terreno", "local", "oficina", "galpon"}

LLM_SCHEMA = 5
_urgent: deque[str] = deque()
_queue: deque[str] = deque()
_seen: set[str] = set()
_lock = threading.Lock()
_workers = 0
MAX_TRIES = 2
JSON_RE = re.compile(r"\{[\s\S]{0,4000}\}")
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
    "city_label", "barrio", "zona", "foreign", "property_type", "rooms",
    "bedrooms", "bathrooms", "parking", "covered_m2", "total_m2", "age_years",
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
            "description": "Valida una ciudad, localidad o barrio contra Georef/Nominatim. Usala si el aviso nombra un lugar.",
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
]


def llm_url() -> str:
    return (os.environ.get("LLM_URL") or os.environ.get("OLLAMA_URL") or "").rstrip("/")


def llm_model() -> str:
    return (os.environ.get("LLM_MODEL") or os.environ.get("OLLAMA_MODEL") or "qwen35-9b-iq2xxs").strip()


def enabled() -> bool:
    if os.environ.get("PROPMAP_TEST") == "1":
        return False
    return bool(llm_url())


def llm_workers() -> int:
    raw = os.environ.get("LLM_WORKERS") or "1"
    try:
        return max(1, min(8, int(raw)))
    except ValueError:
        return 2


def should_publish(item: Listing) -> bool:
    extra = item.extra or {}
    if extra.get("duplicate_of") or extra.get("dedupe_hidden"):
        return False
    if not enabled():
        return True
    if extra.get("llm_ready") or extra.get("llm_ver") == LLM_SCHEMA:
        return True
    return can_place_on_map(item)


def needs_improve(item: Listing) -> bool:
    """La LLM solo toca avisos incompletos. Si ya hay calle/cruce, se deja como está."""
    extra = item.extra or {}
    if extra.get("llm_ver") == LLM_SCHEMA and extra.get("llm_ready") and not extra.get("llm_partial"):
        return False
    if extra.get("llm_partial"):
        return False
    return bool(location_incomplete(item) or extra.get("await_llm"))


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
    with _lock:
        pending = len(_urgent) + len(_queue)
        return {
            "pending": pending,
            "cleaning": int(_workers),
            "workers": llm_workers(),
            "enabled": enabled(),
        }


def enqueue(listings: list[Listing] | None, *, urgent: bool = False) -> None:
    if not enabled() or not listings:
        return
    with _lock:
        for item in listings:
            extra = item.extra or {}
            if extra.get("llm_ver") == LLM_SCHEMA and extra.get("llm_ready") and not extra.get("llm_partial"):
                continue
            if not needs_improve(item):
                continue
            if item.id in _seen:
                if location_incomplete(item):
                    _promote(item.id)
                continue
            _seen.add(item.id)
            if location_incomplete(item):
                _urgent.appendleft(item.id)
            elif extra.get("await_llm") or extra.get("llm_partial") or urgent:
                _urgent.append(item.id)
            else:
                _queue.append(item.id)
        _ensure_workers_locked()


def _promote(listing_id: str) -> None:
    if listing_id in _urgent:
        _urgent.remove(listing_id)
        _urgent.appendleft(listing_id)
        return
    if listing_id in _queue:
        _queue.remove(listing_id)
        _urgent.appendleft(listing_id)


def _has_work() -> bool:
    return bool(_urgent or _queue)


def _ensure_workers_locked() -> None:
    global _workers
    while _has_work() and _workers < llm_workers():
        _workers += 1
        threading.Thread(target=_drain, daemon=True, name=f"llm-enrich-{_workers}").start()


def _drain() -> None:
    global _workers
    try:
        while True:
            with _lock:
                if _urgent:
                    listing_id = _urgent.popleft()
                elif _queue:
                    listing_id = _queue.popleft()
                else:
                    return
            _enrich_id(listing_id)
    finally:
        with _lock:
            _workers = max(0, _workers - 1)
            _ensure_workers_locked()


def _enrich_id(listing_id: str) -> None:
    item = store.get_listing(listing_id)
    if not item:
        return
    extra = dict(item.extra or {})
    if extra.get("llm_ver") == LLM_SCHEMA and extra.get("llm_ready") and not extra.get("llm_partial"):
        return
    if not needs_improve(item):
        return
    from .freshness import needs_detail_fetch
    from .scrapers.details import enrich_details

    if needs_detail_fetch(item):
        try:
            enrich_details(item)
        except Exception:
            from .features import analyze

            analyze(item)
        extra = dict(item.extra or {})
        item.extra = extra
        store.upsert_many([item])
        item = store.get_listing(listing_id) or item
    data = analyze_listing(item)
    if not data:
        extra = dict(item.extra or {})
        extra["llm_tries"] = int(extra.get("llm_tries") or 0) + 1
        item.extra = extra
        if extra["llm_tries"] >= MAX_TRIES:
            extra["llm_ready"] = True
            extra["llm_partial"] = True
            extra["llm_ver"] = LLM_SCHEMA
            extra["await_llm"] = False
            item.extra = extra
            pin_listing_city(item)
            store.upsert_many([item])
        else:
            store.upsert_many([item])
            with _lock:
                _seen.discard(listing_id)
                if location_incomplete(item) or extra.get("await_llm"):
                    _urgent.appendleft(listing_id)
                else:
                    _queue.append(listing_id)
        return
    apply_analysis(item, data)
    extra = dict(item.extra or {})
    extra["llm_ready"] = True
    extra["await_llm"] = False
    extra["llm_partial"] = False
    extra["llm_ver"] = LLM_SCHEMA
    item.extra = extra
    pin_listing_city(item)
    store.upsert_many([item])


def _scrub(text: str) -> str:
    cleaned = PHONE_RE.sub("[oculto]", text or "")
    return EMAIL_RE.sub("[oculto]", cleaned)


def _looks_like_intersection(text: str) -> bool:
    return looks_like_intersection(text)


def analyze_listing(item: Listing) -> dict[str, Any] | None:
    if not enabled():
        return None
    from .listing_tags import catalog_text

    cities = ", ".join(sorted({cfg.get("label") or cid for cid, cfg in CITIES.items()}))
    search = str((item.extra or {}).get("search_city") or item.city or "")
    prompt = (
        "Extraé datos estructurales de un aviso inmobiliario de Argentina. "
        "Usá las herramientas si el texto trae calle, altura, esquina o un lugar. "
        "PROHIBIDO extraer teléfono, WhatsApp, mail, inmobiliaria, empresa o vendedor. "
        "Al final respondé SOLO un JSON, sin markdown, con: "
        "city_label, barrio, zona, foreign (true si NO es del lugar_buscado), "
        "property_type (casa|departamento|ph|terreno|local|oficina|galpon), rooms, bedrooms, bathrooms, "
        "parking, covered_m2, total_m2, age_years, expenses, "
        "floor, orientation, condition, street, street_number, corner_a, corner_b, "
        "mortgage_credit (true si dice apto crédito hipotecario / UVA / Procrear), "
        "address_text (SOLO calle y altura, nunca la esquina), "
        "tags (atributos repetibles que el aviso confirma), "
        "notes (muy corto, sin contacto). "
        "Si hay dos calles unidas por 'y', van en corner_a y corner_b y llamá validar_esquina. "
        "Si nombra una ciudad o localidad, llamá validar_lugar. "
        f"Tags preferidos: {catalog_text(24)}. "
        "Podés agregar un tag nuevo solo si es comparable entre avisos, nunca una dirección.\n"
        f"lugar_buscado: {city_label(search) or search}\n"
        f"titulo: {_scrub(item.title or '')[:180]}\n"
        f"direccion: {_scrub(item.address or '')[:140]}\n"
        f"barrio: {item.barrio}\n"
        f"texto: {_scrub(item.description or '')[:520]}\n"
    )
    raw = _chat_with_tools(prompt, search)
    if not raw:
        return None
    match = JSON_RE.search(raw)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    blob = " ".join(p for p in (item.title, item.address, item.description) if p)
    data["geo_tools"] = run_location_tools(blob, search, data)
    return data


def _chat_with_tools(prompt: str, city: str) -> str:
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "Sos un extractor. No inventes. No copies teléfonos ni vendedores. "
                "Si hay esquina, calle o una ciudad/localidad, llamá la herramienta. "
                "Cuando termines, devolvé solo JSON. /no_think"
            ),
        },
        {"role": "user", "content": prompt},
    ]
    last = ""
    for _ in range(4):
        payload = _chat(messages)
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


def _chat(messages: list[dict[str, Any]]) -> dict[str, Any]:
    import urllib.error
    import urllib.request

    body = {
        "model": llm_model(),
        "stream": False,
        "think": False,
        "tools": TOOLS,
        "chat_template_kwargs": {"enable_thinking": False},
        "options": {"temperature": 0.1, "num_predict": 360, "num_ctx": 4096},
        "max_tokens": 360,
        "temperature": 0.1,
        "messages": messages,
    }
    endpoints = (f"{llm_url()}/v1/chat/completions", f"{llm_url()}/api/chat")
    for url in endpoints:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
            continue
        if isinstance(payload, dict):
            return payload
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
    ptype = str(data.get("property_type") or "").strip().lower()
    if ptype in KNOWN_TYPES:
        item.property_type = ptype
    credit = data.get("mortgage_credit")
    if credit in {True, "true", "si", "sí", 1, "1"}:
        extra["mortgage_credit"] = True
    elif credit in {False, "false", "no", 0, "0"}:
        extra["mortgage_credit"] = False
    blob = " ".join(p for p in (item.title, item.address, item.description) if p)
    if extra.get("mortgage_credit") is None and CREDIT_RE.search(blob or ""):
        extra["mortgage_credit"] = True
    corner_a = str(data.get("corner_a") or "").strip()
    corner_b = str(data.get("corner_b") or "").strip()
    if corner_a and corner_b:
        extra["intersection"] = f"{corner_a} y {corner_b}"
    barrio = str(data.get("barrio") or "").strip()
    if barrio and len(barrio) < 48 and barrio.lower() not in {"sin clasificar", "n/a"}:
        item.barrio = barrio
    zona = str(data.get("zona") or "").strip()
    if zona and len(zona) < 40:
        item.zona = zona
    _fill_number(item, "rooms", data.get("rooms"))
    _fill_number(item, "bedrooms", data.get("bedrooms"))
    _fill_number(item, "bathrooms", data.get("bathrooms"), allow_float=True)
    _fill_number(item, "parking", data.get("parking"))
    _fill_number(item, "covered_m2", data.get("covered_m2"), allow_float=True)
    _fill_number(item, "total_m2", data.get("total_m2"), allow_float=True)
    _fill_number(item, "age_years", data.get("age_years"))
    expenses = data.get("expenses")
    if expenses not in {None, ""}:
        extra["expenses"] = expenses
        item.extra = extra
    from .listing_tags import apply_tags

    apply_tags(item, data)
    extra = dict(item.extra or {})
    address = str(data.get("address_text") or "").strip()
    if address and 6 <= len(address) <= 80 and not PHONE_RE.search(address):
        if _looks_like_intersection(address):
            extra.setdefault("intersection", address)
            extra.setdefault("location_kind", "intersection")
        elif not item.address or len(address) > len(item.address):
            item.address = address
    street = str(data.get("street") or "").strip()
    number = data.get("street_number") or data.get("number")
    if street and number and (not item.address or not re.search(r"\d", item.address or "")):
        try:
            item.address = f"{street} {int(number)}"
        except (TypeError, ValueError):
            pass
    item.extra = extra
    _apply_place_api(item, data)
    extra = dict(item.extra or {})
    if data.get("foreign") in {True, "true", "si", "sí", 1, "1"}:
        label = str(data.get("city_label") or "")
        guessed = _city_from_label(label)
        search = str(extra.get("search_city") or "")
        from .geo import in_city_radius, same_place_ids

        local = guessed and search and (guessed == search or guessed in same_place_ids(search) or search in same_place_ids(guessed))
        portal_here = (
            extra.get("portal_lat") is not None
            and extra.get("portal_lon") is not None
            and item.lat is not None
            and item.lon is not None
            and (not search or in_city_radius(item.lat, item.lon, search))
        )
        if local or portal_here:
            extra["llm"] = dict(extra.get("llm") or {})
            extra["llm"]["foreign"] = False
            item.extra = extra
        else:
            item.city = guessed or "fuera"
            if item.lat is not None and item.lon is not None:
                from .geo import in_city_radius

                if not search or in_city_radius(item.lat, item.lon, search):
                    item.lat = None
                    item.lon = None
                    item.has_exact_location = False
            return
    llm_address = bool(address and not _looks_like_intersection(address)) or bool(street and number)
    llm_crossing = bool(str(data.get("corner_a") or "").strip() and str(data.get("corner_b") or "").strip())
    if address and _looks_like_intersection(address):
        llm_crossing = True
    if llm_address or llm_crossing:
        from .scrapers import locate_item

        locate_item(item)
        extra = dict(item.extra or {})
        item.extra = extra


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
    if getattr(item, field, None) in {None, 0}:
        setattr(item, field, number)


def _apply_place_api(item: Listing, data: dict[str, Any]) -> None:
    from .place_api import lookup_place, place_conflicts_city

    extra = dict(item.extra or {})
    search = str(extra.get("search_city") or item.city or "")
    label = str(data.get("city_label") or "").strip()
    if not label:
        return
    place = lookup_place(label)
    if not place:
        return
    extra["llm_place"] = {
        "name": place.get("name") or label,
        "province": place.get("province") or "",
        "lat": place.get("lat"),
        "lon": place.get("lon"),
    }
    item.extra = extra
    if search and place_conflicts_city(place, search):
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
    for city_id, cfg in CITIES.items():
        if fold(cfg.get("label") or "") == token:
            return city_id
    place = lookup_place(label)
    if place and place.get("name"):
        return resolve_city(str(place.get("name"))) or slug_place(str(place.get("name")))
    return resolve_city(label) or None
