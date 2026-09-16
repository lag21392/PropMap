from __future__ import annotations

import os
import secrets

from fastapi import HTTPException


def search_password() -> str:
    return (os.environ.get("SEARCH_PASSWORD") or "").strip()


def _candidates() -> list[str]:
    seen: list[str] = []
    for key in ("SEARCH_PASSWORD", "MATOMO_PASSWORD"):
        value = (os.environ.get(key) or "").strip()
        if value and value not in seen:
            seen.append(value)
    return seen


def password_matches(got: str | None) -> bool:
    provided = (got or "").encode()
    if not provided:
        return False
    for expected in _candidates():
        want = expected.encode()
        if len(provided) == len(want) and secrets.compare_digest(provided, want):
            return True
    return False


def require_search_password(got: str | None) -> None:
    if password_matches(got):
        return
    raise HTTPException(status_code=401, detail="Contraseña incorrecta")
