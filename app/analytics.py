from __future__ import annotations

import json
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from threading import Lock, Thread
from urllib.parse import parse_qs, urlparse

from starlette.responses import Response

from . import store

_rate: dict[str, list[float]] = {}
_rate_lock = Lock()
_recent: dict[tuple[str, str, str], float] = {}
_prune_at = 0.0
MAX_PER_MIN = 80
KEEP_DAYS = 90
WRITE_WAIT_SEC = 0.05
PRUNE_EVERY_SEC = 6 * 3600
COOKIE = "propmap_vid"
OWN_HOSTS = {"", "127.0.0.1", "localhost"}
PIXEL_GIF = bytes.fromhex(
    "47494638396101000100800000ffffff00000021f90401000000002c000000000100010000020144003b"
)


def visitor_from_request(request, hinted: str = "") -> str:
    raw = (hinted or request.cookies.get(COOKIE) or "").strip()
    if raw:
        return raw[:40]
    return uuid.uuid4().hex


def stamp_cookie(response: Response, vid: str) -> None:
    if not vid:
        return
    response.set_cookie(
        COOKIE,
        vid,
        max_age=365 * 24 * 3600,
        httponly=False,
        samesite="lax",
        path="/",
    )


def record_later(payload: dict, ua: str = "", header_ref: str = "") -> None:
    """La portada no puede esperar el candado de sqlite (geo/LLM lo ocupan minutos)."""
    Thread(target=_record_safe, args=(payload, ua, header_ref), daemon=True, name="visit").start()


def _record_safe(payload: dict, ua: str = "", header_ref: str = "") -> None:
    try:
        record(payload, ua=ua, header_ref=header_ref)
    except Exception:
        pass


def record(payload: dict, ua: str = "", header_ref: str = "") -> None:
    store.init()
    name = str(payload.get("n") or payload.get("name") or "pageview")[:40]
    if name not in {"pageview", "place", "listing", "tab"}:
        name = "pageview"
    vid = str(payload.get("vid") or "")[:40]
    if not _allow(vid or "anon"):
        return
    path = str(payload.get("p") or payload.get("path") or "/")[:180]
    if _dup(vid, name, path):
        return
    source = _source(payload, header_ref)
    city = str(payload.get("city") or "")[:80]
    extra = {k: payload.get(k) for k in ("q", "listing", "tab", "utm") if payload.get(k)}
    extra["source"] = source
    now = datetime.now(timezone.utc).isoformat()
    got = store._write.acquire(timeout=WRITE_WAIT_SEC)
    if not got:
        return
    try:
        with store.connect() as conn:
            conn.execute(
                """
                INSERT INTO visits (seen_at, name, path, referrer, city, vid, ua_kind, extra_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    name,
                    path,
                    source,
                    city,
                    vid,
                    _ua_kind(ua),
                    json.dumps(extra, ensure_ascii=False),
                ),
            )
            global _prune_at
            tick = time.time()
            if tick - _prune_at >= PRUNE_EVERY_SEC:
                cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat()
                conn.execute("DELETE FROM visits WHERE seen_at < ?", (cutoff,))
                _prune_at = tick
            conn.commit()
    except Exception:
        return
    finally:
        store._write.release()


def summary(days: int = 14) -> dict:
    store.init()
    since = (datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 90)))).isoformat()
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT seen_at, name, path, referrer, city, vid, ua_kind FROM visits WHERE seen_at >= ?",
            (since,),
        ).fetchall()
    pages = Counter()
    places = Counter()
    refs = Counter()
    names = Counter()
    devices = Counter()
    days_c = Counter()
    uniques: dict[str, set[str]] = {}
    pageviews = 0
    for row in rows:
        names[row["name"]] += 1
        pages[row["path"] or "/"] += 1
        if row["name"] == "pageview":
            pageviews += 1
        if row["city"]:
            places[row["city"]] += 1
        refs[row["referrer"] or "directo"] += 1
        devices[row["ua_kind"] or "otro"] += 1
        day = str(row["seen_at"])[:10]
        days_c[day] += 1
        uniques.setdefault(day, set()).add(row["vid"] or row["seen_at"])
    visitors = len({row["vid"] for row in rows if row["vid"]})
    return {
        "events": len(rows),
        "pageviews": pageviews,
        "visitors": visitors,
        "days": [
            {"day": day, "events": days_c[day], "visitors": len(uniques.get(day) or [])}
            for day in sorted(days_c)
        ],
        "pages": _top(pages),
        "places": _top(places),
        "referrers": _top(refs),
        "events_by_name": _top(names),
        "devices": _top(devices),
    }


def _top(counter: Counter, n: int = 12) -> list[dict]:
    return [{"name": key, "count": val} for key, val in counter.most_common(n)]


def _allow(vid: str) -> bool:
    now = time.time()
    with _rate_lock:
        hits = [t for t in _rate.get(vid, []) if now - t < 60]
        if len(hits) >= MAX_PER_MIN:
            _rate[vid] = hits
            return False
        hits.append(now)
        _rate[vid] = hits
        return True


def _dup(vid: str, name: str, path: str) -> bool:
    key = (vid or "anon", name, path)
    now = time.time()
    prev = _recent.get(key, 0)
    if now - prev < 5:
        return True
    _recent[key] = now
    if len(_recent) > 4000:
        stale = [item for item, ts in _recent.items() if now - ts > 30]
        for item in stale:
            _recent.pop(item, None)
    return False


def _source(payload: dict, header_ref: str = "") -> str:
    utm = str(payload.get("utm") or "").strip().lower()[:80]
    if not utm:
        query = str(payload.get("q") or "").lstrip("?")
        params = parse_qs(query)
        utm = ((params.get("utm_source") or params.get("ref") or [""])[0] or "").lower()[:80]
    if utm:
        return utm
    host = _clean_ref(payload.get("r") or header_ref)
    return host or "directo"


def _clean_ref(raw: str) -> str:
    text = str(raw or "").strip()[:300]
    if not text:
        return ""
    try:
        parsed = urlparse(text)
        host = (parsed.netloc or parsed.path or "").lower()
        if ":" in host and host.split(":")[0] in OWN_HOSTS:
            return ""
        if host in OWN_HOSTS:
            return ""
        return host.split(":")[0]
    except Exception:
        return ""


def _ua_kind(ua: str) -> str:
    low = (ua or "").lower()
    if any(token in low for token in ("iphone", "android", "mobile", "ipad")):
        return "celular"
    if "tablet" in low:
        return "tablet"
    return "computadora"
