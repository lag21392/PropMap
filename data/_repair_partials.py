"""Devuelve a la cola los avisos que se marcaron listos con extracción vacía.

Mientras el pedido llevaba response_format, llama.cpp contestaba vacío: esos avisos
agotaron los reintentos y quedaron guardados como parciales sin un solo dato.
"""
import json
import sys

from app import store
from app.store import connect, _write

DRY = "--apply" not in sys.argv

with connect() as conn:
    rows = conn.execute(
        """
        SELECT id, extra_json
        FROM listings
        WHERE llm_partial = 1 AND llm_ver = 7
        """
    ).fetchall()

vacios = []
for row in rows:
    try:
        extra = json.loads(row[1] or "{}")
    except Exception:
        extra = {}
    llm = extra.get("llm") or {}
    if isinstance(llm, dict) and any(v not in (None, "", [], {}) for v in llm.values()):
        continue
    vacios.append(row[0])

print("parciales del schema 7: %s · sin ningún dato extraído: %s" % (len(rows), len(vacios)))
if DRY:
    print("modo prueba; corré con --apply para devolverlos a la cola")
    raise SystemExit(0)

hechos = 0
for lid in vacios:
    item = store.get_listing(lid)
    if not item:
        continue
    extra = dict(item.extra or {})
    for key in ("llm_ready", "llm_partial", "llm_ver", "llm_tries", "llm_at", "llm_city_ok", "llm_repair", "llm"):
        extra.pop(key, None)
    extra["await_llm"] = True
    item.extra = extra
    store.upsert_listings([item], notify=False)
    hechos += 1

print("devueltos a la cola: %s" % hechos)
