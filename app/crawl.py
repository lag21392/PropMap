from __future__ import annotations

import random
import threading
import time

FAST_MIN, FAST_MAX = 0.08, 0.28
SLOW_MIN, SLOW_MAX = 0.7, 1.6
FAST_PAGES = 24
SLOW_PAGES = 48
REST_MIN, REST_MAX = 20, 40
SOON_MIN, SOON_MAX = 8, 16
SLOW_HOSTS = ("properati.com",)
# 429/401/403 superan esto; el pacing normal no (máx. 6 s).
COOL_SEC = 8.0

_gate = threading.Lock()
_abort = threading.Event()
_skip_wait = threading.local()
_job = threading.local()
_host_busy: dict[str, float] = {}
_state = {
    "slow": False,
    "requests": 0,
    "busy_until": 0.0,
    "last_status": 0,
    "last_host": "",
}


def reset() -> None:
    with _gate:
        _host_busy.clear()
        _state.update(requests=0, busy_until=0.0, last_status=0, last_host="")


def without_pace():
    class _Ctx:
        def __enter__(self):
            _skip_wait.active = True
            return self

        def __exit__(self, *_exc):
            _skip_wait.active = False

    return _Ctx()


def job_context(slow: bool = False, should_stop=None):
    class _Ctx:
        def __enter__(self):
            _job.slow = bool(slow)
            _job.should_stop = should_stop
            return self

        def __exit__(self, *_exc):
            _job.slow = None
            _job.should_stop = None

    return _Ctx()


def set_slow(enabled: bool) -> None:
    _state["slow"] = bool(enabled)


def is_slow() -> bool:
    bound = getattr(_job, "slow", None)
    if bound is not None:
        return bool(bound)
    return bool(_state["slow"])


def list_page_limit() -> int:
    return SLOW_PAGES if is_slow() else FAST_PAGES


def rest_seconds(soon: bool = False) -> float:
    if soon:
        return random.uniform(SOON_MIN, SOON_MAX)
    return random.uniform(REST_MIN, REST_MAX)


def request_abort() -> None:
    _abort.set()


def clear_abort() -> None:
    _abort.clear()


def aborted() -> bool:
    if _abort.is_set():
        return True
    stop = getattr(_job, "should_stop", None)
    return bool(stop and stop())


def _pace_key(host: str = "", lane: str = "direct") -> str:
    token = (host or _state["last_host"] or "global").lower().strip()
    return f"{lane or 'direct'}|{token}"


def _host_of_key(key: str) -> str:
    _lane, sep, host = (key or "").partition("|")
    return host if sep else key


def _lane_of_key(key: str) -> str:
    lane, sep, _host = (key or "").partition("|")
    return lane if sep else "direct"


def note_http(status: int, host: str = "", lane: str = "direct") -> None:
    _state["last_status"] = int(status or 0)
    _state["last_host"] = host or _state["last_host"]
    if status in {429, 503}:
        backoff(random.uniform(18, 35), host, lane=lane)
    elif status == 403:
        # Tor: 12-20 min. IP de casa: una página ~20 s, las otras siguen.
        if (lane or "direct") == "direct":
            backoff(random.uniform(18, 22), host, lane=lane)
        else:
            backoff(random.uniform(12 * 60, 20 * 60), host, lane=lane)
    elif status == 401:
        if (lane or "direct") == "direct":
            backoff(random.uniform(18, 22), host, lane=lane)
        else:
            backoff(random.uniform(12, 22), host, lane=lane)


def busy_until(host: str = "", lane: str = "direct") -> float:
    key = _pace_key(host, lane)
    with _gate:
        return float(_host_busy.get(key, 0.0))


def cooling(host: str = "", lane: str = "direct") -> bool:
    """True si ese carril está en cooldown de bloqueo, no en la pausa corta entre requests."""
    return busy_until(host, lane) - time.time() >= COOL_SEC


def host_paused(host: str, lane: str | None = None) -> bool:
    return _host_busy_match(host, lane, min_left=0.0)


def host_cooling(host: str, lane: str | None = None) -> bool:
    """True si algún host emparentado está en cooldown de bloqueo (>= COOL_SEC)."""
    return _host_busy_match(host, lane, min_left=COOL_SEC)


def _host_busy_match(host: str, lane: str | None, min_left: float) -> bool:
    token = (host or "").lower().strip()
    if not token:
        return False
    now = time.time()
    with _gate:
        for key, until in _host_busy.items():
            if until - now < min_left:
                continue
            if lane and _lane_of_key(key) != lane:
                continue
            key_host = _host_of_key(key)
            if token == key_host or token in key_host or key_host in token:
                return True
    return False


def backoff(seconds: float, host: str = "", lane: str = "direct") -> None:
    until = time.time() + max(1.0, seconds)
    key = _pace_key(host, lane)
    with _gate:
        _host_busy[key] = max(_host_busy.get(key, 0.0), until)
        _state["busy_until"] = max(_host_busy.values())


def wait(should_stop=None, host: str = "", lane: str = "direct") -> None:
    if getattr(_skip_wait, "active", False):
        return
    stop = should_stop or getattr(_job, "should_stop", None)
    slow = is_slow() or any(part in (host or "").lower() for part in SLOW_HOSTS)
    lo, hi = (SLOW_MIN, SLOW_MAX) if slow else (FAST_MIN, FAST_MAX)
    gap = random.uniform(lo, hi)
    key = _pace_key(host, lane)
    with _gate:
        now = time.time()
        start = max(now, _host_busy.get(key, 0.0))
        deadline = start + gap
        _host_busy[key] = deadline
        _state["busy_until"] = max(_host_busy.values())
    tick = 0.05 if not is_slow() else 0.15
    while time.time() < deadline:
        if _abort.is_set() or (stop and stop()):
            return
        time.sleep(tick)
    with _gate:
        _state["requests"] += 1


def busy_rows() -> list[dict]:
    now = time.time()
    with _gate:
        rows = [
            {
                "lane": _lane_of_key(key),
                "host": _host_of_key(key),
                "wait_s": round(until - now, 1),
                "cooling": (until - now) >= COOL_SEC,
            }
            for key, until in _host_busy.items()
            if until > now
        ]
    rows.sort(key=lambda row: row["wait_s"], reverse=True)
    return rows[:40]


def snapshot() -> dict:
    now = time.time()
    lo, hi = (SLOW_MIN, SLOW_MAX) if is_slow() else (FAST_MIN, FAST_MAX)
    with _gate:
        waits = [until - now for until in _host_busy.values() if until > now]
        wait_s = round(max(waits, default=0.0), 1)
    return {
        "slow": is_slow(),
        "requests": _state["requests"],
        "wait_s": wait_s,
        "gap_s": [round(lo, 1), round(hi, 1)],
        "pages": list_page_limit(),
        "last_status": _state["last_status"],
        "last_host": _state["last_host"],
    }
