from __future__ import annotations

import re
from urllib.parse import urlparse

from .geo import distance_km, fold, parse_street, same_place_ids
from .models import SOURCE_LABELS, Listing
from .text_quality import address_quality, title_quality

DEDUPE_VERSION = "1"
_PHOTO_SKIP = ("unsplash.com", "placeholder", "data:image", "gravatar")
_SIZE_SUFFIX = re.compile(r"[-_]\d{2,4}x\d{2,4}")
_EXT = re.compile(r"\.(?:jpe?g|png|webp|gif|avif).*$", re.I)


def add_photos(item: Listing, urls: list[str] | None) -> None:
    extra = dict(item.extra or {})
    photos: list[str] = list(extra.get("photos") or [])
    if item.image:
        photos.insert(0, item.image)
    for url in urls or []:
        url = str(url or "").strip()
        if url and url not in photos:
            photos.append(url)
    extra["photos"] = list(dict.fromkeys(photos))[:24]
    if not item.image and extra["photos"]:
        item.image = extra["photos"][0]
    item.extra = extra


def photo_key(url: str) -> str:
    raw = str(url or "").strip()
    if not raw or any(skip in raw.lower() for skip in _PHOTO_SKIP):
        return ""
    path = urlparse(raw).path.lower()
    name = path.rsplit("/", 1)[-1]
    name = _SIZE_SUFFIX.sub("", name)
    name = _EXT.sub("", name)
    name = re.sub(r"[^a-z0-9]", "", name)
    return name if len(name) >= 8 else ""


def photo_keys(item: Listing) -> set[str]:
    extra = item.extra or {}
    urls = list(extra.get("photos") or [])
    if item.image:
        urls.append(item.image)
    return {key for key in (photo_key(url) for url in urls) if key}


def source_entry(item: Listing) -> dict[str, str]:
    return {
        "id": item.id,
        "source": item.source,
        "url": item.url,
        "label": SOURCE_LABELS.get(item.source, item.source),
    }


def is_duplicate(item: Listing) -> bool:
    extra = item.extra or {}
    return bool(extra.get("duplicate_of") or extra.get("dedupe_loser"))


def collapse_duplicates(items: list[Listing]) -> list[Listing]:
    visible = [item for item in items if item and item.id]
    by_id = {item.id: item for item in visible}
    _clear_marks(visible)
    parent = {item.id: item.id for item in visible}

    def find(listing_id: str) -> str:
        while parent[listing_id] != listing_id:
            parent[listing_id] = parent[parent[listing_id]]
            listing_id = parent[listing_id]
        return listing_id

    def union(left: str, right: str) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[b] = a

    ordered = list(visible)
    for i, left in enumerate(ordered):
        for right in ordered[i + 1 :]:
            if _same_cluster(left, right):
                union(left.id, right.id)

    groups: dict[str, list[Listing]] = {}
    for item in visible:
        groups.setdefault(find(item.id), []).append(item)

    changed: list[Listing] = []
    for group in groups.values():
        if len(group) < 2:
            continue
        winner = max(group, key=_richness)
        others = [item for item in group if item.id != winner.id]
        _merge_group(winner, others)
        changed.append(winner)
        changed.extend(others)
    return [by_id[item.id] for item in changed]


def _clear_marks(items: list[Listing]) -> None:
    for item in items:
        extra = dict(item.extra or {})
        if not extra.get("dedupe_loser") and not extra.get("duplicate_of"):
            continue
        extra.pop("duplicate_of", None)
        extra.pop("duplicate_ids", None)
        extra.pop("dedupe_loser", None)
        extra.pop("dedupe_hidden", None)
        if extra.get("dedupe_exclude"):
            extra.pop("exclude_from_comps", None)
            extra.pop("dedupe_exclude", None)
        item.extra = extra


def _same_cluster(left: Listing, right: Listing) -> bool:
    if left.id == right.id:
        return False
    if (left.property_type or "") != (right.property_type or ""):
        return False
    if not _same_city(left, right):
        return False
    if _conflicting_unit(left, right):
        return False
    return _match_score(left, right) >= 7


