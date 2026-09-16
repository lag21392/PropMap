from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, local

from .models import Listing
from .text_quality import address_quality, title_quality

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "listings.sqlite"
_write = Lock()
_counts_lock = Lock()
_tls = local()
_counts_memo: dict[str, int] = {}
_counts_at = 0.0
COUNTS_TTL_SEC = 25.0
# 4 MB por hilo. -80000 (80 MB) × el pool de uvicorn se iba a varios GB y no volvía.
CACHE_KB = 4000
EDIT_FIELDS = (
    "title", "price", "currency", "address", "covered_m2", "total_m2",
    "bedrooms", "bathrooms", "description", "property_type",
)


class _TlsConn:
    """`with connect()` commitea o hace rollback; no cierra. Cerrar reciclaba 80 MB de caché."""

    __slots__ = ("_conn",)

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __enter__(self) -> sqlite3.Connection:
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        return False

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def close(self) -> None:
        try:
            self._conn.close()
        finally:
            if getattr(_tls, "conn", None) is self._conn:
                _tls.conn = None
                _tls.key = None


def _open_conn(timeout: float = 10) -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute(f"PRAGMA cache_size=-{CACHE_KB}")
    conn.execute("PRAGMA busy_timeout=8000")
    conn.execute("PRAGMA wal_autocheckpoint=1000")
    return conn


def connect() -> _TlsConn:
    key = str(DB_PATH)
    conn = getattr(_tls, "conn", None)
    if conn is not None and getattr(_tls, "key", None) != key:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        conn = None
    if conn is not None:
        try:
            conn.execute("SELECT 1")
        except sqlite3.Error:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            conn = None
    if conn is None:
        conn = _open_conn()
        _tls.conn = conn
        _tls.key = key
    return _TlsConn(conn)


