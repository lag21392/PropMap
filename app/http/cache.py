"""Page cache for listings."""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

from .validators import _listing_html_ok

PAGE_CACHE_TTL = 12 * 3600

def _page_cache_dir() -> Path | None:
    if os.environ.get("PROPMAP_TEST") == "1":
        return None
    from app.store import DATA_DIR

    path = DATA_DIR / "page_cache"
    path.mkdir(parents=True, exist_ok=True)
    return path

def _read_page_cache(url: str) -> str | None:
    from .validators import _looks_like_listing

    folder = _page_cache_dir()
    if folder is None or not _looks_like_listing(url):
        return None
    path = folder / f"{hashlib.sha1(url.encode()).hexdigest()}.html"
    if not path.is_file():
        return None
    age = time.time() - path.stat().st_mtime
    if age > PAGE_CACHE_TTL:
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    if not _listing_html_ok(url, text):
        return None
    return text

def _remember_page(url: str, text: str) -> str:
    from .validators import _looks_like_listing, _listing_html_ok

    folder = _page_cache_dir()
    if folder is not None and _looks_like_listing(url) and _listing_html_ok(url, text):
        path = folder / f"{hashlib.sha1(url.encode()).hexdigest()}.html"
        try:
            # Cachear HTML crudo limitado a 2 MB para ahorrar disco
            max_cache = 2 * 1024 * 1024
            to_write = text if len(text) <= max_cache else text[:max_cache]
            path.write_text(to_write, encoding="utf-8")
        except OSError:
            pass
    return text
