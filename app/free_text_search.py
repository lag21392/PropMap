"""
free_text_search.py
Parser ligero de búsqueda libre para PropMap (backend).
Convierte texto natural en filtros existentes.
Estrategia: reglas con regex, sin dependencias externas pesadas.
Compatible con parse-free-text.js del frontend.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional


# Mapeo de tipos de propiedad
TYPE_MAP = {
    "casa": "casa",
    "casas": "casa",
    "departamento": "departamento",
    "departamentos": "departamento",
    "depto": "departamento",
    "deptos": "departamento",
    "ph": "ph",
    "duplex": "ph",
    "dúplex": "ph",
    "terreno": "terreno",
    "lote": "terreno",
    "lotes": "terreno",
    "local": "local",
    "locales": "local",
    "oficina": "oficina",
    "oficinas": "oficina",
    "galpon": "galpon",
    "galpón": "galpon",
    "galpones": "galpon",
    "monoambiente": "monoambiente",
    "monoambientes": "monoambiente",
}

# Mapeo de traits/características
TRAIT_MAP = [
    {"keys": ["credito", "apto credito", "hipotecable", "hipoteca"], "value": "credit"},
    {"keys": ["dueno", "dueño directo", "dueño", "owner"], "value": "owner"},
    {"keys": ["expensas bajas", "bajas expensas", "expensas baja"], "value": "expenses"},
    {"keys": ["urgente", "venta urgente", "rapido"], "value": "urgent"},
    {"keys": ["tranquilo", "silencioso", "quiet"], "value": "quiet"},
    {"keys": ["buen estado", "bueno estado", "renovado", "impecable"], "value": "good"},
    {"keys": ["balcon", "balcón"], "value": "balcony"},
    {"keys": ["luminoso", "luz", "luminoso"], "value": "bright"},
    {"keys": ["crecimiento", "crece", "en crecimiento"], "value": "growing"},
    {"keys": ["vista", "vista abierta", "vista panoramica"], "value": "view"},
    {"keys": ["patio", "jardin"], "value": "patio"},
    {"keys": ["cochera", "garage", "garaje", "parking"], "value": "garage"},
    {"keys": ["terraza", "terraza"], "value": "terrace"},
    {"keys": ["reciclado", "reciclada", "renovado", "renovada"], "value": "renovated"},
    {"keys": ["a estrenar", "estrenar", "nuevo", "nueva", "obra nueva"], "value": "new"},
]


@dataclass
class ParsedFilters:
    """Resultado del parsing de texto libre."""
    type: Optional[str] = None
    city: Optional[str] = None
    barrio: Optional[str] = None
    zona: Optional[str] = None
    min_beds: Optional[int] = None
    min_rooms: Optional[int] = None
    min_baths: Optional[int] = None
    min_m2: Optional[int] = None
    max_m2: Optional[int] = None
    max_price: Optional[float] = None
    price_currency: Optional[str] = None
    traits: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convierte a diccionario compatible con filtros frontend."""
        result = {}
        if self.type:
            result["typeFilter"] = self.type
        if self.city:
            result["cityFilter"] = self.city
        if self.barrio:
            result["barrioFilter"] = self.barrio
        if self.zona:
            result["zonaFilter"] = self.zona
        if self.min_beds is not None:
            result["minBeds"] = self.min_beds
        if self.min_rooms is not None:
            result["minRooms"] = self.min_rooms
        if self.min_baths is not None:
            result["minBaths"] = self.min_baths
        if self.min_m2 is not None:
            result["minM2"] = self.min_m2
        if self.max_m2 is not None:
            result["maxM2"] = self.max_m2
        if self.max_price is not None:
            result["maxPrice"] = self.max_price
        if self.price_currency:
            result["priceCurrency"] = self.price_currency
        if self.traits:
            result["traits"] = self.traits
        return result


