from __future__ import annotations

import gzip
import io
import json
import time
from typing import Any

try:
    import orjson
except ImportError:
    orjson = None

# Nivel 4: el gzip se arma una vez al cerrar el cache, no en cada GET.
GZIP_LEVEL = 4
_GZIP_YIELD_EVERY = 262_144


def dumps(obj: Any) -> bytes:
    """JSON compacto. orjson es C sobre CPython; PyPy acá iría más lento."""
    if orjson is not None:
        return orjson.dumps(obj)
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def loads(raw: bytes | str | bytearray) -> Any:
    if orjson is not None:
        return orjson.loads(raw)
    if isinstance(raw, (bytes, bytearray)):
        return json.loads(raw.decode("utf-8"))
    return json.loads(raw)


def gzip_bytes(raw: bytes) -> bytes:
    if len(raw) < _GZIP_YIELD_EVERY:
        return gzip.compress(raw, compresslevel=GZIP_LEVEL, mtime=0)
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=GZIP_LEVEL, mtime=0) as zh:
        view = memoryview(raw)
        for i in range(0, len(raw), _GZIP_YIELD_EVERY):
            zh.write(view[i : i + _GZIP_YIELD_EVERY])
            time.sleep(0)
    return buf.getvalue()


def gunzip_bytes(raw: bytes) -> bytes:
    return gzip.decompress(raw)