def init() -> None:
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS listings (
                id TEXT PRIMARY KEY,
                source TEXT,
                source_id TEXT,
                url TEXT,
                title TEXT,
                property_type TEXT,
                price REAL,
                currency TEXT,
                price_usd REAL,
                address TEXT,
                barrio TEXT,
                zona TEXT,
                lat REAL,
                lon REAL,
                covered_m2 REAL,
                total_m2 REAL,
                rooms INTEGER,
                bedrooms INTEGER,
                bathrooms REAL,
                parking INTEGER,
                age_years INTEGER,
                image TEXT,
                publisher TEXT,
                description TEXT,
                published_at TEXT,
                price_m2 REAL,
                score REAL,
                deal_label TEXT,
                vs_barrio_pct REAL,
                fingerprint TEXT,
                extra_json TEXT,
                scraped_at TEXT,
                has_exact_location INTEGER DEFAULT 0,
                city TEXT DEFAULT 'caba',
                quality_score REAL,
                quality_label TEXT,
                details_scraped INTEGER DEFAULT 0,
                needs_llm INTEGER NOT NULL DEFAULT 0,
                is_hidden INTEGER NOT NULL DEFAULT 0,
                llm_ready INTEGER NOT NULL DEFAULT 0,
                llm_ver INTEGER NOT NULL DEFAULT 0,
                llm_partial INTEGER NOT NULL DEFAULT 0,
                llm_await INTEGER NOT NULL DEFAULT 0,
                llm_fix INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ops_minute (
                minute INTEGER NOT NULL,
                metric TEXT NOT NULL,
                n INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (minute, metric)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pins (
                listing_id TEXT PRIMARY KEY,
                favorite INTEGER DEFAULT 0,
                notes TEXT DEFAULT '',
                contacted INTEGER DEFAULT 0
            )
            """
        )
        conn.commit()
        cols = {row[1] for row in conn.execute("PRAGMA table_info(listings)").fetchall()}
        if "has_exact_location" not in cols:
            conn.execute("ALTER TABLE listings ADD COLUMN has_exact_location INTEGER DEFAULT 0")
        if "city" not in cols:
            conn.execute("ALTER TABLE listings ADD COLUMN city TEXT DEFAULT 'caba'")
        if "quality_score" not in cols:
            conn.execute("ALTER TABLE listings ADD COLUMN quality_score REAL")
        if "quality_label" not in cols:
            conn.execute("ALTER TABLE listings ADD COLUMN quality_label TEXT")
        if "details_scraped" not in cols:
            conn.execute("ALTER TABLE listings ADD COLUMN details_scraped INTEGER DEFAULT 0")
        _ensure_listing_query_columns(conn)
        pin_cols = {row[1] for row in conn.execute("PRAGMA table_info(pins)").fetchall()}
        if "contacted" not in pin_cols:
            conn.execute("ALTER TABLE pins ADD COLUMN contacted INTEGER DEFAULT 0")
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rental_comps (
                id TEXT PRIMARY KEY,
                source TEXT,
                source_id TEXT,
                url TEXT,
                title TEXT,
                property_type TEXT,
                price REAL,
                currency TEXT,
                price_usd REAL,
                period TEXT,
                address TEXT,
                barrio TEXT,
                zona TEXT,
                lat REAL,
                lon REAL,
                covered_m2 REAL,
                bedrooms INTEGER,
                city TEXT,
                extra_json TEXT,
                scraped_at TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rentals_city ON rental_comps(city, period, property_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_listings_city ON listings(city)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_listings_city_price ON listings(city, price_usd)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_listings_source ON listings(source)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_listings_details_scraped ON listings(details_scraped)"
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_listings_needs_llm
            ON listings(details_scraped, scraped_at DESC)
            WHERE needs_llm = 1 AND is_hidden = 0
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_listings_need_details
            ON listings(city, scraped_at DESC)
            WHERE details_scraped = 0 AND is_hidden = 0
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS visits (
                id INTEGER PRIMARY KEY,
                seen_at TEXT NOT NULL,
                name TEXT NOT NULL,
                path TEXT,
                referrer TEXT,
                city TEXT,
                vid TEXT,
                ua_kind TEXT,
                extra_json TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_visits_seen ON visits(seen_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_visits_name ON visits(name, city)")
        conn.commit()
    from .accounts import init_tables

    init_tables()


def replace_portal_listings(source: str, listings: list[Listing]) -> None:
    upsert_listings(listings, delete_source=source)


def _merge_existing(item: Listing, row: sqlite3.Row) -> None:
    old_extra = json.loads(row["extra_json"] or "{}")
    if len(item.description or "") < len(row["description"] or ""):
        item.description = row["description"] or ""
    if not item.image and row["image"]:
        item.image = row["image"]
    if address_quality(row["address"] or "") > address_quality(item.address or ""):
        item.address = row["address"] or ""
    if title_quality(row["title"] or "") > title_quality(item.title or ""):
        item.title = row["title"] or item.title
    old_exact = bool(row["has_exact_location"] if "has_exact_location" in row.keys() else 0)
    approx_portal = (item.source or "").lower() in {"properati"}
    from .geo import foreign_locality, parse_street

    extra = dict(item.extra or {})
    street, number = parse_street(
        " ".join(p for p in (item.title, item.address, item.description, extra.get("intersection")) if p)
    )
    keep_old = (
        old_exact
        and not item.has_exact_location
        and row["lat"] is not None
        and not approx_portal
        and not (street and number)
        and not extra.get("intersection")
    )
    if keep_old and item.city not in {"fuera", "otros"} and not foreign_locality(item, item.city or ""):
        item.lat = row["lat"]
        item.lon = row["lon"]
        item.has_exact_location = True
        item.barrio = row["barrio"] or item.barrio
        item.zona = row["zona"] or item.zona
    if not item.covered_m2 and row["covered_m2"]:
        item.covered_m2 = row["covered_m2"]
    if not item.total_m2 and row["total_m2"]:
        item.total_m2 = row["total_m2"]
    if item.bedrooms is None and row["bedrooms"] is not None:
        item.bedrooms = row["bedrooms"]
    if item.bathrooms is None and row["bathrooms"] is not None:
        item.bathrooms = row["bathrooms"]
    if item.parking is None and row["parking"] is not None:
        item.parking = row["parking"]
    if item.age_years is None and row["age_years"] is not None:
        item.age_years = row["age_years"]
    if row["details_scraped"] and not item.details_scraped:
        item.details_scraped = True
    extra = {**old_extra, **(item.extra or {})}
    extra["amenities"] = list(
        dict.fromkeys((old_extra.get("amenities") or []) + ((item.extra or {}).get("amenities") or []))
    )
    for key in ("intersection", "between", "street", "street_number", "approx_address"):
        if not extra.get(key) and old_extra.get(key):
            extra[key] = old_extra[key]
    photos = list(old_extra.get("photos") or [])
    photos.extend((item.extra or {}).get("photos") or [])
    if row["image"]:
        photos.append(row["image"])
    if item.image:
        photos.append(item.image)
    extra["photos"] = list(dict.fromkeys(url for url in photos if url))[:24]
    if not extra.get("expenses"):
        extra["expenses"] = old_extra.get("expenses")
    if not extra.get("pdf_text"):
        extra["pdf_text"] = old_extra.get("pdf_text")
    if not extra.get("details_at"):
        extra["details_at"] = old_extra.get("details_at")
    old_edits = old_extra.get("user_edits") or {}
    new_edits = (item.extra or {}).get("user_edits") or {}
    if old_edits or new_edits:
        extra["user_edits"] = {**old_edits, **new_edits}
    if "contacted" in (item.extra or {}):
        extra["contacted"] = bool(item.extra.get("contacted"))
    elif old_extra.get("contacted"):
        extra["contacted"] = True
    item.extra = extra
    apply_user_edits(item)


def apply_user_edits(item: Listing) -> Listing:
    edits = ((item.extra or {}).get("user_edits") or {})
    casters = {
        "title": str,
        "currency": str,
        "address": str,
        "description": str,
        "property_type": str,
        "price": float,
        "covered_m2": float,
        "total_m2": float,
        "bedrooms": int,
        "bathrooms": float,
    }
    for key, caster in casters.items():
        if key in edits and edits[key] not in (None, ""):
            try:
                setattr(item, key, caster(edits[key]))
            except (TypeError, ValueError):
                pass
    return item


_QUERY_FLAG_COLS = (
    "needs_llm",
    "is_hidden",
    "llm_ready",
    "llm_ver",
    "llm_partial",
    "llm_await",
    "llm_fix",
)
_LLM_FLAG_META = "llm_flag_schema"


def _listing_query_flags(item: Listing) -> dict[str, int]:
    extra = item.extra or {}
    hidden = 1 if extra.get("duplicate_of") or extra.get("dedupe_hidden") else 0
    ready = 1 if extra.get("llm_ready") else 0
    try:
        ver = int(extra.get("llm_ver") or 0)
    except (TypeError, ValueError):
        ver = 0
    partial = 1 if extra.get("llm_partial") else 0
    await_llm = 1 if extra.get("await_llm") else 0
    fix = 1 if (extra.get("data_fixes") and not extra.get("llm_repair")) else 0
    city = (item.city or "").strip()
    unassigned = city in {"", "fuera", "otros", "argentina"}
    from .llm_enrich import LLM_SCHEMA

    if hidden:
        needs = 0
    elif unassigned and not extra.get("llm_city_ok"):
        needs = 1
    elif fix:
        needs = 1
    elif ver == LLM_SCHEMA and ready and not partial:
        needs = 0
    elif partial and ver == LLM_SCHEMA:
        needs = 0
    else:
        needs = 1
    return {
        "needs_llm": needs,
        "is_hidden": hidden,
        "llm_ready": ready,
        "llm_ver": ver,
        "llm_partial": partial,
        "llm_await": await_llm,
        "llm_fix": fix,
    }


def _ensure_listing_query_columns(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(listings)").fetchall()}
    added = False
    for name in _QUERY_FLAG_COLS:
        if name in cols:
            continue
        conn.execute(f"ALTER TABLE listings ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
        added = True
    from .llm_enrich import LLM_SCHEMA

    row = conn.execute("SELECT value FROM meta WHERE key = ?", (_LLM_FLAG_META,)).fetchone()
    marked = str(row[0]) if row else ""
    if added or marked != str(LLM_SCHEMA):
        _backfill_listing_query_flags(conn, LLM_SCHEMA)
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            (_LLM_FLAG_META, str(LLM_SCHEMA)),
        )


def _backfill_listing_query_flags(conn: sqlite3.Connection, schema: int) -> None:
    conn.execute(
        """
        UPDATE listings SET
          is_hidden = CASE
            WHEN IFNULL(json_extract(extra_json, '$.duplicate_of'), '') != ''
              OR IFNULL(json_extract(extra_json, '$.dedupe_hidden'), 0) != 0
            THEN 1 ELSE 0 END,
          llm_ready = IFNULL(CAST(json_extract(extra_json, '$.llm_ready') AS INTEGER), 0),
          llm_ver = IFNULL(CAST(json_extract(extra_json, '$.llm_ver') AS INTEGER), 0),
          llm_partial = IFNULL(CAST(json_extract(extra_json, '$.llm_partial') AS INTEGER), 0),
          llm_await = IFNULL(CAST(json_extract(extra_json, '$.await_llm') AS INTEGER), 0),
          llm_fix = CASE
            WHEN IFNULL(json_array_length(json_extract(extra_json, '$.data_fixes')), 0) > 0
             AND IFNULL(json_extract(extra_json, '$.llm_repair'), 0) = 0
            THEN 1 ELSE 0 END
        """
    )
    conn.execute(
        """
        UPDATE listings SET needs_llm = CASE
          WHEN is_hidden = 1 THEN 0
          WHEN IFNULL(city, '') IN ('', 'fuera', 'otros', 'argentina')
           AND IFNULL(json_extract(extra_json, '$.llm_city_ok'), 0) = 0 THEN 1
          WHEN llm_fix = 1 THEN 1
          WHEN llm_ver = ? AND llm_ready = 1 AND llm_partial = 0 THEN 0
          WHEN llm_partial = 1 AND llm_ver = ? THEN 0
          ELSE 1 END
        """,
        (int(schema), int(schema)),
    )


def upsert_listings(listings: list[Listing], delete_source: str | None = None, *, notify: bool = True) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _write:
        with connect() as conn:
            if delete_source:
                conn.execute("DELETE FROM listings WHERE source = ?", (delete_source,))
            for item in listings:
                row = conn.execute("SELECT * FROM listings WHERE id = ?", (item.id,)).fetchone()
                if row:
                    _merge_existing(item, row)
                data = item.to_dict()
                flags = _listing_query_flags(item)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO listings (
                        id, source, source_id, url, title, property_type, price, currency,
                        price_usd, address, barrio, zona, lat, lon, covered_m2, total_m2,
                        rooms, bedrooms, bathrooms, parking, age_years, image, publisher,
                        description, published_at, price_m2, score, deal_label, vs_barrio_pct,
                        fingerprint, extra_json, scraped_at, has_exact_location, city,
                        quality_score, quality_label, details_scraped,
                        needs_llm, is_hidden, llm_ready, llm_ver, llm_partial, llm_await, llm_fix
                    ) VALUES (
                        :id, :source, :source_id, :url, :title, :property_type, :price, :currency,
                        :price_usd, :address, :barrio, :zona, :lat, :lon, :covered_m2, :total_m2,
                        :rooms, :bedrooms, :bathrooms, :parking, :age_years, :image, :publisher,
                        :description, :published_at, :price_m2, :score, :deal_label, :vs_barrio_pct,
                        :fingerprint, :extra_json, :scraped_at, :has_exact_location, :city,
                        :quality_score, :quality_label, :details_scraped,
                        :needs_llm, :is_hidden, :llm_ready, :llm_ver, :llm_partial, :llm_await, :llm_fix
                    )
                    """,
                    {
                        **{k: data.get(k) for k in [
                            "id", "source", "source_id", "url", "title", "property_type", "price",
                            "currency", "price_usd", "address", "barrio", "zona", "lat", "lon",
                            "covered_m2", "total_m2", "rooms", "bedrooms", "bathrooms", "parking",
                            "age_years", "image", "publisher", "description", "published_at",
                            "price_m2", "score", "deal_label", "vs_barrio_pct", "fingerprint",
                            "city", "quality_score", "quality_label",
                        ]},
                        "extra_json": json.dumps(data.get("extra") or {}, ensure_ascii=False),
                        "scraped_at": now,
                        "has_exact_location": 1 if data.get("has_exact_location") else 0,
                        "details_scraped": 1 if data.get("details_scraped") else 0,
                        **flags,
                    },
                )
            conn.commit()
    from .market import sync_listing_prices

    sync_listing_prices(listings)
    if notify:
        _notify_listings(listings)


def upsert_one(item: Listing) -> None:
    replace_portal_listings(item.source, [item] + [x for x in all_listings() if x.source == item.source and x.id != item.id])


def save_manual(item: Listing) -> None:
    existing = [x for x in all_listings() if x.source == "manual"]
    existing = [x for x in existing if x.id != item.id]
    replace_portal_listings("manual", existing + [item])


def _pins_map(conn: sqlite3.Connection) -> dict:
    pins = {}
    try:
        for pin in conn.execute("SELECT * FROM pins").fetchall():
            pins[pin["listing_id"]] = pin
    except sqlite3.OperationalError:
        pass
    return pins


def _listing_from_row(row: sqlite3.Row, pin) -> Listing:
    extra = json.loads(row["extra_json"] or "{}")
    return Listing(
        source=row["source"],
        source_id=row["source_id"],
        url=row["url"],
        title=row["title"],
        property_type=row["property_type"],
        price=row["price"],
        currency=row["currency"],
        price_usd=row["price_usd"],
        address=row["address"] or "",
        barrio=row["barrio"] or "Sin clasificar",
        zona=row["zona"] or "Sin clasificar",
        lat=row["lat"],
        lon=row["lon"],
        covered_m2=row["covered_m2"],
        total_m2=row["total_m2"],
        rooms=row["rooms"],
        bedrooms=row["bedrooms"],
        bathrooms=row["bathrooms"],
        parking=row["parking"],
        age_years=row["age_years"],
        image=row["image"] or "",
        publisher=row["publisher"] or "",
        description=row["description"] or "",
        published_at=row["published_at"] or "",
        price_m2=row["price_m2"],
        score=row["score"],
        deal_label=row["deal_label"] or "",
        vs_barrio_pct=row["vs_barrio_pct"],
        fingerprint=row["fingerprint"] or "",
        extra=extra,
        has_exact_location=bool(row["has_exact_location"] if "has_exact_location" in row.keys() else 0),
        city=row["city"] if "city" in row.keys() and row["city"] else "caba",
        quality_score=row["quality_score"] if "quality_score" in row.keys() else None,
        quality_label=(row["quality_label"] if "quality_label" in row.keys() else "") or "",
        details_scraped=bool(row["details_scraped"] if "details_scraped" in row.keys() else 0),
        favorite=bool(pin["favorite"] if pin else 0),
        notes=(pin["notes"] if pin else "") or "",
        contacted=bool(
            (pin["contacted"] if pin and "contacted" in pin.keys() else 0)
            or extra.get("contacted")
        ),
    )


def all_listings() -> list[Listing]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM listings ORDER BY price_usd IS NULL, price_usd ASC").fetchall()
        pins = _pins_map(conn)
    items = []
    for i, row in enumerate(rows):
        items.append(_listing_from_row(row, pins.get(row["id"])))
        if os.environ.get("PROPMAP_TEST") != "1" and i % 80 == 0:
            time.sleep(0)
    for item in items:
        apply_user_edits(item)
    return items


def get_listing(listing_id: str) -> Listing | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
        if not row:
            return None
        pins = _pins_map(conn)
    item = _listing_from_row(row, pins.get(row["id"]))
    apply_user_edits(item)
    return item


def fetch_by_cities(cities: set[str] | list[str]) -> list[Listing]:
    wanted = [cid for cid in cities if cid]
    if not wanted:
        return []
    marks = ",".join("?" * len(wanted))
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM listings WHERE city IN ({marks}) ORDER BY price_usd IS NULL, price_usd ASC",
            tuple(wanted),
        ).fetchall()
        pins = _pins_map(conn)
    items = []
    for i, row in enumerate(rows):
        items.append(_listing_from_row(row, pins.get(row["id"])))
        if os.environ.get("PROPMAP_TEST") != "1" and i % 80 == 0:
            time.sleep(0)
    for item in items:
        apply_user_edits(item)
    return items


