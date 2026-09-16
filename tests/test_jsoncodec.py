import gzip

from app.jsoncodec import dumps, gunzip_bytes, gzip_bytes, loads


def test_dumps_roundtrip_unicode():
    raw = dumps({"title": "Palermo, CABA", "n": 2, "ok": True})
    assert isinstance(raw, bytes)
    data = loads(raw)
    assert data["title"] == "Palermo, CABA"
    assert data["n"] == 2
    assert data["ok"] is True


def test_gzip_bytes_are_stable_and_gunzip():
    payload = dumps({"listings": [{"id": "a"}]})
    packed = gzip_bytes(payload)
    again = gzip_bytes(payload)
    assert packed == again
    assert packed[:2] == b"\x1f\x8b"
    assert gunzip_bytes(packed) == payload
    assert gzip.decompress(packed) == payload


def test_gzip_bytes_chunked_roundtrip():
    payload = dumps({"listings": [{"id": f"x{i}", "title": "Departamento en venta Palermo"} for i in range(12000)]})
    assert len(payload) > 262_144
    packed = gzip_bytes(payload)
    assert packed[:2] == b"\x1f\x8b"
    assert gunzip_bytes(packed) == payload
    assert gzip.decompress(packed) == payload
