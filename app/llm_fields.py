"""Catálogos y clasificadores que la LLM puede consultar por campo, por ciudad."""
from __future__ import annotations

import json
import re
from typing import Any

from .features import CREDIT_RE, TYPE_LABEL
from .geo import fold
from .listing_tags import catalog as tag_catalog, normalize_tag

KNOWN_TYPES = ("casa", "departamento", "ph", "terreno", "local", "oficina", "galpon")
TYPE_ALIASES = {
    "apartamento": "departamento",
    "depto": "departamento",
    "dpto": "departamento",
    "departamento": "departamento",
    "casa": "casa",
    "chalet": "casa",
    "quinta": "casa",
    "ph": "ph",
    "duplex": "ph",
    "dúplex": "ph",
    "triplex": "ph",
    "terreno": "terreno",
    "lote": "terreno",
    "lotes": "terreno",
    "fraccion": "terreno",
    "fracción": "terreno",
    "local": "local",
    "oficina": "oficina",
    "galpon": "galpon",
    "galpón": "galpon",
}
ORIENTATIONS = ("frente", "contrafrente", "lateral", "interno", "norte", "sur", "este", "oeste")
CONDITIONS = ("a estrenar", "en pozo", "en construcción", "reciclado", "a reciclar", "bueno")
LOCATION_KINDS = ("exact", "intersection", "approx", "unknown")
GENERIC_ZONAS = ("Zona Norte", "Zona Sur", "Zona Este", "Zona Oeste", "Centro")
CREDIT_VALUES = (
    {"valor": True, "cuando": "dice apto crédito hipotecario, UVA, Procrear o apto bancario"},
    {"valor": False, "cuando": "dice no apto crédito o no acepta crédito"},
    {"valor": None, "cuando": "no lo menciona"},
)
NO_CREDIT_RE = re.compile(
    r"no\s+apto\s+(?:a\s+)?(?:cr[eé]dito|bancario)|no\s+acepta\s+cr[eé]dito",
    re.I,
)
FIELDS = ("tipo", "credito", "barrio", "zona", "tags", "orientacion", "estado", "location_kind", "ciudad")
UNASSIGNED_CITIES = {"", "fuera", "otros", "argentina"}
CITY_PROMPT_MAX = 12
EXTRACT_SYSTEM = (
    "Extractor inmobiliario de Argentina. JSON de UNA línea, compacto. "
    "Sin markdown, sin notes, sin copiar el texto. /no_think\n"
    "Claves: city_label, barrio, zona, foreign, property_type, rooms, bedrooms, bathrooms, "
    "parking, covered_m2, uncovered_m2, total_m2, street, street_number, corner_a, corner_b, "
    "mortgage_credit, address_text. Omití vacíos.\n"
    f"property_type: {'|'.join(KNOWN_TYPES)}. lote/fracción=terreno; depto/apartamento=departamento; dúplex=ph.\n"
    "mortgage_credit true solo si dice apto crédito/UVA/Procrear; false si no apto; null si no lo menciona.\n"
    "barrio SOLO del catálogo OSM; si no, omitir. Sin teléfono ni vendedor.\n"
    "foreign=true si el aviso es de OTRO lugar, no del lugar_buscado.\n"
    "address_text = calle y altura, nunca lote/parcela/manzana. Si hay dos calles: corner_a y corner_b.\n"
    "covered_m2 cubiertos; uncovered_m2 descubiertos; total_m2 = lote o covered_m2 + uncovered_m2; "
    "depto sin patio: total=cubiertos. 'más de 200 m²' → 200.\n"
    "rooms=ambientes; bedrooms=dormitorios. Si hay N habitaciones y no dice ambientes: rooms=N+1 "
    "(2 habitaciones → 3 ambientes). Monoambiente: rooms=1 bedrooms=0. No copies bedrooms a rooms.\n"
    "Ejemplo: {\"property_type\":\"departamento\",\"rooms\":3,\"bedrooms\":2,\"foreign\":false,"
    "\"street\":\"Mitre\",\"street_number\":100,\"mortgage_credit\":null}"
)


def normalize_type(raw: str | None, fallback: str = "") -> str:
    token = fold(raw or "")
    if token in TYPE_ALIASES:
        return TYPE_ALIASES[token]
    if token in KNOWN_TYPES:
        return token
    from .scrapers import detect_type

    guessed = detect_type(raw or "", fallback if fallback in KNOWN_TYPES else "")
    return guessed if guessed in KNOWN_TYPES else (fallback if fallback in KNOWN_TYPES else "")


def classify_credit(text: str) -> bool | None:
    blob = text or ""
    if NO_CREDIT_RE.search(blob):
        return False
    if CREDIT_RE.search(blob):
        return True
    return None


