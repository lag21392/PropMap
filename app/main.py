from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Receive, Scope, Send

from . import store
from .listings_cache import start_warmup
from .pipeline import add_manual, listings_payload, refresh, request_pause, set_slow_crawl, start_background_scraper, status
from .places import load_custom_places, place_from_suggestion, public_place, search_places
from .search_auth import require_search_password

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from anyio.to_thread import current_default_thread_limiter

    current_default_thread_limiter().total_tokens = max(
        64, current_default_thread_limiter().total_tokens
    )
    store.init()
    load_custom_places()
    if os.environ.get("PROPMAP_TEST") != "1":
        start_warmup()
        start_background_scraper()
        from .watchdog import start as start_watchdog

        start_watchdog()
        from .matomo import start_matomo_sync

        start_matomo_sync()
    yield


_CSP = (
    "default-src 'self'; "
    "img-src 'self' data: blob: https://*.openstreetmap.org https://tile.openstreetmap.org https: http://127.0.0.1:3102 http://localhost:3102; "
    "style-src 'self' 'unsafe-inline' https://unpkg.com https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "script-src 'self' https://unpkg.com; "
    "connect-src 'self' https://unpkg.com; "
    "frame-ancestors 'none'"
)


def _stats_is_asset(path: str, query: str) -> bool:
    if "module=Proxy" in query:
        return True
    return path.endswith((".js", ".css", ".png", ".svg", ".jpg", ".woff", ".woff2", ".ico"))


