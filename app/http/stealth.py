"""Stealth fetch via just-scrape and SGAI."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import httpx

SGAI_SCRAPE = "https://v2-api.scrapegraphai.com/api/scrape"

def _sgai_key() -> str:
    return (os.environ.get("SGAI_API_KEY") or os.environ.get("SGAI_APIKEY") or "").strip()

def _try_stealth_fetch(url: str, timeout: float) -> str | None:
    from .validators import _looks_like_listing

    if os.environ.get("PROPMAP_TEST") == "1" and not os.environ.get("PROPMAP_STEALTH_TEST"):
        return None
    if not _looks_like_listing(url) or not _sgai_key():
        return None
    html = _fetch_just_scrape_cli(url, timeout)
    if html:
        return html
    return _fetch_sgai_scrape(url, timeout)

def _fetch_just_scrape_cli(url: str, timeout: float) -> str | None:
    binary = shutil.which("just-scrape")
    if not binary:
        return None
    try:
        proc = subprocess.run(
            [binary, "scrape", url, "--stealth", "--json", "--country-code", "AR"],
            capture_output=True,
            text=True,
            timeout=max(timeout, 90.0),
            env={**os.environ, "SGAI_API_KEY": _sgai_key()},
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return _html_from_stealth(payload)

def _fetch_sgai_scrape(url: str, timeout: float) -> str | None:
    key = _sgai_key()
    if not key:
        return None
    try:
        with httpx.Client(timeout=max(timeout, 90.0)) as client:
            response = client.post(
                SGAI_SCRAPE,
                headers={"SGAI-APIKEY": key, "Content-Type": "application/json"},
                json={
                    "url": url,
                    "formats": [{"type": "html"}],
                    "fetchConfig": {"stealth": True, "country": "ar", "mode": "js"},
                },
            )
            if response.status_code >= 400:
                return None
            return _html_from_stealth(response.json())
    except Exception:
        return None

def _html_from_stealth(payload) -> str | None:
    if isinstance(payload, str) and len(payload) > 800:
        return payload
    if not isinstance(payload, dict):
        return None
    for key in ("html", "content", "result", "data"):
        val = payload.get(key)
        if isinstance(val, str) and len(val) > 800:
            return val
        if isinstance(val, dict):
            nested = _html_from_stealth(val)
            if nested:
                return nested
    return None