def osm_barrios(city: str, query: str = "") -> list[dict[str, str]]:
    from .geo import _GENERIC_BARRIO, city_polygons

    needle = fold(query)
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in city_polygons(city):
        name = str(row.get("name") or "").strip()
        key = fold(name)
        if not name or key in seen or key in _GENERIC_BARRIO:
            continue
        if needle and needle not in key:
            continue
        seen.add(key)
        rows.append({"nombre": name, "zona": str(row.get("zona") or "")})
    rows.sort(key=lambda item: item["nombre"])
    return rows


def match_osm_barrio(name: str, city: str) -> dict[str, str] | None:
    text = fold(name)
    if not text or len(text) < 4:
        return None
    best = None
    best_len = 0
    for row in osm_barrios(city):
        alias = fold(row["nombre"])
        if alias == text:
            return row
        if alias in text and len(alias) >= best_len:
            best = row
            best_len = len(alias)
    return best


def field_catalog(campo: str, city: str = "", query: str = "") -> dict[str, Any]:
    key = fold(campo).replace("é", "e")
    if key in {"tipo", "property_type", "propiedad"}:
        return {
            "ok": True,
            "campo": "tipo",
            "valores": [{"id": tid, "label": TYPE_LABEL.get(tid, tid)} for tid in KNOWN_TYPES],
            "nota": "Elegí uno. Lote/fracción es terreno; depto/apartamento es departamento; dúplex es ph.",
        }
    if key in {"credito", "apto credito", "mortgage_credit"}:
        return {"ok": True, "campo": "credito", "valores": CREDIT_VALUES}
    if key in {"orientacion", "orientation"}:
        return {"ok": True, "campo": "orientacion", "valores": list(ORIENTATIONS)}
    if key in {"estado", "condition"}:
        return {"ok": True, "campo": "estado", "valores": list(CONDITIONS)}
    if key in {"location_kind", "ubicacion"}:
        return {"ok": True, "campo": "location_kind", "valores": list(LOCATION_KINDS)}
    if key in {"tags", "amenities"}:
        tags = tag_catalog()
        needle = fold(query)
        if needle:
            tags = [tag for tag in tags if needle in fold(tag)]
        return {"ok": True, "campo": "tags", "valores": tags[:40], "n": len(tags)}
    if key in {"zona", "zonas"}:
        zonas = list(dict.fromkeys([row["zona"] for row in osm_barrios(city) if row.get("zona")] + list(GENERIC_ZONAS)))
        return {"ok": True, "campo": "zona", "ciudad": city, "valores": zonas}
    if key in {"barrio", "barrios"}:
        rows = osm_barrios(city, query)
        return {
            "ok": True,
            "campo": "barrio",
            "ciudad": city,
            "valores": rows[:60],
            "n": len(rows),
            "nota": "Solo barrios OSM de la ciudad. Si no está, no inventes uno.",
        }
    if key in {"ciudad", "ciudades", "lugar", "localidad"}:
        return {
            "ok": True,
            "campo": "ciudad",
            "valores": cities_for_catalog(query),
            "nota": "Lugares ya cargados en PropMap. Si el aviso nombra otro, usá validar_lugar (Georef).",
        }
    return {"ok": False, "reason": "campo desconocido", "campos": list(FIELDS)}


def classify_field(campo: str, texto: str, city: str = "") -> dict[str, Any]:
    key = fold(campo).replace("é", "e")
    blob = (texto or "").strip()
    if key in {"tipo", "property_type", "propiedad"}:
        value = normalize_type(blob)
        return {"ok": bool(value), "campo": "tipo", "valor": value or None, "en_catalogo": value in KNOWN_TYPES}
    if key in {"credito", "apto credito", "mortgage_credit"}:
        value = classify_credit(blob)
        return {"ok": True, "campo": "credito", "valor": value}
    if key in {"barrio", "barrios"}:
        hit = match_osm_barrio(blob, city)
        if not hit:
            return {"ok": False, "campo": "barrio", "valor": None, "reason": "no está en OSM de la ciudad"}
        return {"ok": True, "campo": "barrio", "valor": hit["nombre"], "zona": hit["zona"]}
    if key in {"tags", "amenities"}:
        label = normalize_tag(blob)
        return {"ok": bool(label), "campo": "tags", "valor": label}
    if key in {"orientacion", "orientation"}:
        token = fold(blob)
        hit = next((item for item in ORIENTATIONS if fold(item) == token or fold(item) in token), None)
        return {"ok": bool(hit), "campo": "orientacion", "valor": hit}
    if key in {"estado", "condition"}:
        token = fold(blob)
        hit = next((item for item in CONDITIONS if fold(item) == token or fold(item) in token), None)
        return {"ok": bool(hit), "campo": "estado", "valor": hit}
    if key in {"ciudad", "ciudades", "lugar", "localidad"}:
        rows = cities_for_catalog(blob)
        exact = next(
            (row for row in rows if fold(row["label"]) == fold(blob) or fold(row["id"]) == fold(blob)),
            None,
        )
        if exact:
            return {"ok": True, "campo": "ciudad", "valor": exact["label"], "id": exact["id"]}
        return field_catalog("ciudad", city, blob)
    return field_catalog(campo, city, blob)