class SecureHeaders:
    """Cabeceras en el start del response. No lee el cuerpo: un JSON de avisos no puede clavar la portada."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")

        async def send_with_headers(message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
                headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
                if path.startswith("/stats"):
                    query = (scope.get("query_string") or b"").decode("latin-1")
                    headers["X-Frame-Options"] = "SAMEORIGIN"
                    headers["Content-Security-Policy"] = (
                        "default-src 'self'; "
                        "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
                        "style-src 'self' 'unsafe-inline' data:; "
                        "img-src 'self' data: blob: https:; "
                        "font-src 'self' data:; "
                        "connect-src 'self'; "
                        "frame-src 'self'; "
                        "frame-ancestors 'self'"
                    )
                    if not _stats_is_asset(path, query):
                        headers["Cache-Control"] = "no-store"
                else:
                    headers["Content-Security-Policy"] = _CSP
                if path.startswith("/static/"):
                    headers.setdefault("Cache-Control", "public, max-age=604800, immutable")
            await send(message)

        await self.app(scope, receive, send_with_headers)


class CachedStatic(StaticFiles):
    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers.setdefault("Cache-Control", "public, max-age=604800, immutable")
        return resp


class SkipListingsGZip:
    """No comprimir /api/listings en el pedido: el gzip ya está precocinado en disco."""

    def __init__(self, app: ASGIApp, minimum_size: int = 800):
        self.gzip = GZipMiddleware(app, minimum_size=minimum_size)
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = str(scope.get("path") or "")
        if scope.get("type") == "http" and (
            path.startswith("/api/listings") or path == "/api/alive"
        ):
            await self.app(scope, receive, send)
            return
        await self.gzip(scope, receive, send)


app = FastAPI(title="PropMap", docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(SecureHeaders)
app.add_middleware(SkipListingsGZip, minimum_size=800)
app.mount("/static", CachedStatic(directory=str(STATIC)), name="static")


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


class ResendIn(BaseModel):
    username: str = ""
    password: str = ""


class DeleteAccountIn(BaseModel):
    password: str = ""
    confirm: str = ""


class PinsImportIn(BaseModel):
    pins: dict = Field(default_factory=dict)


class AdminStatsIn(BaseModel):
    password: str = ""
    days: int = 14
    client: dict = Field(default_factory=dict)


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
@app.get("/q/l.js")
def matomo_script() -> Response:
    from .matomo import script_response

    return script_response()


@app.api_route("/matomo.php", methods=["GET", "POST"])
@app.api_route("/q/l", methods=["GET", "POST"])
async def matomo_tracker(request: Request) -> Response:
    from .matomo import proxy_tracker

    body = await request.body() if request.method == "POST" else b""
    return proxy_tracker(request, body)


def _html_page(request: Request) -> FileResponse:
    from .analytics import record_later, stamp_cookie, visitor_from_request

    vid = visitor_from_request(request)
    record_later(
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
    from .matomo import queue_visit

    queue_visit(
        request,
        vid=vid,
        path=request.url.path,
        referrer=request.headers.get("referer") or "",
        query=request.url.query,
        name="pageview",
    )
    response = FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})
    stamp_cookie(response, vid)
    return response


@app.get("/")
async def index(request: Request) -> FileResponse:
    return _html_page(request)


@app.get("/legal")
@app.get("/privacidad")
@app.get("/terminos")
@app.get("/aviso")
async def legal_page() -> FileResponse:
    return FileResponse(STATIC / "legal.html", headers={"Cache-Control": "no-store"})


@app.get("/verificar")
def verify_email(request: Request, token: str = Query("")) -> RedirectResponse:
    from .accounts import create_session, stamp_session, verify_token

    if not token:
        return RedirectResponse("/?cuenta=falta", status_code=302)
    try:
        user = verify_token(token)
    except HTTPException:
        return RedirectResponse("/?cuenta=error", status_code=302)
    response = RedirectResponse("/?cuenta=ok", status_code=302)
    stamp_session(response, create_session(user.id), request)
    return response


@app.post("/api/auth/register")
def auth_register(payload: RegisterIn, request: Request) -> JSONResponse:
    from .accounts import register

    data = register(payload.username, payload.email, payload.password, payload.accept_terms, request)
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
    if not user or not user.email_verified:
        return {"user": None}
    return {"user": public_account(user)}


@app.post("/api/auth/resend")
def auth_resend(request: Request, payload: ResendIn | None = None) -> dict:
    from .accounts import optional_user, resend_verification, resend_with_password

    body = payload or ResendIn()
    user = optional_user(request)
    if user:
        return resend_verification(user, request)
    if body.username and body.password:
        return resend_with_password(body.username, body.password, request)
    raise HTTPException(401, "Entrá con tu usuario y contraseña para reenviar el mail")


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
async def admin(request: Request) -> FileResponse:
    return _html_page(request)


@app.get("/flujo")
async def flujo_page(request: Request) -> Response:
    from .matomo_gate import has_access, login_page

    if not has_access(request):
        return login_page(next_url="/flujo")
    return FileResponse(
        STATIC / "flujo.html",
        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"},
    )


@app.get("/tablero")
async def tablero_page(request: Request) -> Response:
    from .matomo_gate import has_access, login_page

    if not has_access(request):
        return login_page(next_url="/tablero")
    return FileResponse(
        STATIC / "tablero.html",
        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"},
    )


def _stamp_ops_if_password(request: Request, password: str, data: dict):
    from .matomo_gate import _stamp, has_access
    from .search_auth import password_matches

    if password_matches(password) and not has_access(request):
        return _stamp(JSONResponse(data), request)
    return data


def _require_ops(request: Request, password: str = "") -> None:
    from .matomo_gate import _note_login_attempt, has_access, login_locked
    from .search_auth import password_matches

    if has_access(request):
        return
    got = (password or "").strip() or (request.headers.get("x-propmap-ops") or "")
    if password_matches(got):
        return
    if got:
        if login_locked(request):
            raise HTTPException(status_code=429, detail="Demasiados intentos")
        _note_login_attempt(request)
    raise HTTPException(status_code=401, detail="Contraseña incorrecta")


@app.get("/api/ops")
async def ops_dashboard(request: Request) -> dict:
    _require_ops(request)
    from .ops import dashboard

    return await asyncio.to_thread(dashboard)


@app.post("/api/ops")
async def ops_dashboard_post(payload: AdminStatsIn, request: Request) -> dict:
    _require_ops(request, payload.password)
    from .ops import dashboard

    data = await asyncio.to_thread(dashboard)
    return _stamp_ops_if_password(request, payload.password, data)


@app.get("/api/ops/metrics")
def ops_metrics(request: Request) -> Response:
    _require_ops(request)
    from .ops import prometheus

    return Response(prometheus(), media_type="text/plain; version=0.0.4; charset=utf-8")


@app.post("/api/lineage")
def lineage_graph(payload: AdminStatsIn, request: Request) -> dict:
    from .matomo_gate import has_access

    if not has_access(request):
        require_search_password(payload.password)
    from .lineage import blueprint

    return _stamp_ops_if_password(request, payload.password, blueprint())


@app.get("/robots.txt")
def robots_txt() -> Response:
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /stats\n"
        "Disallow: /stats/\n"
        "Disallow: /tablero\n"
        "Disallow: /flujo\n"
        "Disallow: /admin\n"
    )
    return Response(body, media_type="text/plain; charset=utf-8")


@app.get("/stats")
async def stats_root(request: Request) -> Response:
    from .matomo_gate import has_access, login_page

    if not has_access(request):
        return login_page()
    return RedirectResponse("/stats/", status_code=308)


@app.get("/stats/")
async def stats_home(request: Request) -> Response:
    from .matomo_gate import has_access, login_page, proxy

    if not has_access(request):
        return login_page()
    return await proxy(request, "")


@app.post("/stats/login")
async def stats_login(request: Request) -> Response:
    from .matomo_gate import handle_login

    return await handle_login(request)


@app.api_route("/stats/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def stats_proxy(path: str, request: Request) -> Response:
    from .matomo_gate import handle_login, proxy

    if path == "login":
        return await handle_login(request)
    return await proxy(request, path)


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
    from .matomo import queue_visit

    queue_visit(
        request,
        vid=vid,
        path=path[:180],
        referrer=request.headers.get("referer") or "",
        name="pageview",
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
    from .matomo import queue_visit

    queue_visit(
        request,
        vid=vid,
        path=str(data.get("p") or "/"),
        referrer=str(data.get("r") or ""),
        query=str(data.get("q") or ""),
        name=str(data.get("n") or "pageview"),
    )
    response = JSONResponse({"ok": True})
    stamp_cookie(response, vid)
    return response


def _with_timing(name: str, t0: float, result, extra: dict | None = None):
    from .http_timing import note

    ms = (time.perf_counter() - t0) * 1000
    note(name, ms, extra)
    if isinstance(result, dict):
        result = JSONResponse(result)
    if isinstance(result, Response):
        result.headers["Server-Timing"] = f"{name};dur={ms:.1f}"
    return result


@app.post("/api/admin/stats")
def admin_stats(payload: AdminStatsIn) -> dict:
    require_search_password(payload.password)
    from .analytics import summary
    from .http_timing import snapshot

    data = summary(payload.days)
    data["perf"] = snapshot()
    if payload.client:
        data["client_perf"] = payload.client
    return data


def _wants_gzip(request: Request) -> bool:
    return "gzip" in (request.headers.get("accept-encoding") or "").lower()


@app.get("/api/listings")
def listings(
    request: Request,
    live: bool = Query(False),
    city: str = Query(""),
    since: int = Query(-1),
    pins: bool = Query(False),
):
    from .accounts import optional_user, overlay_pins
    from .jsoncodec import gunzip_bytes, loads as json_loads
    from .listings_cache import listings_body, listings_pins_body, request_city_bytes, unchanged_listings, warming_payload

    t0 = time.perf_counter()
    extra = {"city": city or "", "pins": int(bool(pins))}
    user = optional_user(request)
    request_city_bytes(city or None)
    from .http_timing import note

    note("listings.warm", (time.perf_counter() - t0) * 1000, extra)
    want_gzip = (not user) and _wants_gzip(request)
    if pins:
        raw, encoding = listings_pins_body(city or None, gzip=want_gzip)
        if raw:
            headers = {"Cache-Control": "public, max-age=45, stale-while-revalidate=120"}
            if encoding:
                headers["Content-Encoding"] = encoding
                headers["Vary"] = "Accept-Encoding"
            return _with_timing("listings", t0, Response(content=raw, media_type="application/json", headers=headers), extra)
        return _with_timing("listings", t0, warming_payload(city or None), extra)
    if not user:
        stale = unchanged_listings(city or None, since)
        if stale:
            return _with_timing("listings", t0, Response(content=stale, media_type="application/json"), extra)
    raw, encoding = listings_body(city or None, gzip=want_gzip)
    if raw:
        if user:
            body = gunzip_bytes(raw) if encoding == "gzip" else raw
            data = json_loads(body)
            data["listings"] = overlay_pins(data.get("listings") or [], user)
            data["live"] = bool(live)
            return _with_timing("listings", t0, data, extra)
        headers = {"Cache-Control": "public, max-age=45, stale-while-revalidate=120"}
        if encoding:
            headers["Content-Encoding"] = encoding
            headers["Vary"] = "Accept-Encoding"
        return _with_timing("listings", t0, Response(content=raw, media_type="application/json", headers=headers), extra)
    if user:
        data = listings_payload(live=live, city=city or None, since=None if since < 0 else since)
        data.pop("encoded", None)
        data["listings"] = overlay_pins(data.get("listings") or [], user)
        return _with_timing("listings", t0, data, extra)
    return _with_timing("listings", t0, warming_payload(city or None), extra)


@app.get("/api/pois")
def pois(city: str = Query(""), summary: bool = Query(True)) -> dict:
    from .osm_poi import CAT_LABEL, city_pois, ensure_city_pois, origin_for_city, pending

    cid = (city or "").strip()
    ensure_city_pois(cid, blocking=False)
    cats = city_pois(cid)
    origin = origin_for_city(cid) if cid else None
    payload = {
        "city": cid,
        "pending": pending(cid),
        "origin": {"lat": origin[0], "lon": origin[1]} if origin else None,
        "labels": CAT_LABEL,
        "counts": {key: len(cats.get(key) or []) for key in cats},
    }
    if not summary:
        payload["categories"] = cats
    return payload


@app.get("/api/near")
def near(
    city: str = Query(""),
    lat: float = Query(...),
    lon: float = Query(...),
    listing_id: str = Query(""),
) -> dict:
    """Cercanías del pin. El proceso de fondo las guarda; acá se leen."""
    from .access import near_payload

    return near_payload(listing_id=listing_id, city=city, lat=lat, lon=lon)


@app.get("/api/places")
def places(q: str = Query("", min_length=0)) -> dict:
    return {"places": search_places(q)}


@app.post("/api/place")
def pick_place(payload: PlaceIn) -> dict:
    store.init()
    load_custom_places()
    try:
        city_id = place_from_suggestion(
            payload.city,
            query=payload.query or payload.city,
            label=payload.label,
            lat=payload.lat,
            lon=payload.lon,
            province=payload.province,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    from .pipeline import refresh

    status = refresh(city_id, fast=True, interactive=True)
    from .pipeline import eta_minutes

    return {"ok": True, "city": public_place(city_id), "eta_min": eta_minutes(city_id), **status}


@app.get("/api/status")
def scrape_status():
    t0 = time.perf_counter()
    return _with_timing("status", t0, status())


@app.post("/api/refresh")
def scrape_now(payload: RefreshIn = RefreshIn()) -> dict:
    require_search_password(payload.password)
    store.init()
    load_custom_places()
    try:
        city_id = place_from_suggestion(
            payload.city,
            query=payload.query or payload.city,
            label=payload.label,
            lat=payload.lat,
            lon=payload.lon,
            province=payload.province,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {**refresh(city_id, fast=True, interactive=True), "city": city_id, "place": public_place(city_id)}


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


@app.get("/api/favorites-report")
def favorites_report(request: Request) -> Response:
    from .accounts import favorite_entries, require_user
    from .fav_report import MAX_FAVORITES, build_report

    user = require_user(request, verified=True)
    store.init()
    entries, total = favorite_entries(user, MAX_FAVORITES)
    if not entries:
        raise HTTPException(400, "No tenés favoritos para armar el reporte")
    rows: list[dict] = []
    for listing_id, pin in entries:
        item = store.get_listing(listing_id)
        public = item.to_public_dict() if item else {
            "id": listing_id,
            "title": "Este aviso ya no está en el mapa",
            "url": "",
        }
        public["notes"] = str(pin.get("notes") or "")[:2000]
        public["contacted"] = bool(pin.get("contacted"))
        public["favorite"] = True
        rows.append(public)
    pdf = build_report(rows, username=user.username, total=total)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="favoritos-propmap.pdf"'},
    )


@app.get("/api/listing")
def listing_get(request: Request, id: str = Query(..., min_length=3)) -> dict:
    from .accounts import optional_user, overlay_pins

    t0 = time.perf_counter()
    store.init()
    item = store.get_listing(id)
    if item is None:
        raise HTTPException(404, "No está ese aviso")
    public = item.to_public_dict()
    user = optional_user(request)
    if user:
        public = overlay_pins([public], user)[0]
    return _with_timing("listing", t0, {"ok": True, "listing": public}, {"id": id})


@app.post("/api/listing")
def listing_edit(payload: ListingEditIn, request: Request) -> dict:
    from .accounts import overlay_pins, require_user, save_pin
    from .store import EDIT_FIELDS

    user = require_user(request, verified=True)
    store.init()
    body = payload.model_dump(exclude_unset=True)
    listing_id = str(body.pop("id") or "")
    notes = body.pop("notes", None)
    contacted = body.pop("contacted", None)
    edits = {key: body[key] for key in EDIT_FIELDS if key in body}
    try:
        save_pin(
            user,
            listing_id,
            notes=notes,
            contacted=contacted,
            edits=edits or None,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    item = store.get_listing(listing_id)
    if item is None:
        raise HTTPException(404, "No está ese aviso")
    public = overlay_pins([item.to_public_dict()], user)[0]
    return {"ok": True, "listing": public}


@app.get("/api/market")
def market(city: str = Query("caba"), type: str = Query("")):
    t0 = time.perf_counter()
    store.init()
    from .market import market_payload

    return _with_timing("market", t0, market_payload(city, type), {"city": city or "", "type": type or ""})


@app.get("/api/listing-history")
def listing_price_history(id: str = Query(..., min_length=3)) -> dict:
    store.init()
    from .market import listing_history

    return listing_history(id)


@app.get("/api/alive")
async def alive() -> dict:
    return {"ok": True}


@app.get("/api/health")
async def health() -> dict:
    from .watchdog import snapshot

    beat = snapshot()
    if os.environ.get("PROPMAP_TEST") != "1" and not beat["ok"]:
        raise HTTPException(503, "watchdog")
    return {"ok": True, "city": "multi", "watchdog": beat}
