from __future__ import annotations

import ipaddress
import os
import re
import threading
import time
import uuid
from datetime import datetime

import httpx
from fastapi import Request
from fastapi.responses import Response

from . import store

META_SITE = "matomo_site_id"
META_TOKEN = "matomo_token"
META_PUBLIC_IP = "matomo_public_ip"

SCRIPT_PATH = "/q/l.js"
HIT_PATH = "/q/l"
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_TRUSTED_IP_HEADERS = ("cf-connecting-ip", "true-client-ip", "x-real-ip")
_BOT_UA = re.compile(
    r"(?:"
    r"googlebot|google-inspectiontool|storebot-google|adsbot-google|googleother|"
    r"google-extended|google-cloudvertexbot|apis-google|duplexweb-google|"
    r"bingbot|bingpreview|adidxbot|msnbot|slurp|duckduckbot|baiduspider|"
    r"yandex(bot|\.com/bots)|facebookexternalhit|facebot|meta-externalagent|"
    r"meta-externalfetcher|twitterbot|linkedinbot|embedly|quora link preview|"
    r"telegrambot|applebot|petalbot|semrushbot|ahrefsbot|mj12bot|"
    r"dotbot|bytespider|gptbot|chatgpt-user|claudebot|anthropic|ccbot|"
    r"amazonbot|ia_archiver|pingdom|uptimerobot|"
    r"headlesschrome|puppeteer|playwright|phantomjs|"
    r"python-requests|python-urllib|go-http-client|"
    r"curl/|wget/"
    r")",
    re.I,
)
_recent_hits: dict[str, float] = {}
_recent_lock = threading.Lock()
HIT_DEDUP_SEC = 15.0
_ids = {"site": "", "token": ""}
_ids_lock = threading.Lock()
_public_ip_cache = {"ip": "", "at": 0.0}
_visits_cache: dict[str, object] = {"rows": [], "at": 0.0}


def internal_url() -> str:
    return (os.environ.get("MATOMO_INTERNAL_URL") or "").rstrip("/")


def public_url() -> str:
    forced = (os.environ.get("MATOMO_PUBLIC_URL") or "").rstrip("/")
    if forced:
        return forced
    app = (os.environ.get("APP_PUBLIC_URL") or "").rstrip("/")
    return f"{app}/stats" if app else "/stats"


def config() -> dict:
    store.init()
    site_id = store.get_meta(META_SITE) or os.environ.get("MATOMO_SITE_ID") or ""
    token = store.get_meta(META_TOKEN) or ""
    remember_ids(site=site_id, token=token)
    return {
        "app": public_url(),
        "siteId": site_id,
        "src": SCRIPT_PATH if site_id else "",
        "tracker": HIT_PATH if site_id else "",
    }


def remember_ids(*, site: str = "", token: str = "") -> None:
    with _ids_lock:
        if site:
            _ids["site"] = site
        if token:
            _ids["token"] = token


def start_matomo_sync() -> None:
    threading.Thread(target=_sync, daemon=True, name="matomo-sync").start()


def script_response() -> Response:
    return _proxy_get("/matomo.js", "application/javascript; charset=utf-8")


def proxy_tracker(request: Request, body: bytes) -> Response:
    base = internal_url()
    if not base:
        return Response(status_code=204)
    geo_ip = cip_for_matomo(ip_for_geo(request))
    headers = {
        "user-agent": request.headers.get("user-agent") or "PropMap",
        "content-type": request.headers.get("content-type") or "application/x-www-form-urlencoded",
        "x-forwarded-proto": request.url.scheme,
    }
    if geo_ip:
        headers["x-forwarded-for"] = geo_ip
        headers["x-real-ip"] = geo_ip
    params = dict(request.query_params)
    token = _token()
    if geo_ip:
        params["cip"] = geo_ip
    if token:
        params["token_auth"] = token
    url = f"{base}/matomo.php"
    try:
        with httpx.Client(timeout=8.0, follow_redirects=True) as client:
            if request.method == "GET":
                res = client.get(url, params=params, headers=headers)
            else:
                res = client.post(url, params=params, content=body, headers=headers)
        return Response(
            content=res.content,
            status_code=res.status_code,
            media_type=res.headers.get("content-type") or "application/octet-stream",
        )
    except Exception:
        return Response(status_code=204)


def looks_like_bot(ua: str) -> bool:
    """Crawlers, previews y clientes HTTP. No cuentan como visita de una persona."""
    text = (ua or "").strip()
    if not text:
        return True
    return bool(_BOT_UA.search(text))


