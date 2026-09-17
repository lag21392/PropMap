"""Carriles de salida para el scrape: Tor, proxies propios y la IP local.

Las VPN gratis de internet no entran: cambian de IP sin API, caen seguido
y suelen venir con malware o MITM. Tor sí se puede automatizar: varios
circuitos por IsolateSOCKSAuth. Proxies HTTP/SOCKS van por env. La IP
local queda apagada si hay Tor o proxy, salvo SCRAPE_USE_LOCAL=1.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from . import crawl

FAIL_COOLDOWN_SEC = 45.0
MAX_TOR_CIRCUITS = 16
GROW_STEP = 2
GROW_EVERY_SEC = 20.0


@dataclass(frozen=True)
class Lane:
    id: str
    kind: str
    proxy: str | None = None


_lock = threading.Lock()
_fail_until: dict[str, float] = {}
_cached: tuple[str, tuple[Lane, ...]] | None = None
_extra_circuits = 0
_last_grow = 0.0
_local_at = 0.0
_local_day = ""
_local_n = 0
_local_inflight = 0
_local_hold = threading.local()


def reset() -> None:
    global _cached, _extra_circuits, _last_grow, _local_at, _local_day, _local_n, _local_inflight
    with _lock:
        _fail_until.clear()
        _cached = None
        _extra_circuits = 0
        _last_grow = 0.0
        _local_at = 0.0
        _local_day = ""
        _local_n = 0
        _local_inflight = 0


def lanes() -> tuple[Lane, ...]:
    fingerprint = _env_fingerprint()
    global _cached
    with _lock:
        if _cached and _cached[0] == fingerprint:
            return _cached[1]
        built = _build_lanes()
        _cached = (fingerprint, built)
        return built


def lane_count() -> int:
    return max(1, len(lanes()))


def use_local() -> bool:
    raw = (os.environ.get("SCRAPE_USE_LOCAL") or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return not _tor_socks_list() and not _proxy_urls()


def local_limits() -> tuple[float, int]:
    """Segundos que ocupa una ficha por IP de casa, y tope diario."""
    try:
        gap = float(os.environ.get("SCRAPE_LOCAL_GAP_SEC") or 20.0)
    except ValueError:
        gap = 20.0
    try:
        cap = int(os.environ.get("SCRAPE_LOCAL_MAX_DAY") or 600)
    except ValueError:
        cap = 600
    return max(0.0, gap), max(0, cap)


def local_parallel() -> int:
    try:
        n = int(os.environ.get("SCRAPE_LOCAL_PARALLEL") or 4)
    except ValueError:
        n = 4
    return max(1, min(8, n))


def local_has_room() -> bool:
    _, cap = local_limits()
    today = time.strftime("%Y-%m-%d")
    with _lock:
        if _local_day == today and cap and _local_n >= cap:
            return False
        return _local_inflight < local_parallel()


def local_ready(host: str = "") -> bool:
    """True si este portal no está en la pausa corta de 20 s tras un 403."""
    _, cap = local_limits()
    today = time.strftime("%Y-%m-%d")
    with _lock:
        if _local_day == today and cap and _local_n >= cap:
            return False
    if host and crawl.cooling(host, "direct"):
        return False
    return True


def note_local_use(host: str = "") -> None:
    global _local_at, _local_day, _local_n
    today = time.strftime("%Y-%m-%d")
    with _lock:
        if _local_day != today:
            _local_day = today
            _local_n = 0
        _local_at = time.monotonic()
        _local_n += 1


def local_used_today() -> int:
    with _lock:
        return _local_n if _local_day == time.strftime("%Y-%m-%d") else 0


def acquire_local(host: str = "", timeout: float | None = 0.0) -> bool:
    """Toma un hueco local. Varias páginas a la vez; cada una dura ~20 s."""
    global _local_at, _local_day, _local_n, _local_inflight
    wait_s = 0.0 if timeout is None else max(0.0, timeout)
    deadline = time.monotonic() + wait_s
    while True:
        if host and crawl.cooling(host, "direct"):
            return False
        with _lock:
            today = time.strftime("%Y-%m-%d")
            if _local_day != today:
                _local_day = today
                _local_n = 0
            _, cap = local_limits()
            if cap and _local_n >= cap:
                return False
            if _local_inflight < local_parallel():
                _local_inflight += 1
                _local_n += 1
                _local_at = time.monotonic()
                _local_hold.t = _local_at
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def release_local(host: str = "", hold: bool = False) -> None:
    """Libera el hueco. Si hold, la página ocupó los 20 s aunque el GET haya sido corto."""
    global _local_inflight
    gap, _ = local_limits()
    started = float(getattr(_local_hold, "t", 0.0) or 0.0)
    _local_hold.t = 0.0
    if hold and gap and started:
        left = gap - (time.monotonic() - started)
        if left > 0:
            time.sleep(left)
    with _lock:
        _local_inflight = max(0, _local_inflight - 1)


def can_fetch(host: str, exclude: set[str] | None = None) -> bool:
    return pick(host, exclude=exclude) is not None


def pick(host: str, exclude: set[str] | None = None, *, _grew: bool = False) -> Lane | None:
    skip = exclude or set()
    now = time.time()
    options: list[Lane] = []
    with _lock:
        dead = {lid for lid, until in _fail_until.items() if until > now}
    for lane in lanes():
        if lane.id in skip or lane.id in dead:
            continue
        if crawl.cooling(host, lane.id):
            continue
        if lane.kind == "direct" and not local_ready(host):
            continue
        options.append(lane)
    if not options:
        if not _grew and _maybe_grow():
            return pick(host, exclude, _grew=True)
        return None
    idle = [lane for lane in options if crawl.busy_until(host, lane.id) <= now]
    pool = idle or options
    if not use_local():
        hidden = [lane for lane in pool if lane.kind != "direct"]
        if hidden:
            pool = hidden
    return min(pool, key=lambda lane: (_kind_pref(lane), crawl.busy_until(host, lane.id)))


def note_error(lane_id: str, seconds: float = FAIL_COOLDOWN_SEC) -> None:
    if not lane_id:
        return
    with _lock:
        _fail_until[lane_id] = max(_fail_until.get(lane_id, 0.0), time.time() + max(8.0, seconds))


def snapshot() -> dict:
    now = time.time()
    rows = lanes()
    with _lock:
        down_until = {lid: until for lid, until in _fail_until.items() if until > now}
    busy = crawl.busy_rows()
    by_lane: dict[str, list[dict]] = {}
    for row in busy:
        by_lane.setdefault(row["lane"], []).append(row)
    tracks = []
    for lane in rows:
        tracks.append(
            {
                "id": lane.id,
                "kind": lane.kind,
                "down_s": round(max(0.0, down_until.get(lane.id, 0.0) - now), 1),
                "busy": by_lane.get(lane.id, []),
            }
        )
    return {
        "lanes": len(rows),
        "kinds": [lane.kind for lane in rows],
        "down": list(down_until),
        "tracks": tracks,
        "use_local": use_local(),
        "local_today": local_used_today(),
        "local_cap": local_limits()[1],
        "tor_circuits": sum(1 for lane in rows if lane.kind == "tor"),
        "tor_extra": _extra_circuits,
    }


def _kind_pref(lane: Lane) -> int:
    if lane.kind in {"tor", "proxy"}:
        return 0
    if lane.kind == "direct":
        return 2
    return 1


def _env_fingerprint() -> str:
    return "|".join(
        (
            os.environ.get("PROPMAP_TEST") or "",
            os.environ.get("PROPMAP_TOR_TEST") or "",
            os.environ.get("TOR_ENABLED") or "",
            os.environ.get("TOR_SOCKS") or "",
            os.environ.get("TOR_CIRCUITS") or "",
            str(_extra_circuits),
            os.environ.get("SCRAPE_PROXIES") or "",
            os.environ.get("SCRAPE_USE_LOCAL") or "",
        )
    )


def _build_lanes() -> tuple[Lane, ...]:
    proxies = []
    for i, proxy in enumerate(_proxy_urls()):
        if _socks_supported(proxy):
            proxies.append(Lane(id=f"proxy-{i}", kind="proxy", proxy=proxy))
    tors = _tor_lanes()
    rows: list[Lane] = []
    if use_local() or not (proxies or tors):
        rows.append(Lane(id="direct", kind="direct", proxy=None))
    rows.extend(proxies)
    rows.extend(tors)
    return tuple(rows)


def _tor_lanes() -> list[Lane]:
    urls = _tor_socks_list()
    if not urls:
        return []
    circuits = _tor_circuit_count()
    out: list[Lane] = []
    several = len(urls) > 1
    for i, url in enumerate(urls):
        for circ in range(circuits):
            lid = f"tor-{i}-{circ}" if several else f"tor-{circ}"
            out.append(Lane(id=lid, kind="tor", proxy=_socks_isolate(url, f"c{i}{circ}")))
    return out


def _tor_circuit_count() -> int:
    raw = (os.environ.get("TOR_CIRCUITS") or "4").strip()
    try:
        base = max(1, min(MAX_TOR_CIRCUITS, int(raw)))
    except ValueError:
        base = 4
    return max(1, min(MAX_TOR_CIRCUITS, base + _extra_circuits))


def _maybe_grow() -> bool:
    """Abre más circuitos IsolateSOCKSAuth cuando todos los actuales están caídos."""
    global _extra_circuits, _last_grow, _cached
    if not _tor_socks_list():
        return False
    now = time.time()
    with _lock:
        if now - _last_grow < GROW_EVERY_SEC:
            return False
        if _tor_circuit_count() >= MAX_TOR_CIRCUITS:
            return False
        _extra_circuits += GROW_STEP
        _last_grow = now
        _cached = None
        return True


def _proxy_urls() -> list[str]:
    raw = os.environ.get("SCRAPE_PROXIES") or ""
    out: list[str] = []
    for part in raw.replace(";", ",").replace("\n", ",").split(","):
        url = part.strip()
        if url and not url.startswith("#"):
            out.append(url)
    return out


def _tor_socks_list() -> list[str]:
    if os.environ.get("PROPMAP_TEST") == "1" and os.environ.get("PROPMAP_TOR_TEST") != "1":
        return []
    flag = (os.environ.get("TOR_ENABLED") or "").strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return []
    raw = (os.environ.get("TOR_SOCKS") or "").strip()
    urls = [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]
    if not urls and flag in {"1", "true", "yes", "on"}:
        urls = ["socks5h://127.0.0.1:9050"]
    return [url for url in urls if _socks_supported(url)]


def _socks_isolate(url: str, user: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme.lower().startswith("socks"):
        return url
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 9050
    return f"{parsed.scheme}://{user}:{user}@{host}:{port}"


def _socks_supported(url: str) -> bool:
    if not url.lower().startswith("socks"):
        return True
    try:
        import socksio  # noqa: F401
    except ImportError:
        return False
    return True
