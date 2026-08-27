from __future__ import annotations

import random
import threading
import time

FAST_MIN, FAST_MAX = 0.25, 0.85
SLOW_MIN, SLOW_MAX = 2.5, 6.0
FAST_PAGES = 8
SLOW_PAGES = 40
REST_MIN, REST_MAX = 20, 40
SOON_MIN, SOON_MAX = 8, 16
SLOW_HOSTS = ("properati.com",)

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


def note_http(status: int, host: str = "") -> None:
    _state["last_status"] = int(status or 0)
    _state["last_host"] = host or _state["last_host"]
    if status in {429, 503}:
        backoff(random.uniform(18, 35), host)
    elif status in {401, 403}:
        backoff(random.uniform(12, 22), host)


def backoff(seconds: float, host: str = "") -> None:
    until = time.time() + max(1.0, seconds)
    key = host or _state["last_host"] or "global"
    with _gate:
        _host_busy[key] = max(_host_busy.get(key, 0.0), until)
        _state["busy_until"] = max(_host_busy.values())


def wait(should_stop=None, host: str = "") -> None:
    if getattr(_skip_wait, "active", False):
        return
    stop = should_stop or getattr(_job, "should_stop", None)
    slow = is_slow() or any(part in (host or "").lower() for part in SLOW_HOSTS)
    lo, hi = (SLOW_MIN, SLOW_MAX) if slow else (FAST_MIN, FAST_MAX)
    gap = random.uniform(lo, hi)
    key = host or _state["last_host"] or "global"
    with _gate:
        now = time.time()
        start = max(now, _host_busy.get(key, 0.0))
        deadline = start + gap
        _host_busy[key] = deadline
        _state["busy_until"] = max(_host_busy.values())
    tick = 0.25 if not is_slow() else 0.4
    while time.time() < deadline:
        if _abort.is_set() or (stop and stop()):
            return
        time.sleep(tick)
    with _gate:
        _state["requests"] += 1


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