def queue_visit(
    request: Request,
    *,
    vid: str = "",
    path: str = "/",
    referrer: str = "",
    query: str = "",
    name: str = "pageview",
) -> None:
    """Manda la visita a Matomo desde el servidor. No espera la respuesta."""
    if not internal_url():
        return
    if looks_like_bot(request.headers.get("user-agent") or ""):
        return
    snapshot = {
        "geo_ip": ip_for_geo(request),
        "ua": request.headers.get("user-agent") or "",
        "lang": request.headers.get("accept-language") or "",
        "url": _page_url(path, query),
        "ref": referrer or request.headers.get("referer") or "",
        "vid": vid,
        "name": name,
    }
    threading.Thread(target=_send_visit, args=(snapshot,), daemon=True, name="matomo-hit").start()


def last_visits(limit: int = 10) -> list[dict]:
    """Últimas visitas de Matomo para el tablero. No espera más de un segundo."""
    now = time.time()
    cached = list(_visits_cache.get("rows") or [])
    if cached and now - float(_visits_cache.get("at") or 0) < 4.0:
        return cached
    base = internal_url()
    site_id = _site_id()
    token = _token()
    if not base or not site_id or not token:
        return cached
    try:
        with httpx.Client(timeout=1.2, follow_redirects=True) as client:
            res = client.get(
                f"{base}/index.php",
                params={
                    "module": "API",
                    "method": "Live.getLastVisitsDetails",
                    "idSite": site_id,
                    "period": "day",
                    "date": "today",
                    "format": "JSON",
                    "token_auth": token,
                    "filter_limit": str(max(1, min(limit, 24))),
                    "language": "es",
                },
            )
        data = res.json()
        rows = _parse_live_visits(data)
        _visits_cache["rows"] = rows
        _visits_cache["at"] = now
        return rows
    except Exception:
        return cached


def _parse_live_visits(data: object) -> list[dict]:
    if isinstance(data, dict) and data.get("result") == "error":
        return []
    raw = data if isinstance(data, list) else []
    out: list[dict] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        ts = int(row.get("lastActionTimestamp") or row.get("firstActionTimestamp") or 0)
        out.append(
            {
                "when": _visit_when(ts, str(row.get("serverDatePretty") or "")),
                "sec": int(row.get("visitDuration") or 0),
                "country": str(row.get("country") or row.get("countryCode") or "").strip(),
                "city": str(row.get("city") or "").strip(),
                "source": str(row.get("referrerTypeName") or "directo").strip(),
                "on_map": row.get("latitude") not in (None, "", 0, "0")
                and row.get("longitude") not in (None, "", 0, "0"),
            }
        )
    return out


def _visit_when(ts: int, pretty: str) -> str:
    if ts > 0:
        try:
            from zoneinfo import ZoneInfo

            local = datetime.fromtimestamp(ts, ZoneInfo("America/Argentina/Buenos_Aires"))
            return local.strftime("%H:%M:%S")
        except Exception:
            pass
    return pretty.strip()


def ip_for_geo(request: Request) -> str:
    """IP del visitante. Si solo hay LAN, cip_for_matomo usa la pública del server para el pin."""
    trusted = []
    for header in _TRUSTED_IP_HEADERS:
        ip = _clean_ip(request.headers.get(header) or "")
        if ip:
            trusted.append(ip)
    xff = [_clean_ip(part) for part in (request.headers.get("x-forwarded-for") or "").split(",")]
    xff = [ip for ip in xff if ip]
    forwarded = _ips_from_forwarded(request.headers.get("forwarded") or "")
    client = ""
    if request.client and request.client.host:
        client = _clean_ip(request.client.host)

    for ip in trusted:
        if not _is_lan_ip(ip):
            return ip
    for ip in reversed(xff):
        if not _is_lan_ip(ip):
            return ip
    for ip in reversed(forwarded):
        if not _is_lan_ip(ip):
            return ip
    if client and not _is_lan_ip(client):
        return client
    for ip in trusted + list(reversed(xff)) + list(reversed(forwarded)) + ([client] if client else []):
        if ip:
            return ip
    return ""


def _send_visit(snapshot: dict) -> None:
    try:
        if looks_like_bot(str(snapshot.get("ua") or "")):
            return
        base = internal_url()
        site_id = _site_id()
        if not base or not site_id:
            return
        vid = _visitor_hex(str(snapshot.get("vid") or ""))
        if _dup_hit(vid or str(snapshot.get("url") or "")):
            return
        params = {
            "idsite": site_id,
            "rec": "1",
            "apiv": "1",
            "send_image": "0",
            "url": snapshot.get("url") or "",
            "urlref": snapshot.get("ref") or "",
            "ua": snapshot.get("ua") or "",
            "_id": vid,
        }
        visitor_ip = str(snapshot.get("geo_ip") or "")
        geo_ip = cip_for_matomo(visitor_ip)
        token = _token()
        if geo_ip and not _is_lan_ip(geo_ip):
            params["cip"] = geo_ip
            if snapshot.get("lang"):
                params["lang"] = snapshot.get("lang")
        if token:
            params["token_auth"] = token
        name = str(snapshot.get("name") or "pageview")
        if name and name != "pageview":
            params["e_c"] = "app"
            params["e_a"] = name[:40]
        headers = {"user-agent": params["ua"] or "PropMap"}
        if geo_ip and not _is_lan_ip(geo_ip):
            headers["x-forwarded-for"] = geo_ip
            headers["x-real-ip"] = geo_ip
        with httpx.Client(timeout=8.0, follow_redirects=True) as client:
            client.get(f"{base}/matomo.php", params=params, headers=headers)
    except Exception:
        return


