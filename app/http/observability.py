"""Observability helpers for HTTP."""

from __future__ import annotations

def _observe(host: str, lane: str, status: int) -> None:
    try:
        from app.ops import note

        note("http", lane=lane or "direct", host=(host or "")[:60], status=int(status or 0))
    except Exception:
        return
