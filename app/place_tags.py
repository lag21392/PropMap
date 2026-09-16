from __future__ import annotations

from typing import Any, Iterable

from .geo import CABA_IDS, CITIES, DEFAULT_CITY, fold, resolve_city
from .models import Listing
from .place_api import _looks_province, _same_province, lookup_place

_ANCESTOR_KINDS = frozenset({"localidad", "municipio", "city", "town"})
_GENERIC_PREFIX = frozenset(
    {
        "puerto",
        "villa",
        "los",
        "las",
        "san",
        "santa",
        "santo",
        "general",
        "rio",
        "bahia",
        "mar",
        "colonia",
        "paso",
        "capilla",
        "don",
        "nueva",
        "nuevo",
        "barrio",
    }
)
_SKIP_NAMES = frozenset(
    {
        "",
        "sin clasificar",
        "sin barrio",
        "sin zona",
        "centro",
        "norte",
        "sur",
        "este",
        "oeste",
        "fuera",
        "otros",
        "argentina",
        "buenos aires",
    }
)
MAX_TAGS = 8


def place_token(value: str | None) -> str:
    return fold(value or "").replace("-", " ")


def city_query_tokens(city: str | None) -> set[str]:
    """Nombre específico de la ciudad vista. Sin ancestros: 'pilar norte' no incluye 'pilar'."""
    raw = (city or "").strip()
    tokens = {place_token(raw)}
    resolved = ""
    if raw:
        try:
            resolved = resolve_city(raw)
        except Exception:
            resolved = raw
    cfg = CITIES.get(raw) or CITIES.get(resolved) or {}
    tokens.add(place_token(cfg.get("label") or ""))
    tokens.add(place_token(cfg.get("id") or raw))
    if raw in CABA_IDS or resolved in CABA_IDS or str(cfg.get("province") or "") == "capital-federal":
        tokens.update(place_token(item) for item in CABA_IDS)
        tokens.add(place_token(DEFAULT_CITY))
        for alias in cfg.get("aliases") or []:
            tokens.add(place_token(alias))
    return {token for token in tokens if token and (token not in _SKIP_NAMES or token == place_token(DEFAULT_CITY))}


def tags_for_place(
    name: str | None,
    *,
    province: str | None = None,
    city_id: str | None = None,
    remote: bool = True,
) -> list[str]:
    """Tags del lugar: el nombre completo y los prefijos que existen como ciudad."""
    raw = (name or "").strip()
    out: list[str] = []
    seen: set[str] = set()

    def add(label: str) -> None:
        token = place_token(label)
        if not token or token in seen or token in _SKIP_NAMES:
            return
        seen.add(token)
        pretty = str(label).strip()
        out.append(pretty)
        if len(out) >= MAX_TAGS:
            return

    add(raw)
    if city_id:
        add(city_id.replace("-", " "))
    words = place_token(raw).split()
    for idx in range(1, len(words)):
        prefix = " ".join(words[:idx])
        if len(prefix) < 4 or prefix in _GENERIC_PREFIX:
            continue
        if _looks_province(prefix):
            continue
        place = lookup_place(prefix, province_hint=province, remote=remote)
        if not place:
            continue
        kind = str(place.get("kind") or "")
        if kind not in _ANCESTOR_KINDS:
            continue
        official = place_token(str(place.get("name") or ""))
        if official != prefix:
            continue
        if province and place.get("province") and not _same_province(province, str(place.get("province") or "")):
            continue
        add(str(place.get("name") or prefix))
        if len(out) >= MAX_TAGS:
            break
    return out


def tags_for_listing(item: Listing, *, remote: bool = True) -> list[str]:
    extra = getattr(item, "extra", None) or {}
    city = str(getattr(item, "city", None) or "").strip()
    cfg = CITIES.get(city) or {}
    province = str(cfg.get("province") or extra.get("province") or "")
    names: list[tuple[str, str | None]] = []
    if cfg.get("label"):
        names.append((str(cfg["label"]), city))
    if city:
        names.append((city.replace("-", " "), city))
    search = str(extra.get("search_city") or "").strip()
    if search and search != city:
        search_cfg = CITIES.get(search) or {}
        names.append((str(search_cfg.get("label") or search.replace("-", " ")), search))
    barrio = str(getattr(item, "barrio", None) or "").strip()
    if barrio:
        names.append((barrio, None))
    out: list[str] = []
    seen: set[str] = set()
    for raw, cid in names:
        for tag in tags_for_place(raw, province=province or None, city_id=cid, remote=remote):
            token = place_token(tag)
            if not token or token in seen:
                continue
            seen.add(token)
            out.append(tag)
            if len(out) >= MAX_TAGS:
                return out
    return out


def listing_place_tags(item: Listing, *, remote: bool = False) -> list[str]:
    extra = getattr(item, "extra", None) or {}
    cached = extra.get("place_tags")
    if isinstance(cached, list) and cached:
        return [str(tag) for tag in cached if str(tag).strip()]
    return tags_for_listing(item, remote=remote)


def apply_place_tags(item: Listing, *, remote: bool | None = None) -> list[str]:
    import os

    if remote is None:
        remote = os.environ.get("PROPMAP_TEST") != "1"
    tags = tags_for_listing(item, remote=remote)
    extra = dict(getattr(item, "extra", None) or {})
    extra["place_tags"] = tags
    item.extra = extra
    return tags


def matches_place_query(tags: Iterable[str] | None, city: str | None) -> bool:
    wanted = city_query_tokens(city)
    if not wanted:
        return False
    have = {place_token(tag) for tag in (tags or ()) if tag}
    return bool(have & wanted)


def listing_matches_city(item: Listing | Any, city: str | None) -> bool:
    return matches_place_query(listing_place_tags(item, remote=False), city)


def is_narrower_place_query(tags: Iterable[str] | None, city: str | None) -> bool:
    """La vista es más específica que el aviso: Pilar Norte no debe mezclar avisos solo de Pilar."""
    wanted = city_query_tokens(city)
    have = {place_token(tag) for tag in (tags or ()) if tag}
    if not wanted or not have:
        return False
    for view in wanted:
        for tag in have:
            if view != tag and view.startswith(f"{tag} "):
                return True
    return False


def related_place_ids(city: str | None) -> set[str]:
    """Ciudades del catálogo cubiertas por este nombre: Pilar incluye Pilar Norte."""
    raw = (city or "").strip()
    if not raw:
        return set()
    try:
        resolved = resolve_city(raw)
    except Exception:
        resolved = raw
    out = {item for item in (raw, resolved) if item}
    if raw in CABA_IDS or resolved in CABA_IDS:
        from .geo import same_place_ids

        out.update(same_place_ids(raw) or set())
        return {item for item in out if item}
    cfg = CITIES.get(raw) or CITIES.get(resolved) or {}
    token = place_token(str(cfg.get("label") or raw))
    if not token:
        return out
    for other_id, other in list(CITIES.items()):
        name = place_token(str(other.get("label") or other_id))
        if name == token or name.startswith(f"{token} "):
            out.add(other_id)
    return {item for item in out if item}
