from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from . import store
from .listings_cache import start_warmup
from .pipeline import add_manual, edit_listing, listings_payload, refresh, request_pause, set_slow_crawl, start_background_scraper, status
from .places import ensure_place, load_custom_places, public_place, search_places
from .search_auth import require_search_password

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    store.init()
    load_custom_places()
    if os.environ.get("PROPMAP_TEST") != "1":
        start_warmup()
        start_background_scraper()
        from .matomo import start_matomo_sync

        start_matomo_sync()
    yield


class SecureHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "img-src 'self' data: blob: https://*.openstreetmap.org https://tile.openstreetmap.org https: http://127.0.0.1:3102 http://localhost:3102; "
            "style-src 'self' 'unsafe-inline' https://unpkg.com https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "script-src 'self' https://unpkg.com; "
            "connect-src 'self' https://unpkg.com; "
            "frame-ancestors 'none'"
        )
        return response


app = FastAPI(title="PropMap", docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(SecureHeaders)
app.add_middleware(GZipMiddleware, minimum_size=800)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


class TrackIn(BaseModel):
    n: str = "pageview"
    p: str = "/"
    r: str = ""
    q: str = ""
    utm: str = ""
    city: str = ""
    vid: str = ""
    listing: str = ""
    tab: str = ""


class ManualIn(BaseModel):
    title: str = "Aviso de Facebook"
    url: str = ""
    property_type: str = "casa"
    price: float | None = None
    currency: str = "USD"
    password: str | None = None
    address: str = ""
    covered_m2: float | None = None
    total_m2: float | None = None
    bedrooms: int | None = None
    bathrooms: float | None = None
    notes: str = ""
    source_id: str = ""
    city: str = "caba"


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
    password: str | None = None


class PlaceIn(BaseModel):
    query: str | None = None
    city: str | None = None
    label: str | None = None
    lat: float | None = None
    lon: float | None = None
    province: str | None = None


class PauseIn(BaseModel):
    city: str | None = None
    password: str | None = None


class CrawlIn(BaseModel):
    enabled: bool
    city: str | None = None
    query: str | None = None
    password: str | None = None


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


class RegisterIn(BaseModel):
    username: str = ""
    email: str = ""
    password: str = ""
    accept_terms: bool = False


class LoginIn(BaseModel):
    username: str = ""
    password: str = ""


class DeleteAccountIn(BaseModel):
    password: str = ""
    confirm: str = ""


class PinsImportIn(BaseModel):
    pins: dict = Field(default_factory=dict)


@app.get("/api/config")
def public_config() -> dict:
    from .accounts import mail_address, mail_inbox_url, smtp_configured
    from .matomo import config as matomo_config

    return {
        "matomo": matomo_config(),
        "auth": {"enabled": True, "smtp": smtp_configured()},
        "mail": {
            "address": mail_address(),
            "inbox": mail_inbox_url(),
            "smtp": smtp_configured(),
        },
    }


@app.get("/matomo.js")
def matomo_script() -> Response:
    from .matomo import script_response

    return script_response()


@app.api_route("/matomo.php", methods=["GET", "POST"])
async def matomo_tracker(request: Request) -> Response:
    from .matomo import proxy_tracker

    body = await request.body() if request.method == "POST" else b""
    return proxy_tracker(request, body)


def _html_page(request: Request) -> FileResponse:
    from .analytics import record, stamp_cookie, visitor_from_request

    vid = visitor_from_request(request)
    record(
        {
            "n": "pageview",
            "p": request.url.path,
            "r": request.headers.get("referer") or "",
            "q": request.url.query,
            "vid": vid,
        },
        ua=request.headers.get("user-agent") or "",
        header_ref=request.headers.get("referer") or "",
    )
    response = FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})
    stamp_cookie(response, vid)
    return response


@app.get("/")
def index(request: Request) -> FileResponse:
    return _html_page(request)


@app.get("/legal")
@app.get("/privacidad")
@app.get("/terminos")
@app.get("/aviso")
def legal_page() -> FileResponse:
    return FileResponse(STATIC / "legal.html", headers={"Cache-Control": "no-store"})


@app.get("/verificar")
def verify_email(token: str = Query("")) -> RedirectResponse:
    from .accounts import verify_token

    if not token:
        return RedirectResponse("/?cuenta=falta", status_code=302)
    try:
        verify_token(token)
    except HTTPException:
        return RedirectResponse("/?cuenta=error", status_code=302)
    return RedirectResponse("/?cuenta=ok", status_code=302)


@app.post("/api/auth/register")
def auth_register(payload: RegisterIn, request: Request) -> JSONResponse:
    from .accounts import login, public_account, register, stamp_session

    data = register(payload.username, payload.email, payload.password, payload.accept_terms, request)
    try:
        user, token = login(payload.username, payload.password, request)
        data["user"] = public_account(user)
        response = JSONResponse(data)
        stamp_session(response, token, request)
        return response
    except HTTPException:
        return JSONResponse(data)


@app.post("/api/auth/login")
def auth_login(payload: LoginIn, request: Request) -> JSONResponse:
    from .accounts import login, public_account, stamp_session

    user, token = login(payload.username, payload.password, request)
    response = JSONResponse({"ok": True, "user": public_account(user)})
    stamp_session(response, token, request)
    return response


@app.post("/api/auth/logout")
def auth_logout(request: Request) -> JSONResponse:
    from .accounts import logout

    response = JSONResponse({"ok": True})
    logout(request, response)
    return response


@app.get("/api/auth/me")
def auth_me(request: Request) -> dict:
    from .accounts import optional_user, public_account

    user = optional_user(request)
    if not user:
        return {"user": None}
    return {"user": public_account(user)}


