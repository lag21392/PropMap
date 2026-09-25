"""Utility functions for HTTP package."""

from __future__ import annotations

import json

from . import client, validators

def translate_proxy_url(url: str, lang: str = "es-en") -> str:
    from .fallbacks import translate_proxy_url as _t
    return _t(url, lang)

def proxy_html_looks_valid(text: str) -> bool:
    return validators.proxy_html_looks_valid(text)

def decode_js_object(text: str, marker: str):
    idx = text.find(marker)
    if idx < 0:
        return None
    start = text.find("{", idx)
    if start < 0:
        return None
    decoder = json.JSONDecoder()
    try:
        data, _end = decoder.raw_decode(text[start:])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None
