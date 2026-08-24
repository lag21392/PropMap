from __future__ import annotations

import random
import threading
import time

FAST_MIN, FAST_MAX = 0.25, 0.85
SLOW_MIN, SLOW_MAX = 10.0, 22.0
FAST_PAGES = 8
SLOW_PAGES = 40
REST_MIN, REST_MAX = 8 * 60, 14 * 60
SOON_MIN, SOON_MAX = 40, 90

_gate = threading.Lock()
_abort = threading.Event()
_state = {
    "slow": False,
    "requests": 0,
    "busy_until": 0.0,
    "cooldown_until": 0.0,
    "last_status": 0,
    "last_host": "",
}


def set_slow(enabled: bool) -> None:
    _state["slow"] = bool(enabled)


def is_slow() -> bool:
    return bool(_state["slow"])


def list_page_limit() -> int:
    return SLOW_PAGES if _state["slow"] else FAST_PAGES


def rest_seconds(soon: bool = False) -> float:
    if soon:
        return random.uniform(SOON_MIN, SOON_MAX)
    return random.uniform(REST_MIN, REST_MAX)


def request_abort() -> None:
    _abort.set()


def clear_abort() -> None:
    _abort.clear()


def aborted() -> bool:
    return _abort.is_set()


def note_http(status: int, host: str = "") -> None:
    _state["last_status"] = int(status or 0)
    _state["last_host"] = host or _state["last_host"]
    if status in {429, 503}:
        backoff(random.uniform(75, 140))
    elif status == 403:
        backoff(random.uniform(40, 80))


def backoff(seconds: float) -> None:
    _state["cooldown_until"] = max(_state["cooldown_until"], time.time() + max(5.0, seconds))


def wait(should_stop=None) -> None:
    lo, hi = (SLOW_MIN, SLOW_MAX) if _state["slow"] else (FAST_MIN, FAST_MAX)
    extra = max(0.0, _state["cooldown_until"] - time.time())
    gap = random.uniform(lo, hi) + extra
    with _gate:
        start = max(time.time(), _state["busy_until"])
        deadline = start + gap
        _state["busy_until"] = deadline
    tick = 0.08 if not _state["slow"] else 0.35
    while time.time() < deadline:
        if _abort.is_set() or (should_stop and should_stop()):
            return
        time.sleep(tick)
    with _gate:
        _state["requests"] += 1


def snapshot() -> dict:
    now = time.time()
    lo, hi = (SLOW_MIN, SLOW_MAX) if _state["slow"] else (FAST_MIN, FAST_MAX)
    return {
        "slow": _state["slow"],
        "requests": _state["requests"],
        "wait_s": round(max(0.0, _state["busy_until"] - now), 1),
        "gap_s": [round(lo, 1), round(hi, 1)],
        "pages": list_page_limit(),
        "last_status": _state["last_status"],
        "last_host": _state["last_host"],
    }
