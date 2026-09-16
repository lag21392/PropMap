from __future__ import annotations

import math
import statistics
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from .geo import default_city
from .models import Listing
from .places import public_place
from .scoring import PRICE_BOUNDS, excluded_from_comps

try:
    from zoneinfo import ZoneInfo

    _AR = ZoneInfo("America/Argentina/Buenos_Aires")
except Exception:
    _AR = timezone.utc

TYPES = ("departamento", "casa", "ph", "terreno")
TYPE_LABEL = {
    "departamento": "Deptos",
    "casa": "Casas",
    "ph": "PH",
    "terreno": "Terrenos",
    "all": "Todos",
}
NEW_HOURS = 14 * 24
DROP_PCT = 0.03
DROP_USD = 800.0
PRICE_TICK_PCT = 0.01
CACHE_TTL_SEC = 45.0
_PAYLOAD_CACHE: dict[tuple, tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()
PRICE_TICK_USD = 200.0
COHORT_DAYS = 21
# A tick this far from the listing's real cluster is a typo, not a baja.
_OUTLIER_RATIO = 8.0
_POWER10_SLACK = 0.12
_WILD_LO = 0.2
_WILD_HI = 1.5


def _now() -> datetime:
    return datetime.now(_AR)


def today() -> str:
    return _now().date().isoformat()


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = str(raw).strip().replace("Z", "+00:00")
    if len(text) >= 5 and text[-5] in "+-" and text[-3] != ":":
        text = f"{text[:-2]}:{text[-2:]}"
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(_AR)


def _cohort_trend(items: list[Listing]) -> tuple[str, float | None]:
    cut = _now() - timedelta(days=COHORT_DAYS)
    recent, older = [], []
    for item in items:
        if excluded_from_comps(item) or not item.price_m2:
            continue
        when = _parse_dt(item.published_at)
        if not when:
            continue
        (recent if when >= cut else older).append(item.price_m2)
    if len(recent) < 6 or len(older) < 6:
        return "new", None
    return _trend(_median(recent), _median(older))


def _median(values: list[float]) -> float | None:
    clean = sorted(v for v in values if v and v > 0)
    if not clean:
        return None
    if len(clean) == 1:
        return float(clean[0])
    if len(clean) >= 8:
        lo = int(len(clean) * 0.15)
        hi = int(len(clean) * 0.85) or len(clean)
        clean = clean[lo:hi] or clean
    return float(statistics.median(clean))


def _changed(old: float | None, new: float | None) -> bool:
    if new is None:
        return False
    if old is None:
        return True
    if abs(new - old) < 1:
        return False
    gap = abs(new - old)
    return gap >= PRICE_TICK_USD or gap / max(old, 1) >= PRICE_TICK_PCT


def ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY,
            listing_id TEXT NOT NULL,
            seen_at TEXT NOT NULL,
            price_usd REAL,
            price_m2 REAL,
            deal_label TEXT,
            deal_score REAL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_price_history_listing ON price_history(listing_id, seen_at)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_snapshots (
            day TEXT NOT NULL,
            city TEXT NOT NULL,
            property_type TEXT NOT NULL,
            median_m2 REAL,
            median_usd REAL,
            listing_count INTEGER,
            deals INTEGER,
            PRIMARY KEY (day, city, property_type)
        )
        """
    )


def _last_point(conn, listing_id: str) -> dict | None:
    row = conn.execute(
        """
        SELECT seen_at, price_usd, price_m2, deal_label, deal_score
        FROM price_history
        WHERE listing_id = ?
        ORDER BY seen_at DESC, id DESC
        LIMIT 1
        """,
        (listing_id,),
    ).fetchone()
    return dict(row) if row else None


def _insert_point(conn, item: Listing, seen_at: str) -> None:
    extra = item.extra or {}
    conn.execute(
        """
        INSERT INTO price_history (listing_id, seen_at, price_usd, price_m2, deal_label, deal_score)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            item.id,
            seen_at,
            item.price_usd,
            item.price_m2,
            item.deal_label or "",
            extra.get("deal_score"),
        ),
    )


def sync_listing_prices(listings: list[Listing]) -> None:
    from . import store

    if not listings:
        return
    now = _now().isoformat()
    with store._write:
        with store.connect() as conn:
            ensure_tables(conn)
            for item in listings:
                if item.price_usd is None:
                    continue
                last = _last_point(conn, item.id)
                if last and not _changed(last.get("price_usd"), item.price_usd):
                    continue
                _insert_point(conn, item, now)
            conn.commit()