BARRIO_PROMPT_MAX = 12
DESC_PROMPT_MAX = 1600
JSON_RE = re.compile(r"\{[\s\S]{0,8000}\}")
_THINK_RE = re.compile(r"<think>[\s\S]*?</think>", re.I)


def extract_json_obj(raw: str | None) -> dict[str, Any] | None:
    """Lee el JSON del modelo aunque Qwen lo corte al tope de tokens."""
    if not raw:
        return None
    text = _THINK_RE.sub("", str(raw))
    text = text.replace("```json", "```")
    text = text.replace("```", "\n")
    start = text.find("{")
    if start < 0:
        return None
    blob = text[start:].strip()
    for candidate in (blob, _close_json_object(blob)):
        data = _loads_obj(candidate)
        if data:
            return data
        match = JSON_RE.search(candidate)
        if match:
            data = _loads_obj(match.group(0))
            if data:
                return data
    return None


def _loads_obj(raw: str) -> dict[str, Any] | None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not data:
        return None
    if not any(value not in (None, "", [], {}) for value in data.values()):
        return None
    return data


def _close_json_object(blob: str) -> str:
    out: list[str] = []
    in_str = False
    escape = False
    stack: list[str] = []
    for ch in blob:
        out.append(ch)
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in "}]" and stack and stack[-1] == ch:
            stack.pop()
    if in_str:
        out.append('"')
    text = "".join(out).rstrip()
    text = re.sub(r',\s*"[^"]*"\s*:\s*$', "", text)
    text = re.sub(r',\s*"[^"]*"?\s*$', "", text)
    text = re.sub(r",\s*$", "", text)
    return text + "".join(reversed(stack))


def city_is_unassigned(city: str | None) -> bool:
    return fold(city or "") in UNASSIGNED_CITIES


def cities_for_catalog(query: str = "") -> list[dict[str, str]]:
    """Lugares ya cargados. No es un listado hardcodeado: sale de CITIES (Georef/Nominatim)."""
    from .geo import CITIES

    needle = fold(query)
    rows: list[dict[str, str]] = []
    for cfg in list(CITIES.values()):
        cid = str(cfg.get("id") or "").strip()
        label = str(cfg.get("label") or cid.replace("-", " ")).strip()
        if not cid or cid in UNASSIGNED_CITIES:
            continue
        hay = f"{fold(label)} {fold(cid).replace('-', ' ')}"
        if needle and needle not in hay and fold(cid) != needle:
            continue
        rows.append({"id": cid, "label": label, "provincia": str(cfg.get("province") or "")})
        if len(rows) >= 40:
            break
    rows.sort(key=lambda row: fold(row["label"]))
    return rows


def cities_for_prompt(blob: str, extra: list[str] | None = None) -> list[str]:
    """Solo nombres que aparecen en el aviso, más los que ya resolvió Georef."""
    text = fold(blob)
    out: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        token = fold(name)
        if not token or token in seen or token in UNASSIGNED_CITIES or len(token) < 4:
            return
        seen.add(token)
        pretty = str(name).strip()
        if pretty:
            out.append(pretty)

    for name in extra or []:
        add(name)
        if len(out) >= CITY_PROMPT_MAX:
            return out
    if not text:
        return out
    from .geo import CITIES

    for cfg in list(CITIES.values()):
        label = str(cfg.get("label") or "").strip()
        cid = str(cfg.get("id") or "")
        token = fold(label)
        slug = fold(cid.replace("-", " "))
        if token and len(token) >= 4 and token in text:
            add(label)
        elif slug and len(slug) >= 5 and slug in text:
            add(label or cid.replace("-", " "))
        if len(out) >= CITY_PROMPT_MAX:
            break
    return out


def barrios_for_prompt(city: str, blob: str = "") -> list[dict[str, str]]:
    """Solo barrios que el aviso nombra. El catálogo entero no entra al prompt."""
    if city_is_unassigned(city):
        return []
    text = fold(blob)
    if not text:
        return []
    matched = [row for row in osm_barrios(city) if fold(row["nombre"]) in text]
    return matched[:BARRIO_PROMPT_MAX]


def zonas_for_prompt(city: str, barrios: list[dict[str, str]] | None = None) -> list[str]:
    rows = barrios if barrios is not None else osm_barrios(city)
    zonas = list(dict.fromkeys([row.get("zona") or "" for row in rows if row.get("zona")] + list(GENERIC_ZONAS)))
    return [z for z in zonas if z]


