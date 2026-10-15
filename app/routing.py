"""Por dónde conviene salir en cada portal, según lo que viene andando.

El orden fijo de `portal_way` (traductor para fichas, Tor para listados) es el
punto de partida. Si Yandex empieza a fallar en ZonaProp, los pedidos que
siguen arrancan por Tor en vez de comerse el intento perdido cada vez.

Las fichas y los listados se miden aparte: el traductor sirve para una ficha
pero casi nunca para un listado, así que mezclarlos ensuciaría la cuenta.
"""

from __future__ import annotations

import threading
import time

WAYS = ("translate", "tor", "local")
WINDOW_SEC = 300.0
# Con menos intentos que esto en el portal, manda el orden fijo.
MIN_TRIES = 6
# Desempate a favor del método de siempre, para no saltar por ruido.
PRIOR_BONUS = 0.15
MAX_KEYS = 600

_lock = threading.Lock()
_hits: dict[tuple[str, str, str], list[tuple[float, bool]]] = {}


def kind_of(listing: bool) -> str:
    return "ficha" if listing else "lista"


def way_of_lane(lane: str) -> str:
    """Traduce el carril que anotó el fetch al método que lo mandó."""
    token = (lane or "").lower()
    if token == "translate":
        return "translate"
    if token.startswith("tor") or token.startswith("proxy"):
        return "tor"
    if token in {"direct", "urllib"}:
        return "local"
    return ""


def note(host: str, kind: str, way: str, ok: bool) -> None:
    key = _key(host, kind, way)
    if key is None:
        return
    now = time.time()
    with _lock:
        row = _hits.setdefault(key, [])
        row.append((now, bool(ok)))
        _hits[key] = _fresh(row, now)
        if len(_hits) > MAX_KEYS:
            _gc(now)


def best(host: str, kind: str, prior: str) -> str:
    """El método con el que conviene arrancar en este portal."""
    return order(host, kind, prior)[0]


def order(host: str, kind: str, prior: str) -> list[str]:
    base = _static_order(prior)
    now = time.time()
    with _lock:
        rows = {way: list(_hits.get(_key(host, kind, way)) or []) for way in WAYS}
    rates: dict[str, float] = {}
    tries = 0
    for way, row in rows.items():
        fresh = _fresh(row, now)
        tries += len(fresh)
        good = sum(1 for _at, ok in fresh if ok)
        # Laplace: un método sin datos arranca en 0.5 y se sigue probando.
        rates[way] = (good + 1) / (len(fresh) + 2)
    if tries < MIN_TRIES:
        return base
    rates[prior] = min(1.0, rates[prior] + PRIOR_BONUS)
    return sorted(base, key=lambda way: (-rates[way], base.index(way)))


def snapshot() -> dict:
    """Tasa de éxito por portal y método, para el tablero."""
    now = time.time()
    with _lock:
        items = [(key, _fresh(row, now)) for key, row in _hits.items()]
    rows = []
    for (host, kind, way), fresh in items:
        if not fresh:
            continue
        good = sum(1 for _at, ok in fresh if ok)
        rows.append(
            {
                "host": host,
                "kind": kind,
                "way": way,
                "n": len(fresh),
                "ok_pct": round(100 * good / len(fresh)),
            }
        )
    rows.sort(key=lambda row: (row["host"], row["kind"], -row["n"]))
    return {"window_s": int(WINDOW_SEC), "rows": rows[:40]}


def reset() -> None:
    with _lock:
        _hits.clear()


def _key(host: str, kind: str, way: str) -> tuple[str, str, str] | None:
    token = (host or "").lower().strip()
    if not token or way not in WAYS:
        return None
    return token, (kind or "lista"), way


def _fresh(row: list[tuple[float, bool]], now: float) -> list[tuple[float, bool]]:
    return [hit for hit in row if now - hit[0] <= WINDOW_SEC]


def _gc(now: float) -> None:
    for key in [key for key, row in _hits.items() if not _fresh(row, now)]:
        _hits.pop(key, None)


def _static_order(prior: str) -> list[str]:
    head = prior if prior in WAYS else "tor"
    return [head] + [way for way in WAYS if way != head]