def _dup_hit(key: str) -> bool:
    now = time.time()
    with _recent_lock:
        prev = _recent_hits.get(key, 0.0)
        if now - prev < HIT_DEDUP_SEC:
            return True
        _recent_hits[key] = now
        if len(_recent_hits) > 4000:
            stale = [item for item, ts in _recent_hits.items() if now - ts > 60]
            for item in stale:
                _recent_hits.pop(item, None)
        return False


def cip_for_matomo(visitor_ip: str) -> str:
    """IP que Matomo usa para el mapa. LAN no tiene ciudad: se usa la pública del server."""
    ip = _clean_ip(visitor_ip)
    if ip and not _is_lan_ip(ip):
        return ip
    return _server_public_ip() or ""


def _server_public_ip() -> str:
    now = time.time()
    if _public_ip_cache["ip"] and now - _public_ip_cache["at"] < 3600:
        return _public_ip_cache["ip"]
    forced = (os.environ.get("MATOMO_GEO_FALLBACK_IP") or "").strip()
    if forced and not _is_lan_ip(forced):
        _public_ip_cache.update({"ip": forced, "at": now})
        return forced
    try:
        store.init()
        stored = store.get_meta(META_PUBLIC_IP, timeout=0.05)
    except Exception:
        stored = ""
    if stored and not _is_lan_ip(stored):
        _public_ip_cache.update({"ip": stored, "at": now})
        return stored
    try:
        with httpx.Client(timeout=4.0) as client:
            ip = (client.get("https://api.ipify.org").text or "").strip()
        if ip and not _is_lan_ip(ip):
            _public_ip_cache.update({"ip": ip, "at": now})
            try:
                store.set_meta(META_PUBLIC_IP, ip)
            except Exception:
                pass
            return ip
    except Exception:
        pass
    return ""


def _site_id() -> str:
    cached = _cached_id("site")
    if cached:
        return cached
    got = _meta_or_env(META_SITE, "MATOMO_SITE_ID")
    if got:
        remember_ids(site=got)
    return got


def _token() -> str:
    cached = _cached_id("token")
    if cached:
        return cached
    got = _meta_or_env(META_TOKEN, "")
    if got:
        remember_ids(token=got)
    return got


def _cached_id(key: str) -> str:
    with _ids_lock:
        return _ids.get(key) or ""


def _meta_or_env(meta_key: str, env_key: str) -> str:
    if env_key:
        env = (os.environ.get(env_key) or "").strip()
        if env:
            return env
    try:
        store.init()
        return store.get_meta(meta_key, timeout=0.05) or ""
    except Exception:
        return ""


def _page_url(path: str, query: str = "") -> str:
    base = (os.environ.get("APP_PUBLIC_URL") or "http://127.0.0.1:8000").rstrip("/")
    loc = path if str(path).startswith("/") else f"/{path or ''}"
    q = str(query or "").lstrip("?")
    return f"{base}{loc}?{q}" if q else f"{base}{loc}"


def _visitor_hex(vid: str) -> str:
    hexed = re.sub(r"[^0-9a-f]", "", (vid or "").lower())
    if len(hexed) >= 16:
        return hexed[:16]
    return (hexed + uuid.uuid4().hex)[:16]


def _proxy_get(path: str, media: str) -> Response:
    base = internal_url()
    if not base:
        return Response(status_code=503)
    try:
        with httpx.Client(timeout=12.0, follow_redirects=True) as client:
            res = client.get(f"{base}{path}")
        if res.status_code >= 400:
            return Response(status_code=res.status_code)
        return Response(content=res.content, media_type=media, headers={"Cache-Control": "public, max-age=3600"})
    except Exception:
        return Response(status_code=503)


def _clean_ip(raw: str) -> str:
    text = (raw or "").strip().strip('"').strip("'")
    if text.lower().startswith("for="):
        text = text[4:].strip().strip('"')
    text = text.strip().strip("[]")
    if "%" in text:
        text = text.split("%", 1)[0]
    if text.count(":") == 1 and "." in text:
        host, port = text.rsplit(":", 1)
        if port.isdigit():
            text = host
    return text.strip()


