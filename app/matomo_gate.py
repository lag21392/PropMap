from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .search_auth import password_matches, search_password

COOKIE = "propmap_ops"
PREFIX = "/stats"
_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "content-encoding",
    "host",
}
# Solo paths de Matomo. Un "/" suelto ( "/>" , split("/") ) no se toca:
# si no, el HTML del login queda roto y el botón no se puede usar.
_APP_PATH = (
    r"(?:index\.php|piwik\.(?:js|php)|plugins/|js/|libs/|misc/|core/"
    r"|node_modules/|themes/|favicon(?:\.ico|\.png)?)"
)
_ROOT_REF = re.compile(rf'(?<=["\'(=])/(?={_APP_PATH})')
_SKIP_PREFIX = ("/stats", "/matomo.js", "/matomo.php", "/static/", "/q/")


def cookie_token() -> str:
    secret = search_password() or (os.environ.get("AUTH_SECRET") or "propmap")
    return hmac.new(secret.encode("utf-8"), b"stats-ok", hashlib.sha256).hexdigest()


def has_access(request: Request) -> bool:
    got = request.cookies.get(COOKIE) or ""
    want = cookie_token()
    if got and hmac.compare_digest(got, want):
        return True
    return password_matches(request.headers.get("x-propmap-ops") or "")


def _stamp(response: Response, request: Request) -> Response:
    secure = request.url.scheme == "https" or (request.headers.get("x-forwarded-proto") or "") == "https"
    response.set_cookie(
        COOKIE,
        cookie_token(),
        max_age=12 * 3600,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )
    return response


def _safe_next(raw: str) -> str:
    path = (raw or "").strip() or (PREFIX + "/")
    if not path.startswith("/") or path.startswith("//") or "://" in path:
        return PREFIX + "/"
    if path in {"/stats", "/stats/", "/tablero", "/flujo", "/admin"}:
        return "/stats/" if path == "/stats" else path
    if path.startswith("/stats/"):
        return path
    return PREFIX + "/"


def login_page(error: str = "", next_url: str = "", heading: str = "") -> HTMLResponse:
    dest = _safe_next(next_url)
    title = heading or (
        "Tablero de scrape" if dest == "/tablero"
        else "Cómo está armado" if dest == "/flujo"
        else "Tablero Matomo"
    )
    note = "<p class='err'>Contraseña incorrecta.</p>" if error else ""
    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>PropMap · {title}</title>
  <link rel="stylesheet" href="/static/styles.css?v=ui29" />
</head>
<body class="ops-gate">
  <main class="ops-card">
    <p class="ops-kicker">Solo administración</p>
    <h1>{title}</h1>
    <p>Una sola contraseña. La sesión vale para Matomo, el scrape y el flujo.</p>
    {note}
    <form method="post" action="{PREFIX}/login">
      <input type="hidden" name="next" value="{dest}" />
      <label>Contraseña <input type="password" name="password" autocomplete="current-password" required /></label>
      <button type="submit" class="primary">Entrar</button>
    </form>
    <p class="muted"><a href="/admin">Volver</a></p>
  </main>