def fetch_for_city(city_id: str) -> list[Listing]:
    from .geo import listing_fits_city, same_place_ids
    from .place_tags import related_place_ids

    wanted = [cid for cid in (related_place_ids(city_id) | (same_place_ids(city_id) or {city_id or ""})) if cid]
    if not wanted:
        return []
    marks = ",".join("?" * len(wanted))
    sql = (
        f"SELECT * FROM listings WHERE city IN ({marks}) "
        "ORDER BY price_usd IS NULL, price_usd ASC"
    )
    with connect() as conn:
        rows = list(conn.execute(sql, tuple(wanted)).fetchall())
        pins = _pins_map(conn)
    items = []
    seen: set[str] = set()
    for i, row in enumerate(rows):
        item = _listing_from_row(row, pins.get(row["id"]))
        if item.id in seen:
            continue
        seen.add(item.id)
        items.append(item)
        if os.environ.get("PROPMAP_TEST") != "1" and i % 80 == 0:
            time.sleep(0)
    fitted: list[Listing] = []
    for i, item in enumerate(items):
        apply_user_edits(item)
        if os.environ.get("PROPMAP_TEST") != "1" and i % 20 == 0:
            time.sleep(0)
        if not listing_fits_city(item, city_id, remote=False, require_radius=True):
            continue
        if (item.city or "") not in wanted:
            item.city = city_id
        fitted.append(item)
    return fitted


