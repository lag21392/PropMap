from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import store
from .pipeline import add_manual, edit_listing, listings_payload, refresh, request_pause, set_slow_crawl, start_background_scraper, status
from .places import ensure_place, load_custom_places, public_place, search_places

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    store.init()
    load_custom_places()
    start_background_scraper()
    yield


app = FastAPI(title="PropMap Puerto Madryn", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


class ManualIn(BaseModel):
    title: str = "Aviso de Facebook"
    url: str = ""
    property_type: str = "casa"
    price: float | None = None
    currency: str = "USD"
    address: str = ""
    covered_m2: float | None = None
    total_m2: float | None = None
    bedrooms: int | None = None
    bathrooms: float | None = None
    notes: str = ""
    source_id: str = ""
    city: str = "puerto-madryn"


class PinIn(BaseModel):
    id: str
    favorite: bool | None = None
    notes: str | None = None
    contacted: bool | None = None


class RefreshIn(BaseModel):
    city: str | None = None
    query: str | None = None
    label: str | None = None
    lat: float | None = None
    lon: float | None = None
    province: str | None = None
    slow: bool | None = None


class PlaceIn(BaseModel):
    query: str | None = None
    city: str | None = None
    label: str | None = None
    lat: float | None = None
    lon: float | None = None
    province: str | None = None


class PauseIn(BaseModel):
    city: str | None = None


class CrawlIn(BaseModel):
    enabled: bool
    city: str | None = None
    query: str | None = None


class ListingEditIn(BaseModel):
    id: str
    contacted: bool | None = None
    notes: str | None = None
    title: str | None = None
    price: float | None = None
    currency: str | None = None
    address: str | None = None
    covered_m2: float | None = None
    total_m2: float | None = None
    bedrooms: int | None = None
    bathrooms: float | None = None
    description: str | None = None
    property_type: str | None = None


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/listings")
def listings(live: bool = Query(False), city: str = Query("")) -> dict:
    store.init()
    return listings_payload(live=live, city=city or None)


@app.get("/api/places")
def places(q: str = Query("", min_length=0)) -> dict:
    store.init()
    load_custom_places()
    return {"places": search_places(q)}


@app.post("/api/place")
def pick_place(payload: PlaceIn) -> dict:
    store.init()
    load_custom_places()
    city_id = ensure_place(
        payload.city,
        query=payload.query or payload.city,
        label=payload.label,
        lat=payload.lat,
        lon=payload.lon,
        province=payload.province,
    )
    return {"ok": True, "city": public_place(city_id)}


@app.get("/api/status")
def scrape_status() -> dict:
    store.init()
    return status()


@app.post("/api/refresh")
def scrape_now(payload: RefreshIn = RefreshIn()) -> dict:
    store.init()
    load_custom_places()
    city_id = ensure_place(
        payload.city,
        query=payload.query or payload.city,
        label=payload.label,
        lat=payload.lat,
        lon=payload.lon,
        province=payload.province,
    )
    store.set_meta("slow_crawl", "1")
    store.set_meta("slow_crawl_city", city_id)
    return {**refresh(city_id, fast=True), "city": city_id, "place": public_place(city_id)}


@app.post("/api/crawl")
def crawl_toggle(payload: CrawlIn) -> dict:
    return set_slow_crawl(payload.enabled, payload.query or payload.city)


@app.post("/api/pause")
def scrape_pause(payload: PauseIn = PauseIn()) -> dict:
    return request_pause(payload.city)


@app.post("/api/manual")
def manual(payload: ManualIn) -> dict:
    try:
        item = add_manual(payload.model_dump())
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "listing": item.to_public_dict()}


@app.post("/api/pin")
def pin(payload: PinIn) -> dict:
    store.init()
    try:
        saved = store.save_pin(payload.id, payload.favorite, payload.notes, payload.contacted)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, **saved}


@app.post("/api/listing")
def listing_edit(payload: ListingEditIn) -> dict:
    store.init()
    try:
        item = edit_listing(payload.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "listing": item.to_public_dict()}


@app.get("/api/market")
def market(city: str = Query("puerto-madryn"), type: str = Query("")) -> dict:
    store.init()
    from .market import market_payload

    return market_payload(city, type)


@app.get("/api/listing-history")
def listing_price_history(id: str = Query(..., min_length=3)) -> dict:
    store.init()
    from .market import listing_history

    return listing_history(id)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "city": "multi"}