@app.post("/api/auth/resend")
def auth_resend(request: Request) -> dict:
    from .accounts import require_user, resend_verification

    return resend_verification(require_user(request))


@app.post("/api/auth/delete")
def auth_delete(payload: DeleteAccountIn, request: Request) -> JSONResponse:
    from .accounts import clear_session, delete_account, require_user

    user = require_user(request)
    delete_account(user, payload.password, payload.confirm)
    response = JSONResponse({"ok": True})
    clear_session(response)
    return response


@app.post("/api/auth/import-pins")
def auth_import_pins(payload: PinsImportIn, request: Request) -> dict:
    from .accounts import import_pins, require_user

    user = require_user(request, verified=True)
    return import_pins(user, payload.pins)


@app.get("/admin")
def admin(request: Request) -> FileResponse:
    return _html_page(request)


@app.get("/px.gif")
def tracking_pixel(request: Request, p: str = "/") -> Response:
    from .analytics import PIXEL_GIF, record, stamp_cookie, visitor_from_request

    vid = visitor_from_request(request)
    path = p or request.headers.get("referer") or "/"
    if path.startswith("http"):
        from urllib.parse import urlparse as _urlparse

        path = _urlparse(path).path or "/"
    record(
        {
            "n": "pageview",
            "p": path[:180],
            "r": request.headers.get("referer") or "",
            "q": request.url.query,
            "vid": vid,
        },
        ua=request.headers.get("user-agent") or "",
        header_ref=request.headers.get("referer") or "",
    )
    response = Response(content=PIXEL_GIF, media_type="image/gif")
    response.headers["Cache-Control"] = "no-store"
    stamp_cookie(response, vid)
    return response


@app.post("/api/t")
def track_event(payload: TrackIn, request: Request) -> JSONResponse:
    from .analytics import record, stamp_cookie, visitor_from_request

    data = payload.model_dump()
    vid = visitor_from_request(request, data.get("vid") or "")
    data["vid"] = vid
    if not data.get("r"):
        data["r"] = request.headers.get("referer") or ""
    if not data.get("q"):
        data["q"] = request.url.query
    record(data, ua=request.headers.get("user-agent") or "", header_ref=request.headers.get("referer") or "")
    response = JSONResponse({"ok": True})
    stamp_cookie(response, vid)
    return response


class AdminStatsIn(BaseModel):
    password: str = ""
    days: int = 14


@app.post("/api/admin/stats")
def admin_stats(payload: AdminStatsIn) -> dict:
    require_search_password(payload.password)
    from .analytics import summary

    return summary(payload.days)


@app.get("/api/listings")
def listings(request: Request, live: bool = Query(False), city: str = Query(""), since: int = Query(-1)):
    from .accounts import optional_user, overlay_pins
    from .listings_cache import disk_response_bytes

    user = optional_user(request)
    if not user:
        raw = disk_response_bytes(city or None)
        if raw:
            if city and not live:
                from .pipeline import _kick_llm_enrich

                _kick_llm_enrich(city)
            return Response(content=raw, media_type="application/json")
    data = listings_payload(live=live, city=city or None, since=None if since < 0 else since)
    encoded = data.pop("encoded", None)
    if user:
        data["listings"] = overlay_pins(data.get("listings") or [], user)
        return data
    if encoded:
        return Response(content=encoded, media_type="application/json")
    return data


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
    from .pipeline import maybe_daily_refresh
    from .schedule import note_search

    note_search(city_id)
    threading.Thread(target=maybe_daily_refresh, daemon=True, name="kick-schedule").start()
    return {"ok": True, "city": public_place(city_id)}


@app.get("/api/status")
def scrape_status() -> dict:
    store.init()
    return status()


@app.post("/api/refresh")
def scrape_now(payload: RefreshIn = RefreshIn()) -> dict:
    require_search_password(payload.password)
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
    return {**refresh(city_id, fast=True), "city": city_id, "place": public_place(city_id)}


@app.post("/api/crawl")
def crawl_toggle(payload: CrawlIn) -> dict:
    return set_slow_crawl(payload.enabled, payload.query or payload.city)


@app.post("/api/pause")
def scrape_pause(payload: PauseIn = PauseIn()) -> dict:
    require_search_password(payload.password)
    return request_pause(payload.city)


@app.post("/api/manual")
def manual(payload: ManualIn) -> dict:
    require_search_password(payload.password)
    try:
        item = add_manual(payload.model_dump(exclude={"password"}))
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "listing": item.to_public_dict()}


@app.post("/api/pin")
def pin(payload: PinIn, request: Request) -> dict:
    from .accounts import require_user, save_pin

    user = require_user(request, verified=True)
    saved = save_pin(user, payload.id, payload.favorite, payload.notes, payload.contacted)
    return {"ok": True, **saved}


@app.post("/api/listing")
def listing_edit(payload: ListingEditIn, request: Request) -> dict:
    from .accounts import require_user, save_pin

    user = require_user(request, verified=True)
    store.init()
    body = payload.model_dump(exclude_unset=True)
    notes = body.pop("notes", None)
    contacted = body.pop("contacted", None)
    item = None
    try:
        if any(k != "id" for k in body):
            item = edit_listing(body)
        if notes is not None or contacted is not None:
            save_pin(user, payload.id, notes=notes, contacted=contacted)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    if item is None:
        return {"ok": True, "id": payload.id, "notes": notes or "", "contacted": bool(contacted)}
    public = item.to_public_dict()
    if notes is not None:
        public["notes"] = notes
    if contacted is not None:
        public["contacted"] = bool(contacted)
    return {"ok": True, "listing": public}


@app.get("/api/market")
def market(city: str = Query("caba"), type: str = Query("")) -> dict:
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