def fetch_llm_backlog(limit: int, prefer_city: str = "", schema: int = 7) -> list[Listing]:
    """Avisos a enriquecer: primero lo nuevo, sin provincia, errores y sin ciudad/ubicación."""
    n = max(1, min(160, int(limit or 1)))
    prefer = (prefer_city or "").strip()
    sql = """
        SELECT * FROM listings
        WHERE needs_llm = 1 AND is_hidden = 0
        ORDER BY
          CASE WHEN details_scraped = 1
                 OR length(trim(IFNULL(description, ''))) >= 160
               THEN 0 ELSE 1 END,
          CASE WHEN scraped_at >= date('now', '-2 days') THEN 0 ELSE 1 END,
          CASE WHEN llm_fix = 1 THEN 0 ELSE 1 END,
          CASE WHEN IFNULL(city, '') IN ('', 'fuera', 'otros', 'argentina') THEN 0
               WHEN llm_await = 1 THEN 1
               WHEN lat IS NULL THEN 2
               ELSE 3 END,
          CASE WHEN IFNULL(json_extract(extra_json, '$.llm_place.province'), '') = ''
                AND IFNULL(json_extract(extra_json, '$.llm.province'), '') = ''
               THEN 0 ELSE 1 END,
          CASE WHEN city = ? THEN 0 ELSE 1 END,
          CASE WHEN details_scraped = 1 THEN 0 ELSE 1 END,
          scraped_at DESC
        LIMIT ?
    """
    return _fetch_backlog_rows(sql, (prefer, n))


