from __future__ import annotations

import hashlib
import math
import re
import unicodedata

CITY_LAT = -42.7692
CITY_LON = -65.0385
GEO_VERSION = "5"

TRELEW_LAT = -43.2489
TRELEW_LON = -65.3051
RAWSON_LAT = -43.3002
RAWSON_LON = -65.1023
GAIMAN_LAT = -43.2897
GAIMAN_LON = -65.4927
CABA_LAT = -34.6037
CABA_LON = -58.3816

# Centroides aproximados de barrios y zonas de Puerto Madryn.
# No requieren API: se usan para ubicar avisos sin coordenadas propias.
BARRIOS: list[dict] = [
    {"name": "Centro", "zona": "Centro y costanera", "lat": -42.7686, "lon": -65.0364, "aliases": ["centro", "microcentro", "peatonal", "25 de mayo"]},
    {"name": "Comercio", "zona": "Oeste residencial", "lat": -42.7599, "lon": -65.0548, "aliases": ["barrio comercio", "sindicato de empleados del comercio"]},
    {"name": "Bahía Nueva", "zona": "Centro y costanera", "lat": -42.7796, "lon": -65.0407, "aliases": ["bahia nueva", "bahía nueva"]},
    {"name": "Parry Madryn", "zona": "Centro y costanera", "lat": -42.7684, "lon": -65.0345, "aliases": ["parry", "parry madryn"]},
    {"name": "Luis Piedrabuena", "zona": "Zona Sur", "lat": -42.7848, "lon": -65.0269, "aliases": ["piedrabuena", "barrio piedrabuena"]},
    {"name": "Zona Sur", "zona": "Zona Sur", "lat": -42.7865, "lon": -65.0285, "aliases": ["zona sur", "barrio sur"]},
    {"name": "Punta Cuevas", "zona": "Zona Sur", "lat": -42.7825, "lon": -65.0088, "aliases": ["punta cuevas", "indio tehuelche", "desembarco"]},
    {"name": "Santa María del Mar", "zona": "Centro y costanera", "lat": -42.7758, "lon": -65.0412, "aliases": ["santa maria del mar", "santa maría del mar"]},
    {"name": "Pioneros del Sur", "zona": "Zona Sur", "lat": -42.7842, "lon": -65.0288, "aliases": ["pioneros del sur"]},
    {"name": "Solana de la Patagonia", "zona": "Zona Sur", "lat": -42.8171, "lon": -65.0339, "aliases": ["solana", "solana de la patagonia", "arcos de solana", "estilo solana"]},
    {"name": "Zona Norte", "zona": "Zona Norte", "lat": -42.7540, "lon": -65.0420, "aliases": ["zona norte"]},
    {"name": "América", "zona": "Zona Norte", "lat": -42.7531, "lon": -65.0565, "aliases": ["america", "américa", "barrio america"]},
    {"name": "21 de Enero", "zona": "Zona Norte", "lat": -42.7546, "lon": -65.0576, "aliases": ["21 de enero", "veintiuno de enero"]},
    {"name": "287 Viviendas", "zona": "Zona Norte", "lat": -42.7548, "lon": -65.0592, "aliases": ["287 viviendas", "barrio 287", "b 287 viviendas", "bo 287 viviendas", "287 viv", "bº 287"]},
    {"name": "Inmigrantes", "zona": "Oeste residencial", "lat": -42.7697, "lon": -65.0463, "aliases": ["inmigrantes"]},
    {"name": "Aluar", "zona": "Zona Norte", "lat": -42.7390, "lon": -65.0534, "aliases": ["aluar", "casas de los jefes", "barrio aluar"]},
    {"name": "Industrial Liviano", "zona": "Oeste residencial", "lat": -42.7663, "lon": -65.0615, "aliases": ["industrial liviano", "parque industrial liviano", "parque industrial", "mega madryn"]},
    {"name": "Güemes", "zona": "Oeste residencial", "lat": -42.7709, "lon": -65.0531, "aliases": ["guemes", "güemes"]},
    {"name": "Don Bosco", "zona": "Oeste residencial", "lat": -42.7604, "lon": -65.0417, "aliases": ["don bosco"]},
    {"name": "Fontana", "zona": "Oeste residencial", "lat": -42.7802, "lon": -65.0549, "aliases": ["fontana", "gobernador fontana"]},
    {"name": "Pujol", "zona": "Oeste residencial", "lat": -42.7595, "lon": -65.0608, "aliases": ["pujol", "agustin pujol", "agustín pujol"]},
    {"name": "Perón", "zona": "Oeste residencial", "lat": -42.7895, "lon": -65.0732, "aliases": ["peron", "perón", "presidente peron"]},
    {"name": "Roca", "zona": "Zona Norte", "lat": -42.7538, "lon": -65.0496, "aliases": ["barrio roca", "julio argentino roca"]},
    {"name": "San Miguel", "zona": "Oeste residencial", "lat": -42.7851, "lon": -65.0677, "aliases": ["san miguel"]},
    {"name": "Villa del Parque", "zona": "Zona Sur", "lat": -42.7786, "lon": -65.0325, "aliases": ["villa del parque"]},
    {"name": "Unión Obrera", "zona": "Oeste residencial", "lat": -42.7601, "lon": -65.0464, "aliases": ["union obrera", "unión obrera", "uocra"]},
    {"name": "Las Bardas", "zona": "Oeste residencial", "lat": -42.7831, "lon": -65.0457, "aliases": ["las bardas", "bardas"]},
    {"name": "Perito Moreno", "zona": "Oeste residencial", "lat": -42.7614, "lon": -65.0469, "aliases": ["perito moreno"]},
    {"name": "Patagonia", "zona": "Zona Norte", "lat": -42.7536, "lon": -65.0412, "aliases": ["barrio patagonia"]},
    {"name": "Nueva Chubut", "zona": "Oeste residencial", "lat": -42.7587, "lon": -65.0696, "aliases": ["nueva chubut"]},
    {"name": "Gobernador Gallina", "zona": "Zona Sur", "lat": -42.7871, "lon": -65.0366, "aliases": ["gallina", "galina", "gobernador gallina", "gobernador galina"]},
    {"name": "Roque González", "zona": "Oeste residencial", "lat": -42.7848, "lon": -65.0614, "aliases": ["roque gonzalez", "roque gonzález", "gobernador roque gonzalez"]},
    {"name": "Francisco Falcón", "zona": "Zona Norte", "lat": -42.7515, "lon": -65.0478, "aliases": ["falcon", "falcón", "francisco falcon"]},
    {"name": "Provincias Unidas", "zona": "Zona Sur", "lat": -42.7823, "lon": -65.0385, "aliases": ["provincias unidas"]},
    {"name": "El Porvenir", "zona": "Zona Norte", "lat": -42.7573, "lon": -65.0477, "aliases": ["el porvenir", "porvenir"]},
    {"name": "Colonos Galeses", "zona": "Oeste residencial", "lat": -42.7658, "lon": -65.0431, "aliases": ["colonos galeses", "galeses"]},
    {"name": "Conquistadores del Desierto", "zona": "Oeste residencial", "lat": -42.7775, "lon": -65.0610, "aliases": ["conquistadores", "conquista del desierto"]},
    {"name": "Del Villar", "zona": "Oeste residencial", "lat": -42.7709, "lon": -65.0506, "aliases": ["del villar", "manuel del villar"]},
    {"name": "Troperos Patagónicos", "zona": "Zona Norte", "lat": -42.7534, "lon": -65.0536, "aliases": ["troperos", "troperos patagonicos"]},
    {"name": "Villa Padilla", "zona": "Oeste residencial", "lat": -42.7744, "lon": -65.0469, "aliases": ["villa padilla", "padilla"]},
    {"name": "Ruca Hué", "zona": "Oeste residencial", "lat": -42.7647, "lon": -65.0491, "aliases": ["ruca hue", "ruca hué", "rucalhue"]},
    {"name": "Los Médanos", "zona": "Zona Sur", "lat": -42.7830, "lon": -65.0140, "aliases": ["los medanos", "los médanos", "medanos"]},
    {"name": "Quintas del Mirador", "zona": "Quintas y periurbano", "lat": -42.8093, "lon": -65.0430, "aliases": ["quintas del mirador", "el chaja", "el chajá", "quinta el mirador"]},
    {"name": "Mapu Ngefu", "zona": "Quintas y periurbano", "lat": -42.7353, "lon": -65.0931, "aliases": ["mapu ngefu", "mapu ngefü", "mapu ngenfu", "mapu"]},
    {"name": "Agropecuario", "zona": "Quintas y periurbano", "lat": -42.7885, "lon": -65.0780, "aliases": ["agropecuario", "chacra"]},
    {"name": "Miradores de la Costa", "zona": "Quintas y periurbano", "lat": -42.8045, "lon": -65.0468, "aliases": ["miradores de la costa", "miradores"]},
    {"name": "Altos de la Colina", "zona": "Quintas y periurbano", "lat": -42.7988, "lon": -65.0542, "aliases": ["altos de la colina"]},
    {"name": "Barrancas del Golfo", "zona": "Zona Norte", "lat": -42.7564, "lon": -65.0377, "aliases": ["barrancas del golfo", "barracas del golfo", "barrancas"]},
    {"name": "El Doradillo", "zona": "Playas norte", "lat": -42.6440, "lon": -65.0641, "aliases": ["doradillo", "el doradillo", "playa doradillo", "parque ecologico", "parque ecológico", "las canteras doradillo"]},
    {"name": "Playa Paraná", "zona": "Playas sur", "lat": -42.7957, "lon": -64.9439, "aliases": ["playa parana", "playa paraná", "parana", "paraná"]},
    {"name": "Cerro Avanzado", "zona": "Playas sur", "lat": -42.8264, "lon": -64.8847, "aliases": ["cerro avanzado", "avanzado", "punta ninfas"]},
    {"name": "El Pozo", "zona": "Playas sur", "lat": -42.7765, "lon": -65.0120, "aliases": ["el pozo", "playa el pozo", "playa pozo"]},
    {"name": "Anon Car", "zona": "Oeste residencial", "lat": -42.7690, "lon": -65.0620, "aliases": ["anon car", "annon car"]},
    {"name": "Vepam", "zona": "Oeste residencial", "lat": -42.7568, "lon": -65.0565, "aliases": ["vepam"]},
]