</body>
</html>"""
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


async def _posted_fields(request: Request) -> dict[str, str]:
    raw = await request.body()
    content_type = (request.headers.get("content-type") or "").lower()
    if "json" in content_type:
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            return {"password": "", "next": ""}
        return {
            "password": str((data or {}).get("password") or ""),
            "next": str((data or {}).get("next") or ""),
        }
    parsed = parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {
        "password": (parsed.get("password") or [""])[0],
        "next": (parsed.get("next") or [""])[0],
    }


async def _posted_password(request: Request) -> str:
    return (await _posted_fields(request))["password"]


async def handle_login(request: Request) -> Response:
    fields = await _posted_fields(request)
    dest = _safe_next(fields.get("next") or "")
    if not password_matches(fields.get("password") or ""):
        return login_page("bad", next_url=dest)
    response = RedirectResponse(dest, status_code=303)
    return _stamp(response, request)


def _internal() -> str:
    return (os.environ.get("MATOMO_INTERNAL_URL") or "http://matomo").rstrip("/")


def _rewrite_hosts(extra: str = "") -> list[str]:
    hosts = ["matomo", "127.0.0.1:3102", "localhost:3102", "127.0.0.1:8000", "localhost:8000"]
    for key in ("APP_PUBLIC_URL", "MATOMO_PUBLIC_URL"):
        netloc = urlparse((os.environ.get(key) or "").strip()).netloc.lower()
        if netloc:
            hosts.append(netloc)
    extra_host = (extra or "").strip().lower()
    if extra_host:
        hosts.append(extra_host)
    seen: set[str] = set()
    out: list[str] = []
    for host in hosts:
        if host not in seen:
            seen.add(host)
            out.append(host)
    return out


def _abs_pattern(extra: str = "") -> re.Pattern[str]:
    alt = "|".join(re.escape(host) for host in sorted(_rewrite_hosts(extra), key=len, reverse=True))
    return re.compile(rf"https?://(?:{alt})(?![A-Za-z0-9.-])(/[^\"\s'<>\\\\]*)?", re.I)


def _map_path(path: str) -> str:
    target = path or "/"
    if target.startswith(_SKIP_PREFIX):
        return target
    return PREFIX + target


def _rewrite_abs(match: re.Match[str]) -> str:
    path = match.group(1) or "/"
    if path.startswith(_SKIP_PREFIX):
        return match.group(0) if not path.startswith(PREFIX) else _map_path(path)
    return _map_path(path)


_ASSET_CACHE: dict[str, bytes] = {}
_ASSET_CACHE_MAX = 48


def _media_kind(media: str) -> str:
    low = (media or "").lower()
    if "javascript" in low or low.endswith("json"):
        return "javascript" if "javascript" in low else "json"
    if "css" in low:
        return "css"
    if "html" in low:
        return "html"
    return ""


def _rewrite_text(text: str, extra: str = "", media: str = "html") -> str:
    out = _ROOT_REF.sub(PREFIX + "/", _abs_pattern(extra).sub(_rewrite_abs, text))
    if media in {"html", ""}:
        out = re.sub(
            r'(id="login_form_submit"[^>]*)\sdisabled="disabled"',
            r"\1",
            out,
        )
    return out


def rewrite_asset(raw: bytes, media: str, extra: str = "", cache_key: str = "") -> bytes:
    kind = _media_kind(media) or "html"
    if cache_key and kind in {"javascript", "css"}:
        hit = _ASSET_CACHE.get(cache_key)
        if hit is not None:
            return hit
    out = _rewrite_text(
        raw.decode("utf-8", errors="surrogateescape"), extra=extra, media=kind
    ).encode("utf-8", errors="surrogateescape")
    if cache_key and kind in {"javascript", "css"}:
        if len(_ASSET_CACHE) >= _ASSET_CACHE_MAX:
            _ASSET_CACHE.pop(next(iter(_ASSET_CACHE)))
        _ASSET_CACHE[cache_key] = out
    return out


def _rewrite_cookie(value: str) -> str:
    out = re.sub(r"(?i);\s*domain=[^;]*", "", value)
    return re.sub(r"(?i)(path=)/(?!stats)", rf"\1{PREFIX}/", out)


def _rewrite_location(value: str, extra: str = "") -> str:
    raw = (value or "").strip()
    if not raw:
        return raw
    if raw.startswith(PREFIX):
        return raw
    if raw.startswith("/") and not raw.startswith("//"):
        return PREFIX + raw
    parsed = urlparse(raw)
    if parsed.netloc.lower() in {host.lower() for host in _rewrite_hosts(extra)}:
        mapped = _map_path(parsed.path or "/")
        return mapped + (("?" + parsed.query) if parsed.query else "")
    return raw


async def proxy(request: Request, path: str) -> Response:
    if not has_access(request):
        if request.method == "GET":
            return login_page()
        return Response(status_code=401)
    suffix = "/" + (path or "").lstrip("/")
    if suffix == "/":
        suffix = "/index.php"
    url = _internal() + suffix
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _HOP
    }
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    incoming_host = request.headers.get("host") or ""
    headers.pop("host", None)
    headers["x-forwarded-proto"] = proto
    headers["x-forwarded-host"] = incoming_host
    headers["x-forwarded-prefix"] = PREFIX
    headers["x-real-ip"] = request.client.host if request.client else ""
    body = await request.body() if request.method != "GET" else b""
    try:
        async with httpx.AsyncClient(timeout=45.0, follow_redirects=False) as client:
            res = await client.request(
                request.method,
                url,
                params=request.query_params,
                content=body or None,
                headers=headers,
            )
    except Exception:
        return Response("Matomo no responde", status_code=502)
    media = (res.headers.get("content-type") or "").lower()
    payload = res.content
    kind = _media_kind(media)
    if kind:
        cache_key = ""
        if request.method == "GET" and res.status_code == 200 and kind in {"javascript", "css"}:
            cache_key = f"{suffix}?{request.query_params}|{incoming_host}|{kind}|{len(payload)}"
        payload = rewrite_asset(payload, media, extra=incoming_host, cache_key=cache_key)
    response = Response(
        content=payload,
        status_code=res.status_code,
        media_type=res.headers.get("content-type"),
    )
    for key, value in res.headers.items():
        low = key.lower()
        if low in _HOP or low == "set-cookie":
            continue
        if low == "cache-control" and kind in {"javascript", "css"}:
            continue
        if low == "location":
            response.headers[key] = _rewrite_location(value, extra=incoming_host)
        else:
            response.headers[key] = value
    for cookie in res.headers.get_list("set-cookie"):
        response.headers.append("set-cookie", _rewrite_cookie(cookie))
    if kind in {"javascript", "css"} and request.method == "GET" and res.status_code == 200:
        response.headers["Cache-Control"] = "private, max-age=3600"
    return response
