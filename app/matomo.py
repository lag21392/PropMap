from __future__ import annotations

import ipaddress
import os
import re
import threading
import time

import httpx
from fastapi import Request
from fastapi.responses import Response

from . import store

META_SITE = "matomo_site_id"
META_TOKEN = "matomo_token"
META_PUBLIC_IP = "matomo_public_ip"

_public_ip_cache = {"ip": "", "at": 0.0}


def internal_url() -> str:
    return (os.environ.get("MATOMO_INTERNAL_URL") or "").rstrip("/")


def public_url() -> str:
    return (os.environ.get("MATOMO_PUBLIC_URL") or "http://127.0.0.1:3102").rstrip("/")


def config() -> dict:
    store.init()
    site_id = store.get_meta(META_SITE) or os.environ.get("MATOMO_SITE_ID") or ""
    return {
        "app": public_url(),
        "siteId": site_id,
        "src": "/matomo.js" if site_id else "",
        "tracker": "/matomo.php" if site_id else "",
    }


def start_matomo_sync() -> None:
    threading.Thread(target=_sync, daemon=True, name="matomo-sync").start()


def script_response() -> Response:
    return _proxy_get("/matomo.js", "application/javascript; charset=utf-8")


def proxy_tracker(request: Request, body: bytes) -> Response:
    base = internal_url()
    if not base:
        return Response(status_code=204)
    geo_ip = _ip_for_geo(request)
    headers = {
        "user-agent": request.headers.get("user-agent") or "PropMap",
        "content-type": request.headers.get("content-type") or "application/x-www-form-urlencoded",
        "x-forwarded-for": geo_ip,
        "x-real-ip": geo_ip,
        "x-forwarded-proto": request.url.scheme,
    }
    url = f"{base}/matomo.php"
    try:
        with httpx.Client(timeout=12.0, follow_redirects=True) as client:
            if request.method == "GET":
                res = client.get(url, params=dict(request.query_params), headers=headers)
            else:
                res = client.post(url, params=dict(request.query_params), content=body, headers=headers)
        return Response(
            content=res.content,
            status_code=res.status_code,
            media_type=res.headers.get("content-type") or "application/octet-stream",
        )
    except Exception:
        return Response(status_code=204)


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


def _is_private_ip(ip: str) -> bool:
    raw = (ip or "").strip().strip("[]").split("%")[0]
    if not raw:
        return True
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return True
    return bool(addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved)


def _candidate_ips(request: Request) -> list[str]:
    found: list[str] = []
    forwarded = request.headers.get("x-forwarded-for") or ""
    for part in forwarded.split(","):
        ip = part.strip()
        if ip:
            found.append(ip)
    if request.client and request.client.host:
        found.append(request.client.host)
    return found


def _cached_public_ip() -> str:
    now = time.time()
    if _public_ip_cache["ip"] and now - _public_ip_cache["at"] < 3600:
        return _public_ip_cache["ip"]
    store.init()
    stored = store.get_meta(META_PUBLIC_IP)
    forced = (os.environ.get("MATOMO_GEO_FALLBACK_IP") or "").strip()
    if forced and not _is_private_ip(forced):
        _public_ip_cache.update({"ip": forced, "at": now})
        return forced
    try:
        with httpx.Client(timeout=4.0) as client:
            ip = (client.get("https://api.ipify.org").text or "").strip()
        if ip and not _is_private_ip(ip):
            _public_ip_cache.update({"ip": ip, "at": now})
            store.set_meta(META_PUBLIC_IP, ip)
            return ip
    except Exception:
        pass
    if stored and not _is_private_ip(stored):
        _public_ip_cache.update({"ip": stored, "at": now})
        return stored
    return ""


def _ip_for_geo(request: Request) -> str:
    for ip in _candidate_ips(request):
        if not _is_private_ip(ip):
            return ip
    return _cached_public_ip() or next(iter(_candidate_ips(request)), "")


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
    else:
        return
    params = {"module": "API", "format": "JSON", "token_auth": token}
    with httpx.Client(base_url=base, timeout=20.0, follow_redirects=True) as client:
        sites = client.get("/index.php", params={**params, "method": "SitesManager.getAllSites"}).json()
        site_id = _site_id_from(sites)
        if site_id:
            store.set_meta(META_SITE, site_id)
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
            client.get(
                "/index.php",
                params={
                    **params,
                    "method": "SitesManager.addSiteAliasUrls",
                    "idSite": site_id,
                    "urls": "http://localhost:8000",
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
