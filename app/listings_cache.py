from __future__ import annotations

import gc
import gzip as gzip_mod
import json
import logging
import os
import re
import statistics
import threading
import time
from collections import defaultdict
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import quote

from .geo import (
    GEO_VERSION,
    in_city_radius,
    listing_fits_city,
    public_row_fits_city,
    resolve_city,
    same_place_ids,
)
from .place_tags import listing_matches_city, place_token, related_place_ids
from .jsoncodec import dumps as json_dumps_bytes
from .jsoncodec import gzip_bytes as gzip_encode
from .jsoncodec import loads as json_loads
from .models import SOURCE_LABELS, Listing
from .scoring import SCORE_VERSION, USD_FALLBACK, apply_unit_price

log = logging.getLogger(__name__)

_lock = RLock()
_rev = 0
_by_id: dict[str, dict[str, Any]] = {}
_item_rev: dict[str, int] = {}
_ready = False
_warming = False
_usd = USD_FALLBACK
_last_run = ""
_cities: list[dict] = []
_city_snaps: dict[str, dict[str, Any]] = {}
_persist_queued: set[str] = set()
_encode_queued: set[str] = set()
_loading: set[str] = set()
_warm_queued: set[str] = set()
_warmed: set[str] = set()
_encode_at: dict[str, float] = {}
_disk_preloaded = False
_force_cache_dir: Path | None = None
_watch_started = False
_db_loaded: set[str] = set()
_catalog_keep_fp = ""
_build_depth = 0
_build_gate = threading.Lock()
_meta_at = 0.0
_meta_gate = threading.Lock()
META_REFRESH_SEC = 20.0
CITY_CACHE_TTL_SEC = 48 * 3600
KEEP_CITY_GAP_SEC = 1.2
MAX_RAM_SNAPS = 4
HYDRATE_RAM = 3
RAM_SOFT_KB = 3_800_000
SNAP_VER = "14"
SNAP_READ_VERS = frozenset({"14"})
MIN_TRUSTED_SNAP = 80
TINY_SNAP = 8
PIN_FLUSH_FIRST = 80
PIN_FLUSH_STEP = 250
PIN_FLUSH_DISK = 1000
PIN_KEYS = (
    "id",
    "source",
    "url",
    "title",
    "property_type",
    "price",
    "currency",
    "price_usd",
    "price_m2",
    "address",
    "barrio",
    "zona",
    "lat",
    "lon",
    "covered_m2",
    "total_m2",
    "rooms",
    "bedrooms",
    "image",
    "deal_label",
    "deal_score",
    "profile",
    "vs_barrio_pct",
    "has_exact_location",
    "city",
    "street",
    "street_number",
    "intersection",
    "between",
    "location_kind",
    "portal_exact",
    "portal_approx",
    "portal_lat",
    "portal_lon",
    "approx_address",
        "search_city",
        "place_tags",
        "pin_kind",
    "source_label",
    "favorite",
    "exclude_from_comps",
    "location_approx",
    "location_real",
    "location_missing",
    "approx_span_m",
    "approx_cell",
    "lot_m2",
)
_REV_HEAD = re.compile(rb'"rev"\s*:\s*(\d+)')
_TOTAL_TAIL = re.compile(rb'"total":(\d+)')
_STATS_HEAD = re.compile(rb'"stats":\{"total":(\d+),"deals":(\d+)')
_DISK_STATS_TAIL = 262_144


def _gz_path(folder: Path, city_id: str) -> Path:
    return folder / f"{city_id}.json.gz"


def _meta_path(folder: Path, city_id: str) -> Path:
    return folder / f"{city_id}.meta.json"


def _json_path(folder: Path, city_id: str) -> Path:
    return folder / f"{city_id}.json"


def _pins_path(folder: Path, city_id: str) -> Path:
    return folder / f"{city_id}.pins.json"


def _pins_gz_path(folder: Path, city_id: str) -> Path:
    return folder / f"{city_id}.pins.json.gz"


def _drop_http_blobs(snap: dict[str, Any]) -> None:
    snap.pop("encoded", None)
    snap.pop("encoded_gzip", None)
    snap.pop("pins_encoded", None)
    snap.pop("pins_gzip", None)


def _read_meta(city_id: str, *, allow_stale: bool = False) -> dict[str, Any] | None:
    folder = _disk_dir()
    if folder is None or not city_id or city_id == "*":
        return None
    path = _meta_path(folder, city_id)
    try:
        raw = json_loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    saved = float(raw.get("saved_at") or 0)
    if saved and time.time() - saved > CITY_CACHE_TTL_SEC and not allow_stale:
        return None
    if str(raw.get("snap_ver") or SNAP_VER) not in SNAP_READ_VERS:
        return None
    return raw


def _disk_geo_ok(city_id: str) -> bool:
    meta = _read_meta(city_id)
    return bool(meta) and str(meta.get("geo_ver") or "") == GEO_VERSION


def _disk_snap_complete(meta: dict[str, Any] | None) -> bool:
    """Un JSON parcial (p. ej. 2 o 10 avisos de CABA/Quilmes) no cuenta: hay que releer SQLite."""
    if not meta:
        return False
    if meta.get("warming"):
        return False
    n = int(meta.get("n") or 0)
    if n <= 0:
        return False
    if n < MIN_TRUSTED_SNAP and not meta.get("db_loaded"):
        return False
    return True


def _disk_cache_ok(city_id: str) -> bool:
    meta = _read_meta(city_id)
    return _disk_snap_complete(meta) and _disk_geo_ok(city_id) and _meta_scores_ok(city_id, meta)


def _disk_peek_deals(city_id: str) -> int | None:
    folder = _disk_dir()
    if folder is None or not city_id:
        return None
    path = _json_path(folder, city_id)
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - _DISK_STATS_TAIL))
            tail = fh.read()
    except OSError:
        return None
    hit = _STATS_HEAD.search(tail)
    return int(hit.group(2)) if hit else None


def _meta_scores_ok(city_id: str, meta: dict[str, Any] | None, *, deals: int | None = None, n: int | None = None) -> bool:
    """El mapa no puede reutilizar un snap de ciudad armado antes de puntuar gangas."""
    if not meta:
        return False
    ver = str(meta.get("score_ver") or "")
    if ver == SCORE_VERSION:
        return True
    if ver:
        return False
    count = n if n is not None else int(meta.get("n") or 0)
    found = deals if deals is not None else meta.get("deals")
    if found is None:
        found = _disk_peek_deals(city_id)
    if count >= 8 and int(found or 0) <= 0:
        return False
    return True


def _ram_scores_ok(snap: dict[str, Any]) -> bool:
    ver = str(snap.get("score_ver") or "")
    if ver == SCORE_VERSION:
        return True
    if ver:
        return False
    stats = snap.get("stats") or {}
    n = len(snap.get("listings") or [])
    deals = int(stats.get("deals") or 0)
    if n >= 8 and deals <= 0:
        return False
    return True


def _ram_http_ok(snap: dict[str, Any] | None, city_id: str) -> bool:
    """No servir un snap de 1 aviso como si fuera toda CABA."""
    if not snap:
        return False
    if str(snap.get("geo_ver") or "") != GEO_VERSION:
        return False
    if str(snap.get("snap_ver") or "") not in SNAP_READ_VERS:
        return False
    if not _ram_scores_ok(snap):
        return False
    if snap.get("warming"):
        return True
    if city_id in _db_loaded:
        return True
    n = len(snap.get("listings") or [])
    if n == 0 and (snap.get("encoded") or snap.get("encoded_gzip")):
        return True
    if n >= MIN_TRUSTED_SNAP:
        return True
    catalog_n = 0
    for row in _cities:
        if row.get("id") == city_id:
            try:
                catalog_n = int(row.get("n") or 0)
            except (TypeError, ValueError):
                catalog_n = 0
            break
    if catalog_n >= MIN_TRUSTED_SNAP and n < MIN_TRUSTED_SNAP:
        return False
    return True


def _parsed_scores_ok(parsed: dict[str, Any], meta: dict[str, Any] | None) -> bool:
    ver = str(parsed.get("score_ver") or (meta or {}).get("score_ver") or "")
    if ver == SCORE_VERSION:
        return True
    if ver:
        return False
    stats = parsed.get("stats") or {}
    n = len(parsed.get("listings") or [])
    deals = int(stats.get("deals") or (meta or {}).get("deals") or 0)
    if n >= 8 and deals <= 0:
        return False
    return True


def _disk_http_meta_ok(city_id: str, meta: dict[str, Any] | None, *, deals: int | None = None) -> bool:
    return bool(
        _disk_snap_complete(meta)
        and str((meta or {}).get("snap_ver") or "") in SNAP_READ_VERS
        and str((meta or {}).get("geo_ver") or "") == GEO_VERSION
        and _meta_scores_ok(city_id, meta, deals=deals)
    )


def _disk_http_ready(city_id: str, *, allow_stale: bool = False) -> bool:
    folder = _disk_dir()
    if folder is None or not city_id or city_id == "*":
        return False
    gz = _gz_path(folder, city_id)
    try:
        if gz.is_file() and gz.stat().st_size > 40:
            expired = time.time() - gz.stat().st_mtime > CITY_CACHE_TTL_SEC
            if allow_stale or not expired:
                meta = _read_meta(city_id, allow_stale=allow_stale)
                if _disk_http_meta_ok(city_id, meta):
                    return True
    except OSError:
        pass
    path = _json_path(folder, city_id)
    try:
        if path.is_file() and path.stat().st_size > 80:
            if time.time() - path.stat().st_mtime > CITY_CACHE_TTL_SEC and not allow_stale:
                return False
            meta = _read_meta(city_id, allow_stale=allow_stale)
            if not _disk_http_meta_ok(city_id, meta):
                return False
            with path.open("rb") as fh:
                head = fh.read(160)
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - _DISK_STATS_TAIL))
                tail = fh.read()
            if not _snap_ver_ok_bytes(b"", head=head, tail=tail):
                return False
            peeked = _STATS_HEAD.search(tail)
            deals = int(peeked.group(2)) if peeked else None
            if not _meta_scores_ok(city_id, meta, deals=deals):
                return False
            return True
    except OSError:
        return False
    return False


