from __future__ import annotations

import os
import secrets

from fastapi import HTTPException


def search_password() -> str:
    return (os.environ.get("SEARCH_PASSWORD") or "").strip()


def password_matches(got: str | None) -> bool:
    expected = search_password()
    if not expected:
        return False
    provided = (got or "").encode()
    want = expected.encode()
    if len(provided) != len(want):
        return False
    return secrets.compare_digest(provided, want)


def require_search_password(got: str | None) -> None:
    if password_matches(got):
        return
    raise HTTPException(status_code=401, detail="Contraseña incorrecta")