# Calles con un punto de ancla. Si el aviso trae altura, se desplaza un poco.
STREETS: list[tuple[str, float, float, str]] = [
    ("roca", -42.7690, -65.0342, "Centro"),
    ("avenida roca", -42.7690, -65.0342, "Centro"),
    ("julio a. roca", -42.7690, -65.0342, "Centro"),
    ("julio a roca", -42.7690, -65.0342, "Centro"),
    ("25 de mayo", -42.7688, -65.0364, "Centro"),
    ("28 de julio", -42.7702, -65.0378, "Centro"),
    ("9 de julio", -42.7718, -65.0395, "Centro"),
    ("belgrano", -42.7705, -65.0368, "Centro"),
    ("mitre", -42.7696, -65.0388, "Centro"),
    ("albarracin", -42.7684, -65.0396, "Centro"),
    ("albarracín", -42.7684, -65.0396, "Centro"),
    ("lewis jones", -42.7672, -65.0372, "Centro"),
    ("gales", -42.7664, -65.0360, "Centro"),
    ("españa", -42.7726, -65.0384, "Centro"),
    ("12 de octubre", -42.7734, -65.0402, "Zona Sur"),
    ("misiones", -42.7768, -65.0295, "Zona Sur"),
    ("nueva leon", -42.7488, -65.0375, "Aluar"),
    ("nueva león", -42.7488, -65.0375, "Aluar"),
    ("colon", -42.7670, -65.0410, "Centro"),
    ("colón", -42.7670, -65.0410, "Centro"),
    ("ameghino", -42.7662, -65.0428, "Güemes"),
    ("la rioja", -42.7648, -65.0455, "Oeste residencial"),
    ("fournier", -42.7635, -65.0408, "Zona Norte"),
    ("villarino", -42.7628, -65.0395, "Zona Norte"),
    ("gabriel luna", -42.7940, -65.0580, "Mapu Ngefu"),
    ("gabriel san luna", -42.7940, -65.0580, "Mapu Ngefu"),
    ("alsua de corbetto", -42.7855, -65.0240, "Zona Sur"),
    ("corbetto", -42.7855, -65.0240, "Zona Sur"),
    ("piquillin", -42.7920, -65.0680, "Quintas del Mirador"),
    ("piquillín", -42.7920, -65.0680, "Quintas del Mirador"),
    ("escribano menendez", -42.7564, -65.0377, "Barrancas del Golfo"),
    ("escribano menéndez", -42.7564, -65.0377, "Barrancas del Golfo"),
    ("kenneth woodley", -42.7568, -65.0374, "Barrancas del Golfo"),
    ("domecq garcia", -42.7572, -65.0372, "Barrancas del Golfo"),
    ("domecq garcía", -42.7572, -65.0372, "Barrancas del Golfo"),
    ("pedro derbes", -42.7558, -65.0375, "Barrancas del Golfo"),
    ("yamanas", -42.7555, -65.0378, "Barrancas del Golfo"),
    ("yámanas", -42.7555, -65.0378, "Barrancas del Golfo"),
    ("angelo mistrangelo", -42.7548, -65.0592, "287 Viviendas"),
    ("mistrangelo", -42.7548, -65.0592, "287 Viviendas"),
]