def _snap_ver_ok_bytes(raw: bytes, *, head: bytes | None = None, tail: bytes | None = None) -> bool:
    blob = raw if raw else (head or b"") + (tail or b"")
    return any(f'"snap_ver":"{ver}"'.encode("ascii") in blob for ver in SNAP_READ_VERS)


def reset() -> None:
    global _rev, _ready, _warming, _usd, _last_run, _disk_preloaded, _force_cache_dir, _catalog_keep_fp, _build_depth, _meta_at
    with _lock:
        _rev = 0
        _by_id.clear()
        _item_rev.clear()
        _ready = False
        _warming = False
        _usd = USD_FALLBACK
        _last_run = ""
        _cities.clear()
        _city_snaps.clear()
        _persist_queued.clear()
        _encode_queued.clear()
        _loading.clear()
        _warm_queued.clear()
        _warmed.clear()
        _encode_at.clear()
        _disk_preloaded = False
        _force_cache_dir = None
        _db_loaded.clear()
        _catalog_keep_fp = ""
    with _build_gate:
        _build_depth = 0
    with _meta_gate:
        _meta_at = 0.0


def cache_ready() -> bool:
    with _lock:
        return _ready


def city_loaded_from_db(city_id: str) -> bool:
    if not city_id:
        return False
    with _lock:
        return city_id in _db_loaded


def cities_loading() -> bool:
    got = _lock.acquire(timeout=0.05)
    if not got:
        return False
    try:
        return bool(_loading)
    finally:
        _lock.release()


def busy_building() -> bool:
    """True mientras se arma o comprime el cache de una ciudad. No es un deadlock."""
    with _build_gate:
        return _build_depth > 0


class _Building:
    def __enter__(self):
        global _build_depth
        with _build_gate:
            _build_depth += 1
        return self

    def __exit__(self, *_exc):
        global _build_depth
        with _build_gate:
            _build_depth = max(0, _build_depth - 1)


def ping_lock(timeout: float = 0.4) -> bool:
    got = _lock.acquire(timeout=timeout)
    if not got:
        return False
    _lock.release()
    return True


def current_rev() -> int:
    return _rev


def _listing_from_public(row: dict[str, Any]) -> Listing:
    lid = str(row.get("id") or "")
    source = str(row.get("source") or "")
    source_id = ""
    if ":" in lid:
        src, _, rest = lid.partition(":")
        source = source or src
        source_id = rest
    extra = dict(row.get("extra") or {})
    for key in (
        "deal_score",
        "is_outlier",
        "monthly_yield_pct",
        "temporal_yield_pct",
        "rental_score",
        "exclude_from_comps",
        "duplicate_of",
    ):
        if key not in extra and row.get(key) is not None:
            extra[key] = row[key]
    return Listing(
        source=source or "web",
        source_id=source_id or lid,
        url=str(row.get("url") or ""),
        title=str(row.get("title") or ""),
        property_type=str(row.get("property_type") or ""),
        price_usd=row.get("price_usd"),
        price_m2=row.get("price_m2"),
        deal_label=str(row.get("deal_label") or ""),
        vs_barrio_pct=row.get("vs_barrio_pct"),
        published_at=str(row.get("published_at") or ""),
        city=str(row.get("city") or ""),
        extra=extra,
    )


def market_items(city_id: str) -> list[Listing]:
    """Avisos ya filtrados del snap. Evita SQLite y listing_fits_city en el GET."""
    view = resolve_city(city_id) if city_id else None
    if not view or view in {"fuera", "otros", "argentina"}:
        return []
    rows: list[dict[str, Any]] = []
    snap = _city_snaps.get(view)
    if snap:
        rows = list(snap.get("listings") or [])
    if not rows:
        disk = _read_disk(view)
        rows = list((disk or {}).get("listings") or [])
    return [_listing_from_public(row) for row in rows if isinstance(row, dict)]


def drop_city_snap(city_id: str | None) -> None:
    if not city_id:
        return
    with _lock:
        _city_snaps.pop(city_id, None)
        _city_snaps.pop("*", None)
        _rev_bump_locked()
    folder = _disk_dir()
    if folder is None:
        return
    for path in (
        _json_path(folder, city_id),
        _gz_path(folder, city_id),
        _meta_path(folder, city_id),
        _pins_path(folder, city_id),
        _pins_gz_path(folder, city_id),
    ):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def invalidate_city_geo(city_id: str | None) -> None:
    """El contorno de la ciudad cambió: el mapa deja afuera lo que ya no cae adentro."""
    if not city_id or os.environ.get("PROPMAP_TEST") == "1":
        return
    drop_city_snap(city_id)
    try:
        request_city_bytes(city_id)
    except Exception:
        pass


def forget_ids(ids: list[str] | tuple[str, ...] | set[str]) -> None:
    """Saca avisos de RAM y de los snaps. El mapa deja de mostrarlos."""
    wanted = [lid for lid in ids if lid]
    if not wanted:
        return
    with _lock:
        affected: set[str] = set()
        drop = list(wanted)
        drop_set = set(drop)
        for lid in drop:
            old = _by_id.pop(lid, None)
            _item_rev.pop(lid, None)
            if not old:
                continue
            for cid in (old.get("city"), old.get("search_city")):
                token = str(cid or "").strip()
                if token and token not in {"fuera", "otros", "argentina"}:
                    affected.add(resolve_city(token) or token)
        for cid, snap in _city_snaps.items():
            if cid == "*":
                continue
            if any((row.get("id") in drop_set) for row in (snap.get("listings") or [])):
                affected.add(cid)
        if not affected and not drop:
            return
        _rev_bump_locked()
        for cid in affected:
            snap = _city_snaps.get(cid)
            if snap and snap.get("listings"):
                _upsert_snap_rows_locked(cid, [], drop_ids=drop)
                _schedule_encode(cid)
        _city_snaps.pop("*", None)


def public_meta() -> dict[str, Any]:
    """Cotización, última corrida y catálogo desde RAM. Sin sqlite ni candado."""
    return {
        "usd_ars": _usd,
        "last_run": _last_run,
        "cities": list(_cities),
    }


def catalog_cities(view_city: str | None = None) -> list[dict]:
    got = _lock.acquire(timeout=0.15)
    if not got:
        return _catalog_cities()
    try:
        return _catalog_cities()
    finally:
        _lock.release()


def cached_city_ids() -> list[str]:
    with _lock:
        return [row["id"] for row in _cities if row.get("id")]


def city_counts() -> dict[str, int]:
    with _lock:
        counts: dict[str, int] = {}
        for row in _by_id.values():
            cid = row.get("city") or ""
            if not cid:
                continue
            counts[cid] = counts.get(cid, 0) + 1
        return counts


def refresh_city_catalog() -> None:
    _refresh_meta(force=True)


def start_warmup() -> None:
    global _warming, _watch_started
    from .cache_keep import start as start_keep

    start_keep()
    if os.environ.get("PROPMAP_TEST") != "1":
        _hydrate_default()
    with _lock:
        if _ready or _warming:
            return
        _warming = True
    threading.Thread(target=_warm_places, daemon=True, name="places-cache").start()
    threading.Thread(target=_warm, daemon=True, name="listings-cache").start()
    if not _watch_started:
        _watch_started = True
        threading.Thread(target=_watch_ram, daemon=True, name="ram-watch").start()


def _warm_places() -> None:
    try:
        from .places import preload_priority_places

        preload_priority_places()
    except Exception:
        log.exception("no pude precargar lugares")


def ingest(listings: list[Listing] | None) -> None:
    global _usd
    if not listings:
        return
    rate = _read_rate()
    missing: list[str] = []
    encode_ids: list[str] = []
    got = _lock.acquire(timeout=0.4)
    if not got:
        return
    try:
        from .llm_enrich import should_publish

        dropped = False
        affected: set[str] = set()
        for item in listings:
            if not should_publish(item):
                old = _by_id.pop(item.id, None)
                if old is not None:
                    dropped = True
                    cid = resolve_city(old.get("city") or "") or old.get("city")
                    if cid:
                        affected.add(cid)
                continue
            old = _by_id.get(item.id)
            old_city = (old or {}).get("city") or ""
            old_search = (old or {}).get("search_city") or ""
            apply_unit_price(item, rate)
            _put_locked(item)
            extra = item.extra or {}
            for cid in (item.city, old_city, extra.get("search_city"), old_search):
                token = str(cid or "").strip()
                if not token or token in {"fuera", "otros", "argentina"}:
                    continue
                affected.add(resolve_city(token) or token)
        if dropped:
            _rev_bump_locked()
        rebuilt = _ready
        patched_rows = [_by_id[item.id] for item in listings if item.id in _by_id]
        drop_ids = [item.id for item in listings if item.id not in _by_id]
        for cid in affected:
            if not cid:
                continue
            if cid in _city_snaps and (_city_snaps[cid].get("listings")):
                _upsert_snap_rows_locked(cid, patched_rows, drop_ids=drop_ids)
                encode_ids.append(cid)
            else:
                missing.append(cid)
        _city_snaps.pop("*", None)
        _usd = rate
    finally:
        _lock.release()
    for cid in encode_ids:
        _schedule_encode(cid)
    cities_to_enrich = list(affected)
    for cid in missing:
        loaded = _read_disk(cid)
        if loaded and loaded.get("listings"):
            with _lock:
                _city_snaps.setdefault(cid, loaded)
                _upsert_snap_rows_locked(cid, patched_rows, drop_ids=drop_ids)
            _schedule_encode(cid)
        elif len(_by_id) < 400:
            _ensure_snap(cid, None)
        else:
            request_city_bytes(cid)
    if rebuilt:
        _refresh_meta()
    if cities_to_enrich:
        from .access import kick_access_later

        for cid in cities_to_enrich:
            kick_access_later(cid)


