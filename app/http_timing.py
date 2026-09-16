"""Duraciones recientes de la web. Sin sqlite: no pelea el candado del scraper."""

from __future__ import annotations

import statistics
import threading
from collections import deque
from typing import Any

KEEP = 40

_lock = threading.Lock()
_samples: dict[str, deque[float]] = {}
_counts: dict[str, int] = {}
_extra: dict[str, dict[str, Any]] = {}


def reset() -> None:
    with _lock:
        _samples.clear()
        _counts.clear()
        _extra.clear()


def note(name: str, ms: float, extra: dict[str, Any] | None = None) -> None:
    if ms < 0 or not name:
        return
    key = str(name)[:80]
    with _lock:
        bucket = _samples.get(key)
        if bucket is None:
            bucket = deque(maxlen=KEEP)
            _samples[key] = bucket
        bucket.append(float(ms))
        _counts[key] = _counts.get(key, 0) + 1
        if extra:
            _extra[key] = {str(k)[:40]: _clip(v) for k, v in list(extra.items())[:8]}


def snapshot() -> dict[str, dict[str, Any]]:
    with _lock:
        names = list(_samples)
        out: dict[str, dict[str, Any]] = {}
        for name in names:
            vals = list(_samples[name])
            if not vals:
                continue
            out[name] = {
                "n": _counts.get(name, len(vals)),
                "last_ms": round(vals[-1], 1),
                "p50_ms": round(_pct(vals, 0.5), 1),
                "p95_ms": round(_pct(vals, 0.95), 1),
                "max_ms": round(max(vals), 1),
                "extra": dict(_extra.get(name) or {}),
            }
        return out


def _pct(vals: list[float], q: float) -> float:
    if len(vals) == 1:
        return vals[0]
    try:
        return float(statistics.quantiles(vals, n=20, method="inclusive")[max(0, min(19, int(q * 20) - 1))])
    except Exception:
        ordered = sorted(vals)
        return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * q))]


def _clip(value: Any) -> Any:
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:80]