def _same_city(left: Listing, right: Listing) -> bool:
    a, b = left.city or "", right.city or ""
    if not a or not b or a in {"fuera", "otros", "argentina"} or b in {"fuera", "otros", "argentina"}:
        return False
    return a == b or b in same_place_ids(a) or a in same_place_ids(b)


def _conflicting_unit(left: Listing, right: Listing) -> bool:
    floor_a, floor_b = _floor(left), _floor(right)
    if floor_a is not None and floor_b is not None and str(floor_a) != str(floor_b):
        return True
    if left.bedrooms is not None and right.bedrooms is not None and abs(int(left.bedrooms) - int(right.bedrooms)) >= 2:
        return True
    size_a = left.covered_m2 or left.total_m2
    size_b = right.covered_m2 or right.total_m2
    if size_a and size_b and size_a > 15 and size_b > 15:
        ratio = min(size_a, size_b) / max(size_a, size_b)
        if ratio < 0.82:
            return True
    return False


def _match_score(left: Listing, right: Listing) -> int:
    score = 0
    street_hit = _same_street(left, right)
    photos = photo_keys(left) & photo_keys(right)
    same_fp = bool(left.fingerprint and left.fingerprint == right.fingerprint)
    near = _near_pins(left, right)
    if not street_hit and not same_fp and not (photos and near):
        return 0
    if same_fp:
        score += 8
    if street_hit:
        score += 4
    if photos:
        score += 5
    score += _close_num(left.price_usd or left.price, right.price_usd or right.price, 0.08, 2, 0.15, 1)
    size_a = left.covered_m2 or left.total_m2
    size_b = right.covered_m2 or right.total_m2
    score += _close_num(size_a, size_b, 0.08, 2, 0.15, 1)
    if left.bedrooms is not None and right.bedrooms is not None and int(left.bedrooms) == int(right.bedrooms):
        score += 1
    if _same_publisher(left, right):
        score += 1
    if near:
        score += 2
    return score


def _same_street(left: Listing, right: Listing) -> bool:
    street_a, num_a = parse_street(f"{left.address or ''} {left.title or ''}")
    street_b, num_b = parse_street(f"{right.address or ''} {right.title or ''}")
    if not street_a or not street_b or not num_a or not num_b:
        return False
    if int(num_a) != int(num_b):
        return False
    a, b = fold(street_a), fold(street_b)
    return a == b or a in b or b in a


def _close_num(a, b, tight: float, tight_pts: int, loose: float, loose_pts: int) -> int:
    try:
        va, vb = float(a), float(b)
    except (TypeError, ValueError):
        return 0
    if va <= 0 or vb <= 0:
        return 0
    ratio = abs(va - vb) / max(va, vb)
    if ratio <= tight:
        return tight_pts
    if ratio <= loose:
        return loose_pts
    return 0


def _same_publisher(left: Listing, right: Listing) -> bool:
    a, b = fold(left.publisher or ""), fold(right.publisher or "")
    if len(a) < 5 or len(b) < 5:
        return False
    return a == b or a in b or b in a


def _near_pins(left: Listing, right: Listing) -> bool:
    if left.lat is None or left.lon is None or right.lat is None or right.lon is None:
        return False
    if not left.has_exact_location or not right.has_exact_location:
        return False
    return distance_km(left.lat, left.lon, right.lat, right.lon) * 1000 <= 80


def _floor(item: Listing):
    llm = (item.extra or {}).get("llm")
    if isinstance(llm, dict) and llm.get("floor") not in (None, ""):
        return llm.get("floor")
    extra = item.extra or {}
    return extra.get("floor")