def seed_existing(listings: list[Listing] | None = None) -> None:
    from . import store

    items = listings if listings is not None else []
    if not items:
        return
    fallback = (_now() - timedelta(days=30)).isoformat()
    with store._write:
        with store.connect() as conn:
            ensure_tables(conn)
            seeded = conn.execute("SELECT DISTINCT listing_id FROM price_history").fetchall()
            have = {row[0] for row in seeded}
            for item in items:
                if item.id in have or item.price_usd is None:
                    continue
                when = _parse_dt(item.published_at)
                seen = when.isoformat() if when else fallback
                _insert_point(conn, item, seen)
            conn.commit()
    store.set_meta("price_track_seed", "1")


def take_snapshots(listings: list[Listing]) -> None:
    from . import store

    if not listings:
        return
    day = today()
    groups: dict[tuple[str, str], list[Listing]] = {}
    for item in listings:
        city = item.city or default_city()
        groups.setdefault((city, "all"), []).append(item)
        if item.property_type in TYPES:
            groups.setdefault((city, item.property_type), []).append(item)
    with store._write:
        with store.connect() as conn:
            ensure_tables(conn)
            for (city, kind), rows in groups.items():
                usable = [i for i in rows if not excluded_from_comps(i)]
                usd = [i.price_usd for i in usable if i.price_usd]
                m2 = [i.price_m2 for i in usable if i.price_m2]
                conn.execute(
                    """
                    INSERT OR REPLACE INTO market_snapshots
                    (day, city, property_type, median_m2, median_usd, listing_count, deals)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        day,
                        city,
                        kind,
                        round(_median(m2) or 0),
                        round(_median(usd) or 0),
                        len(rows),
                        sum(1 for i in rows if i.deal_label == "oportunidad"),
                    ),
                )
            conn.commit()


def ensure_ready(listings: list[Listing] | None = None, *, seed: bool = True) -> None:
    from . import store

    store.init()
    with store.connect() as conn:
        ensure_tables(conn)
        conn.commit()
    if seed and store.get_meta("price_track_seed") != "1":
        seed_existing(listings)


def reset_cache() -> None:
    with _CACHE_LOCK:
        _PAYLOAD_CACHE.clear()


def _price_history_rows(conn, listing_ids: list[str]) -> list:
    if not listing_ids:
        return []
    out: list = []
    for i in range(0, len(listing_ids), 400):
        chunk = listing_ids[i : i + 400]
        marks = ",".join("?" * len(chunk))
        out.extend(
            conn.execute(
                f"""
                SELECT listing_id, seen_at, price_usd, price_m2, deal_label, deal_score
                FROM price_history
                WHERE listing_id IN ({marks})
                ORDER BY listing_id, seen_at ASC, id ASC
                """,
                chunk,
            ).fetchall()
        )
    return out


def _row_usd(row: dict[str, Any] | None) -> float | None:
    if not row:
        return None
    raw = row.get("price_usd")
    if raw is None:
        return None
    try:
        usd = float(raw)
    except (TypeError, ValueError):
        return None
    return usd if usd > 0 else None


def _wild_absolute(usd: float, property_type: str | None) -> bool:
    lo, hi = PRICE_BOUNDS.get(property_type or "", (5_000, 8_000_000))
    return usd < lo * _WILD_LO or usd > hi * _WILD_HI


def _power10_typo(a: float, b: float) -> bool:
    if a <= 0 or b <= 0:
        return False
    ratio = max(a, b) / min(a, b)
    if ratio < _OUTLIER_RATIO:
        return False
    log10 = math.log10(ratio)
    nearest = round(log10)
    return nearest >= 1 and abs(log10 - nearest) <= _POWER10_SLACK


def _cluster_ref(usds: list[float], property_type: str | None) -> float | None:
    vals = [v for v in usds if v > 0]
    if not vals:
        return None
    sane = [v for v in vals if not _wild_absolute(v, property_type)]
    pool = sane or vals
    best: list[float] = []
    for v in pool:
        group = [x for x in pool if 0.25 <= x / v <= 4]
        if len(group) > len(best):
            best = group
    return statistics.median(best) if best else None


def _is_bogus_tick(usd: float, ref: float, property_type: str | None) -> bool:
    if usd <= 0 or ref <= 0:
        return False
    ratio = max(usd, ref) / min(usd, ref)
    if ratio < _OUTLIER_RATIO:
        return False
    if _wild_absolute(usd, property_type):
        return True
    return _power10_typo(usd, ref) and ratio >= 50


def mark_price_outliers(
    points: list,
    current_usd: float | None = None,
    property_type: str | None = None,
) -> list[dict[str, Any]]:
    """Tag typo ticks (extra zeros, prices outside type bounds) without dropping them from the list."""
    tagged = [dict(row) for row in points]
    usds = [u for u in (_row_usd(p) for p in tagged) if u]
    if current_usd:
        try:
            cur = float(current_usd)
        except (TypeError, ValueError):
            cur = 0.0
        if cur > 0:
            usds.append(cur)
    ref = _cluster_ref(usds, property_type)
    for row in tagged:
        usd = _row_usd(row)
        row["outlier"] = bool(usd and ref and _is_bogus_tick(usd, ref, property_type))
    return tagged


def history_payload(
    listing_id: str,
    points: list,
    current_usd: float | None = None,
    property_type: str | None = None,
) -> dict[str, Any]:
    tagged = mark_price_outliers(points, current_usd=current_usd, property_type=property_type)
    usable = [p for p in tagged if _row_usd(p) and not p.get("outlier")]
    change_pct = None
    if len(usable) >= 2:
        first, last = float(usable[0]["price_usd"]), float(usable[-1]["price_usd"])
        change_pct = round((last - first) / first * 100, 1)
    return {
        "id": listing_id,
        "points": tagged,
        "change_pct": change_pct,
        "outlier_n": sum(1 for p in tagged if p.get("outlier")),
    }


def listing_history(listing_id: str) -> dict[str, Any]:
    from . import store

    ensure_ready()
    with store.connect() as conn:
        ensure_tables(conn)
        rows = conn.execute(
            """
            SELECT seen_at, price_usd, price_m2, deal_label, deal_score
            FROM price_history
            WHERE listing_id = ?
            ORDER BY seen_at ASC, id ASC
            """,
            (listing_id,),
        ).fetchall()
    points = [
        {
            "seen_at": row["seen_at"],
            "price_usd": row["price_usd"],
            "price_m2": row["price_m2"],
            "deal_label": row["deal_label"] or "",
            "deal_score": row["deal_score"],
        }
        for row in rows
    ]
    item = store.get_listing(listing_id)
    return history_payload(
        listing_id,
        points,
        current_usd=item.price_usd if item else None,
        property_type=item.property_type if item else None,
    )


def _card(item: Listing, extra: dict | None = None) -> dict[str, Any]:
    data = {
        "id": item.id,
        "title": item.title,
        "property_type": item.property_type,
        "price_usd": item.price_usd,
        "price_m2": item.price_m2,
        "covered_m2": item.covered_m2,
        "barrio": item.barrio,
        "address": item.address,
        "deal_label": item.deal_label,
        "deal_score": (item.extra or {}).get("deal_score"),
        "vs_barrio_pct": item.vs_barrio_pct,
        "city": item.city,
        "source": item.source,
    }
    if extra:
        data.update(extra)
    return data


def _drop_from_points(
    points: list,
    current_usd: float | None = None,
    property_type: str | None = None,
) -> dict[str, Any] | None:
    priced = [dict(row) for row in points if row.get("price_usd")]
    if current_usd:
        last_seen = priced[-1]["seen_at"] if priced else None
        if not priced or abs(float(priced[-1]["price_usd"]) - float(current_usd)) >= 1:
            priced = [*priced, {"price_usd": current_usd, "seen_at": last_seen or ""}]
    priced = mark_price_outliers(priced, current_usd=current_usd, property_type=property_type)
    usable = [row for row in priced if _row_usd(row) and not row.get("outlier")]
    if len(usable) < 2:
        return None
    peak = max(usable, key=lambda row: float(row["price_usd"]))
    last = usable[-1]
    old_usd = float(peak["price_usd"])
    new_usd = float(last["price_usd"])
    if new_usd >= old_usd:
        return None
    gap = old_usd - new_usd
    pct = gap / old_usd
    if pct < PRICE_TICK_PCT and gap < PRICE_TICK_USD:
        return None
    return {
        "old_usd": old_usd,
        "new_usd": new_usd,
        "change_pct": round(-pct * 100, 1),
        "seen_at": last.get("seen_at"),
    }


def price_drops(pool: list[Listing], grouped: dict[str, list]) -> list[dict[str, Any]]:
    drops = []
    for item in pool:
        found = _drop_from_points(
            grouped.get(item.id) or [],
            item.price_usd,
            item.property_type,
        )
        if found:
            drops.append(_card(item, found))
    drops.sort(key=lambda r: (r.get("change_pct") or 0, -(r.get("old_usd") or 0)))
    return drops


def relevant_deals(pool: list[Listing], grouped: dict[str, list], cutoff: datetime) -> list[dict[str, Any]]:
    highlights = []
    for item in pool:
        if excluded_from_comps(item) or not item.price_m2:
            continue
        vs = item.vs_barrio_pct or 0
        if item.deal_label != "oportunidad" and vs < 8:
            continue
        score = (item.extra or {}).get("deal_score") or 0
        published = _parse_dt(item.published_at)
        hist_pts = grouped.get(item.id) or []
        first = _parse_dt(hist_pts[0]["seen_at"] if hist_pts else None)
        born = published or first
        fresh = bool(born and born >= cutoff)
        strong = item.deal_label == "oportunidad" or vs >= 18 or score >= 80
        highlights.append(
            _card(
                item,
                {
                    "first_seen": (born.isoformat() if born else None),
                    "fresh": fresh,
                    "strong": strong,
                },
            )
        )
    highlights.sort(
        key=lambda r: (
            r.get("deal_label") != "oportunidad",
            not r.get("fresh"),
            not r.get("strong"),
            -(r.get("vs_barrio_pct") or 0),
            -(r.get("deal_score") or 0),
        )
    )
    return highlights


def _trend(latest: float | None, previous: float | None) -> tuple[str, float | None]:
    if not latest or not previous:
        return "new", None
    delta = (latest - previous) / previous * 100
    if abs(delta) < 0.8:
        return "flat", round(delta, 1)
    return ("up" if delta > 0 else "down"), round(delta, 1)


def _headline(
    city_label: str,
    kind: str,
    trend: str,
    delta: float | None,
    latest: dict | None,
    deals: int = 0,
    source: str = "",
) -> str:
    what = TYPE_LABEL.get(kind or "all", "Avisos")
    if not latest or not latest.get("median_m2"):
        return f"{city_label}: todavía no hay suficiente USD/m² para armar la curva."
    median = f"USD {int(latest['median_m2']):,}/m²".replace(",", ".")
    deal_bit = f" · {deals} oportunidad{'es' if deals != 1 else ''}" if deals else ""
    if trend == "new" or delta is None:
        return f"{what} en {city_label} · mediana {median}{deal_bit}. La curva de suba/baja se completa con cada búsqueda."
    verb = "subió" if trend == "up" else "bajó" if trend == "down" else "se mantuvo"
    pct = f"{abs(delta):.1f}%".replace(".", ",")
    window = "los avisos nuevos vs los más viejos" if source == "cohort" else "el último registro"
    if trend == "flat":
        return f"{what} en {city_label} · el m² se mantuvo ({median}){deal_bit}."
    tone = "el mercado se encareció" if trend == "up" else "el mercado se abarató"
    return f"{what} en {city_label} · el m² {verb} {pct} según {window} · {tone} ({median}){deal_bit}."


def _yield_block(items: list[Listing], city: str) -> dict[str, Any]:
    from . import store
    from .yields import yield_stats

    try:
        comps = store.fetch_rentals(city)
    except Exception:
        comps = []
    return yield_stats(items, comps=comps)


def market_payload(city: str, property_type: str = "") -> dict[str, Any]:
    city = city or default_city()
    kind = property_type if property_type in TYPES else "all"
    key = (city, kind)
    now = time.time()
    with _CACHE_LOCK:
        hit = _PAYLOAD_CACHE.get(key)
        if hit and now - hit[0] < CACHE_TTL_SEC:
            return hit[1]
    data = _build_market(city, kind)
    with _CACHE_LOCK:
        _PAYLOAD_CACHE[key] = (now, data)
        if len(_PAYLOAD_CACHE) > 24:
            oldest = min(_PAYLOAD_CACHE, key=lambda item: _PAYLOAD_CACHE[item][0])
            _PAYLOAD_CACHE.pop(oldest, None)
    return data


def _build_market(city: str, kind: str) -> dict[str, Any]:
    from . import store
    from .http_timing import note

    t0 = time.perf_counter()
    from .listings_cache import market_items
    from .geo import same_place_ids
    from .place_tags import related_place_ids

    items = market_items(city)
    source = "snap"
    if not items:
        wanted = related_place_ids(city) | (same_place_ids(city) or {city})
        items = store.fetch_by_cities(wanted)
        source = "sql"
    note("market.fetch", (time.perf_counter() - t0) * 1000, {"city": city, "n": len(items), "src": source})
    ensure_ready(items, seed=False)
    place = public_place(city)
    city_label = place.get("label") or city

    t1 = time.perf_counter()
    ids = [item.id for item in items if item.id]
    with store.connect() as conn:
        ensure_tables(conn)
        series_rows = conn.execute(
            """
            SELECT day, median_m2, median_usd, listing_count, deals
            FROM market_snapshots
            WHERE city = ? AND property_type = ?
            ORDER BY day ASC
            """,
            (city, kind),
        ).fetchall()
        type_rows = conn.execute(
            """
            SELECT property_type, day, median_m2, listing_count, deals
            FROM market_snapshots
            WHERE city = ? AND property_type != 'all'
            ORDER BY day ASC
            """,
            (city,),
        ).fetchall()
        hist = _price_history_rows(conn, ids)
    note("market.sql", (time.perf_counter() - t1) * 1000, {"city": city, "ids": len(ids)})

    series = [
        {
            "day": row["day"],
            "median_m2": row["median_m2"],
            "median_usd": row["median_usd"],
            "count": row["listing_count"],
            "deals": row["deals"],
        }
        for row in series_rows
    ]
    latest = series[-1] if series else None
    previous = series[-2] if len(series) >= 2 else None
    week = None
    if series:
        cut = (datetime.fromisoformat(series[-1]["day"]) - timedelta(days=7)).date().isoformat()
        older = [row for row in series if row["day"] <= cut]
        week = older[-1] if older else series[0] if len(series) > 1 else None
    trend, delta = _trend(latest["median_m2"] if latest else None, previous["median_m2"] if previous else None)
    week_trend, week_delta = _trend(
        latest["median_m2"] if latest else None,
        week["median_m2"] if week and week is not latest else None,
    )
    cohort_trend, cohort_delta = _cohort_trend(
        [i for i in items if kind == "all" or i.property_type == kind]
    )
    if week_delta is not None:
        used_trend, used_delta, used_source = week_trend, week_delta, "week"
    elif delta is not None:
        used_trend, used_delta, used_source = trend, delta, "day"
    else:
        used_trend, used_delta, used_source = cohort_trend, cohort_delta, "cohort"

    by_type_latest: dict[str, list[dict]] = {}
    for row in type_rows:
        by_type_latest.setdefault(row["property_type"], []).append(
            {"day": row["day"], "median_m2": row["median_m2"], "count": row["listing_count"], "deals": row["deals"]}
        )
    by_type = []
    for type_id, points in by_type_latest.items():
        last = points[-1]
        prev = points[-2] if len(points) >= 2 else None
        t, d = _trend(last.get("median_m2"), prev.get("median_m2") if prev else None)
        by_type.append(
            {
                "property_type": type_id,
                "label": TYPE_LABEL.get(type_id, type_id),
                "median_m2": last.get("median_m2"),
                "count": last.get("count"),
                "deals": last.get("deals"),
                "trend": t,
                "delta_pct": d,
            }
        )
    by_type.sort(key=lambda r: TYPES.index(r["property_type"]) if r["property_type"] in TYPES else 9)

    grouped: dict[str, list] = {}
    for row in hist:
        grouped.setdefault(row["listing_id"], []).append(dict(row))

    cutoff = _now() - timedelta(hours=NEW_HOURS)
    pool = [i for i in items if kind == "all" or i.property_type == kind]
    mix = {"oportunidad": 0, "bueno": 0, "mercado": 0, "caro": 0}
    for item in pool:
        if item.deal_label in mix:
            mix[item.deal_label] += 1
    drops = price_drops(pool, grouped)
    highlights = relevant_deals(pool, grouped, cutoff)
    deals_n = mix["oportunidad"]

    return {
        "city": city,
        "city_label": city_label,
        "property_type": kind,
        "headline": _headline(city_label, kind, used_trend, used_delta, latest, deals_n, used_source),
        "trend": used_trend,
        "delta_pct": used_delta,
        "trend_source": used_source,
        "latest": latest,
        "previous": previous,
        "series": series,
        "by_type": by_type,
        "mix": mix,
        "drops": drops[:8],
        "new_deals": highlights[:8],
        "tracked": sum(1 for i in items if i.price_usd),
        "history_n": sum(1 for item in pool if grouped.get(item.id)),
        "days": len(series),
        "yields": _yield_block(items, city),
    }
