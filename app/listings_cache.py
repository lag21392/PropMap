from __future__ import annotations

import json
import logging
import os
import statistics
import threading
import time
from collections import defaultdict
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import quote

from .geo import public_row_fits_city, resolve_city, same_place_ids
from .models import Listing
from .scoring import USD_FALLBACK, apply_unit_price

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
_disk_preloaded = False
_force_cache_dir: Path | None = None
CITY_CACHE_TTL_SEC = 48 * 3600


def reset() -> None:
    global _rev, _ready, _warming, _usd, _last_run, _disk_preloaded, _force_cache_dir
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
        _disk_preloaded = False
        _force_cache_dir = None


def cache_ready() -> bool:
    with _lock:
        return _ready


def current_rev() -> int:
    return _rev


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


def start_warmup() -> None:
    global _warming
    with _lock:
        if _ready or _warming:
            return
        _warming = True
    threading.Thread(target=_warm_places, daemon=True, name="places-cache").start()
    threading.Thread(target=_warm, daemon=True, name="listings-cache").start()


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
    with _lock:
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
            apply_unit_price(item, rate)
            _put_locked(item)
            if item.city:
                affected.add(resolve_city(item.city) or item.city)
        if dropped:
            _rev_bump_locked()
        if _ready:
            _refresh_meta_locked()
        _usd = rate
        rebuilt = _ready
        patched_rows = [_by_id[item.id] for item in listings if item.id in _by_id]
        drop_ids = [item.id for item in listings if item.id not in _by_id]
        for cid in affected:
            if rebuilt or cid not in _city_snaps:
                _city_snaps.pop(cid, None)
                _city_snaps.pop("*", None)
                _payload_locked(cid, None)
            else:
                _upsert_snap_rows_locked(cid, patched_rows, drop_ids=drop_ids)
                _schedule_persist(cid)


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
        fits = wanted is None or city in wanted
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
    snap.pop("encoded", None)
    _rev_bump_locked()
    snap["rev"] = _rev
    snap["src_rev"] = _rev


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
    with _lock:
        if _ready:
            data = _payload_locked(view_city, since)
        else:
            snap = _city_snaps.get(key)
            data = _serve_snap_locked(snap, view_city, since) if snap else None
    if data is None:
        loaded = _read_disk(key)
        if loaded:
            with _lock:
                _city_snaps.setdefault(key, loaded)
                data = _serve_snap_locked(_city_snaps[key], view_city, since)
        else:
            return _warming_payload(view_city)
    _attach_encoded(key, data)
    return data


def disk_response_bytes(city: str | None) -> bytes | None:
    if _ready:
        return None
    view_city = resolve_city(city) if city else None
    if not view_city:
        return None
    snap = _city_snaps.get(view_city)
    if snap and snap.get("encoded"):
        return snap["encoded"]
    return _read_disk_bytes(view_city)


def _attach_encoded(key: str, data: dict[str, Any]) -> None:
    if data.get("unchanged") or data.get("warming"):
        return
    with _lock:
        snap = _city_snaps.get(key)
        encoded = snap.get("encoded") if snap else None
    if encoded:
        data["encoded"] = encoded
        return
    body = {k: v for k, v in data.items() if k != "encoded"}
    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with _lock:
        snap = _city_snaps.get(key)
        if snap is not None and "encoded" not in snap:
            snap["encoded"] = encoded
            encoded = snap["encoded"]
        elif snap is not None:
            encoded = snap.get("encoded") or encoded
    data["encoded"] = encoded


def _warm() -> None:
    global _ready, _warming, _usd, _last_run
    try:
        from . import store
        from .geo import same_place_ids
        from .places import listed_cities, load_custom_places

        store.init()
        load_custom_places()
        _preload_disk()
        ids = _priority_city_ids()
        rate = _read_rate()
        for cid in ids:
            with _lock:
                have = bool((_city_snaps.get(cid) or {}).get("listings"))
            if have:
                log.info("cache de ciudad listo desde disco: %s", cid)
                time.sleep(0.02)
                continue
            wanted = same_place_ids(cid) or {cid}
            log.info("precargando avisos de %s", cid)
            _ingest_rows(store.fetch_by_cities(wanted), rate)
            with _lock:
                _payload_locked(cid, None)
            time.sleep(0.05)
        cities = listed_cities([])
        last_run = store.get_meta("last_run") or ""
        with _lock:
            _cities[:] = cities
            _usd = rate
            _last_run = last_run
    except Exception:
        log.exception("listings cache warmup failed")
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
            _refresh_meta_locked()


def _encode_snap(key: str) -> None:
    with _lock:
        snap = _city_snaps.get(key)
        if not snap or snap.get("encoded") or not snap.get("listings"):
            return
        view_city = None if key == "*" else key
        body = {
            "rev": int(snap.get("rev") or 0),
            "unchanged": False,
            "live": True,
            "warming": False,
            "listings": snap.get("listings") or [],
            "stats": snap.get("stats") or {},
            "barrios": [],
            "cities": snap.get("cities") or list(_cities),
            "usd_ars": snap.get("usd_ars") or _usd,
            "last_run": snap.get("last_run") or _last_run,
            "facebook": snap.get("facebook") if "facebook" in snap else _marketplace_links(view_city),
        }
        rev = body["rev"]
    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with _lock:
        snap = _city_snaps.get(key)
        if snap is not None and int(snap.get("rev") or 0) == rev:
            snap["encoded"] = encoded


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
    rev = int(snap.get("rev") or 0)
    cities = snap.get("cities") or _with_view_city(list(_cities), view_city)
    facebook = snap.get("facebook") if "facebook" in snap else _marketplace_links(view_city)
    if since is not None and since == rev:
        return {
            "rev": rev,
            "unchanged": True,
            "live": True,
            "warming": False,
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
        "warming": False,
        "listings": snap.get("listings") or [],
        "stats": snap.get("stats") or {},
        "barrios": [],
        "cities": cities,
        "usd_ars": snap.get("usd_ars") or _usd,
        "last_run": snap.get("last_run") or _last_run,
        "facebook": facebook,
    }
    return body


