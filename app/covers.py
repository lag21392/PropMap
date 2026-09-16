"""Portada local: los CDN de los portales dejan las cards en blanco si se hotlinkea."""
from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any

from .models import Listing

MIN_BYTES = 1800
DIGEST_RE = re.compile(r"^[a-f0-9]{40}$")
_SKIP = ("unsplash.com", "placeholder", "gravatar", "data:", "via.placeholder")


def digest_id(listing_id: str) -> str:
    return hashlib.sha1((listing_id or "").encode("utf-8")).hexdigest()


def public_url(item: Listing) -> str:
    digest = str((item.extra or {}).get("cover") or "").strip()
    if digest and DIGEST_RE.match(digest):
        return f"/media/cover/{digest}"
    return item.image or ""


def apply_public(data: dict[str, Any], extra: dict[str, Any] | None) -> dict[str, Any]:
    digest = str((extra or {}).get("cover") or "").strip()
    if digest and DIGEST_RE.match(digest):
        data["image"] = f"/media/cover/{digest}"
        data["cover"] = digest
    return data


def looks_like_image(blob: bytes) -> str | None:
    if not blob or len(blob) < MIN_BYTES:
        return None
    if blob[:3] == b"\xff\xd8\xff":
        return "jpg"
    if blob[:4] == b"\x89PNG":
        return "png"
    if blob[:6] in {b"GIF87a", b"GIF89a"}:
        return "gif"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "webp"
    return None


def file_for_digest(digest: str) -> Path | None:
    token = (digest or "").strip().lower()
    if not DIGEST_RE.match(token):
        return None
    from .store import DATA_DIR

    folder = Path(DATA_DIR) / "covers" / token[:2]
    for ext in ("jpg", "webp", "png", "gif"):
        path = folder / f"{token}.{ext}"
        if path.is_file() and path.stat().st_size >= MIN_BYTES:
            return path
    return None


def forget(ids: list[str] | tuple[str, ...] | set[str]) -> None:
    for lid in ids or []:
        digest = digest_id(lid)
        path = file_for_digest(digest)
        if path:
            try:
                path.unlink()
            except OSError:
                pass


def ensure(item: Listing) -> bool:
    """Baja la foto principal a disco. No usa Tor: el CDN no es el portal."""
    if not item or not item.id:
        return False
    extra = dict(item.extra or {})
    digest = str(extra.get("cover") or "").strip()
    if digest and file_for_digest(digest):
        return True
    if os.environ.get("PROPMAP_TEST") == "1":
        return bool(digest)
    tried = extra.get("cover_try_at") or ""
    if tried and _age_sec(tried) is not None and _age_sec(tried) < 6 * 3600:
        return bool(digest)
    urls: list[str] = []
    for url in [item.image, *((extra.get("photos") or [])[:8])]:
        href = str(url or "").strip()
        if href and href not in urls and _usable_url(href):
            urls.append(href)
    blob = b""
    kind = None
    for href in urls:
        blob = _download(href, item.url or href) or b""
        kind = looks_like_image(blob)
        if kind:
            break
    extra["cover_try_at"] = _now()
    if not kind or not blob:
        item.extra = extra
        return False
    digest = digest_id(item.id)
    from .store import DATA_DIR

    folder = Path(DATA_DIR) / "covers" / digest[:2]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{digest}.{kind}"
    path.write_bytes(blob)
    extra["cover"] = digest
    extra["cover_kind"] = kind
    extra["cover_at"] = _now()
    item.extra = extra
    return True


def refill(limit: int = 4) -> int:
    if os.environ.get("PROPMAP_TEST") == "1":
        return 0
    from . import store

    n = 0
    for item in store.fetch_cover_backlog(limit):
        if ensure(item):
            store.upsert_many([item])
            n += 1
        else:
            store.upsert_many([item])
    return n


def _usable_url(url: str) -> bool:
    low = url.lower()
    if not low.startswith("http"):
        return False
    return not any(token in low for token in _SKIP)


def _download(url: str, referer: str) -> bytes | None:
    import httpx

    from .http_client import HEADERS

    headers = {
        **HEADERS,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Referer": referer or url,
    }
    try:
        with httpx.Client(headers=headers, follow_redirects=True, timeout=18.0) as client:
            res = client.get(url)
    except Exception:
        return None
    if res.status_code >= 400:
        return None
    return res.content or b""


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _age_sec(raw: str) -> float | None:
    if not raw:
        return None
    from datetime import datetime, timezone

    try:
        when = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, time.time() - when.timestamp())