def _upsert_snap_rows_locked(
    city_id: str,
    rows: list[dict[str, Any]],
    drop_ids: list[str] | None = None,
) -> None:
    if not city_id or city_id == "*":
        return
    snap = _city_snaps.get(city_id)
    if not snap:
        return
    current = list(snap.get("listings") or [])
    if not current:
        return
    index = {row.get("id"): i for i, row in enumerate(current) if row.get("id")}
    for lid in drop_ids or []:
        if lid in index:
            current.pop(index[lid])
            index = {row.get("id"): i for i, row in enumerate(current) if row.get("id")}
    wanted = _wanted_ids(city_id)
    for row in rows:
        lid = row.get("id")
        if not lid:
            continue
        city = row.get("city") or ""
        search = row.get("search_city") or ""
        fits = wanted is None or city in wanted or search in wanted
        if fits:
            fits = public_row_fits_city(row, city_id)
        if lid in index:
            if fits:
                current[index[lid]] = row
            else:
                current.pop(index[lid])
                index = {r.get("id"): i for i, r in enumerate(current) if r.get("id")}
        elif fits:
            current.append(row)
            index[lid] = len(current) - 1
    snap["listings"] = current
    snap["stats"] = _stats(current)
    _drop_http_blobs(snap)
    _rev_bump_locked()
    snap["rev"] = _rev
    snap["src_rev"] = _rev
    _schedule_encode(city_id)


def patch_pin(listing_id: str, favorite: bool, notes: str, contacted: bool) -> None:
    with _lock:
        row = _by_id.get(listing_id)
        if not row:
            return
        updated = dict(row)
        updated["favorite"] = bool(favorite)
        updated["notes"] = notes or ""
        updated["contacted"] = bool(contacted)
        _by_id[listing_id] = updated
        _bump_locked(listing_id)


def payload(city: str | None = None, since: int | None = None) -> dict:
    start_warmup()
    view_city = resolve_city(city) if city else None
    key = view_city or "*"
    got = _lock.acquire(timeout=0.4)
    if not got:
        return _warming_payload(view_city)
    try:
        ready = _ready
        snap = _city_snaps.get(key)
        data = _serve_snap_locked(snap, view_city, since) if snap else None
    finally:
        _lock.release()
    if data is not None and (data.get("unchanged") or data.get("listings")):
        _attach_encoded(key, data)
        return data
    loaded = _read_disk(key)
    if loaded:
        data = _serve_snap_locked(loaded, view_city, since)
        return data
    if ready:
        data = _ensure_snap(view_city, since)
        _attach_encoded(key, data)
        return data
    return _warming_payload(view_city)


def disk_response_bytes(city: str | None) -> bytes | None:
    raw, _enc = listings_body(city, gzip=False)
    return raw


def _trim_foreign_snap(city_id: str, snap: dict[str, Any]) -> bool:
    rows = list(snap.get("listings") or [])
    kept: list[dict[str, Any]] = []
    changed = False
    for row in rows:
        lat, lon = row.get("lat"), row.get("lon")
        try:
            if lat is not None and lon is not None and in_city_radius(float(lat), float(lon), city_id):
                kept.append(row)
                continue
        except (TypeError, ValueError):
            pass
        if public_row_fits_city(row, city_id):
            kept.append(row)
        else:
            changed = True
    if not changed:
        snap["geo_ver"] = GEO_VERSION
        return False
    snap["listings"] = kept
    snap["geo_ver"] = GEO_VERSION
    snap["stats"] = _stats(kept)
    _drop_http_blobs(snap)
    return True


def listings_body(city: str | None, *, gzip: bool = False) -> tuple[bytes | None, str | None]:
    """Sirve bytes ya cerrados. gzip=True usa el blob precocinado, no comprime en el GET."""
    view_city = resolve_city(city) if city else None
    if not view_city:
        return None, None
    snap = _city_snaps.get(view_city)
    ram_ok = _ram_http_ok(snap, view_city)
    warming = bool(snap and snap.get("warming"))
    if ram_ok and not warming:
        if gzip:
            packed = snap.get("encoded_gzip")
            if packed:
                return packed, "gzip"
        if snap.get("encoded"):
            return snap["encoded"], None
    if gzip:
        packed = _read_disk_gzip(view_city, allow_stale=True)
        if packed:
            return packed, "gzip"
    raw = _read_disk_bytes(view_city, allow_stale=True)
    if raw:
        return raw, None
    if ram_ok:
        if gzip:
            packed = snap.get("encoded_gzip")
            if packed:
                return packed, "gzip"
        if snap.get("encoded"):
            return snap["encoded"], None
    if gzip:
        packed = _read_disk_gzip(view_city, allow_stale=True, allow_incomplete=True)
        if packed:
            return packed, "gzip"
    raw = _read_disk_bytes(view_city, allow_stale=True, allow_incomplete=True)
    if raw:
        return raw, None
    return None, None


def listings_pins_body(city: str | None, *, gzip: bool = False) -> tuple[bytes | None, str | None]:
    """Capa liviana para el mapa. Sale antes que las fichas."""
    view_city = resolve_city(city) if city else None
    if not view_city:
        return None, None
    snap = _city_snaps.get(view_city)
    geo_ok = _ram_http_ok(snap, view_city)
    if geo_ok and snap.get("pins_gzip") and gzip:
        return snap["pins_gzip"], "gzip"
    if geo_ok and snap.get("pins_encoded"):
        return snap["pins_encoded"], None
    if geo_ok and snap.get("warming") and snap.get("encoded") and str(snap.get("snap_ver") or "") in SNAP_READ_VERS:
        if gzip and snap.get("encoded_gzip"):
            return snap["encoded_gzip"], "gzip"
        return snap["encoded"], None
    folder = _disk_dir()
    if folder is None:
        return None, None
    meta = _read_meta(view_city, allow_stale=True)
    if not _disk_meta_readable(view_city, meta):
        return None, None
    if gzip:
        gz = _pins_gz_path(folder, view_city)
        try:
            if gz.is_file() and gz.stat().st_size > 40:
                return gz.read_bytes(), "gzip"
        except OSError:
            pass
    path = _pins_path(folder, view_city)
    try:
        if path.is_file() and path.stat().st_size > 80:
            raw = path.read_bytes()
            if _snap_ver_ok_bytes(raw):
                return raw, None
    except OSError:
        return None, None
    return None, None


def unchanged_listings(city: str | None, since: int) -> bytes | None:
    if since < 0:
        return None
    view_city = resolve_city(city) if city else None
    if not view_city:
        return None
    rev = _city_rev(view_city)
    if rev is None or rev != since:
        return None
    warming = "true" if (_city_snaps.get(view_city) or {}).get("warming") else "false"
    return (
        f'{{"rev":{rev},"unchanged":true,"live":true,"warming":{warming},'
        f'"listings":[],"stats":{{}},"cities":[],"facebook":[]}}'
    ).encode("utf-8")


def _city_rev(city_id: str) -> int | None:
    snap = _city_snaps.get(city_id)
    if snap and snap.get("rev") is not None and (
        snap.get("encoded") or snap.get("loaded") or snap.get("listings")
    ):
        try:
            return int(snap["rev"])
        except (TypeError, ValueError):
            pass
    meta = _read_meta(city_id)
    if meta and meta.get("rev") is not None:
        try:
            return int(meta["rev"])
        except (TypeError, ValueError):
            pass
    folder = _disk_dir()
    if folder is None:
        return None
    path = _json_path(folder, city_id)
    try:
        with path.open("rb") as fh:
            head = fh.read(96)
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 160))
            tail = fh.read()
        if not _snap_ver_ok_bytes(b"", head=head, tail=tail):
            return None
    except OSError:
        return None
    hit = _REV_HEAD.search(head)
    if not hit:
        return None
    return int(hit.group(1))


def _warm_city_extras(view_city: str) -> None:
    try:
        from .access import kick_access_later
        from .osm_poi import ensure_city_pois
        from .places import ensure_city_outline, ensure_view_city

        if os.environ.get("PROPMAP_TEST") != "1":
            ensure_view_city(view_city)
        ensure_city_outline(view_city, blocking=False)
        ensure_city_pois(view_city, blocking=False)
        kick_access_later(view_city)
    except Exception:
        log.exception("no pude precargar POIs de %s", view_city)
    finally:
        with _lock:
            _warm_queued.discard(view_city)
            _warmed.add(view_city)


def _schedule_city_warm(view_city: str) -> None:
    if os.environ.get("PROPMAP_TEST") == "1":
        _warm_city_extras(view_city)
        return
    start = False
    with _lock:
        if view_city in _warmed or view_city in _warm_queued:
            return
        _warm_queued.add(view_city)
        start = True
    if start:
        threading.Thread(
            target=_warm_city_extras,
            args=(view_city,),
            daemon=True,
            name=f"warm-{view_city}",
        ).start()


def request_city_bytes(city: str | None, *, refresh: bool = False) -> None:
    view_city = resolve_city(city) if city else None
    if not view_city or view_city in {"fuera", "otros", "argentina"}:
        return
    _schedule_city_warm(view_city)
    if not refresh:
        snap = _city_snaps.get(view_city) or {}
        if _ram_http_ok(snap, view_city) and snap.get("encoded_gzip") and not snap.get("warming"):
            return
        if _disk_http_ready(view_city):
            return
    start_load = False
    with _lock:
        if view_city in _loading:
            return
        snap = _city_snaps.get(view_city) or {}
        n = len(snap.get("listings") or [])
        incomplete = n < MIN_TRUSTED_SNAP and view_city not in _db_loaded
        if not refresh:
            if view_city in _db_loaded and not incomplete:
                if n == 0:
                    return
                if snap.get("encoded") and not snap.get("warming"):
                    return
                if n and not snap.get("warming"):
                    _schedule_encode(view_city)
                    return
            if snap.get("encoded") and not snap.get("warming") and not incomplete:
                return
            trusted = n >= MIN_TRUSTED_SNAP or view_city in _db_loaded
            if trusted and n and not snap.get("warming") and not incomplete:
                _schedule_encode(view_city)
                return
        _loading.add(view_city)
        start_load = True
    if start_load:
        threading.Thread(
            target=_load_city, args=(view_city,), daemon=True, name=f"city-load-{view_city}"
        ).start()