def fetch_detail_backlog(limit: int, prefer_city: str = "") -> list[Listing]:
    """Fichas que todavía no se bajaron, de toda la base."""
    n = max(1, min(80, int(limit or 1)))
    prefer = (prefer_city or "").strip()
    sql = """
        SELECT * FROM listings
        WHERE details_scraped = 0
          AND IFNULL(url, '') != ''
          AND is_hidden = 0
        ORDER BY
          CASE WHEN llm_await = 1 THEN 0 ELSE 1 END,
          CASE WHEN city = ? THEN 0 ELSE 1 END,
          scraped_at DESC
        LIMIT ?
    """
    return _fetch_backlog_rows(sql, (prefer, n))


def _fetch_backlog_rows(sql: str, params: tuple) -> list[Listing]:
    with connect() as conn:
        rows = list(conn.execute(sql, params).fetchall())
        pins = _pins_map(conn)
    items = []
    for row in rows:
        item = _listing_from_row(row, pins.get(row["id"]))
        apply_user_edits(item)
        items.append(item)
    return items


def _notify_listings(listings: list[Listing] | None) -> None:
    try:
        from .listings_cache import ingest

        ingest(listings)
    except Exception:
        pass


def update_scores(listings: list[Listing]) -> None:
    with _write:
        with connect() as conn:
            for item in listings:
                flags = _listing_query_flags(item)
                conn.execute(
                    """
                    UPDATE listings
                    SET barrio = ?, zona = ?, lat = ?, lon = ?, price_usd = ?, price_m2 = ?,
                        score = ?, deal_label = ?, vs_barrio_pct = ?, fingerprint = ?,
                        has_exact_location = ?, quality_score = ?, quality_label = ?,
                        extra_json = ?, description = ?, details_scraped = ?, city = ?,
                        price = ?, currency = ?, covered_m2 = ?, total_m2 = ?,
                        property_type = ?, address = ?,
                        needs_llm = ?, is_hidden = ?, llm_ready = ?, llm_ver = ?,
                        llm_partial = ?, llm_await = ?, llm_fix = ?
                    WHERE id = ?
                    """,
                    (
                        item.barrio,
                        item.zona,
                        item.lat,
                        item.lon,
                        item.price_usd,
                        item.price_m2,
                        item.score,
                        item.deal_label,
                        item.vs_barrio_pct,
                        item.fingerprint,
                        1 if item.has_exact_location else 0,
                        item.quality_score,
                        item.quality_label,
                        json.dumps(item.extra or {}, ensure_ascii=False),
                        item.description,
                        1 if item.details_scraped else 0,
                        item.city,
                        item.price,
                        item.currency,
                        item.covered_m2,
                        item.total_m2,
                        item.property_type,
                        item.address,
                        flags["needs_llm"],
                        flags["is_hidden"],
                        flags["llm_ready"],
                        flags["llm_ver"],
                        flags["llm_partial"],
                        flags["llm_await"],
                        flags["llm_fix"],
                        item.id,
                    ),
                )
            conn.commit()
    from .market import sync_listing_prices

    sync_listing_prices(listings)
    _notify_listings(listings)