def _warming_payload(view_city: str | None) -> dict:
    cities = list(_cities)
    if view_city and not any(row.get("id") == view_city for row in cities):
        cities = list(cities) + [{"id": view_city, "label": view_city.replace("-", " ").title()}]
    return {
        "rev": _rev,
        "unchanged": False,
        "live": True,
        "warming": True,
        "listings": [],
        "stats": {},
        "barrios": [],
        "cities": cities,
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
        with _lock:
            snap = _city_snaps.get(city_id)
        if snap:
            _write_disk(city_id, snap)
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


def _write_disk(city_id: str, snap: dict[str, Any]) -> None:
    folder = _disk_dir()
    if folder is None:
        return
    blob = {
        "rev": snap.get("rev") or 0,
        "listings": snap.get("listings") or [],
        "stats": snap.get("stats") or {},
        "cities": snap.get("cities") or [],
        "usd_ars": snap.get("usd_ars") or _usd,
        "last_run": snap.get("last_run") or "",
        "facebook": snap.get("facebook") or [],
        "saved_at": time.time(),
    }
    tmp = folder / f".{city_id}.tmp"
    path = folder / f"{city_id}.json"
    try:
        tmp.write_text(json.dumps(blob, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        log.exception("no pude guardar cache de ciudad %s", city_id)
        try:
            tmp.unlink()
        except OSError:
            pass


def _read_disk_bytes(city_id: str) -> bytes | None:
    folder = _disk_dir()
    if folder is None or not city_id or city_id == "*":
        return None
    path = folder / f"{city_id}.json"
    try:
        if time.time() - path.stat().st_mtime > CITY_CACHE_TTL_SEC:
            return None
        return path.read_bytes()
    except OSError:
        return None


def _read_disk(city_id: str) -> dict[str, Any] | None:
    folder = _disk_dir()
    if folder is None or not city_id or city_id == "*":
        return None
    path = folder / f"{city_id}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("listings"), list):
        return None
    saved = float(raw.get("saved_at") or 0)
    if saved and time.time() - saved > CITY_CACHE_TTL_SEC:
        return None
    raw["src_rev"] = -1
    return raw


def _priority_city_ids() -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()

    def add(raw: str | None) -> None:
        cid = resolve_city(raw) if raw else ""
        if not cid or cid in seen or cid in {"fuera", "otros"}:
            return
        seen.add(cid)
        ids.append(cid)

    try:
        from .places import priority_place_ids

        for cid in priority_place_ids():
            add(cid)
    except Exception:
        pass
    from .geo import DEFAULT_CITY

    add(DEFAULT_CITY)
    try:
        from .schedule import searched_ids

        for cid in searched_ids():
            add(cid)
    except Exception:
        pass
    folder = _disk_dir()
    if folder is not None:
        try:
            files = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            files = []
        for path in files[:12]:
            add(path.stem)
    return ids


def _preload_disk() -> None:
    global _disk_preloaded
    with _lock:
        if _disk_preloaded:
            return
        _disk_preloaded = True
    for cid in _priority_city_ids():
        loaded = _read_disk(cid)
        if not loaded:
            continue
        with _lock:
            _city_snaps.setdefault(cid, loaded)
        time.sleep(0.02)


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


def _refresh_meta_locked() -> None:
    global _last_run, _usd
    from .places import listed_cities

    class _Row:
        def __init__(self, row: dict):
            self.city = row.get("city") or ""
            self.lat = row.get("lat")
            self.lon = row.get("lon")

    _cities[:] = listed_cities([_Row(row) for row in _by_id.values()])
    from . import store

    _last_run = store.get_meta("last_run") or _last_run
    _usd = _read_rate()


def _with_view_city(cities: list[dict], view_city: str | None) -> list[dict]:
    from .places import public_place

    rows = list(cities or [])
    if view_city and not any(row.get("id") == view_city for row in rows):
        try:
            rows.append(public_place(view_city))
        except Exception:
            rows.append({"id": view_city, "label": view_city.replace("-", " ").title()})
    return rows


def _wanted_ids(view_city: str | None) -> set[str] | None:
    if not view_city:
        return None
    return same_place_ids(view_city)


def _scoped(view_city: str | None) -> list[dict]:
    wanted = _wanted_ids(view_city)
    rows = []
    for row in _by_id.values():
        city = row.get("city") or ""
        if row.get("duplicate_of"):
            continue
        if wanted is not None and city not in wanted:
            continue
        if view_city and not public_row_fits_city(row, view_city):
            continue
        if view_city and city != view_city:
            row = dict(row)
            row["city"] = view_city
        rows.append(row)
    return rows


def _payload_locked(view_city: str | None, since: int | None) -> dict:
    key = view_city or "*"
    snap = _city_snaps.get(key)
    if not snap or snap.get("src_rev") != _rev:
        listings = _scoped(view_city)
        snap = {
            "rev": _rev,
            "listings": listings,
            "stats": _stats(listings),
            "cities": _with_view_city(list(_cities), view_city),
            "usd_ars": _usd,
            "last_run": _last_run,
            "facebook": _marketplace_links(view_city),
            "src_rev": _rev,
        }
        _city_snaps[key] = snap
        _schedule_persist(key)
    return _serve_snap_locked(snap, view_city, since)


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