def _city_fetch_ids(city_id: str) -> list[str]:
    """IDs a leer de SQLite. No trae todo lo que cae a 25 km (eso hinchaba CABA en Piñero)."""
    from .geo import CABA_IDS

    wanted = {city_id}
    wanted.update(cid for cid in (related_place_ids(city_id) or set()) if cid)
    if city_id in CABA_IDS:
        wanted.update(cid for cid in (same_place_ids(city_id) or set()) if cid)
    return [cid for cid in wanted if cid]


def _sqlite_city_n(city_id: str) -> int:
    try:
        from .store import city_listing_counts

        return int(city_listing_counts().get(city_id) or 0)
    except Exception:
        return 0


def _load_city(city_id: str) -> None:
    with _Building():
        _load_city_body(city_id)


def _load_city_body(city_id: str) -> None:
    try:
        from . import store
        from .llm_enrich import should_publish

        log.warning("cargando avisos de %s", city_id)
        store.init()
        rate = _read_rate()
        wanted = _city_fetch_ids(city_id)
        items = store.fetch_by_cities(wanted or [city_id])
        sqlite_n = _sqlite_city_n(city_id) or len(items)
        log.warning("lei %s avisos de sqlite para %s", len(items), city_id)
        pins: list[dict[str, Any]] = []
        last_flush = 0
        last_disk = 0
        for i, item in enumerate(items):
            if i % 20 == 0:
                time.sleep(0)
            if not should_publish(item):
                continue
            if not listing_fits_city(item, city_id, remote=False, require_radius=True):
                continue
            if (item.city or "") != city_id:
                tagged = listing_matches_city(item, city_id)
                if not tagged:
                    item.city = city_id
            apply_unit_price(item, rate)
            pins.append(_pin_row(item))
            n = len(pins)
            if n == PIN_FLUSH_FIRST or n - last_flush >= PIN_FLUSH_STEP:
                persist_warm = n == PIN_FLUSH_FIRST or n - last_disk >= PIN_FLUSH_DISK
                _commit_snap(city_id, pins, warming=True, persist=persist_warm)
                last_flush = n
                if persist_warm:
                    last_disk = n
                log.warning("mapa de %s: %s pines", city_id, n)
        with _lock:
            _db_loaded.add(city_id)
        if pins:
            _commit_snap(city_id, pins, warming=False, persist=True, db_n=sqlite_n)
            log.warning("cache de %s en disco: %s pines", city_id, len(pins))
        log.warning("cache de %s listo: %s avisos", city_id, len(pins))
    except Exception:
        log.exception("no pude armar cache de %s", city_id)
    finally:
        with _lock:
            _loading.discard(city_id)
            _encode_queued.discard(city_id)
        gc.collect()


def _attach_encoded(key: str, data: dict[str, Any]) -> None:
    if data.get("unchanged") or data.get("warming"):
        return
    with _lock:
        encoded = (_city_snaps.get(key) or {}).get("encoded")
    if encoded:
        data["encoded"] = encoded
        return
    _schedule_encode(key)


def _warm() -> None:
    global _ready, _warming, _usd, _last_run
    try:
        from . import store
        from .places import listed_cities, load_custom_places

        store.init()
        load_custom_places()
        cities = listed_cities([])
        with _lock:
            _cities[:] = cities
        _preload_disk()
        ids = _priority_city_ids()
        from .geo import DEFAULT_CITY

        if _disk_http_ready(DEFAULT_CITY, allow_stale=True):
            _hydrate_ram_gzip(DEFAULT_CITY)
        if os.environ.get("PROPMAP_TEST") != "1":
            from .places import ensure_view_city

            try:
                ensure_view_city(DEFAULT_CITY)
            except Exception:
                log.exception("no pude registrar %s", DEFAULT_CITY)
        rate = _read_rate()
        started_missing = False
        for i, cid in enumerate(ids):
            if _disk_http_ready(cid):
                log.info("cache de ciudad listo en disco: %s", cid)
                if i < HYDRATE_RAM:
                    _hydrate_ram_gzip(cid)
                continue
            if _disk_http_ready(cid, allow_stale=True):
                log.info("cache de ciudad vencido, se sirve y se refresca: %s", cid)
                if i < HYDRATE_RAM:
                    _hydrate_ram_gzip(cid)
                continue
            with _lock:
                n = len((_city_snaps.get(cid) or {}).get("listings") or [])
                trusted = n >= MIN_TRUSTED_SNAP or cid in _db_loaded
            if trusted:
                log.info("cache de ciudad listo en RAM: %s", cid)
                continue
            if cid != DEFAULT_CITY:
                continue
            if started_missing:
                continue
            log.warning("precargando avisos de %s", cid)
            if os.environ.get("PROPMAP_TEST") == "1":
                with _lock:
                    if cid in _loading:
                        started_missing = True
                        continue
                    _loading.add(cid)
                _load_city(cid)
            else:
                request_city_bytes(cid)
            started_missing = True
        cities = listed_cities([])
        last_run = store.get_meta("last_run") or ""
        with _lock:
            _cities[:] = cities
            _usd = rate
            _last_run = last_run
    except Exception:
        log.exception("listings cache warmup failed")
    else:
        with _lock:
            _ready = True
        try:
            from .access import kick_access_later
            from .osm_poi import ensure_city_pois
            from .scrapers import kick_repair_far_pins

            ensure_city_pois(DEFAULT_CITY, blocking=False)
            kick_access_later(DEFAULT_CITY)
            kick_repair_far_pins(DEFAULT_CITY)
        except Exception:
            log.exception("no pude encolar cercanías")
    finally:
        with _lock:
            _warming = False


def _ingest_rows(items: list[Listing], rate: float) -> None:
    from .llm_enrich import should_publish

    prepared: list[tuple[str, dict[str, Any]]] = []
    for i, item in enumerate(items):
        if not should_publish(item):
            continue
        apply_unit_price(item, rate)
        prepared.append((item.id, item.to_public_dict()))
        if i % 80 == 0:
            time.sleep(0.005)
    with _lock:
        for listing_id, public in prepared:
            _by_id[listing_id] = public
            _item_rev.setdefault(listing_id, _rev)
        if prepared:
            _rev_bump_locked()
    if prepared:
        _refresh_meta()


def _pin_row(item: Listing) -> dict[str, Any]:
    """Fila liviana para pintar el mapa antes de armar la ficha completa."""
    from .geo import apply_public_location, recovered_location_overlay
    from .layout import apply_layout_counts

    apply_layout_counts(item)
    extra = item.extra or {}
    loc = recovered_location_overlay(item)
    data = {
        "id": item.id,
        "source": item.source,
        "url": item.url,
        "title": item.title,
        "property_type": item.property_type,
        "price": item.price,
        "currency": item.currency,
        "price_usd": item.price_usd,
        "price_m2": item.price_m2,
        "address": loc.get("address") or item.address,
        "barrio": item.barrio,
        "zona": item.zona,
        "lat": item.lat,
        "lon": item.lon,
        "covered_m2": item.covered_m2,
        "total_m2": item.total_m2,
        "rooms": item.rooms,
        "bedrooms": item.bedrooms,
        "image": item.image,
        "deal_label": item.deal_label,
        "deal_score": extra.get("deal_score"),
        "profile": _slim_profile(extra),
        "vs_barrio_pct": item.vs_barrio_pct,
        "has_exact_location": item.has_exact_location,
        "city": item.city,
        "street": loc.get("street") or extra.get("street") or "",
        "street_number": loc.get("street_number") if loc.get("street_number") not in {None, ""} else extra.get("street_number"),
        "intersection": loc.get("intersection") or extra.get("intersection") or "",
        "between": extra.get("between") or "",
        "location_kind": extra.get("location_kind") or "",
        "portal_exact": bool(extra.get("portal_exact")),
        "portal_approx": bool(extra.get("portal_approx")),
        "portal_lat": extra.get("portal_lat"),
        "portal_lon": extra.get("portal_lon"),
        "approx_address": extra.get("approx_address") or "",
        "search_city": extra.get("search_city") or "",
        "place_tags": extra.get("place_tags") or [],
        "pin_kind": extra.get("pin_kind") or "",
        "source_label": SOURCE_LABELS.get(item.source, item.source),
        "favorite": bool(item.favorite),
        "exclude_from_comps": bool(extra.get("exclude_from_comps")),
    }
    return apply_public_location(data)


def _slim_profile(extra: dict[str, Any] | None) -> dict[str, Any]:
    blob = extra if isinstance(extra, dict) else {}
    profile = blob.get("profile")
    if not isinstance(profile, dict):
        return {}
    axes_in = profile.get("axes") if isinstance(profile.get("axes"), dict) else {}
    axes: dict[str, Any] = {}
    for key, val in axes_in.items():
        if not isinstance(val, dict):
            continue
        axes[str(key)] = {
            "score": val.get("score"),
            "confidence": val.get("confidence"),
            "note": val.get("note"),
        }
    if not axes:
        return {
            "axes": {},
            "pin_grade": profile.get("pin_grade") or blob.get("pin_grade") or "",
            "total": profile.get("total"),
            "total_n": profile.get("total_n"),
            "pending": profile.get("pending") or [],
            "labels": profile.get("labels") or {},
            "version": profile.get("version"),
        }
    return {
        "axes": axes,
        "pin_grade": profile.get("pin_grade") or blob.get("pin_grade") or "",
        "total": profile.get("total"),
        "total_n": profile.get("total_n"),
        "pending": profile.get("pending") or [],
        "labels": profile.get("labels") or {},
        "version": profile.get("version"),
    }


