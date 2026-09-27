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


def find_type(text: str) -> Optional[str]:
    """Encuentra tipo de propiedad en el texto."""
    for key, value in TYPE_MAP.items():
        pattern = r"\b" + re.escape(key) + r"\b"
        if re.search(pattern, text):
            return value
    return None


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