TRELEW_BARRIOS: list[dict] = [
    {"name": "Centro", "zona": "Centro", "lat": -43.2530, "lon": -65.3095, "aliases": ["centro", "microcentro", "25 de mayo trelew"]},
    {"name": "Norte", "zona": "Norte", "lat": -43.2380, "lon": -65.3080, "aliases": ["zona norte", "barrio norte"]},
    {"name": "Sur", "zona": "Sur", "lat": -43.2660, "lon": -65.3050, "aliases": ["zona sur", "barrio sur"]},
    {"name": "Este", "zona": "Este", "lat": -43.2490, "lon": -65.2920, "aliases": ["zona este"]},
    {"name": "Oeste", "zona": "Oeste", "lat": -43.2500, "lon": -65.3200, "aliases": ["zona oeste"]},
    {"name": "Don Bosco", "zona": "Norte", "lat": -43.2415, "lon": -65.3010, "aliases": ["don bosco"]},
    {"name": "Los Olmos", "zona": "Oeste", "lat": -43.2555, "lon": -65.3250, "aliases": ["los olmos"]},
    {"name": "Santa Mónica", "zona": "Sur", "lat": -43.2700, "lon": -65.3120, "aliases": ["santa monica", "santa mónica"]},
    {"name": "INTA", "zona": "Norte", "lat": -43.2305, "lon": -65.3180, "aliases": ["inta"]},
]

RAWSON_BARRIOS: list[dict] = [
    {"name": "Centro", "zona": "Centro", "lat": -43.3002, "lon": -65.1023, "aliases": ["centro", "rawson centro"]},
    {"name": "Playa Unión", "zona": "Costa", "lat": -43.3380, "lon": -65.0470, "aliases": ["playa union", "playa unión"]},
    {"name": "Puerto Rawson", "zona": "Costa", "lat": -43.3445, "lon": -65.0550, "aliases": ["puerto rawson"]},
    {"name": "Norte", "zona": "Norte", "lat": -43.2880, "lon": -65.1050, "aliases": ["zona norte"]},
    {"name": "Sur", "zona": "Sur", "lat": -43.3120, "lon": -65.1000, "aliases": ["zona sur"]},
]

PLAYA_UNION_LAT = -43.3380
PLAYA_UNION_LON = -65.0470
PLAYA_UNION_BARRIOS: list[dict] = [
    {"name": "Centro", "zona": "Centro", "lat": -43.3380, "lon": -65.0470, "aliases": ["centro", "playa union centro"]},
    {"name": "Costanera", "zona": "Costa", "lat": -43.3398, "lon": -65.0432, "aliases": ["costanera", "la playa", "balneario"]},
    {"name": "Puerto Rawson", "zona": "Puerto", "lat": -43.3445, "lon": -65.0550, "aliases": ["puerto rawson", "puerto"]},
    {"name": "Norte", "zona": "Norte", "lat": -43.3285, "lon": -65.0485, "aliases": ["zona norte"]},
    {"name": "Sur", "zona": "Sur", "lat": -43.3485, "lon": -65.0465, "aliases": ["zona sur"]},
    {"name": "Oeste", "zona": "Oeste", "lat": -43.3380, "lon": -65.0585, "aliases": ["zona oeste"]},
]