def _commit_snap(
    city_id: str,
    listings: list[dict[str, Any]],
    *,
    warming: bool,
    persist: bool,
    bump: bool = True,
    expected_rev: int | None = None,
    db_n: int = 0,
) -> None:
    rows = list(listings)
    if not warming:
        rows = [_http_list_row(row) for row in rows]
    with _lock:
        if bump:
            _rev_bump_locked()
            rev = _rev
        else:
            current = _city_snaps.get(city_id)
            if current is None:
                return
            rev = int(current.get("rev") or _rev)
            if expected_rev is not None and rev != expected_rev:
                return
        stats = _stats(rows)
        snap = {
            "rev": rev,
            "listings": rows,
            "stats": stats,
            "cities": _catalog_cities(city_id),
            "usd_ars": _usd,
            "last_run": _last_run,
            "facebook": _marketplace_links(city_id),
            "src_rev": rev,
            "loaded": not warming,
            "warming": warming,
            "geo_ver": GEO_VERSION,
            "snap_ver": SNAP_VER,
            "score_ver": SCORE_VERSION,
        }
        _city_snaps[city_id] = snap
        usd = _usd
        last_run = _last_run
        cities = snap["cities"]
        facebook = snap["facebook"]
    body = {
        "rev": rev,
        "unchanged": False,
        "live": True,
        "warming": warming,
        "listings": rows,
        "stats": stats,
        "barrios": [],
        "cities": cities,
        "usd_ars": usd,
        "last_run": last_run,
        "facebook": facebook,
        "snap_ver": SNAP_VER,
        "geo_ver": GEO_VERSION,
        "score_ver": SCORE_VERSION,
    }
    encoded: bytes
    encoded_gzip: bytes
    pins_encoded: bytes
    pins_gzip: bytes
    pin_body = dict(body)
    pin_body["listings"] = [_public_to_pin(row) for row in rows]
    pin_body["layer"] = "pins"
    with _Building():
        if warming:
            pins_encoded = json_dumps_bytes(pin_body)
            pins_gzip = gzip_encode(pins_encoded)
            encoded = pins_encoded
            encoded_gzip = pins_gzip
        else:
            encoded = json_dumps_bytes(body)
            encoded_gzip = b""
            pins_encoded = b""
            pins_gzip = b""
    from_db = False
    with _lock:
        from_db = city_id in _db_loaded
    if persist and not warming:
        test_mode = os.environ.get("PROPMAP_TEST") == "1"
        should_write = len(rows) >= MIN_TRUSTED_SNAP or from_db or test_mode
        if should_write:
            _write_http_artifacts(
                city_id,
                encoded,
                len(rows),
                rev,
                encoded_gzip=None,
                deals=int(body["stats"].get("deals") or 0),
                db_loaded=from_db or test_mode,
                db_n=db_n,
            )
        with _Building():
            encoded_gzip = gzip_encode(encoded)
            pins_encoded = json_dumps_bytes(pin_body)
            pins_gzip = gzip_encode(pins_encoded)
        if should_write:
            _write_http_gzip(city_id, encoded_gzip)
            _write_pins_artifact(city_id, pins_encoded, packed=pins_gzip)
    elif warming:
        pins_encoded = encoded
        pins_gzip = encoded_gzip
        if persist and len(rows) >= PIN_FLUSH_FIRST and not _disk_http_ready(city_id, allow_stale=True):
            _write_http_artifacts(
                city_id,
                encoded,
                len(rows),
                rev,
                encoded_gzip=encoded_gzip or None,
                deals=int(body["stats"].get("deals") or 0),
                db_loaded=False,
                warming=True,
            )
            if encoded_gzip:
                _write_http_gzip(city_id, encoded_gzip)
            _write_pins_artifact(city_id, pins_encoded, packed=pins_gzip)
    else:
        with _Building():
            encoded_gzip = gzip_encode(encoded)
            pins_encoded = json_dumps_bytes(pin_body)
            pins_gzip = gzip_encode(pins_encoded)
    with _lock:
        snap = _city_snaps.get(city_id)
        if snap is not None and int(snap.get("rev") or 0) == rev:
            snap["encoded"] = encoded
            snap["encoded_gzip"] = encoded_gzip
            snap["pins_encoded"] = pins_encoded
            snap["pins_gzip"] = pins_gzip
            snap["enc_rev"] = rev
            snap["geo_ver"] = GEO_VERSION
            snap["snap_ver"] = SNAP_VER
            snap["score_ver"] = SCORE_VERSION
            snap["warming"] = warming
            snap["loaded"] = not warming
            if persist and not warming:
                _slim_snap_locked(city_id, snap)


def _encode_snap(key: str) -> None:
    with _lock:
        snap = _city_snaps.get(key)
        if not snap or snap.get("warming"):
            return
        rev = int(snap.get("rev") or 0)
        if snap.get("encoded") and int(snap.get("enc_rev") or -1) == rev:
            _slim_snap_locked(key, snap)
            return
        if not snap.get("listings") and not snap.get("loaded"):
            return
        if not snap.get("listings"):
            return
        listings = list(snap.get("listings") or [])
        copied_rev = rev
    _commit_snap(key, listings, warming=False, persist=True, bump=False, expected_rev=copied_rev)


def _http_list_row(row: dict[str, Any]) -> dict[str, Any]:
    """La ficha pide /api/near; no hace falta copiar 32 POIs en cada aviso del mapa."""
    out = dict(row)
    out.pop("nearby", None)
    access = out.get("access")
    if isinstance(access, dict) and access.get("nearby"):
        access = dict(access)
        access.pop("nearby", None)
        out["access"] = access
    profile = out.get("profile")
    if isinstance(profile, dict):
        axes = profile.get("axes")
        if isinstance(axes, dict):
            serv = axes.get("servicios")
            if isinstance(serv, dict) and (serv.get("nearby") or serv.get("details")):
                serv = dict(serv)
                serv.pop("nearby", None)
                serv.pop("details", None)
                axes = dict(axes)
                axes["servicios"] = serv
                profile = dict(profile)
                profile["axes"] = axes
                out["profile"] = profile
    photos = out.get("photos")
    if isinstance(photos, list) and len(photos) > 4:
        out["photos"] = photos[:4]
    return out


def _public_to_pin(row: dict[str, Any]) -> dict[str, Any]:
    out = {key: row[key] for key in PIN_KEYS if key in row}
    slim = _slim_profile(row)
    if slim:
        out["profile"] = slim
    else:
        out.pop("profile", None)
    return out


def _ensure_disk_gzip(city_id: str) -> None:
    """Comprime el JSON de disco una vez. No rearma 5k fichas."""
    if os.environ.get("PROPMAP_TEST") == "1":
        return
    folder = _disk_dir()
    if folder is None or not city_id:
        return
    gz = _gz_path(folder, city_id)
    try:
        if gz.is_file() and gz.stat().st_size > 40:
            return
    except OSError:
        pass
    raw = _read_disk_bytes(city_id)
    if not raw:
        return
    with _Building():
        packed = gzip_encode(raw)
    tmp = folder / f".{city_id}.json.gz.tmp"
    try:
        tmp.write_bytes(packed)
        tmp.replace(gz)
    except OSError:
        log.exception("no pude comprimir cache de %s", city_id)
        try:
            tmp.unlink()
        except OSError:
            pass


def _ensure_disk_pins(city_id: str) -> None:
    if os.environ.get("PROPMAP_TEST") == "1":
        return
    folder = _disk_dir()
    if folder is None or not city_id:
        return
    path = _pins_path(folder, city_id)
    gz = _pins_gz_path(folder, city_id)
    raw: bytes | None = None
    try:
        if path.is_file() and path.stat().st_size > 80:
            candidate = path.read_bytes()
            if _snap_ver_ok_bytes(candidate):
                raw = candidate
    except OSError:
        pass
    gz_ok = False
    try:
        gz_ok = gz.is_file() and gz.stat().st_size > 40
    except OSError:
        pass
    if raw is not None:
        if not gz_ok:
            packed = gzip_encode(raw)
            tmp = folder / f".{city_id}.pins.gz.tmp"
            try:
                tmp.write_bytes(packed)
                tmp.replace(gz)
            except OSError:
                log.exception("no pude comprimir pines de %s", city_id)
                try:
                    tmp.unlink()
                except OSError:
                    pass
        return
    full = _read_disk_bytes(city_id)
    if not full:
        return
    try:
        data = json_loads(full)
    except (ValueError, json.JSONDecodeError, TypeError):
        return
    rows = [_public_to_pin(row) for row in data.get("listings") or []]
    if not rows:
        return
    body = {key: value for key, value in data.items() if key != "listings"}
    body["listings"] = rows
    body["layer"] = "pins"
    packed = json_dumps_bytes(body)
    _write_pins_artifact(city_id, packed, packed=gzip_encode(packed))


def _write_pins_artifact(city_id: str, encoded: bytes, packed: bytes | None = None) -> None:
    folder = _disk_dir()
    if folder is None or not city_id or city_id == "*":
        return
    if len(encoded) < 80:
        return
    tmp = folder / f".{city_id}.pins.tmp"
    path = _pins_path(folder, city_id)
    try:
        tmp.write_bytes(encoded)
        tmp.replace(path)
        if packed:
            gz_tmp = folder / f".{city_id}.pins.gz.tmp"
            gz_tmp.write_bytes(packed)
            gz_tmp.replace(_pins_gz_path(folder, city_id))
    except OSError:
        log.exception("no pude guardar pines de %s", city_id)
        try:
            tmp.unlink()
        except OSError:
            pass


def _slim_snap_locked(key: str, snap: dict[str, Any]) -> None:
    if os.environ.get("PROPMAP_TEST") == "1":
        snap["encoded"] = snap.get("encoded")
        _evict_old_snaps_locked(key)
        return
    for row in snap.get("listings") or []:
        lid = row.get("id")
        if lid:
            _by_id.pop(lid, None)
    snap["listings"] = []
    _evict_old_snaps_locked(key)


