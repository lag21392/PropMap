import pytest
from starlette.requests import Request

from app import matomo
from app.matomo_gate import _rewrite_text


@pytest.fixture(autouse=True)
def _reset_matomo_memory():
    with matomo._ids_lock:
        matomo._ids["site"] = ""
        matomo._ids["token"] = ""
    matomo._recent_hits.clear()
    matomo._public_ip_cache.update({"ip": "", "at": 0.0})
    matomo._visits_cache.update({"rows": [], "at": 0.0})
    yield


def _request(headers=None, client="172.18.0.9", method="GET", path="/", query=b""):
    raw = []
    for key, value in (headers or {}).items():
        raw.append((key.lower().encode("latin-1"), value.encode("latin-1")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query,
        "headers": raw,
        "client": (client, 43210),
        "server": ("propmap", 8000),
    }
    return Request(scope)


def test_prefers_real_ip_from_proxy_over_spoofed_xff():
    req = _request(
        {
            "x-real-ip": "181.15.10.20",
            "x-forwarded-for": "1.1.1.1, 181.15.10.20",
        },
        client="172.18.0.4",
    )
    assert matomo.ip_for_geo(req) == "181.15.10.20"


def test_uses_last_public_xff_when_real_ip_missing():
    req = _request({"x-forwarded-for": "8.8.8.8, 190.104.234.246"}, client="172.18.0.4")
    assert matomo.ip_for_geo(req) == "190.104.234.246"


def test_keeps_lan_ip_on_the_request_without_calling_ipify(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("no hay que ir a ipify al leer la IP del pedido")

    monkeypatch.setattr(matomo.httpx, "Client", boom)
    req = _request({"x-real-ip": "192.168.3.10", "x-forwarded-for": "192.168.3.10"}, client="172.18.0.4")
    assert matomo.ip_for_geo(req) == "192.168.3.10"


def test_lan_hit_uses_server_ip_so_the_map_has_coords(monkeypatch):
    captured = []

    class Fake:
        def __init__(self, **_kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def get(self, url, params=None, headers=None):
            captured.append(dict(params or {}))

            class Res:
                status_code = 204
                content = b""
                headers = {}

            return Res()

    monkeypatch.setenv("MATOMO_INTERNAL_URL", "http://matomo")
    monkeypatch.setenv("MATOMO_GEO_FALLBACK_IP", "186.62.8.21")
    monkeypatch.setattr(matomo, "_site_id", lambda: "1")
    monkeypatch.setattr(matomo, "_token", lambda: "tok")
    monkeypatch.setattr(matomo.httpx, "Client", Fake)
    matomo._send_visit(
        {
            "geo_ip": "192.168.3.10",
            "ua": "Mozilla",
            "lang": "es-ES,es;q=0.9",
            "url": "https://propmaplag.duckdns.org/",
            "ref": "",
            "vid": "cccccccccccccccccccccccccccccccc",
            "name": "pageview",
        }
    )
    assert captured[0]["cip"] == "186.62.8.21"
    assert "lang" in captured[0]


def test_public_visitor_is_not_replaced_by_server_ip(monkeypatch):
    monkeypatch.setenv("MATOMO_GEO_FALLBACK_IP", "186.62.8.21")
    assert matomo.cip_for_matomo("190.104.234.246") == "190.104.234.246"
    assert matomo.cip_for_matomo("192.168.3.10") == "186.62.8.21"


def test_cgnat_counts_as_visitor_ip():
    req = _request({"x-real-ip": "100.64.12.40"}, client="172.18.0.4")
    assert matomo.ip_for_geo(req) == "100.64.12.40"


def test_config_uses_names_adblockers_do_not_match(monkeypatch, tmp_path):
    from app import store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.sqlite")
    store.init()
    store.set_meta(matomo.META_SITE, "1")
    cfg = matomo.config()
    assert cfg["src"] == "/q/l.js"
    assert cfg["tracker"] == "/q/l"
    assert "matomo.js" not in cfg["src"]
    assert "matomo.php" not in cfg["tracker"]


def test_queue_visit_sends_cip(monkeypatch):
    matomo._recent_hits.clear()
    captured = []

    class Fake:
        def __init__(self, **_kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def get(self, url, params=None, headers=None):
            captured.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {})})

            class Res:
                status_code = 204
                content = b""
                headers = {}

            return Res()

    monkeypatch.setenv("MATOMO_INTERNAL_URL", "http://matomo")
    monkeypatch.setenv("APP_PUBLIC_URL", "https://propmaplag.duckdns.org")
    monkeypatch.setattr(matomo, "_site_id", lambda: "1")
    monkeypatch.setattr(matomo, "_token", lambda: "tok")
    monkeypatch.setattr(matomo.httpx, "Client", Fake)
    req = _request({"x-real-ip": "181.15.10.20", "user-agent": "Mozilla"})
    matomo._send_visit(
        {
            "geo_ip": matomo.ip_for_geo(req),
            "ua": "Mozilla",
            "lang": "",
            "url": "https://propmaplag.duckdns.org/",
            "ref": "",
            "vid": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "name": "pageview",
        }
    )
    assert captured
    assert captured[0]["params"]["cip"] == "181.15.10.20"
    assert captured[0]["params"]["_id"] == "aaaaaaaaaaaaaaaa"
    assert captured[0]["params"]["token_auth"] == "tok"


def test_proxy_tracker_overrides_cip(monkeypatch):
    captured = []

    class Fake:
        def __init__(self, **_kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def get(self, url, params=None, headers=None):
            captured.append({"params": dict(params or {}), "headers": dict(headers or {})})

            class Res:
                status_code = 204
                content = b"ok"
                headers = {"content-type": "text/plain"}

            return Res()

    monkeypatch.setenv("MATOMO_INTERNAL_URL", "http://matomo")
    monkeypatch.setattr(matomo, "_token", lambda: "tok")
    monkeypatch.setattr(matomo.httpx, "Client", Fake)
    req = _request(
        {"x-real-ip": "190.104.234.246"},
        path="/q/l",
        query=b"idsite=1&rec=1",
    )
    res = matomo.proxy_tracker(req, b"")
    assert res.status_code == 204
    assert captured[0]["params"]["cip"] == "190.104.234.246"
    assert captured[0]["headers"]["x-real-ip"] == "190.104.234.246"


def test_rewrite_does_not_prefix_quiet_tracker(monkeypatch):
    monkeypatch.setenv("APP_PUBLIC_URL", "https://propmaplag.duckdns.org")
    out = _rewrite_text('src="/q/l.js" endpoint="/q/l"')
    assert 'src="/q/l.js"' in out
    assert 'endpoint="/q/l"' in out
    assert "/stats/q/" not in out


def test_install_script_trusts_docker_proxy_ranges():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "scripts" / "matomo-install.php").read_text(encoding="utf-8")
    assert 'proxy_ips[] = "172.16.0.0/12"' in text
    assert "proxy_ip_read_last_in_list = 1" in text


def test_send_visit_uses_cached_ids_without_sqlite(monkeypatch):
    captured = []

    class Fake:
        def __init__(self, **_kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def get(self, url, params=None, headers=None):
            captured.append(dict(params or {}))

            class Res:
                status_code = 204
                content = b""
                headers = {}

            return Res()

    def boom(*_a, **_k):
        raise AssertionError("sqlite no tiene que entrar en el hit")

    monkeypatch.setenv("MATOMO_INTERNAL_URL", "http://matomo")
    monkeypatch.setattr("app.store.get_meta", boom)
    monkeypatch.setattr(matomo.httpx, "Client", Fake)
    matomo.remember_ids(site="1", token="tok")
    matomo._send_visit(
        {
            "geo_ip": "181.15.10.20",
            "ua": "Mozilla",
            "lang": "",
            "url": "https://propmaplag.duckdns.org/",
            "ref": "",
            "vid": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "name": "pageview",
        }
    )
    assert captured[0]["idsite"] == "1"
    assert captured[0]["cip"] == "181.15.10.20"


def test_compose_sets_matomo_site_id():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "compose.yaml").read_text(encoding="utf-8")
    assert "MATOMO_SITE_ID" in text


def test_parse_live_visits_marks_missing_coords():
    rows = matomo._parse_live_visits(
        [
            {
                "lastActionTimestamp": 1_758_043_444,
                "visitDuration": 2,
                "country": "España",
                "city": "",
                "referrerTypeName": "Entrada directa",
                "latitude": False,
                "longitude": False,
            }
        ]
    )
    assert rows[0]["country"] == "España"
    assert rows[0]["source"] == "Entrada directa"
    assert rows[0]["sec"] == 2
    assert rows[0]["on_map"] is False
    assert ":" in rows[0]["when"]