def set_meta(key: str, value: str) -> None:
    with _write:
        with connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                (key, value),
            )
            conn.commit()


def listing_ids() -> set[str]:
    with connect() as conn:
        return {row[0] for row in conn.execute("SELECT id FROM listings")}


def city_listing_counts() -> dict[str, int]:
    global _counts_memo, _counts_at
    testing = os.environ.get("PROPMAP_TEST") == "1"
    if not testing:
        now = time.time()
        with _counts_lock:
            if _counts_memo and now - _counts_at < COUNTS_TTL_SEC:
                return dict(_counts_memo)
    with connect() as conn:
        rows = conn.execute(
            "SELECT city, COUNT(*) FROM listings WHERE city IS NOT NULL AND city != '' GROUP BY city"
        ).fetchall()
    out = {str(row[0]): int(row[1]) for row in rows if row[0]}
    if not testing:
        with _counts_lock:
            _counts_memo = out
            _counts_at = time.time()
    return dict(out)


def sale_m2_by_city() -> dict[str, float]:
    init()
    import statistics

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT city, price_m2 FROM listings
            WHERE price_m2 IS NOT NULL
              AND property_type IN ('casa', 'departamento', 'ph')
              AND price_m2 >= 80 AND price_m2 <= 8000
              AND price_usd IS NOT NULL
            """
        ).fetchall()
    buckets: dict[str, list[float]] = {}
    for row in rows:
        buckets.setdefault(row["city"] or "", []).append(float(row["price_m2"]))
    out: dict[str, float] = {}
    for city, values in buckets.items():
        values.sort()
        if len(values) < 5:
            continue
        lo = int(len(values) * 0.15)
        hi = int(len(values) * 0.85) or len(values)
        cut = values[lo:hi] or values
        out[city] = float(statistics.median(cut))
    return out


def update_extras(listings: list[Listing]) -> None:
    if not listings:
        return
    with _write:
        with connect() as conn:
            conn.executemany(
                "UPDATE listings SET extra_json = ? WHERE id = ?",
                [
                    (json.dumps(item.extra or {}, ensure_ascii=False), item.id)
                    for item in listings
                ],
            )
            conn.commit()


def get_meta(key: str, default: str = "", *, timeout: float = 5.0) -> str:
    if timeout >= 1.0:
        try:
            with connect() as conn:
                row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default
        except sqlite3.OperationalError:
            return default
    conn = sqlite3.connect(DB_PATH, timeout=max(0.05, timeout))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA cache_size=-500")
        conn.execute(f"PRAGMA busy_timeout={max(50, int(timeout * 1000))}")
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return default
    finally:
        conn.close()
    return row["value"] if row else default


def save_pin(
    listing_id: str,
    favorite: bool | None = None,
    notes: str | None = None,
    contacted: bool | None = None,
) -> dict:
    with _write:
        with connect() as conn:
            current = conn.execute("SELECT * FROM pins WHERE listing_id = ?", (listing_id,)).fetchone()
            fav = int(favorite) if favorite is not None else int(current["favorite"] if current else 0)
            text = notes if notes is not None else (current["notes"] if current else "")
            if contacted is not None:
                cont = int(bool(contacted))
            elif current and "contacted" in current.keys():
                cont = int(current["contacted"] or 0)
            else:
                cont = 0
            conn.execute(
                "INSERT OR REPLACE INTO pins(listing_id, favorite, notes, contacted) VALUES (?, ?, ?, ?)",
                (listing_id, fav, text, cont),
            )
            conn.commit()
    saved = {"id": listing_id, "favorite": bool(fav), "notes": text, "contacted": bool(cont)}
    try:
        from .listings_cache import patch_pin

        patch_pin(listing_id, saved["favorite"], saved["notes"], saved["contacted"])
    except Exception:
        pass
    return saved


def update_listing(payload: dict) -> Listing:
    listing_id = str(payload.get("id") or "")
    items = [item for item in all_listings() if item.id == listing_id]
    if not items:
        raise ValueError("No está ese aviso")
    item = items[0]
    extra = dict(item.extra or {})
    edits = dict(extra.get("user_edits") or {})
    for key in EDIT_FIELDS:
        if key not in payload:
            continue
        value = payload[key]
        if value is None and key in {"price", "covered_m2", "total_m2", "bedrooms", "bathrooms"}:
            continue
        edits[key] = value
    extra["user_edits"] = edits
    if "contacted" in payload and payload["contacted"] is not None:
        extra["contacted"] = bool(payload["contacted"])
        item.contacted = bool(payload["contacted"])
    item.extra = extra
    apply_user_edits(item)
    upsert_listings([item])
    save_pin(
        listing_id,
        notes=payload.get("notes") if "notes" in payload else None,
        contacted=payload.get("contacted") if "contacted" in payload else None,
    )
    return next((row for row in all_listings() if row.id == listing_id), item)


def fetch_rentals(city: str | None = None) -> list[dict]:
    init()
    with connect() as conn:
        if city:
            rows = conn.execute("SELECT * FROM rental_comps WHERE city = ?", (city,)).fetchall()
        else:
            try:
                rows = conn.execute("SELECT * FROM rental_comps").fetchall()
            except sqlite3.OperationalError:
                return []
    out = []
    for row in rows:
        extra = json.loads(row["extra_json"] or "{}")
        out.append(
            {
                "id": row["id"],
                "source": row["source"],
                "source_id": row["source_id"],
                "url": row["url"],
                "title": row["title"],
                "property_type": row["property_type"],
                "price": row["price"],
                "currency": row["currency"],
                "price_usd": row["price_usd"],
                "period": row["period"],
                "address": row["address"] or "",
                "barrio": row["barrio"] or "",
                "zona": row["zona"] or "",
                "lat": row["lat"],
                "lon": row["lon"],
                "covered_m2": row["covered_m2"],
                "bedrooms": row["bedrooms"],
                "city": row["city"] or "",
                "extra": extra,
            }
        )
    return out


def replace_city_rentals(city: str, rows: list[dict]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _write:
        with connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS rental_comps ("
                "id TEXT PRIMARY KEY, source TEXT, source_id TEXT, url TEXT, title TEXT, "
                "property_type TEXT, price REAL, currency TEXT, price_usd REAL, period TEXT, "
                "address TEXT, barrio TEXT, zona TEXT, lat REAL, lon REAL, covered_m2 REAL, "
                "bedrooms INTEGER, city TEXT, extra_json TEXT, scraped_at TEXT)"
            )
            keep = {row["id"] for row in rows if row.get("id")}
            if keep:
                conn.execute("DELETE FROM rental_comps WHERE city = ? AND id NOT IN ({})".format(",".join("?" * len(keep))), (city, *keep))
            elif rows:
                conn.execute("DELETE FROM rental_comps WHERE city = ?", (city,))
            for row in rows:
                if not row.get("id"):
                    continue
                conn.execute(
                    """
                    INSERT OR REPLACE INTO rental_comps (
                        id, source, source_id, url, title, property_type, price, currency,
                        price_usd, period, address, barrio, zona, lat, lon, covered_m2,
                        bedrooms, city, extra_json, scraped_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["id"],
                        row.get("source") or "",
                        row.get("source_id") or "",
                        row.get("url") or "",
                        row.get("title") or "",
                        row.get("property_type") or "",
                        row.get("price"),
                        row.get("currency") or "ARS",
                        row.get("price_usd"),
                        row.get("period") or "monthly",
                        row.get("address") or "",
                        row.get("barrio") or "",
                        row.get("zona") or "",
                        row.get("lat"),
                        row.get("lon"),
                        row.get("covered_m2"),
                        row.get("bedrooms"),
                        row.get("city") or city,
                        json.dumps(row.get("extra") or {}, ensure_ascii=False),
                        now,
                    ),
                )
            conn.commit()