def _write_http_artifacts(
    city_id: str,
    encoded: bytes,
    n: int,
    rev: int,
    encoded_gzip: bytes | None = None,
    deals: int = 0,
    db_loaded: bool = False,
    warming: bool = False,
    db_n: int = 0,
) -> None:
    folder = _disk_dir()
    if folder is None or not city_id or city_id == "*":
        return
    if n <= 0:
        return
    meta = _read_meta(city_id, allow_stale=True)
    if warming and meta and _disk_snap_complete(meta):
        return
    if meta:
        old_n = int(meta.get("n") or 0)
        old_score = str(meta.get("score_ver") or "")
        peeked = _disk_peek_deals(city_id)
        legacy_unscored = (not old_score) and old_n >= 8 and int(peeked or meta.get("deals") or 0) <= 0
        version_stale = bool(old_score) and old_score != SCORE_VERSION
        if (
            not warming
            and not version_stale
            and not legacy_unscored
            and old_n >= TINY_SNAP
            and n < old_n * 0.5
            and n < old_n - 4
            and not db_loaded
        ):
            log.warning("no piso cache de %s (%s → %s avisos)", city_id, old_n, n)
            return
    folder.mkdir(parents=True, exist_ok=True)
    path = _json_path(folder, city_id)
    tmp = folder / f".{city_id}.json.tmp"
    meta_path = _meta_path(folder, city_id)
    meta_tmp = folder / f".{city_id}.meta.tmp"
    try:
        tmp.write_bytes(encoded)
        tmp.replace(path)
        meta_tmp.write_text(
            json.dumps(
                {"rev": rev, "n": n, "deals": deals, "snap_ver": SNAP_VER, "geo_ver": GEO_VERSION, "score_ver": SCORE_VERSION, "saved_at": time.time(), "db_loaded": bool(db_loaded), "warming": bool(warming), "db_n": 0 if warming else int(db_n or (meta or {}).get("db_n") or 0)},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        meta_tmp.replace(meta_path)
        if encoded_gzip:
            gz_tmp = folder / f".{city_id}.json.gz.tmp"
            gz_tmp.write_bytes(encoded_gzip)
            gz_tmp.replace(_gz_path(folder, city_id))
    except OSError:
        log.exception("no pude guardar cache http de %s", city_id)
        for leftover in (tmp, meta_tmp):
            try:
                leftover.unlink()
            except OSError:
                pass


def _write_http_gzip(city_id: str, packed: bytes) -> None:
    if not packed:
        return
    folder = _disk_dir()
    if folder is None or not city_id or city_id == "*":
        return
    gz_tmp = folder / f".{city_id}.json.gz.tmp"
    try:
        gz_tmp.write_bytes(packed)
        gz_tmp.replace(_gz_path(folder, city_id))
    except OSError:
        log.exception("no pude comprimir cache http de %s", city_id)
        try:
            gz_tmp.unlink()
        except OSError:
            pass


def _read_rate() -> float:
    from . import store

    try:
        return float(store.get_meta("usd_ars") or USD_FALLBACK)
    except (TypeError, ValueError):
        return USD_FALLBACK


def _disk_dir() -> Path | None:
    if _force_cache_dir is not None:
        _force_cache_dir.mkdir(parents=True, exist_ok=True)
        return _force_cache_dir
    if os.environ.get("PROPMAP_TEST") == "1":
        return None
    from .store import DATA_DIR

    path = DATA_DIR / "city_cache"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return path


def _serve_snap_locked(snap: dict[str, Any], view_city: str | None, since: int | None) -> dict:
    if str(snap.get("snap_ver") or "") not in SNAP_READ_VERS:
        _drop_http_blobs(snap)
        snap["listings"] = []
        snap["loaded"] = False
        snap["src_rev"] = -1
        snap["rev"] = -1
    rev = int(snap.get("rev") or 0)
    cities = _catalog_cities(view_city)
    facebook = snap.get("facebook") if "facebook" in snap else _marketplace_links(view_city)
    if since is not None and since == rev:
        return {
            "rev": rev,
            "unchanged": True,
            "live": True,
            "warming": bool(snap.get("warming")),
            "listings": [],
            "stats": {},
            "barrios": [],
            "cities": cities,
            "usd_ars": snap.get("usd_ars") or _usd,
            "last_run": snap.get("last_run") or _last_run,
            "facebook": facebook,
        }
    body = {
        "rev": rev,
        "unchanged": False,
        "live": True,
        "warming": bool(snap.get("warming")),
        "listings": snap.get("listings") or [],
        "stats": snap.get("stats") or {},
        "barrios": [],
        "cities": cities,
        "usd_ars": snap.get("usd_ars") or _usd,
        "last_run": snap.get("last_run") or _last_run,
        "facebook": facebook,
    }
    return body


def warming_payload(city: str | None = None) -> dict:
    view_city = resolve_city(city) if city else None
    return _warming_payload(view_city)


def _warming_payload(view_city: str | None) -> dict:
    return {
        "rev": _rev,
        "unchanged": False,
        "live": True,
        "warming": True,
        "listings": [],
        "stats": {},
        "barrios": [],
        "cities": _catalog_cities(view_city),
        "usd_ars": _usd,
        "last_run": _last_run,
        "facebook": _marketplace_links(view_city),
    }


def _schedule_persist(city_id: str) -> None:
    if not city_id or city_id == "*":
        return
    if _disk_dir() is None:
        return
    if _force_cache_dir is not None:
        _encode_snap(city_id)
        return
    with _lock:
        if city_id in _persist_queued:
            return
        _persist_queued.add(city_id)

    def _job() -> None:
        try:
            with _lock:
                snap = _city_snaps.get(city_id)
                if snap:
                    snap = {
                        "rev": snap.get("rev") or 0,
                        "listings": list(snap.get("listings") or []),
                        "stats": snap.get("stats") or {},
                        "cities": list(snap.get("cities") or []),
                        "usd_ars": snap.get("usd_ars"),
                        "last_run": snap.get("last_run") or "",
                        "facebook": list(snap.get("facebook") or []),
                    }
            if snap:
                _write_disk(city_id, snap)
        finally:
            with _lock:
                _persist_queued.discard(city_id)

    threading.Thread(target=_job, daemon=True, name=f"city-cache-{city_id}").start()


def _schedule_encode(city_id: str) -> None:
    if not city_id or city_id == "*":
        return
    with _lock:
        if city_id in _encode_queued:
            return
        snap = _city_snaps.get(city_id)
        if not snap:
            return
        if not snap.get("listings") and not snap.get("loaded"):
            return
        if snap.get("encoded") and int(snap.get("enc_rev") or -1) == int(snap.get("rev") or 0):
            return
        if snap.get("encoded") and time.time() - _encode_at.get(city_id, 0) < 20:
            return
        _encode_queued.add(city_id)
        _encode_at[city_id] = time.time()

    if _force_cache_dir is not None or os.environ.get("PROPMAP_TEST") == "1":
        try:
            _encode_snap(city_id)
        finally:
            with _lock:
                _encode_queued.discard(city_id)
        return

    def _job() -> None:
        try:
            _encode_snap(city_id)
        finally:
            with _lock:
                _encode_queued.discard(city_id)

    threading.Thread(target=_job, daemon=True, name=f"city-encode-{city_id}").start()


def _rss_kb() -> int:
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        return 0
    return 0


def _evict_old_snaps_locked(keep: str | None = None, priority: set[str] | None = None) -> None:
    if len(_city_snaps) <= MAX_RAM_SNAPS:
        return
    hold = set(priority or ())
    if keep:
        hold.add(keep)
    extra = [cid for cid in list(_city_snaps) if cid not in hold and cid != "*"]
    while extra and len(_city_snaps) > MAX_RAM_SNAPS:
        cid = extra.pop()
        snap = _city_snaps.pop(cid, None)
        for row in (snap or {}).get("listings") or []:
            lid = row.get("id")
            if lid:
                _by_id.pop(lid, None)


def _preload_disk() -> None:
    global _disk_preloaded
    with _lock:
        if _disk_preloaded:
            return
        _disk_preloaded = True


def _watch_ram() -> None:
    if os.environ.get("PROPMAP_TEST") == "1":
        return
    while True:
        time.sleep(20)
        try:
            if busy_building():
                continue
            rss = _rss_kb()
            if rss < RAM_SOFT_KB:
                continue
            from .geo import DEFAULT_CITY

            hold = {DEFAULT_CITY}
            try:
                hold.update(_priority_city_ids()[:2])
            except Exception:
                pass
            got = _lock.acquire(timeout=0.4)
            if not got:
                continue
            try:
                if _loading:
                    continue
                log.warning("RAM alta (%s MB), suelto dicts de avisos; el JSON queda en disco", rss // 1024)
                hold.update(_loading)
                keep = {cid: _city_snaps[cid] for cid in hold if cid in _city_snaps}
                if "*" in _city_snaps:
                    keep["*"] = _city_snaps["*"]
                _city_snaps.clear()
                _city_snaps.update(keep)
                _by_id.clear()
                for snap in _city_snaps.values():
                    snap["listings"] = []
                    if snap.get("encoded_gzip"):
                        snap.pop("encoded", None)
                    if snap.get("pins_gzip"):
                        snap.pop("pins_encoded", None)
            finally:
                _lock.release()
            gc.collect()
        except Exception:
            log.exception("ram-watch")


def _disk_listing_count(path: Path) -> int:
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 1200))
            tail = fh.read()
    except OSError:
        return 0
    hit = _TOTAL_TAIL.search(tail)
    return int(hit.group(1)) if hit else 0


def _write_disk(city_id: str, snap: dict[str, Any]) -> None:
    folder = _disk_dir()
    if folder is None:
        return
    listings = snap.get("listings") or []
    if not listings:
        return
    blob = {
        "rev": snap.get("rev") or 0,
        "listings": listings,
        "stats": snap.get("stats") or {},
        "cities": snap.get("cities") or [],
        "usd_ars": snap.get("usd_ars") or _usd,
        "last_run": snap.get("last_run") or "",
        "facebook": snap.get("facebook") or [],
        "saved_at": time.time(),
        "snap_ver": SNAP_VER,
        "geo_ver": GEO_VERSION,
        "score_ver": SCORE_VERSION,
    }
    tmp = folder / f".{city_id}.tmp"
    path = folder / f"{city_id}.json"
    if path.exists():
        old_n = _disk_listing_count(path)
        new_n = len(listings)
        peeked = _disk_peek_deals(city_id)
        meta = _read_meta(city_id) or {}
        legacy_unscored = (not str(meta.get("score_ver") or "")) and old_n >= 8 and int(peeked or 0) <= 0
        if (
            not legacy_unscored
            and str(snap.get("geo_ver") or GEO_VERSION) == GEO_VERSION
            and old_n >= 8
            and new_n < old_n * 0.5
            and new_n < old_n - 4
        ):
            log.warning("no piso cache de %s (%s → %s avisos)", city_id, old_n, new_n)
            return
    try:
        tmp.write_text(json.dumps(blob, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        log.exception("no pude guardar cache de ciudad %s", city_id)
        try:
            tmp.unlink()
        except OSError:
            pass


def _disk_meta_readable(city_id: str, meta: dict[str, Any] | None) -> bool:
    if not meta:
        return False
    if str(meta.get("snap_ver") or "") not in SNAP_READ_VERS:
        return False
    if str(meta.get("geo_ver") or "") != GEO_VERSION:
        return False
    return _meta_scores_ok(city_id, meta)


def _read_disk_gzip(city_id: str, *, allow_stale: bool = False, allow_incomplete: bool = False) -> bytes | None:
    folder = _disk_dir()
    if folder is None or not city_id or city_id == "*":
        return None
    gz = _gz_path(folder, city_id)
    try:
        if not (gz.is_file() and gz.stat().st_size > 40):
            return None
        if time.time() - gz.stat().st_mtime > CITY_CACHE_TTL_SEC and not allow_stale:
            return None
        meta = _read_meta(city_id, allow_stale=allow_stale or allow_incomplete)
        if allow_incomplete:
            if not meta or not meta.get("warming"):
                return None
            if int(meta.get("n") or 0) < PIN_FLUSH_FIRST:
                return None
            if not _disk_meta_readable(city_id, meta):
                return None
        elif not _disk_http_meta_ok(city_id, meta):
            return None
        return gz.read_bytes()
    except OSError:
        return None


def _read_disk_bytes(city_id: str, *, allow_stale: bool = False, allow_incomplete: bool = False) -> bytes | None:
    folder = _disk_dir()
    if folder is None or not city_id or city_id == "*":
        return None
    meta = _read_meta(city_id, allow_stale=allow_stale or allow_incomplete)
    if allow_incomplete:
        if not meta or not meta.get("warming"):
            return None
        if int(meta.get("n") or 0) < PIN_FLUSH_FIRST:
            return None
        if not _disk_meta_readable(city_id, meta):
            return None
    elif allow_stale and not _disk_http_meta_ok(city_id, meta):
        return None
    path = _json_path(folder, city_id)
    try:
        if path.is_file() and (
            allow_stale or time.time() - path.stat().st_mtime <= CITY_CACHE_TTL_SEC
        ):
            raw = path.read_bytes()
            if raw and not (b'"listings":[]' in raw[:160]) and _snap_ver_ok_bytes(raw):
                return raw
    except OSError:
        pass
    gz = _gz_path(folder, city_id)
    try:
        if gz.is_file() and (
            allow_stale or time.time() - gz.stat().st_mtime <= CITY_CACHE_TTL_SEC
        ):
            raw = gzip_mod.decompress(gz.read_bytes())
            if _snap_ver_ok_bytes(raw):
                return raw
    except OSError:
        pass
    return None


def _read_disk(city_id: str) -> dict[str, Any] | None:
    raw = _read_disk_bytes(city_id)
    if not raw:
        folder = _disk_dir()
        if folder is None or not city_id or city_id == "*":
            return None
        path = _json_path(folder, city_id)
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
    else:
        try:
            parsed = json_loads(raw)
        except (ValueError, json.JSONDecodeError):
            return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("listings"), list):
        return None
    saved = float(parsed.get("saved_at") or 0)
    meta = _read_meta(city_id)
    if not saved and meta:
        saved = float(meta.get("saved_at") or 0)
    if saved and time.time() - saved > CITY_CACHE_TTL_SEC:
        return None
    ver = str(parsed.get("snap_ver") or (meta or {}).get("snap_ver") or "")
    if ver and ver not in SNAP_READ_VERS:
        return None
    if meta and not _disk_snap_complete(meta):
        return None
    if not _parsed_scores_ok(parsed, meta):
        return None
    parsed["src_rev"] = -1
    return parsed


def _priority_city_ids() -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()

    def add(raw: str | None) -> None:
        cid = resolve_city(raw) if raw else ""
        if not cid or cid in seen or cid in {"fuera", "otros"}:
            return
        seen.add(cid)
        ids.append(cid)

    from .geo import DEFAULT_CITY

    add(DEFAULT_CITY)
    try:
        from .places import priority_place_ids

        for cid in priority_place_ids():
            add(cid)
    except Exception:
        pass
    try:
        from .schedule import searched_ids

        for cid in searched_ids():
            add(cid)
    except Exception:
        pass
    return ids


def _put_locked(item: Listing, *, bump: bool = True, public: dict[str, Any] | None = None) -> None:
    _by_id[item.id] = public if public is not None else item.to_public_dict()
    if bump:
        _bump_locked(item.id)
    else:
        _item_rev.setdefault(item.id, _rev)


def _bump_locked(listing_id: str) -> None:
    global _rev
    _rev += 1
    _item_rev[listing_id] = _rev


def _rev_bump_locked() -> None:
    global _rev
    _rev += 1


def _refresh_meta(force: bool = False) -> None:
    """El catálogo de ciudades no cambia por aviso: recalcularlo en cada ingest
    deja el candado tomado y el watchdog mata el proceso."""
    global _last_run, _usd, _meta_at
    from .places import listed_cities
    from . import store

    if not force and os.environ.get("PROPMAP_TEST") != "1":
        now = time.monotonic()
        with _meta_gate:
            if now - _meta_at < META_REFRESH_SEC:
                return
            _meta_at = now
    try:
        cities = listed_cities([])
        last_run = store.get_meta("last_run", timeout=0.4) or ""
        usd = _read_rate()
    except Exception:
        return
    with _lock:
        _cities[:] = cities
        _last_run = last_run or _last_run
        _usd = usd


def _refresh_meta_locked() -> None:
    return


def _catalog_cities(view_city: str | None = None) -> list[dict]:
    from .places import is_cache_artifact_id

    return [
        row
        for row in list(_cities)
        if row.get("id") and not is_cache_artifact_id(str(row.get("id")))
    ]


def rewrite_snap_cities() -> None:
    """Actualiza el desplegable en RAM. No recomprime los JSON de avisos."""
    from .places import listed_cities

    cities = listed_cities([])
    with _lock:
        _cities[:] = cities
        for snap in _city_snaps.values():
            snap["cities"] = list(cities)


def _catalog_keep_ids() -> list[str]:
    from .geo import DEFAULT_CITY

    ids: list[str] = []
    seen: set[str] = set()

    def add(raw: str | None) -> None:
        cid = resolve_city(raw) if raw else ""
        if not cid or cid in seen or cid in {"fuera", "otros", "argentina"}:
            return
        seen.add(cid)
        ids.append(cid)

    add(DEFAULT_CITY)
    try:
        from .places import priority_place_ids

        for cid in priority_place_ids():
            add(cid)
    except Exception:
        pass
    return ids


def _snap_behind_db(city_id: str) -> bool:
    if os.environ.get("PROPMAP_TEST") == "1":
        return False
    meta = _read_meta(city_id, allow_stale=True) or {}
    try:
        from .store import city_listing_counts

        db_n = int(city_listing_counts().get(city_id) or 0)
    except Exception:
        return False
    if db_n < MIN_TRUSTED_SNAP:
        return False
    if meta.get("warming") and int(meta.get("n") or 0) >= PIN_FLUSH_FIRST:
        return False
    recorded = int(meta.get("db_n") or 0)
    if recorded > 0:
        return db_n > recorded + max(25, recorded // 12)
    if meta.get("db_loaded") and int(meta.get("n") or 0) >= TINY_SNAP:
        return False
    snap_n = int(meta.get("n") or 0)
    if snap_n <= 0:
        snap = _city_snaps.get(city_id) or {}
        snap_n = len(snap.get("listings") or [])
    return db_n > snap_n + max(25, snap_n // 12)


def _keep_city(city_id: str) -> bool:
    if not city_id:
        return False
    from .geo import DEFAULT_CITY

    if (
        os.environ.get("PROPMAP_TEST") != "1"
        and city_id != DEFAULT_CITY
        and _sqlite_city_n(city_id) < MIN_TRUSTED_SNAP
    ):
        return False
    with _lock:
        if city_id in _loading:
            return False
    behind = _snap_behind_db(city_id)
    if _disk_http_ready(city_id) and not behind:
        folder = _disk_dir()
        if folder is not None:
            try:
                if not _gz_path(folder, city_id).is_file():
                    _ensure_disk_gzip(city_id)
                    _ensure_disk_pins(city_id)
            except OSError:
                pass
        return False
    request_city_bytes(city_id, refresh=behind)
    return True


def _hydrate_default() -> None:
    from .geo import DEFAULT_CITY

    try:
        _hydrate_ram_gzip(DEFAULT_CITY)
    except Exception:
        log.exception("no pude hidratar %s", DEFAULT_CITY)


def _hydrate_ram_gzip(city_id: str) -> None:
    """Deja el gzip en RAM para que el GET no lea 10 MB del disco."""
    packed = _read_disk_gzip(city_id, allow_stale=True)
    pins = None
    folder = _disk_dir()
    if folder is not None:
        try:
            pz = _pins_gz_path(folder, city_id)
            if pz.is_file() and pz.stat().st_size > 40:
                pins = pz.read_bytes()
        except OSError:
            pins = None
    if not packed and not pins:
        return
    meta = _read_meta(city_id, allow_stale=True) or {}
    with _lock:
        snap = _city_snaps.get(city_id)
        if snap and snap.get("encoded_gzip") and not snap.get("warming"):
            return
        snap = dict(snap or {})
        if packed:
            snap["encoded_gzip"] = packed
        if pins:
            snap["pins_gzip"] = pins
        snap["geo_ver"] = str(meta.get("geo_ver") or GEO_VERSION)
        snap["snap_ver"] = str(meta.get("snap_ver") or SNAP_VER)
        snap["score_ver"] = str(meta.get("score_ver") or SCORE_VERSION)
        snap["warming"] = False
        snap["loaded"] = True
        if meta.get("rev") is not None:
            try:
                snap["rev"] = int(meta["rev"])
            except (TypeError, ValueError):
                pass
        _city_snaps[city_id] = snap
        _evict_old_snaps_locked(city_id)
    log.info("cache HTTP de %s en RAM (%s KB)", city_id, (len(packed or b"") + len(pins or b"")) // 1024)


def keep_catalog_cached() -> None:
    """Deja el gzip de cada ciudad del desplegable listo. No corre en el GET."""
    global _catalog_keep_fp
    ids = _catalog_keep_ids()
    testing = os.environ.get("PROPMAP_TEST") == "1"
    for i, cid in enumerate(ids):
        if cities_loading() or busy_building():
            if i < HYDRATE_RAM:
                _hydrate_ram_gzip(cid)
            break
        started = _keep_city(cid)
        if not started and i < HYDRATE_RAM:
            _hydrate_ram_gzip(cid)
        if started:
            if not testing:
                time.sleep(KEEP_CITY_GAP_SEC)
            break
    fp = ",".join(ids)
    prev = _catalog_keep_fp
    _catalog_keep_fp = fp
    if prev and prev != fp:
        try:
            rewrite_snap_cities()
        except Exception:
            log.exception("no pude actualizar ciudades en el cache")


def _with_view_city(cities: list[dict], view_city: str | None) -> list[dict]:
    from .geo import CABA_IDS, CITIES, DEFAULT_CITY
    from .places import _listed_row_is_city, is_cache_artifact_id, public_place

    rows = list(cities or [])
    if view_city and is_cache_artifact_id(view_city):
        return rows
    if view_city and not any(row.get("id") == view_city for row in rows):
        cfg = CITIES.get(view_city) or {}
        if view_city not in CABA_IDS and view_city != DEFAULT_CITY and not _listed_row_is_city(view_city, cfg):
            return rows
        try:
            extra = public_place(view_city)
        except Exception:
            extra = {"id": view_city, "label": view_city.replace("-", " ").title()}
        extra["n"] = int(extra.get("n") or 0)
        rows.append(extra)
    return rows


def _wanted_ids(view_city: str | None) -> set[str] | None:
    if not view_city:
        return None
    return related_place_ids(view_city) | (same_place_ids(view_city) or set())


def _scoped(view_city: str | None) -> list[dict]:
    return _scoped_rows(list(_by_id.values()), view_city)


def _scoped_rows(rows: list[dict], view_city: str | None) -> list[dict]:
    wanted = _wanted_ids(view_city)
    out: list[dict] = []
    for i, row in enumerate(rows):
        if i and i % 80 == 0:
            time.sleep(0)
        city = row.get("city") or ""
        search = row.get("search_city") or ""
        if row.get("duplicate_of"):
            continue
        if wanted is not None and city not in wanted and search not in wanted:
            continue
        if view_city and not public_row_fits_city(row, view_city):
            continue
        if view_city and city != view_city and wanted is not None and city in wanted:
            if place_token(city).startswith(place_token(view_city) + " "):
                pass
            else:
                row = dict(row)
                row["city"] = view_city
        out.append(row)
    return out


def _ensure_snap(view_city: str | None, since: int | None) -> dict:
    """Arma el JSON de una ciudad copiando RAM bajo candado y filtrando afuera."""
    key = view_city or "*"
    with _lock:
        snap = _city_snaps.get(key)
        src_rev = _rev
        if snap and snap.get("listings") is not None and snap.get("src_rev") == src_rev:
            return _serve_snap_locked(snap, view_city, since)
        if snap and (snap.get("warming") or key in _loading) and (
            snap.get("listings") or snap.get("encoded") or snap.get("encoded_gzip")
        ):
            return _serve_snap_locked(snap, view_city, since)
        rows = list(_by_id.values())
        usd = _usd
        last_run = _last_run
        cities = _catalog_cities(view_city)
        facebook = _marketplace_links(view_city)
    listings = _scoped_rows(rows, view_city)
    if view_city and len(listings) < MIN_TRUSTED_SNAP and _snap_behind_db(view_city):
        request_city_bytes(view_city, refresh=True)
        with _lock:
            current = _city_snaps.get(key)
            if current and _ram_http_ok(current, view_city):
                return _serve_snap_locked(current, view_city, since)
        return _warming_payload(view_city)
    snap = {
        "rev": src_rev,
        "listings": listings,
        "stats": _stats(listings),
        "cities": cities,
        "usd_ars": usd,
        "last_run": last_run,
        "facebook": facebook,
        "src_rev": src_rev,
        "geo_ver": GEO_VERSION,
        "snap_ver": SNAP_VER,
        "score_ver": SCORE_VERSION,
    }
    with _lock:
        current = _city_snaps.get(key)
        if current and current.get("listings") is not None:
            if current.get("src_rev") == _rev:
                return _serve_snap_locked(current, view_city, since)
            # Mientras carga la DB, un snap más chico puede ser parcial. Ya listo,
            # un rebuild más chico es un filtro (p. ej. avisos de otra ciudad).
            if not _ready and len(current.get("listings") or []) > len(listings):
                return _serve_snap_locked(current, view_city, since)
        snap["rev"] = _rev
        snap["src_rev"] = _rev
        _city_snaps[key] = snap
        _schedule_encode(key)
        return _serve_snap_locked(snap, view_city, since)


def _payload_locked(view_city: str | None, since: int | None) -> dict:
    """Solo sirve un snap ya armado. Nunca filtrar _by_id con el candado tomado."""
    key = view_city or "*"
    snap = _city_snaps.get(key)
    if snap:
        return _serve_snap_locked(snap, view_city, since)
    return _warming_payload(view_city)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return float(statistics.median(values))


def _stats(listings: list[dict]) -> dict:
    def usable(row: dict) -> bool:
        return not row.get("exclude_from_comps")

    by_barrio: dict[tuple[str, str], list[dict]] = defaultdict(list)
    by_zona: dict[str, list[dict]] = defaultdict(list)
    by_type: dict[str, list[dict]] = defaultdict(list)
    for row in listings:
        by_barrio[(row.get("city") or "", row.get("barrio") or "Sin clasificar")].append(row)
        by_zona[row.get("zona") or "Sin clasificar"].append(row)
        by_type[row.get("property_type") or ""].append(row)

    def block(groups: dict, with_city: bool = False) -> list[dict]:
        out = []
        for key, rows in groups.items():
            name = key[1] if with_city else key
            usd = [r["price_usd"] for r in rows if r.get("price_usd") and usable(r)]
            m2 = [r["price_m2"] for r in rows if r.get("price_m2") and usable(r)]
            rec = {
                "name": name,
                "count": len(rows),
                "median_usd": round(_median(usd) or 0),
                "median_m2": round(_median(m2) or 0),
                "min_usd": round(min(usd)) if usd else None,
                "max_usd": round(max(usd)) if usd else None,
                "deals": sum(1 for r in rows if r.get("deal_label") == "oportunidad"),
            }
            if with_city:
                rec["city"] = key[0]
            out.append(rec)
        out.sort(key=lambda r: (-r["count"], r["name"]))
        return out

    usd_all = [r["price_usd"] for r in listings if r.get("price_usd") and usable(r)]
    m2_all = [r["price_m2"] for r in listings if r.get("price_m2") and usable(r)]
    myields = [float(r["monthly_yield_pct"]) for r in listings if r.get("monthly_yield_pct")]
    tyields = [float(r["temporal_yield_pct"]) for r in listings if r.get("temporal_yield_pct")]
    return {
        "total": len(listings),
        "deals": sum(1 for r in listings if r.get("deal_label") == "oportunidad"),
        "median_usd": round(_median(usd_all) or 0),
        "median_m2": round(_median(m2_all) or 0),
        "by_barrio": block(by_barrio, with_city=True),
        "by_zona": block(by_zona),
        "by_type": block(by_type),
        "rent_comps_monthly": 0,
        "rent_comps_nightly": 0,
        "median_monthly_rent": 0,
        "median_nightly": 0,
        "median_monthly_yield": round(_median(myields) or 0, 1),
        "median_temporal_yield": round(_median(tyields) or 0, 1),
        "occupancy_pct": 0,
        "recommended": sum(1 for r in listings if (r.get("rental_score") or 0) >= 62),
    }


def _marketplace_links(city_id: str | None) -> list[dict]:
    if not city_id or city_id in {"fuera", "otros"}:
        return []
    from .geo import CITIES

    cfg = CITIES.get(city_id) or {}
    label = cfg.get("label") or city_id.replace("-", " ").title()
    query = quote(f"departamento venta {label}")
    return [
        {
            "label": f"Marketplace · {label}",
            "url": f"https://www.facebook.com/marketplace/search/?query={query}",
        }
    ]
