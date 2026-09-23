"""Elige brotli si el proceso lo tiene instalado. Si no, sigue el gzip de Starlette."""

from __future__ import annotations

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Receive, Scope, Send

_COMPRESSIBLE = (
    "text/html",
    "text/plain",
    "text/css",
    "application/javascript",
    "application/json",
    "application/xml",
    "image/svg+xml",
    "application/manifest+json",
)


def brotli_ready() -> bool:
    try:
        import brotli  # noqa: F401
    except ImportError:
        return False
    return True


def encoding_for(accept: str, *, brotli_ok: bool) -> str:
    tokens = []
    for part in (accept or "").lower().split(","):
        token = part.split(";", 1)[0].strip()
        if token:
            tokens.append(token)
    if brotli_ok and "br" in tokens:
        return "br"
    if "gzip" in tokens:
        return "gzip"
    return ""


def compress_br(data: bytes) -> bytes:
    import brotli

    return brotli.compress(data, quality=5)


def _compressible(content_type: str) -> bool:
    base = (content_type or "").split(";", 1)[0].strip().lower()
    return base in _COMPRESSIBLE or base.startswith("text/")


class BrotliOnce:
    """Comprime la respuesta entera. Las páginas SEO y el sitemap entran; un stream largo no."""

    def __init__(self, app: ASGIApp, minimum_size: int = 800, maximum_size: int = 1_500_000):
        self.app = app
        self.minimum_size = minimum_size
        self.maximum_size = maximum_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        start_message: dict | None = None
        chunks: list[bytes] = []
        skip = False

        async def send_body(message: dict) -> None:
            nonlocal start_message, skip
            if message["type"] == "http.response.start":
                start_message = message
                headers = Headers(raw=message.get("headers"))
                if "content-encoding" in headers or not _compressible(headers.get("content-type", "")):
                    skip = True
                return
            if message["type"] != "http.response.body":
                if start_message is not None:
                    await send(start_message)
                    start_message = None
                await send(message)
                return
            if skip or start_message is None:
                if start_message is not None:
                    await send(start_message)
                    start_message = None
                await send(message)
                return
            chunks.append(message.get("body") or b"")
            if message.get("more_body"):
                return
            body = b"".join(chunks)
            headers = MutableHeaders(raw=start_message["headers"])
            headers.add_vary_header("Accept-Encoding")
            if self.minimum_size <= len(body) <= self.maximum_size:
                try:
                    packed = compress_br(body)
                except Exception:
                    packed = body
                if packed and len(packed) < len(body):
                    body = packed
                    headers["Content-Encoding"] = "br"
            headers["Content-Length"] = str(len(body))
            await send(start_message)
            await send({"type": "http.response.body", "body": body, "more_body": False})

        await self.app(scope, receive, send_body)