def _richness(item: Listing) -> tuple:
    extra = item.extra or {}
    street, number = parse_street(f"{item.address or ''} {item.title or ''}")
    photos = len(extra.get("photos") or [])
    return (
        4 if street and number else 0,
        3 if item.has_exact_location else 0,
        2 if item.details_scraped else 0,
        2 if extra.get("llm_ready") else 0,
        min(len(item.description or "") // 120, 4),
        min(photos, 4),
        1 if item.image else 0,
        1 if item.covered_m2 else 0,
        1 if item.price_usd or item.price else 0,
        address_quality(item.address or ""),
        title_quality(item.title or ""),
    )


def _merge_group(winner: Listing, others: list[Listing]) -> None:
    sources = [source_entry(winner)]
    photos = list((winner.extra or {}).get("photos") or [])
    if winner.image:
        photos.insert(0, winner.image)
    ids = []
    for other in others:
        sources.append(source_entry(other))
        ids.append(other.id)
        _fill_from(winner, other)
        photos.extend((other.extra or {}).get("photos") or [])
        if other.image:
            photos.append(other.image)
        other_extra = dict(other.extra or {})
        other_extra["duplicate_of"] = winner.id
        other_extra["dedupe_loser"] = True
        other_extra["dedupe_hidden"] = True
        other_extra["dedupe_exclude"] = True
        other_extra["exclude_from_comps"] = True
        other.extra = other_extra
    extra = dict(winner.extra or {})
    extra["sources"] = _unique_sources(sources)
    extra["photos"] = list(dict.fromkeys(photos))[:24]
    extra["duplicate_ids"] = ids
    extra.pop("duplicate_of", None)
    extra.pop("dedupe_loser", None)
    extra.pop("dedupe_hidden", None)
    if extra.get("dedupe_exclude"):
        extra.pop("exclude_from_comps", None)
        extra.pop("dedupe_exclude", None)
    if extra["photos"] and not winner.image:
        winner.image = extra["photos"][0]
    winner.extra = extra


def _unique_sources(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        key = row.get("url") or row.get("id") or ""
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _fill_from(winner: Listing, other: Listing) -> None:
    if address_quality(other.address or "") > address_quality(winner.address or ""):
        winner.address = other.address
    if title_quality(other.title or "") > title_quality(winner.title or ""):
        winner.title = other.title
    if len(other.description or "") > len(winner.description or ""):
        winner.description = other.description
    if other.has_exact_location and not winner.has_exact_location and other.lat is not None:
        winner.lat, winner.lon = other.lat, other.lon
        winner.has_exact_location = True
        winner.barrio = other.barrio or winner.barrio
        winner.zona = other.zona or winner.zona
    elif winner.lat is None and other.lat is not None:
        winner.lat, winner.lon = other.lat, other.lon
        winner.barrio = other.barrio or winner.barrio
        winner.zona = other.zona or winner.zona
    for field in ("covered_m2", "total_m2", "rooms", "bedrooms", "bathrooms", "parking", "age_years", "publisher"):
        if getattr(winner, field) in (None, "") and getattr(other, field) not in (None, ""):
            setattr(winner, field, getattr(other, field))
    if other.details_scraped:
        winner.details_scraped = True
    win_extra = dict(winner.extra or {})
    other_extra = other.extra or {}
    win_extra["amenities"] = list(dict.fromkeys((win_extra.get("amenities") or []) + (other_extra.get("amenities") or [])))
    if not win_extra.get("intersection") and other_extra.get("intersection"):
        win_extra["intersection"] = other_extra["intersection"]
    if not win_extra.get("expenses") and other_extra.get("expenses"):
        win_extra["expenses"] = other_extra["expenses"]
    if win_extra.get("mortgage_credit") is None and other_extra.get("mortgage_credit") is not None:
        win_extra["mortgage_credit"] = other_extra["mortgage_credit"]
    if other_extra.get("contacted"):
        win_extra["contacted"] = True
        winner.contacted = True
    old_edits = win_extra.get("user_edits") or {}
    new_edits = other_extra.get("user_edits") or {}
    if old_edits or new_edits:
        win_extra["user_edits"] = {**new_edits, **old_edits}
    winner.extra = win_extra
    if not winner.image and other.image:
        winner.image = other.image