def build_extract_prompt(
    *,
    city: str,
    city_label: str,
    title: str,
    address: str,
    portal_type: str,
    description: str,
    known_places: list[str] | None = None,
    fixes: list[str] | None = None,
) -> str:
    from .listing_tags import tags_for_prompt

    blob = " ".join(part for part in (title, address, description) if part)
    unknown = city_is_unassigned(city)
    barrios = barrios_for_prompt(city, blob)
    if barrios:
        barrio_txt = "; ".join(
            f"{row['nombre']}" + (f" ({row['zona']})" if row.get("zona") else "") for row in barrios
        )
        barrio_line = f"barrios OSM del aviso: {barrio_txt}\n"
    else:
        barrio_line = "barrio vacío salvo coincidencia OSM.\n"
    zonas = zonas_for_prompt(city, barrios) if barrios else []
    zona_line = f"zonas: {', '.join(zonas)}\n" if zonas else ""
    places = cities_for_prompt(blob, known_places)
    place_txt = "; ".join(places) if places else ""
    place_line = (
        f"lugares detectados: {place_txt}\n"
        if place_txt
        else "city_label = localidad del texto, no un barrio.\n"
    )
    if unknown:
        lugar = "desconocido (el aviso no está asignado a una ciudad)"
        foreign_rule = (
            "city_label = localidad real, nunca un barrio. foreign=true solo si es de otro país.\n"
        )
    else:
        lugar = city_label or city
        foreign_rule = "foreign=true si el aviso es de OTRO lugar, no del lugar_buscado.\n"
    fix_txt = ""
    if fixes:
        fix_txt = f"errores a corregir: {'; '.join(str(x) for x in fixes if x)[:280]}\n"
    tags = tags_for_prompt(blob)
    tag_line = f"tags vistos: {tags}\n" if tags else ""
    return (
        f"{foreign_rule}{barrio_line}{zona_line}{place_line}{tag_line}{fix_txt}"
        f"lugar_buscado: {lugar}\n"
        f"titulo: {title[:180]}\n"
        f"direccion: {address[:140]}\n"
        f"tipo_portal: {portal_type}\n"
        f"texto: {description[:DESC_PROMPT_MAX]}\n"
    )


def clamp_extracted(data: dict[str, Any], *, city: str, blob: str) -> dict[str, Any]:
    out = dict(data)
    ptype = normalize_type(str(out.get("property_type") or ""))
    out["property_type"] = ptype or None
    guessed = classify_credit(blob)
    if guessed is False:
        out["mortgage_credit"] = False
    elif out.get("mortgage_credit") in {True, "true", "si", "sí", 1, "1"}:
        out["mortgage_credit"] = True
    elif out.get("mortgage_credit") in {False, "false", "no", 0, "0"}:
        out["mortgage_credit"] = False
    elif guessed is True:
        out["mortgage_credit"] = True
    else:
        out["mortgage_credit"] = None
    hit = match_osm_barrio(str(out.get("barrio") or ""), city) or match_osm_barrio(blob, city)
    if hit:
        out["barrio"] = hit["nombre"]
        if not str(out.get("zona") or "").strip():
            out["zona"] = hit.get("zona") or ""
    else:
        out["barrio"] = ""
    zonas = {fold(z): z for z in zonas_for_prompt(city)}
    zona_key = fold(str(out.get("zona") or ""))
    out["zona"] = zonas.get(zona_key) or (hit.get("zona") if hit else "") or ""
    ori = classify_field("orientacion", str(out.get("orientation") or ""), city)
    out["orientation"] = ori.get("valor") if ori.get("ok") else None
    cond = classify_field("estado", str(out.get("condition") or ""), city)
    out["condition"] = cond.get("valor") if cond.get("ok") else None
    from .features import combine_areas, extract_areas, extract_uncovered

    covered, lot = extract_areas(blob)
    uncovered = extract_uncovered(blob)
    llm_unc = out.get("uncovered_m2")
    try:
        if llm_unc not in {None, ""} and float(llm_unc) > 0:
            uncovered = max(uncovered or 0, float(llm_unc))
    except (TypeError, ValueError):
        pass
    try:
        llm_cov = float(out["covered_m2"]) if out.get("covered_m2") not in {None, ""} else None
    except (TypeError, ValueError):
        llm_cov = None
    try:
        llm_tot = float(out["total_m2"]) if out.get("total_m2") not in {None, ""} else None
    except (TypeError, ValueError):
        llm_tot = None
    covered, lot = combine_areas(
        covered or llm_cov,
        lot or llm_tot,
        uncovered,
        property_type=str(out.get("property_type") or ""),
    )
    if covered:
        out["covered_m2"] = covered
    if uncovered:
        out["uncovered_m2"] = uncovered
    if lot:
        out["total_m2"] = lot
    return out