GAIMAN_BARRIOS: list[dict] = [
    {"name": "Centro", "zona": "Centro", "lat": -43.2897, "lon": -65.4927, "aliases": ["centro", "gaiman centro"]},
    {"name": "Norte", "zona": "Norte", "lat": -43.2800, "lon": -65.4900, "aliases": ["zona norte"]},
    {"name": "Sur", "zona": "Sur", "lat": -43.2980, "lon": -65.4950, "aliases": ["zona sur"]},
    {"name": "Bryn Gwyn", "zona": "Periurbano", "lat": -43.2720, "lon": -65.4780, "aliases": ["bryn gwyn"]},
]

CABA_BARRIOS: list[dict] = [
    {"name": "Microcentro", "zona": "Centro", "lat": -34.6037, "lon": -58.3806, "aliases": ["microcentro", "city", "downtown"]},
    {"name": "San Nicolás", "zona": "Centro", "lat": -34.6048, "lon": -58.3802, "aliases": ["san nicolas", "san nicolás", "tribunales", "obelisco"]},
    {"name": "Monserrat", "zona": "Centro", "lat": -34.6115, "lon": -58.3815, "aliases": ["monserrat", "montserrat", "9 de julio"]},
    {"name": "Retiro", "zona": "Norte centro", "lat": -34.5922, "lon": -58.3758, "aliases": ["retiro", "catalinas", "plaza san martin"]},
    {"name": "San Telmo", "zona": "Sur centro", "lat": -34.6212, "lon": -58.3734, "aliases": ["san telmo", "defensa"]},
    {"name": "Congreso", "zona": "Centro", "lat": -34.6098, "lon": -58.3925, "aliases": ["congreso", "balvanera", "once"]},
    {"name": "Puerto Madero", "zona": "Este", "lat": -34.6118, "lon": -58.3634, "aliases": ["puerto madero", "madero"]},
    {"name": "Recoleta", "zona": "Norte centro", "lat": -34.5889, "lon": -58.3972, "aliases": ["recoleta", "barrio norte"]},
]

CITIES: dict[str, dict] = {
    "puerto-madryn": {
        "id": "puerto-madryn",
        "label": "Puerto Madryn",
        "lat": CITY_LAT,
        "lon": CITY_LON,
        "zoom": 12,
        "province": "chubut",
        "slug": "puerto-madryn",
        "builtin": True,
        "aliases": ["madryn", "maryn", "puerto madryn"],
        "barrios": BARRIOS,
        "radius_km": 38,
    },
    "trelew": {
        "id": "trelew",
        "label": "Trelew",
        "lat": TRELEW_LAT,
        "lon": TRELEW_LON,
        "zoom": 13,
        "province": "chubut",
        "slug": "trelew",
        "builtin": True,
        "aliases": ["trelew"],
        "barrios": TRELEW_BARRIOS,
        "radius_km": 18,
    },
    "rawson": {
        "id": "rawson",
        "label": "Rawson",
        "lat": RAWSON_LAT,
        "lon": RAWSON_LON,
        "zoom": 13,
        "province": "chubut",
        "slug": "rawson",
        "builtin": True,
        "aliases": ["rawson"],
        "barrios": RAWSON_BARRIOS,
        "radius_km": 12,
    },
    "gaiman": {
        "id": "gaiman",
        "label": "Gaiman",
        "lat": GAIMAN_LAT,
        "lon": GAIMAN_LON,
        "zoom": 14,
        "province": "chubut",
        "slug": "gaiman",
        "builtin": True,
        "aliases": ["gaiman"],
        "barrios": GAIMAN_BARRIOS,
        "radius_km": 12,
    },
    "playa-union": {
        "id": "playa-union",
        "label": "Playa Unión",
        "lat": PLAYA_UNION_LAT,
        "lon": PLAYA_UNION_LON,
        "zoom": 14,
        "province": "chubut",
        "slug": "playa-union",
        "builtin": True,
        "aliases": ["playa union", "paya union", "paya-union", "playaunion"],
        "barrios": PLAYA_UNION_BARRIOS,
        "radius_km": 10,
    },
    "microcentro-caba": {
        "id": "microcentro-caba",
        "label": "Microcentro CABA",
        "lat": CABA_LAT,
        "lon": CABA_LON,
        "zoom": 15,
        "province": "capital-federal",
        "slug": "microcentro",
        "builtin": True,
        "aliases": ["microcentro", "caba", "microcentro caba"],
        "barrios": CABA_BARRIOS,
        "radius_km": 8,
    },
}


def generic_barrios(lat: float, lon: float) -> list[dict]:
    step = 0.012
    return [
        {"name": "Centro", "zona": "Centro", "lat": lat, "lon": lon, "aliases": ["centro"]},
        {"name": "Norte", "zona": "Norte", "lat": lat - step, "lon": lon, "aliases": ["zona norte", "norte"]},
        {"name": "Sur", "zona": "Sur", "lat": lat + step, "lon": lon, "aliases": ["zona sur", "sur"]},
        {"name": "Este", "zona": "Este", "lat": lat, "lon": lon + step, "aliases": ["zona este", "este"]},
        {"name": "Oeste", "zona": "Oeste", "lat": lat, "lon": lon - step, "aliases": ["zona oeste", "oeste"]},
    ]


