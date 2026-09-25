"""HTTP fetching package for PropMap.

Deep module providing a small interface for fetching pages with
caching, fallbacks, rate limiting and circuit breaking.

Public interface:
- fetch_text
- fetch_bytes
- fetch_json
- decode_js_object
- portal_way
- portal_host
- reset_fetch_state
- host_is_blocked
- PageGone
"""

from .errors import PageGone
from .fetchers import fetch_bytes, fetch_bytes_async, fetch_json, fetch_json_async, fetch_text, fetch_text_async
from .policy import host_is_blocked, portal_host, portal_way, reset_fetch_state
from .utils import decode_js_object, proxy_html_looks_valid, translate_proxy_url

__all__ = [
    "PageGone",
    "fetch_text",
    "fetch_text_async",
    "fetch_bytes",
    "fetch_bytes_async",
    "fetch_json",
    "fetch_json_async",
    "decode_js_object",
    "portal_way",
    "portal_host",
    "reset_fetch_state",
    "host_is_blocked",
    "translate_proxy_url",
    "proxy_html_looks_valid",
]
