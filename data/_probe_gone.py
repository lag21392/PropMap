"""Clasifica las fichas pendientes: aviso caído (404/410) o portal bloqueando (403)."""
from urllib.parse import urlparse

from app import store
from app.http_client import _fetch_urllib, _httpx_get, _listing_html_ok
from app.egress import lanes

items = store.fetch_detail_backlog(120)
por_portal: dict[str, list] = {}
for it in items:
    host = urlparse(it.url or "").netloc
    if host:
        por_portal.setdefault(host, []).append(it)

proxy = (lanes() or [None])[0]
proxy_url = getattr(proxy, "proxy", None)
POR_PORTAL = 5

for host, rows in sorted(por_portal.items(), key=lambda kv: -len(kv[1])):
    cuenta = {"vive": 0, "caido": 0, "bloqueado_igual": 0, "otro": 0}
    tor = {}
    for it in rows[:POR_PORTAL]:
        try:
            r = _httpx_get(it.url, 25.0, proxy=proxy_url)
            tor[r.status_code] = tor.get(r.status_code, 0) + 1
        except Exception:
            tor["error"] = tor.get("error", 0) + 1
        try:
            txt = _fetch_urllib(it.url, 25.0)
            cuenta["vive" if _listing_html_ok(it.url, txt) else "otro"] += 1
        except Exception as exc:
            msg = str(exc)
            if "410" in msg or "404" in msg:
                cuenta["caido"] += 1
            elif "403" in msg or "401" in msg:
                cuenta["bloqueado_igual"] += 1
            else:
                cuenta["otro"] += 1
    print("%-28s pendientes=%-6s muestra=%s" % (host, len(rows), min(POR_PORTAL, len(rows))))
    print("   por tor:   %s" % tor)
    print("   por local: %s" % cuenta)