def barrios_for(city: str | None) -> list[dict]:
    cfg = CITIES.get(city or "")
    if cfg and cfg.get("barrios"):
        return cfg["barrios"]
    if cfg:
        return generic_barrios(cfg["lat"], cfg["lon"])
    return generic_barrios(CITY_LAT, CITY_LON)


def city_center(city: str | None) -> tuple[float, float]:
    cfg = CITIES.get(city or "")
    if cfg:
        return cfg["lat"], cfg["lon"]
    return CITY_LAT, CITY_LON


def city_radius_km(city: str | None) -> float:
    cfg = CITIES.get(city or "") or {}
    return float(cfg.get("radius_km") or 25)


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    rlat1, rlon1, rlat2, rlon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    dlat, dlon = rlat2 - rlat1, rlon2 - rlon1
    h = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371 * math.asin(min(1.0, math.sqrt(h)))


def in_city_radius(lat: float | None, lon: float | None, city: str | None) -> bool:
    if lat is None or lon is None:
        return False
    clat, clon = city_center(city)
    return distance_km(lat, lon, clat, clon) <= city_radius_km(city)


def city_for_point(lat: float, lon: float) -> str | None:
    best_id = None
    best_d = 1e9
    for city_id, cfg in CITIES.items():
        dist = distance_km(lat, lon, cfg["lat"], cfg["lon"])
        if dist <= float(cfg.get("radius_km") or 25) and dist < best_d:
            best_id, best_d = city_id, dist
    return best_id


def pin_listing_city(item) -> bool:
    tagged = item.city or "puerto-madryn"
    changed = False
    if item.lat is not None and item.lon is not None:
        guessed = city_for_point(item.lat, item.lon)
        if guessed and guessed != tagged:
            item.city = guessed
            tagged = guessed
            changed = True
        elif not guessed and not in_city_radius(item.lat, item.lon, tagged):
            item.city = "fuera"
            tagged = "fuera"
            changed = True
    if tagged != "fuera" and foreign_locality(item, tagged):
        item.city = city_from_text(item) or "fuera"
        return True
    return changed


FOREIGN_PHRASES = (
    "carmen de areco",
    "chacras la alameda",
    "puertos / escobar",
    "puertos de escobar",
    "escobar",
    "garin",
    "pilar chico",
    "nordelta",
    "tigre",
    "capital federal",
    "ciudad autonoma de buenos aires",
    "buenos aires",
    "microcentro caba",
    "en trelew",
    "trelew,",
    "en rawson,",
    "comodoro rivadavia",
    "ruta 6",
    "mar del plata",
    "neuquen capital",
    "bariloche",
    "anelo",
    "playa union",
    "puerto piramides",
    "general roca",
    "viedma",
)


def _listing_blob(item) -> str:
    return fold(f"{getattr(item, 'title', '')} {getattr(item, 'address', '')} {getattr(item, 'description', '') or ''}")


def listing_mentions_city(item, city: str) -> bool:
    blob = _listing_blob(item)
    cfg = CITIES.get(city) or {}
    names = [fold(cfg.get("label") or ""), fold(city), *[fold(a) for a in cfg.get("aliases") or []]]
    return any(name and len(name) >= 5 and name in blob for name in names)


def city_from_text(item) -> str | None:
    blob = _listing_blob(item)
    hit = None
    for city_id, cfg in CITIES.items():
        label = fold(cfg.get("label") or "")
        if len(label) >= 5 and (f"en {label}" in blob or f"{label}," in blob):
            hit = city_id
    return hit


def foreign_locality(item, city: str) -> bool:
    if listing_mentions_city(item, city):
        return False
    blob = _listing_blob(item)
    if "comodoro martin" in blob:
        blob = blob.replace("comodoro martin rivadavia", " ").replace("comodoro martin", " ")
    return any(fold(phrase) in blob for phrase in FOREIGN_PHRASES)


def listing_fits_city(item, city: str) -> bool:
    city = city or "puerto-madryn"
    if (item.city or "puerto-madryn") != city:
        return False
    if foreign_locality(item, city):
        return False
    if item.lat is not None and item.lon is not None and not in_city_radius(item.lat, item.lon, city):
        return False
    return True


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text.lower()).strip()


CITY_ALIASES = {
    "puerto-madryn": "puerto-madryn",
    "puerto madryn": "puerto-madryn",
    "madryn": "puerto-madryn",
    "maryn": "puerto-madryn",
    "trelew": "trelew",
    "rawson": "rawson",
    "gaiman": "gaiman",
    "playa-union": "playa-union",
    "playa union": "playa-union",
    "paya union": "playa-union",
    "paya-union": "playa-union",
    "microcentro-caba": "microcentro-caba",
    "microcentro": "microcentro-caba",
    "caba": "microcentro-caba",
}