def normalize(text: str) -> str:
    """Normaliza texto: quita acentos, lower, espacios."""
    if not text:
        return ""
    # NFD + remove combining marks
    text = unicodedata.normalize("NFD", text).encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = text.replace("-", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def parse_number(text: str) -> Optional[float]:
    """Parsea número con soporte para 'k' (miles) y 'mil'."""
    if not text:
        return None
    cleaned = text.replace(",", ".").replace(" ", "")
    # Detectar 'k' al final
    if cleaned.lower().endswith("k"):
        try:
            return float(cleaned[:-1]) * 1000
        except ValueError:
            return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def find_types(text: str) -> list[str]:
    """Todos los tipos que la frase nombra, sin repetir."""
    found: list[str] = []
    seen: set[str] = set()
    if re.search(r"\bmono\s*h?\s*ambientes?\b", text):
        found.append("departamento")
        seen.add("departamento")
    for key, value in TYPE_MAP.items():
        if value == "monoambiente" or value in seen:
            continue
        if re.search(r"\b" + re.escape(key) + r"\b", text):
            seen.add(value)
            found.append(value)
    return found


def find_type(text: str) -> Optional[str]:
    """Encuentra tipo de propiedad en el texto."""
    if re.search(r"\bmono\s*h?\s*ambientes?\b", text):
        return "monoambiente"
    found = find_types(text)
    return found[0] if found else None


def _as_list(value) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


def _one_or_many(values: list[str]):
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return values


def find_traits(text: str) -> list[str]:
    """Encuentra traits en el texto."""
    found = set()
    for trait in TRAIT_MAP:
        for key in trait["keys"]:
            pattern = r"\b" + re.escape(key) + r"\b"
            if re.search(pattern, text):
                found.add(trait["value"])
                break
    return list(found)


def parse_price(text: str) -> Optional[dict]:
    """Parsea precio máximo: 'hasta 200k USD', 'max 200000', '200 mil'."""
    # hasta 200k USD, max 200000, menos de 200 mil
    pattern = r"(?:hasta|max|menos de|up to)\s*([0-9]+(?:[.,][0-9]+)?\s*[kK]?)\s*(usd|ars|pesos)?"
    match = re.search(pattern, text, re.IGNORECASE)
    if not match:
        return None
    
    num = parse_number(match.group(1))
    if num is None:
        return None
    
    # Detectar "200 mil" en el texto original
    if "mil" in text.lower() and not match.group(1).lower().endswith("k"):
        num = num * 1000
    
    currency = match.group(2)
    if currency:
        currency = currency.upper()
    
    return {"max_price": num, "currency": currency}


def parse_m2(text: str) -> Optional[dict]:
    """Parsea metros cuadrados: 'min 50 m2', 'max 100 m2', 'entre 50 y 100 m2'."""
    # Rango: entre 50 y 100 m2
    range_pattern = r"entre\s*([0-9]+)\s*y\s*([0-9]+)\s*m2?"
    range_match = re.search(range_pattern, text, re.IGNORECASE)
    if range_match:
        return {
            "min_m2": int(range_match.group(1)),
            "max_m2": int(range_match.group(2))
        }
    
    # Min: min 50 m2, minimo 50 m2, desde 50 m2
    min_pattern = r"(?:min|minimo|desde)\s*([0-9]+)\s*m2?"
    min_match = re.search(min_pattern, text, re.IGNORECASE)
    
    # Max: max 100 m2, maximo 100 m2, hasta 100 m2
    max_pattern = r"(?:max|maximo|hasta)\s*([0-9]+)\s*m2?"
    max_match = re.search(max_pattern, text, re.IGNORECASE)
    
    result = {}
    if min_match:
        result["min_m2"] = int(min_match.group(1))
    if max_match:
        result["max_m2"] = int(max_match.group(1))
    
    return result if result else None


def parse_rooms(text: str) -> Optional[dict]:
    """Parsea dormitorios, ambientes, baños."""
    pattern = r"(\d+)\s*(dorm|dormitorios|habitaciones|amb|ambientes|baños?|bath)\b"
    matches = re.findall(pattern, text, re.IGNORECASE)
    
    beds = []
    rooms = []
    baths = []
    
    for num_str, key in matches:
        num = int(num_str)
        key_lower = key.lower()
        if "dorm" in key_lower or "habit" in key_lower:
            beds.append(num)
        elif "amb" in key_lower:
            rooms.append(num)
        elif "baño" in key_lower or "bath" in key_lower:
            baths.append(num)
    
    result = {}
    if beds:
        result["min_beds"] = max(beds)
    if rooms:
        result["min_rooms"] = max(rooms)
    if baths:
        result["min_baths"] = max(baths)
    
    return result if result else None


def parse_place(text: str, known_places: list[dict]) -> Optional[dict]:
    """
    Parsea lugar (ciudad/barrio) buscando en lista conocida.
    known_places: lista de dicts con 'id', 'label', 'type' (city/barrio)
    """
    if not known_places:
        return None
    
    words = normalize(text).split(" ")
    # Buscar desde el final (nombres propios suelen ir al final)
    for i in range(len(words) - 1, -1, -1):
        candidate = " ".join(words[i:])
        for place in known_places:
            place_label = normalize(place.get("label", "") or place.get("id", ""))
            if candidate == place_label or candidate.startswith(place_label + " "):
                return {"place": place["id"], "type": place.get("type", "city")}
    return None


def parse_free_text(query: str, known_places: list[dict] = None) -> Optional[ParsedFilters]:
    """
    Función principal: parsea texto libre a filtros.
    
    Args:
        query: Texto de búsqueda libre
        known_places: Lista opcional de lugares conocidos [{id, label, type}]
    
    Returns:
        ParsedFilters o None si no se pudo parsear nada útil
    """
    text = normalize(query)
    if not text:
        return None
    
    result = ParsedFilters()
    
    # Tipo de propiedad
    result.type = find_type(text)
    
    # Precio
    price = parse_price(text)
    if price:
        result.max_price = price["max_price"]
        result.price_currency = price["currency"]
    
    # Metros cuadrados
    m2 = parse_m2(text)
    if m2:
        result.min_m2 = m2.get("min_m2")
        result.max_m2 = m2.get("max_m2")
    
    # Dormitorios, ambientes, baños
    rooms = parse_rooms(text)
    if rooms:
        result.min_beds = rooms.get("min_beds")
        result.min_rooms = rooms.get("min_rooms")
        result.min_baths = rooms.get("min_baths")
    
    # Traits
    result.traits = find_traits(text)
    
    # Lugar (ciudad/barrio)
    if known_places:
        place = parse_place(text, known_places)
        if place:
            if place["type"] == "city":
                result.city = place["place"]
            elif place["type"] == "barrio":
                result.barrio = place["place"]
    
    # Verificar si se encontró algo útil
    has_filters = any([
        result.type, result.city, result.barrio, result.zona,
        result.min_beds, result.min_rooms, result.min_baths,
        result.min_m2, result.max_m2, result.max_price,
        result.traits
    ])
    
    return result if has_filters else None


# Para compatibilidad con tests y uso directo
def parse_free_text_to_dict(query: str, known_places: list[dict] = None) -> Optional[dict]:
    """Wrapper que devuelve dict directamente."""
    parsed = parse_free_text(query, known_places)
    return parsed.to_dict() if parsed else None


_UI_TYPES = {"casa", "departamento", "ph", "terreno", "local", "oficina", "galpon"}
_SKIP_PLACE = set(TYPE_MAP) | {
    "dormitorio", "dormitorios", "ambiente", "ambientes", "bano", "banos",
    "hasta", "desde", "usd", "ars", "patio", "jardin", "cochera", "balcon",
    "luminoso", "terraza", "credito", "urgente",
}

SEARCH_QUESTIONS: dict[str, dict] = {
    "property_kind": {
        "type": "choice",
        "instructions": "¿Qué tipo de inmueble pide quien busca? Si no nombra un tipo, elegí ninguno.",
        "criteria": {
            "ninguno": "No dice casa, departamento, PH, terreno, local, oficina ni galpón.",
            "casa": "Pide una casa, chalet o vivienda sobre un lote.",
            "departamento": "Pide un departamento, apartamento o monoambiente.",
            "ph": "Pide un PH, dúplex o triplex.",
            "terreno": "Pide un lote o terreno.",
            "local": "Pide un local comercial.",
            "oficina": "Pide una oficina.",
            "galpon": "Pide un galpón o nave.",
        },
    },
    "min_beds": {
        "type": "choice",
        "instructions": "¿Cuántos dormitorios pide como mínimo? Si no lo dice, elegí ninguno.",
        "criteria": {
            "ninguno": "No menciona dormitorios ni habitaciones.",
            "1": "Pide al menos 1 dormitorio.",
            "2": "Pide al menos 2 dormitorios.",
            "3": "Pide al menos 3 dormitorios.",
            "4": "Pide al menos 4 dormitorios.",
            "5": "Pide 5 o más dormitorios.",
        },
    },
    "min_rooms": {
        "type": "choice",
        "instructions": "¿Cuántos ambientes pide como mínimo? Si no lo dice, elegí ninguno.",
        "criteria": {
            "ninguno": "No menciona ambientes.",
            "1": "Pide al menos 1 ambiente.",
            "2": "Pide al menos 2 ambientes.",
            "3": "Pide al menos 3 ambientes.",
            "4": "Pide al menos 4 ambientes.",
            "5": "Pide al menos 5 ambientes.",
            "6": "Pide 6 o más ambientes.",
        },
    },
    "min_baths": {
        "type": "choice",
        "instructions": "¿Cuántos baños pide como mínimo? Si no lo dice, elegí ninguno.",
        "criteria": {
            "ninguno": "No menciona baños.",
            "1": "Pide al menos 1 baño.",
            "2": "Pide al menos 2 baños.",
            "3": "Pide 3 o más baños.",
        },
    },
    "deal_bar": {
        "type": "choice",
        "instructions": "¿Pide una ganga o un precio por debajo del mercado? Si no habla de precio relativo, elegí ninguno.",
        "criteria": {
            "ninguno": "No pide ganga, oportunidad ni precio bajo respecto del barrio.",
            "bueno": "Pide un buen precio o algo debajo del mercado, sin decir ganga.",
            "oportunidad": "Pide ganga, oportunidad, precio bajo o muy barato para la zona.",
        },
    },
}

_TRAIT_PROMPTS = {
    "credit": "Pide que sea apto crédito o hipotecable.",
    "owner": "Pide dueño directo, sin inmobiliaria.",
    "expenses": "Pide expensas bajas.",
    "urgent": "Pide venta urgente o que se concrete rápido.",
    "quiet": "Pide un lugar tranquilo o silencioso.",
    "good": "Pide buen estado, renovado o impecable.",
    "balcony": "Pide balcón.",
    "bright": "Pide que sea luminoso.",
    "growing": "Pide una zona en crecimiento.",
    "view": "Pide vista abierta o panorámica.",
    "patio": "Pide patio o jardín.",
    "garage": "Pide cochera, garage o estacionamiento.",
    "terrace": "Pide terraza.",
}
for _trait, _prompt in _TRAIT_PROMPTS.items():
    SEARCH_QUESTIONS[f"trait_{_trait}"] = {
        "type": "noul",
        "instructions": f"¿Quien busca pide esto? {_prompt} Si no lo menciona, es no.",
    }

_DEAL_SCORES = {"bueno": 48, "oportunidad": 70}


def _choice_value(decision) -> str | None:
    if decision.question_type != "choice" or decision.confidence < 0.5:
        return None
    answer = str(decision.answer or "")
    if not answer or answer == "ninguno":
        return None
    return answer


def merge_search_decisions(base: dict | None, decisions: list) -> dict:
    """Aplica cada decisión de Laya sobre el dict de filtros. Los números de precio y m² quedan del texto."""
    out = dict(base or {})
    traits = set(out.get("traits") or [])
    for decision in decisions:
        key = decision.question_key
        if key == "property_kind":
            kind = _choice_value(decision)
            if kind in _UI_TYPES:
                current = [item for item in _as_list(out.get("typeFilter")) if item != "monoambiente"]
                if kind not in current:
                    current.append(kind)
                out["typeFilter"] = _one_or_many(current) or kind
            continue
        if key in {"min_beds", "min_rooms", "min_baths"}:
            raw = _choice_value(decision)
            if raw and raw.isdigit():
                field = {"min_beds": "minBeds", "min_rooms": "minRooms", "min_baths": "minBaths"}[key]
                out[field] = int(raw)
            continue
        if key == "deal_bar":
            raw = _choice_value(decision)
            if raw in _DEAL_SCORES:
                out["dealBar"] = _DEAL_SCORES[raw]
            continue
        if key.startswith("trait_") and decision.question_type == "noul" and decision.answer is True:
            traits.add(key.removeprefix("trait_"))
    if out.get("typeFilter") == "monoambiente":
        out["typeFilter"] = "departamento"
    if traits:
        out["traits"] = sorted(traits)
    return out


def mentions_monoambiente(query: str) -> bool:
    return re.search(r"\bmono\s*h?\s*ambientes?\b", normalize(query)) is not None


def pick_barrios(query: str, names: list[str]) -> list[str]:
    """Cada nombre del catálogo que la frase dice entero. «X de Y» se queda con Y."""
    from .places import fold

    folded = fold(normalize(query))
    hits: list[str] = []
    seen: set[str] = set()
    for name in names:
        label = str(name or "").strip()
        token = fold(label)
        if not token or token in seen or not _contains_name(folded, token):
            continue
        seen.add(token)
        hits.append(label)
    hits.sort(key=lambda label: len(fold(label)), reverse=True)
    kept: list[str] = []
    for label in hits:
        token = fold(label)
        if any(token != fold(longer) and _contains_name(fold(longer), token) for longer in kept):
            continue
        kept.append(label)
    drop: set[str] = set()
    for left in kept:
        for right in kept:
            if left == right:
                continue
            if re.search(rf"(?:^| ){re.escape(fold(left))} de {re.escape(fold(right))}(?: |$)", folded):
                drop.add(left)
    zone_words = {"norte", "sur", "este", "oeste", "centro"}
    result = []
    for label in kept:
        if label in drop:
            continue
        token = fold(label)
        if token in zone_words and re.search(rf"(?:^| )zona {re.escape(token)}(?: |$)", folded):
            continue
        result.append(label)

    def position(label: str) -> int:
        match = re.search(rf"(?:^| ){re.escape(fold(label))}(?: |$)", folded)
        return match.start() if match else 10**9

    result.sort(key=position)
    return result


def pick_barrio(query: str, names: list[str]) -> str:
    """El barrio de la ciudad actual que la frase nombra. «X de Y» elige Y si los dos existen."""
    hits = pick_barrios(query, names)
    return hits[0] if hits else ""


def barrio_in_city(query: str, city_id: str) -> list[str]:
    if not (city_id or "").strip():
        return []
    from .geo import barrios_for

    names = [_barrio_name(item) for item in barrios_for(city_id) or []]
    return pick_barrios(query, names)


def zonas_in_city(query: str, city_id: str) -> list[str]:
    if not (city_id or "").strip():
        return []
    from .geo import barrios_for

    names: list[str] = []
    seen: set[str] = set()
    for item in barrios_for(city_id) or []:
        zona = str(item.get("zona") or "").strip() if isinstance(item, dict) else ""
        if not zona or zona in seen or zona.lower() == "sin clasificar":
            continue
        seen.add(zona)
        names.append(zona)
    return pick_barrios(query, names)


def _contains_name(folded: str, name: str) -> bool:
    if len(name) < 4 or name in _SKIP_PLACE:
        return False
    return re.search(rf"(?:^| ){re.escape(name)}(?: |$)", folded) is not None


def _barrio_name(item) -> str:
    if isinstance(item, dict):
        return str(item.get("name") or item.get("label") or "")
    return str(item or "")


def mentioned_place(query: str) -> dict | None:
    """Lugar que ya está en el catálogo cargado por la API. Sin listas fijas."""
    from .places import CITIES, fold, is_cache_artifact_id

    folded = fold(normalize(query))
    best = None
    best_len = 0
    best_label = ""
    for city_id, cfg in CITIES.items():
        if is_cache_artifact_id(city_id):
            continue
        names = [cfg.get("label") or "", city_id, *(cfg.get("aliases") or [])]
        for raw in names:
            label = fold(str(raw))
            if _contains_name(folded, label) and len(label) > best_len:
                best = cfg
                best_len = len(label)
                best_label = str(cfg.get("label") or city_id)
    if not best:
        return None
    barrio = ""
    for item in best.get("barrios") or []:
        name = _barrio_name(item)
        folded_name = fold(name)
        if _contains_name(folded, folded_name) and len(folded_name) > len(fold(barrio)):
            barrio = name
    return {
        "id": best["id"],
        "label": best_label,
        "lat": best.get("lat"),
        "lon": best.get("lon"),
        "province": best.get("province") or "",
        "zoom": best.get("zoom") or 13,
        "barrio": barrio,
    }


_PLACE_STOP = _SKIP_PLACE | {
    "en", "con", "de", "del", "la", "el", "los", "las", "y", "por", "para",
    "hasta", "desde", "usd", "ars", "m2", "metros", "metro",
    "dormitorio", "dormitorios", "habitacion", "habitaciones",
    "ambiente", "ambientes", "bano", "banos",
}


def _place_phrase(query: str) -> str:
    """Lo que queda del texto cuando se sacan números y palabras de filtro."""
    kept: list[str] = []
    for word in normalize(query).split():
        if word in _PLACE_STOP or re.fullmatch(r"\d+[kmb]?", word):
            if kept:
                break
            continue
        kept.append(word)
        if len(kept) == 4:
            break
    return " ".join(kept)


def _city_for_point(lat: float, lon: float) -> dict | None:
    """Ciudad ya cargada que contiene el punto. Gana el recuadro más chico."""
    from .places import CITIES, is_cache_artifact_id

    best = None
    best_area = 1e18
    for city_id, cfg in CITIES.items():
        if is_cache_artifact_id(city_id):
            continue
        box = cfg.get("bbox")
        area = None
        if box and len(box) == 4:
            south, west, north, east = (float(v) for v in box)
            if south <= lat <= north and west <= lon <= east:
                area = abs(north - south) * abs(east - west)
        else:
            clat, clon = cfg.get("lat"), cfg.get("lon")
            if clat is None or clon is None:
                continue
            radius = float(cfg.get("radius_km") or 25)
            dlat = (lat - float(clat)) * 111
            dlon = (lon - float(clon)) * 85
            if (dlat * dlat + dlon * dlon) ** 0.5 <= radius:
                area = radius * radius
        if area is not None and area < best_area:
            best = cfg
            best_area = area
    return best


def resolve_named_place(phrase: str) -> dict | None:
    """Un nombre de lugar, tal como lo escribió la persona en Dónde."""
    text = (phrase or "").strip()
    if len(text) < 3:
        return None
    hit = mentioned_place(text)
    if hit:
        return hit
    from .place_api import lookup_place
    from .places import fold

    try:
        found = lookup_place(text)
    except Exception:
        return None
    if not found or found.get("lat") is None or found.get("lon") is None:
        return None
    name = str(found.get("name") or text)
    parent = _city_for_point(float(found["lat"]), float(found["lon"]))
    if parent:
        parent_names = {fold(str(parent.get("label") or "")), fold(str(parent.get("id") or ""))}
        barrio = "" if fold(name) in parent_names else name
        return {
            "id": parent["id"],
            "label": parent.get("label") or parent["id"],
            "lat": parent.get("lat"),
            "lon": parent.get("lon"),
            "province": parent.get("province") or "",
            "zoom": parent.get("zoom") or 13,
            "barrio": barrio,
        }
    return {
        "id": "",
        "label": name,
        "lat": found.get("lat"),
        "lon": found.get("lon"),
        "province": found.get("province") or "",
        "zoom": 13,
        "barrio": name,
    }


def resolve_place(query: str) -> dict | None:
    """Catálogo si el nombre ya está cargado; si no, Georef/Nominatim y la ciudad que contiene el punto."""
    hit = mentioned_place(query)
    if hit:
        return hit
    return resolve_named_place(_place_phrase(query))


def _apply_monoambiente(query: str, base: dict) -> dict:
    types = [item for item in _as_list(base.get("typeFilter")) if item != "monoambiente"]
    mono = mentions_monoambiente(query) or base.get("typeFilter") == "monoambiente"
    if mono and "departamento" not in types:
        types.insert(0, "departamento")
    if types:
        base["typeFilter"] = _one_or_many(types)
    if mono and types == ["departamento"]:
        base["minRooms"] = 1
        base["maxRooms"] = 1
    return base


def filters_for_query(query: str, where: str = "", city: str = "") -> tuple[dict, dict | None, str]:
    """Filtros de la búsqueda libre. Laya setea cada filtro discreto; el texto aporta precio, m² y el lugar."""
    base = parse_free_text_to_dict(query) or {}
    types = find_types(normalize(query))
    if types:
        base["typeFilter"] = _one_or_many(types)
    base = _apply_monoambiente(query, base)
    place = resolve_named_place(where) if (where or "").strip() else None
    local_barrios = [] if place else _as_list(barrio_in_city(query, city))
    if local_barrios:
        base["cityFilter"] = city
        base["barrioFilter"] = _one_or_many(local_barrios)
        place = None
    elif not place:
        place = resolve_place(query)
    if place and place.get("id"):
        base["cityFilter"] = place["id"]
    if place and place.get("barrio"):
        base["barrioFilter"] = place["barrio"]
    zona_city = (place or {}).get("id") or city
    if re.search(r"\bzona\b", normalize(query)):
        zonas = zonas_in_city(query, zona_city)
        if zonas:
            base["zonaFilter"] = _one_or_many(zonas)
    source = "rules"
    try:
        from .laya_client import decide_questions

        decisions = decide_questions(query, SEARCH_QUESTIONS)
    except Exception:
        decisions = []
    if decisions:
        base = _apply_monoambiente(query, merge_search_decisions(base, decisions))
        source = "laya"
    else:
        base = _apply_monoambiente(query, merge_search_decisions(base, []))
    public_place = None
    if place and place.get("id"):
        public_place = {key: place.get(key) for key in ("id", "label", "lat", "lon", "province", "zoom")}
    return base, public_place, source


if __name__ == "__main__":
    # Tests rápidos
    test_queries = [
        "casa 3 dormitorios con patio en Palermo hasta 200k USD",
        "departamento 2 ambientes luminoso con balcon",
        "terreno en Cordoba desde 500 m2",
        "ph 3 dormitorios apto credito",
        "local comercial urgente",
        "casa con jardin y cochera en zona norte",
    ]
    
    # Lugares de prueba
    test_places = [
        {"id": "palermo", "label": "Palermo", "type": "barrio"},
        {"id": "caba", "label": "CABA", "type": "city"},
        {"id": "cordoba", "label": "Córdoba", "type": "city"},
        {"id": "belgrano", "label": "Belgrano", "type": "barrio"},
    ]
    
    for query in test_queries:
        result = parse_free_text(query, test_places)
        print(f"Query: {query}")
        print(f"Result: {result.to_dict() if result else None}")
        print()