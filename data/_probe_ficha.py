"""¿Las fichas fallan porque el aviso ya no está o porque nos bloquean?

Prueba cada URL por un carril Tor y por la IP local, y muestra el código HTTP.
"""
from urllib.parse import urlparse

from app import store
from app.egress import lanes, use_local
from app.http_client import _fetch_urllib, _httpx_get, _listing_html_ok

items = store.fetch_detail_backlog(60)
por_portal: dict[str, list] = {}
for it in items:
    host = urlparse(it.url or "").netloc
    if not host:
        continue
    por_portal.setdefault(host, []).append(it)

print("IP local habilitada:", use_local(), "| carriles:", len(lanes()))
proxy = (lanes() or [None])[0]
proxy_url = getattr(proxy, "proxy", None)
print("carril usado:", getattr(proxy, "id", "ninguno"))

for host, rows in por_portal.items():
    print("=" * 74)
    print(host, "· probando %s avisos" % min(3, len(rows)))
    for it in rows[:3]:
        linea = []
        try:
            r = _httpx_get(it.url, 25.0, proxy=proxy_url)
            ok = _listing_html_ok(it.url, r.text) if r.status_code == 200 else False
            linea.append("tor=%s%s" % (r.status_code, "" if r.status_code != 200 else (" html_ok" if ok else " html_incompleto")))
        except Exception as exc:
            linea.append("tor=error(%s)" % type(exc).__name__)
        try:
            txt = _fetch_urllib(it.url, 25.0)
            ok = _listing_html_ok(it.url, txt)
            linea.append("local=200%s" % (" html_ok" if ok else " html_incompleto"))
        except Exception as exc:
            msg = str(exc)
            corto = msg[:60]
            linea.append("local=%s" % corto)
        print("  %-52s %s" % (it.id, " | ".join(linea)))