def slug_place(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", fold(value)).strip("-")
    return slug or "lugar"


def register_city(
    city_id: str,
    *,
    label: str,
    lat: float,
    lon: float,
    province: str = "chubut",
    barrios: list[dict] | None = None,
    zoom: int = 13,
    aliases: list[str] | None = None,
    builtin: bool = False,
    slug: str | None = None,
) -> dict:
    names = [fold(city_id), fold(label), *[fold(a) for a in (aliases or []) if a]]
    CITIES[city_id] = {
        "id": city_id,
        "label": label,
        "lat": float(lat),
        "lon": float(lon),
        "zoom": int(zoom or 13),
        "province": province or "chubut",
        "slug": slug or city_id,
        "builtin": builtin,
        "aliases": [n for n in dict.fromkeys(names) if n],
        "barrios": barrios or generic_barrios(float(lat), float(lon)),
        "radius_km": 25,
    }
    CITY_ALIASES[city_id] = city_id
    CITY_ALIASES[fold(label)] = city_id
    for name in CITIES[city_id]["aliases"]:
        CITY_ALIASES.setdefault(name, city_id)
    return CITIES[city_id]


def resolve_city(value: str | None) -> str:
    raw = fold(value or "")
    if not raw:
        return "puerto-madryn"
    slug = slug_place(raw)
    if slug in CITIES:
        return slug
    if raw in CITIES:
        return raw
    if raw in CITY_ALIASES:
        return CITY_ALIASES[raw]
    if slug in CITY_ALIASES:
        return CITY_ALIASES[slug]
    best = ""
    best_len = 0
    for cfg in CITIES.values():
        names = [fold(cfg["id"]), fold(cfg["label"]), *[fold(a) for a in cfg.get("aliases") or []]]
        for name in names:
            if name and (name == raw or name == slug):
                return cfg["id"]
            if name and len(name) >= 5 and name in raw and len(name) > best_len:
                best, best_len = cfg["id"], len(name)
    if best:
        return best
    for alias, city_id in CITY_ALIASES.items():
        if len(alias) >= 5 and alias in raw:
            return city_id
    return slug


def _haystack(*parts: str) -> str:
    return fold(" | ".join(p for p in parts if p))


def infer_barrio(*parts: str, city: str = "puerto-madryn") -> tuple[str, str, float, float]:
    text = _haystack(*parts)
    city_barrios = barrios_for(city)
    clat, clon = city_center(city)
    label = (CITIES.get(city) or {}).get("label") or (city or "").replace("-", " ").title()
    best = None
    best_len = 0
    weak = {"roca", "centro", "comercio", "patagonia", "america", "union", "pioneros"}
    for barrio in city_barrios:
        names = [fold(barrio["name"]), *barrio["aliases"]]
        for alias in names:
            if not alias or len(alias) < 5:
                continue
            if alias in weak and f"barrio {alias}" not in text:
                continue
            if alias in text and len(alias) >= best_len:
                best = barrio
                best_len = len(alias)
    if best:
        return best["name"], best["zona"], best["lat"], best["lon"]

    if city == "puerto-madryn":
        street_name, number = parse_street(text)
        if street_name:
            for name, lat, lon, barrio_name in STREETS:
                if street_names_match(name, street_name):
                    lat2, lon2 = offset_by_number(lat, lon, number)
                    zona = next((b["zona"] for b in BARRIOS if b["name"] == barrio_name), "Centro y costanera")
                    if barrio_name == "Oeste residencial":
                        return "Sin clasificar", "Oeste residencial", lat2, lon2
                    return barrio_name, zona, lat2, lon2
        if "doradillo" in text:
            return "El Doradillo", "El Doradillo", -42.6430, -65.0640
        if "zona sur" in text or re.search(r"roca\s+\d{4}", text):
            return "Zona Sur", "Zona Sur", -42.7865, -65.0220
        if "zona norte" in text:
            return "Zona Norte", "Zona Norte", -42.7540, -65.0380
    return "Sin clasificar", label, clat, clon


_STREET_SKIP = {
    "venta", "dorm", "dormitorio", "dormitorios", "ambiente", "ambientes",
    "ano", "anos", "año", "años", "mts", "cubiertos", "totales", "departamento",
    "depto", "casa", "terreno", "puerto", "madryn", "trelew", "ph",
    "bano", "banos", "baño", "baños", "publicado", "garage", "cochera",
    "destacado", "video", "usd", "ars", "m2", "amb", "jun", "enero", "febrero",
    "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
    "octubre", "noviembre", "diciembre", "olivan", "propiedades",
}


def parse_street(text: str) -> tuple[str, int | None]:
    folded = fold(text)
    known: tuple[str, int | None] = ("", None)
    other: tuple[str, int | None] = ("", None)
    for match in re.finditer(
        r"(?:avenida|av\.?|calle|pasaje)?\s*"
        r"([a-z0-9áéíóúüñ\.]{3,}(?:\s+[a-z0-9áéíóúüñ\.]{2,}){0,4})"
        r"\s+(?:al\s+)?(\d{2,5})\b",
        folded,
    ):
        name = fold(match.group(1))
        words = name.split()
        if not name or any(word in _STREET_SKIP for word in words):
            continue
        number = int(match.group(2))
        if _street_is_known(name):
            known = (name, number)
        elif not other[0]:
            other = (name, number)
    return known if known[0] else other


def street_names_match(a: str, b: str) -> bool:
    left, right = fold(a), fold(b)
    if not left or not right:
        return False
    if left == right:
        return True
    if min(len(left), len(right)) < 5:
        return False
    return f" {left} " in f" {right} " or f" {right} " in f" {left} "


def _street_is_known(name: str) -> bool:
    if not name:
        return False
    return any(street_names_match(name, street) for street, _lat, _lon, _barrio in STREETS)


def city_only_address(address: str | None) -> bool:
    text = fold(address or "").strip(" ,")
    if not text:
        return True
    weak = {
    "puerto madryn", "puerto madryn chubut", "puerto madryn, chubut",
    "trelew", "trelew chubut", "rawson", "gaiman", "playa union",
    "chubut", "argentina",
    "capital federal", "microcentro", "buenos aires",
}
    compact = re.sub(r"[,/]+", " ", text)
    compact = re.sub(r"\s+", " ", compact).strip()
    return compact in weak or (compact.startswith("puerto madryn") and len(compact) < 32)


def has_street_address(*parts: str) -> bool:
    name, number = parse_street(_haystack(*parts))
    if name and number:
        return True
    text = _haystack(*parts)
    if re.search(r"\b(calle|av\.?|avenida|pasaje)\s+[a-záéíóúüñ]{3,}", text):
        return True
    return find_known_street(*parts) is not None


def offset_by_number(lat: float, lon: float, number: int | None) -> tuple[float, float]:
    if not number:
        return lat, lon
    # ~8 m por número de puerta, hacia el sur (crecimiento típico de Madryn).
    meters = min(number, 4000) * 0.55
    return offset_meters(lat, lon, south=meters, east=0)


def offset_meters(lat: float, lon: float, south: float = 0.0, east: float = 0.0) -> tuple[float, float]:
    dlat = -south / 111_320
    dlon = east / (111_320 * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon


def jitter(lat: float, lon: float, key: str) -> tuple[float, float]:
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()
    angle = int(digest[:4], 16) / 65535 * 2 * math.pi
    radius = 35 + (int(digest[4:8], 16) / 65535) * 90
    east = math.cos(angle) * radius
    south = math.sin(angle) * radius
    lat2, lon2 = offset_meters(lat, lon, south=south, east=east)
    return _snap_west_if_water(lat2, lon2, "puerto-madryn")


def locate(
    listing_id: str, lat: float | None, lon: float | None, *parts: str, city: str = "puerto-madryn"
) -> tuple[str, str, float, float, bool]:
    barrio, zona, blat, blon = infer_barrio(*parts, city=city)
    street_name, number = parse_street(_haystack(*parts))
    if lat and lon and _off_land(lat, lon, city):
        lat = lon = None
    if lat and lon and (_dummy_coords(lat, lon, city) or _fake_number_offset(lat, lon, number, city)):
        lat = lon = None
    if lat and lon and abs(lat) > 1 and abs(lon) > 1 and not _dummy_coords(lat, lon, city):
        if barrio == "Sin clasificar":
            barrio, zona, _, _ = nearest_barrio(lat, lon, city=city)
        return barrio, zona, lat, lon, True
    street_hit = find_known_street(*parts)
    if not street_hit and street_name:
        for name, slat, slon, barrio_name in STREETS:
            if street_names_match(street_name, name):
                street_hit = (name, slat, slon, barrio_name)
                break
    if street_hit:
        _name, slat, slon, barrio_name = street_hit
        long_axis = barrio_name in {"Centro", "Zona Sur", "Zona Norte", "Oeste residencial"}
        if number and long_axis:
            lat2, lon2 = offset_by_number(slat, slon, number)
        else:
            lat2, lon2 = jitter(slat, slon, listing_id)
        zona = next((b["zona"] for b in barrios_for(city) if b["name"] == barrio_name), zona)
        if barrio == "Sin clasificar" and barrio_name != "Oeste residencial":
            barrio = barrio_name
        return barrio, zona, lat2, lon2, True
    if street_name and number:
        slot_lat, slot_lon = approx_slot(barrio, zona, listing_id, city=city)
        return barrio, zona, slot_lat, slot_lon, False
    if has_street_address(*parts):
        jlat, jlon = jitter(blat, blon, listing_id)
        return barrio, zona, jlat, jlon, False
    slot_lat, slot_lon = approx_slot(barrio, zona, listing_id, city=city)
    return barrio, zona, slot_lat, slot_lon, False


def _dummy_coords(lat: float, lon: float, city: str) -> bool:
    clat, clon = city_center(city)
    if abs(lat - clat) < 0.0007 and abs(lon - clon) < 0.0007:
        return True
    if abs(lat + 38.416) < 0.05 and abs(lon + 63.616) < 0.05:
        return True
    return _off_land(lat, lon, city)


def _fake_number_offset(lat: float, lon: float, number: int | None, city: str) -> bool:
    if lat is None or lon is None or not number:
        return False
    samples = [city_center(city), *[(b["lat"], b["lon"]) for b in barrios_for(city)]]
    for slat, slon in samples:
        elat, elon = offset_by_number(slat, slon, number)
        if abs(lat - elat) < 2e-5 and abs(lon - elon) < 2e-5:
            return True
    return False


# Costa este de Madryn (longitud máxima que sigue siendo tierra), de sur a norte.
_MADRYN_SHORE = (
    (-42.830, -65.018),
    (-42.800, -65.010),
    (-42.788, -65.006),
    (-42.780, -65.018),
    (-42.770, -65.031),
    (-42.760, -65.0335),
    (-42.753, -65.0350),
    (-42.745, -65.033),
    (-42.720, -65.040),
    (-42.670, -65.048),
    (-42.640, -65.050),
    (-42.600, -65.050),
)


def _interp_shore(lat: float) -> float:
    pts = _MADRYN_SHORE
    if lat <= pts[0][0]:
        return pts[0][1]
    if lat >= pts[-1][0]:
        return pts[-1][1]
    for (lat_a, lon_a), (lat_b, lon_b) in zip(pts, pts[1:]):
        if lat_a <= lat <= lat_b:
            t = (lat - lat_a) / (lat_b - lat_a) if lat_b != lat_a else 0
            return lon_a + t * (lon_b - lon_a)
    return -65.035


def in_water(lat: float | None, lon: float | None, city: str | None = "puerto-madryn") -> bool:
    return _off_land(lat, lon, city or "puerto-madryn")


def _off_land(lat: float | None, lon: float | None, city: str | None) -> bool:
    if lat is None or lon is None or (city or "puerto-madryn") != "puerto-madryn":
        return False
    if not (-43.05 <= lat <= -42.55 and -65.20 <= lon <= -64.80):
        return False
    if lat <= -42.788 and lon <= -64.85:
        return False
    if -42.790 <= lat <= -42.772 and lon <= -64.990:
        return False
    return lon > _interp_shore(lat)


def _snap_west_if_water(lat: float, lon: float, city: str) -> tuple[float, float]:
    if not _off_land(lat, lon, city):
        return lat, lon
    for _ in range(10):
        lat, lon = offset_meters(lat, lon, east=-80)
        if not _off_land(lat, lon, city):
            break
    return lat, lon


def find_known_street(*parts: str) -> tuple[str, float, float, str] | None:
    text = _haystack(*parts)
    street_name, _number = parse_street(text)
    best = None
    for name, lat, lon, barrio_name in STREETS:
        hit = False
        if len(name) >= 5 and re.search(rf"\b{re.escape(name)}\b", text):
            hit = True
        if street_name and street_names_match(street_name, name):
            hit = True
        if hit and (best is None or len(name) > len(best[0])):
            best = (name, lat, lon, barrio_name)
    return best


def approx_slot(barrio: str, zona: str, listing_id: str, city: str = "puerto-madryn") -> tuple[float, float]:
    """Agrupa avisos sin dirección en una grilla al costado del barrio, no mezclados con las casas geolocalizadas."""
    city_barrios = barrios_for(city)
    clat, clon = city_center(city)
    if barrio == "Sin clasificar":
        clat, clon = clat - 0.008, clon - 0.012
    else:
        match = next((b for b in city_barrios if b["name"] == barrio), None)
        if match:
            clat, clon = match["lat"], match["lon"]
        else:
            members = [b for b in city_barrios if b["zona"] == zona]
            if members:
                clat = sum(b["lat"] for b in members) / len(members)
                clon = sum(b["lon"] for b in members) / len(members)
            else:
                clat, clon = clat - 0.008, clon - 0.012
    digest = hashlib.md5(listing_id.encode("utf-8")).hexdigest()
    idx = int(digest[:4], 16)
    col, row = idx % 5, (idx // 5) % 7
    lat, lon = offset_meters(clat, clon, south=140 + row * 32, east=-240 + col * 32)
    return _snap_west_if_water(lat, lon, city)


def barrio_ring(lat: float, lon: float, radius_m: float, n: int = 28, city: str = "puerto-madryn") -> list[list[float]]:
    pts = []
    for i in range(n):
        ang = 2 * math.pi * i / n
        east = radius_m * 1.15 * math.cos(ang)
        south = radius_m * math.sin(ang)
        plat, plon = offset_meters(lat, lon, south=south, east=east)
        plat, plon = _snap_west_if_water(plat, plon, city)
        pts.append([plat, plon])
    pts.append(pts[0])
    return pts


def barrio_overlays(stats_by_barrio: list[dict] | None = None, city: str | None = None) -> list[dict]:
    stats = {(row.get("city"), row["name"]): row for row in (stats_by_barrio or [])}
    radii = {
        "Centro": 420,
        "Zona Sur": 720,
        "Punta Cuevas": 520,
        "Zona Norte": 620,
        "El Doradillo": 900,
        "Quintas del Mirador": 700,
        "Mapu Ngefu": 650,
        "Solana de la Patagonia": 480,
        "Barrancas del Golfo": 220,
        "Parry Madryn": 220,
        "Microcentro": 280,
        "San Nicolás": 320,
        "Puerto Madero": 420,
    }
    overlays = []
    cities = [city] if city else list(CITIES)
    for city_id in cities:
        for barrio in barrios_for(city_id):
            row = stats.get((city_id, barrio["name"])) or {}
            overlays.append(
                {
                    "name": barrio["name"],
                    "zona": barrio["zona"],
                    "city": city_id,
                    "lat": barrio["lat"],
                    "lon": barrio["lon"],
                    "count": row.get("count") or 0,
                    "median_m2": row.get("median_m2") or 0,
                    "median_usd": row.get("median_usd") or 0,
                    "ring": barrio_ring(barrio["lat"], barrio["lon"], radii.get(barrio["name"], 350), city=city_id),
                }
            )
    return overlays


def nearest_barrio(lat: float, lon: float, city: str = "puerto-madryn") -> tuple[str, str, float, float]:
    city_barrios = barrios_for(city)
    if not city_barrios:
        clat, clon = city_center(city)
        return "Centro", "Centro", clat, clon
    best = city_barrios[0]
    best_d = 1e9
    for barrio in city_barrios:
        d = (barrio["lat"] - lat) ** 2 + (barrio["lon"] - lon) ** 2
        if d < best_d:
            best, best_d = barrio, d
    return best["name"], best["zona"], best["lat"], best["lon"]


def fingerprint(title: str, address: str, price_usd: float | None, covered_m2: float | None, property_type: str) -> str:
    street, number = parse_street(fold(f"{address} {title}"))
    core = f"{property_type}|{street}|{number}|{int(price_usd or 0)}|{int(covered_m2 or 0)}"
    return hashlib.sha1(core.encode("utf-8")).hexdigest()[:16]