def _ips_from_forwarded(header: str) -> list[str]:
    found: list[str] = []
    for part in (header or "").split(","):
        for item in part.split(";"):
            item = item.strip()
            if item.lower().startswith("for="):
                ip = _clean_ip(item)
                if ip:
                    found.append(ip)
    return found


def _parse_ip(ip: str):
    raw = _clean_ip(ip)
    if not raw:
        return None
    try:
        return ipaddress.ip_address(raw)
    except ValueError:
        return None


def _is_lan_ip(ip: str) -> bool:
    addr = _parse_ip(ip)
    if addr is None:
        return True
    if addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_unspecified:
        return True
    if addr.version == 4 and addr in _CGNAT:
        return False
    return bool(addr.is_private)


def _sync() -> None:
    base = internal_url()
    if not base:
        return
    time.sleep(6)
    for _ in range(90):
        try:
            if _looks_ready(base):
                _configure(base)
                return
        except Exception:
            pass
        time.sleep(4)


def _looks_ready(base: str) -> bool:
    with httpx.Client(base_url=base, timeout=20.0, follow_redirects=True) as client:
        return _looks_installed(client.get("/"))


def _configure(base: str) -> None:
    store.init()
    token = store.get_meta(META_TOKEN) or _create_token(base)
    if token:
        store.set_meta(META_TOKEN, token)
        remember_ids(token=token)
    else:
        return
    params = {"module": "API", "format": "JSON", "token_auth": token}
    with httpx.Client(base_url=base, timeout=20.0, follow_redirects=True) as client:
        sites = client.get("/index.php", params={**params, "method": "SitesManager.getAllSites"}).json()
        site_id = _site_id_from(sites)
        if site_id:
            store.set_meta(META_SITE, site_id)
            remember_ids(site=site_id)
        client.get("/index.php", params={**params, "method": "UserCountry.setLocationProvider", "providerId": "geoip2php"})
        client.get(
            "/index.php",
            params={
                **params,
                "method": "PrivacyManager.setAnonymizeIpSettings",
                "anonymizeIPEnable": "0",
                "maskLength": "0",
                "useAnonymizedIpForVisitEnrichment": "0",
            },
        )
        if site_id:
            public = (os.environ.get("APP_PUBLIC_URL") or os.environ.get("MATOMO_SITE_URL") or "").rstrip("/")
            aliases = ["http://localhost:8000", "http://127.0.0.1:8000"]
            if public:
                aliases.insert(0, public)
            previous = "https://propmaplag.duckdns.org"
            if previous not in aliases:
                aliases.append(previous)
            client.get(
                "/index.php",
                params={
                    **params,
                    "method": "SitesManager.updateSite",
                    "idSite": site_id,
                    "siteName": os.environ.get("MATOMO_SITE_NAME") or "PropMap",
                    **{f"urls[{i}]": url for i, url in enumerate(aliases)},
                },
            )


def _create_token(base: str) -> str:
    user = (os.environ.get("MATOMO_USER") or "admin").strip()
    password = (os.environ.get("MATOMO_PASSWORD") or "").strip()
    with httpx.Client(base_url=base, timeout=20.0, follow_redirects=True) as client:
        res = client.post(
            "/index.php",
            data={
                "module": "API",
                "method": "UsersManager.createAppSpecificTokenAuth",
                "format": "JSON",
                "userLogin": user,
                "passwordConfirmation": password,
                "description": f"propmap-{int(time.time())}",
            },
        )
        try:
            data = res.json()
        except Exception:
            data = res.text
    if isinstance(data, dict):
        return str(data.get("value") or data.get("token") or "")
    text = str(data)
    if re.fullmatch(r"[a-f0-9]{32,128}", text.strip()):
        return text.strip()
    return ""


def _looks_installed(res: httpx.Response) -> bool:
    url = str(res.url).lower()
    text = res.text.lower()
    if "installation: true" in text or "module=installation" in url:
        return False
    if "sign in" in text or 'id="loginpage"' in text or "module=login" in url or "module=login" in text:
        return True
    if 'name="form_login"' in text or 'id="login_form"' in text:
        return True
    return False


def _site_id_from(sites: object) -> str:
    if isinstance(sites, dict):
        if sites.get("result") == "error":
            return ""
        if sites.get("idsite") or sites.get("idSite"):
            return str(sites.get("idsite") or sites.get("idSite"))
        for val in sites.values():
            if isinstance(val, dict) and (val.get("idsite") or val.get("idSite")):
                return str(val.get("idsite") or val.get("idSite"))
    if isinstance(sites, list) and sites and isinstance(sites[0], dict):
        return str(sites[0].get("idsite") or sites[0].get("idSite") or "")
    return ""