KEEP_OPS_MIN = 8 * 24 * 60


def ops_bump(metric: str, n: int, minute: int | None = None) -> None:
    name = (metric or "").strip()[:40]
    if not name or n <= 0:
        return
    slot = int(minute if minute is not None else time.time() // 60)
    with _write:
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO ops_minute(minute, metric, n) VALUES (?, ?, ?)
                ON CONFLICT(minute, metric) DO UPDATE SET n = n + excluded.n
                """,
                (slot, name, int(n)),
            )
            if slot % 60 == 0:
                conn.execute("DELETE FROM ops_minute WHERE minute < ?", (slot - KEEP_OPS_MIN,))
            conn.commit()


def ops_window(start_min: int, end_min: int) -> dict[int, dict[str, int]]:
    out: dict[int, dict[str, int]] = {}
    try:
        with connect() as conn:
            rows = conn.execute(
                "SELECT minute, metric, n FROM ops_minute WHERE minute >= ? AND minute <= ?",
                (int(start_min), int(end_min)),
            ).fetchall()
    except Exception:
        return out
    for minute, metric, n in rows:
        out.setdefault(int(minute), {})[str(metric)] = int(n)
    return out


def ops_sum_since(metric: str, start_min: int) -> int:
    try:
        with connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(n), 0) FROM ops_minute WHERE metric = ? AND minute >= ?",
                ((metric or "").strip()[:40], int(start_min)),
            ).fetchone()
    except Exception:
        return 0
    return int(row[0] if row else 0)


def drop_listings(ids: list[str] | tuple[str, ...] | set[str]) -> int:
    wanted = [lid for lid in ids if lid]
    if not wanted:
        return 0
    with _write:
        with connect() as conn:
            conn.executemany("DELETE FROM listings WHERE id = ?", [(lid,) for lid in wanted])
            conn.commit()
    try:
        from .listings_cache import forget_ids

        forget_ids(wanted)
    except Exception:
        pass
    try:
        from .ops import note

        note("gone", n=len(wanted))
    except Exception:
        pass
    return len(wanted)


replace_source = replace_portal_listings
fetch_all = all_listings
upsert_many = upsert_listings
