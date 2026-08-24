from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from .models import Listing
from .text_quality import address_quality, title_quality

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "listings.sqlite"
_write = Lock()
EDIT_FIELDS = (
    "title", "price", "currency", "address", "covered_m2", "total_m2",
    "bedrooms", "bathrooms", "description", "property_type",
)


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


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
                city TEXT DEFAULT 'puerto-madryn',
                quality_score REAL,
                quality_label TEXT,
                details_scraped INTEGER DEFAULT 0
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
            conn.execute("ALTER TABLE listings ADD COLUMN city TEXT DEFAULT 'puerto-madryn'")
        if "quality_score" not in cols:
            conn.execute("ALTER TABLE listings ADD COLUMN quality_score REAL")
        if "quality_label" not in cols:
            conn.execute("ALTER TABLE listings ADD COLUMN quality_label TEXT")
        if "details_scraped" not in cols:
            conn.execute("ALTER TABLE listings ADD COLUMN details_scraped INTEGER DEFAULT 0")
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
        conn.commit()


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
    if old_exact and not item.has_exact_location and row["lat"] is not None:
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


def upsert_listings(listings: list[Listing], delete_source: str | None = None) -> None:
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
                conn.execute(
                    """
                    INSERT OR REPLACE INTO listings (
                        id, source, source_id, url, title, property_type, price, currency,
                        price_usd, address, barrio, zona, lat, lon, covered_m2, total_m2,
                        rooms, bedrooms, bathrooms, parking, age_years, image, publisher,
                        description, published_at, price_m2, score, deal_label, vs_barrio_pct,
                        fingerprint, extra_json, scraped_at, has_exact_location, city,
                        quality_score, quality_label, details_scraped
                    ) VALUES (
                        :id, :source, :source_id, :url, :title, :property_type, :price, :currency,
                        :price_usd, :address, :barrio, :zona, :lat, :lon, :covered_m2, :total_m2,
                        :rooms, :bedrooms, :bathrooms, :parking, :age_years, :image, :publisher,
                        :description, :published_at, :price_m2, :score, :deal_label, :vs_barrio_pct,
                        :fingerprint, :extra_json, :scraped_at, :has_exact_location, :city,
                        :quality_score, :quality_label, :details_scraped
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
                    },
                )
            conn.commit()
    from .market import sync_listing_prices

    sync_listing_prices(listings)


def upsert_one(item: Listing) -> None:
    replace_portal_listings(item.source, [item] + [x for x in all_listings() if x.source == item.source and x.id != item.id])


def save_manual(item: Listing) -> None:
    existing = [x for x in all_listings() if x.source == "manual"]
    existing = [x for x in existing if x.id != item.id]
    replace_portal_listings("manual", existing + [item])


def all_listings() -> list[Listing]:
    pins = {}
    with connect() as conn:
        rows = conn.execute("SELECT * FROM listings ORDER BY price_usd IS NULL, price_usd ASC").fetchall()
        try:
            for pin in conn.execute("SELECT * FROM pins").fetchall():
                pins[pin["listing_id"]] = pin
        except sqlite3.OperationalError:
            pass
    items: list[Listing] = []
    for row in rows:
        extra = json.loads(row["extra_json"] or "{}")
        pin = pins.get(row["id"])
        items.append(
            Listing(
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
                city=row["city"] if "city" in row.keys() and row["city"] else "puerto-madryn",
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
        )
    for item in items:
        apply_user_edits(item)
    return items


def update_scores(listings: list[Listing]) -> None:
    with _write:
        with connect() as conn:
            for item in listings:
                conn.execute(
                    """
                    UPDATE listings
                    SET barrio = ?, zona = ?, lat = ?, lon = ?, price_usd = ?, price_m2 = ?,
                        score = ?, deal_label = ?, vs_barrio_pct = ?, fingerprint = ?,
                        has_exact_location = ?, quality_score = ?, quality_label = ?,
                        extra_json = ?, description = ?, details_scraped = ?, city = ?,
                        price = ?, currency = ?, covered_m2 = ?, total_m2 = ?,
                        property_type = ?, address = ?
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
                        item.id,
                    ),
                )
            conn.commit()
    from .market import sync_listing_prices

    sync_listing_prices(listings)


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


def get_meta(key: str, default: str = "") -> str:
    with connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
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
    return {"id": listing_id, "favorite": bool(fav), "notes": text, "contacted": bool(cont)}


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


replace_source = replace_portal_listings
fetch_all = all_listings
upsert_many = upsert_listings
