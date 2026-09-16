from fastapi.testclient import TestClient

from app import crawl, main, ops
from app.http_client import fetch_text, reset_fetch_state
from app.llm_enrich import LLM_SCHEMA
from app.models import Listing
from tests.test_http_client import _Client, _Resp


def test_note_rolls_http_and_llm():
    ops.reset()
    ops.note("http", lane="direct", host="www.zonaprop.com.ar", status=200)
    ops.note("http", lane="tor", host="www.zonaprop.com.ar", status=403)
    ops.note("llm", outcome="ok")
    ops.note("ingest", n=4, source="argenprop")
    snap = ops.snapshot()
    assert snap["http"]["n"] == 2
    assert snap["http"]["by_lane"]["direct"] == 1
    assert snap["http"]["by_lane"]["tor"] == 1
    assert snap["http"]["by_status"]["403"] == 1
    assert snap["http"]["by_group"]["local"] == 1
    assert snap["http"]["by_group"]["tor"] == 1
    assert snap["series"][-1]["lanes"]["local"] == 1
    assert snap["series"][-1]["lanes"]["tor"] == 1
    assert snap["llm"]["by_outcome"]["ok"] == 1
    assert snap["ingest"]["by_source"]["argenprop"] == 4
    assert snap["series"][-1]["http"] == 2
    text = ops.prometheus()
    assert "propmap_up 1" in text
    assert 'propmap_http_total{lane="direct"}' in text
    assert 'propmap_http_group_total{group="local"}' in text
    assert "propmap_listings" in text


def test_inventory_counts_llm_and_details(tmp_path, monkeypatch):
    from app import store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "listings.sqlite")
    store.init()
    ops.reset()
    ready = Listing(
        source="zonaprop",
        source_id="ops-ready",
        url="https://example.com/a",
        title="Depto",
        property_type="departamento",
        city="caba",
        details_scraped=True,
        extra={"llm_ready": True, "llm_ver": LLM_SCHEMA, "llm_partial": False},
    )
    wait = Listing(
        source="argenprop",
        source_id="ops-wait",
        url="https://example.com/b",
        title="Casa",
        property_type="casa",
        city="cordoba",
        extra={"await_llm": True},
    )
    store.upsert_many([ready, wait])
    inv = ops.inventory(force=True)
    assert inv["listings"] == 2
    assert inv["details"] == 1
    assert inv["llm_done"] == 1
    assert inv["await_llm"] == 1
    assert inv["llm_need"] == 1
    assert inv["details_need"] == 1
    ids = {row["id"] for row in inv["sources"]}
    assert {"zonaprop", "argenprop"} <= ids
    cities = {row["id"] for row in inv["cities"]}
    assert "caba" in cities


def test_fetch_text_notes_lane(monkeypatch):
    reset_fetch_state()
    url = "https://www.argenprop.com/departamentos-venta.html"
    monkeypatch.setattr(
        "app.http_client.httpx.Client",
        lambda **kw: _Client({"https://www.argenprop.com/": _Resp(200, "<html>ok</html>", url)}, **kw),
    )
    monkeypatch.setattr("app.http_client.crawl.wait", lambda **kw: None)
    monkeypatch.setattr("app.http_client.crawl.aborted", lambda: False)
    fetch_text(url, paced=False, retries=1)
    snap = ops.snapshot()
    assert snap["http"]["n"] >= 1
    assert snap["http"]["by_lane"].get("direct", 0) >= 1


def test_tablero_and_ops_api_need_admin(monkeypatch):
    monkeypatch.setenv("SEARCH_PASSWORD", "test-secret")
    with TestClient(main.app) as client:
        gate = client.get("/tablero")
        assert gate.status_code == 200
        assert "Contraseña" in gate.text
        assert b"tablero.js" not in gate.content
        assert client.get("/api/ops").status_code == 401
        assert client.post("/api/ops", json={"password": "nope"}).status_code == 401
        by_password = client.post("/api/ops", json={"password": "test-secret"})
        assert by_password.status_code == 200
        assert "propmap_ops" in by_password.cookies
        assert client.get("/api/ops").status_code == 200
        login = client.post(
            "/stats/login",
            data={"password": "test-secret", "next": "/tablero"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        assert login.headers["location"] == "/tablero"
        page = client.get("/tablero")
        assert page.status_code == 200
        assert b"tablero.js" in page.content
        data = client.get("/api/ops")
        assert data.status_code == 200
        body = data.json()
        assert "telemetry" in body
        assert "inventory" in body
        assert "egress" in body
        assert "live" in body
        assert "movement" in body
        assert "llm_pipe" in body
        assert "details_pipe" in body
        assert "need" in body["llm_pipe"]
        assert b"laneChart" in page.content
        assert b"tablero.js?v=15" in page.content
        prom = client.get("/api/ops/metrics")
        assert prom.status_code == 200
        assert "propmap_up 1" in prom.text
        posted = client.post("/api/ops", json={"password": "nope"})
        assert posted.status_code == 200


def test_ops_header_unlocks_metrics(monkeypatch):
    monkeypatch.setenv("SEARCH_PASSWORD", "test-secret")
    with TestClient(main.app) as client:
        res = client.get("/api/ops", headers={"x-propmap-ops": "test-secret"})
        assert res.status_code == 200
        assert "tracks" in (res.json().get("egress") or {})


def test_login_rejects_external_next(monkeypatch):
    monkeypatch.setenv("SEARCH_PASSWORD", "test-secret")
    with TestClient(main.app) as client:
        res = client.post(
            "/stats/login",
            data={"password": "test-secret", "next": "//evil.example/phish"},
            follow_redirects=False,
        )
        assert res.status_code == 303
        assert res.headers["location"] == "/stats/"


def test_snapshot_groups_vpn_and_movement(monkeypatch):
    ops.reset()
    ops.note("http", lane="direct", host="www.zonaprop.com.ar", status=200)
    ops.note("http", lane="tor", host="www.zonaprop.com.ar", status=200)
    ops.note("http", lane="proxy-0", host="www.argenprop.com", status=200)
    ops.note("new", n=5, source="zonaprop")
    ops.note("gone", n=2)
    ops.note("llm", outcome="ok")
    ops.note("http", lane="translate", host="www.zonaprop.com.ar", status=200)
    snap = ops.snapshot()
    assert snap["http"]["by_group"]["local"] == 1
    assert snap["http"]["by_group"]["tor"] == 1
    assert snap["http"]["by_group"]["vpn"] == 1
    assert snap["http"]["by_group"]["translate"] == 1
    assert snap["new"]["n"] == 5
    assert snap["gone"]["n"] == 2
    assert snap["series"][-1]["new"] == 5
    assert snap["series"][-1]["gone"] == 2
    assert snap["series"][-1]["llm_by"]["ok"] == 1
    monkeypatch.setattr(
        ops,
        "inventory",
        lambda force=False: {
            "listings": 10,
            "details": 4,
            "details_need": 6,
            "details_pct": 40.0,
            "llm_done": 3,
            "llm_partial": 1,
            "await_llm": 2,
            "llm_need": 6,
            "llm_pct": 30.0,
            "sources": [],
            "cities": [],
            "schema": 6,
        },
    )
    dash = ops.dashboard()
    assert dash["movement"]["new"] == 5
    assert dash["movement"]["gone"] == 2
    assert dash["movement"]["net"] == 3
    assert dash["llm_pipe"]["need"] == 6
    assert dash["llm_pipe"]["done"] == 3
    assert dash["llm_pipe"]["this_min"] == 1
    assert dash["llm_pipe"]["ok_h"] == 1
    assert dash["llm_pipe"]["ok_d"] == 1
    assert dash["llm_pipe"]["ok_24"] == 1
    assert dash["llm_pipe"]["per_hour"] == 1
    assert dash["llm_pipe"]["rate_scope"] == "hour"
    assert dash["details_pipe"]["need"] == 6
    text = ops.prometheus()
    assert "propmap_listings_new_total 5" in text
    assert "propmap_listings_gone_total 2" in text
    assert "propmap_listings_llm_need 6" in text
    assert 'propmap_http_group_total{group="vpn"} 1' in text


def test_drop_listings_notes_gone(tmp_path, monkeypatch):
    from app import store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "listings.sqlite")
    store.init()
    ops.reset()
    item = Listing(
        source="zonaprop",
        source_id="ops-gone",
        url="https://example.com/gone",
        title="Depto",
        property_type="departamento",
        city="caba",
    )
    store.upsert_many([item])
    assert store.drop_listings([item.id]) == 1
    assert ops.snapshot()["gone"]["n"] == 1


def test_movement_survives_process_reset(tmp_path, monkeypatch):
    from app import store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "listings.sqlite")
    monkeypatch.setenv("PROPMAP_OPS_PERSIST", "1")
    store.init()
    ops.reset()
    ops.note("new", n=4, source="zonaprop")
    ops.note("gone", n=1)
    ops.note("ingest", n=9, source="zonaprop")
    ops.reset()
    snap = ops.snapshot()
    assert snap["series"][-1]["new"] == 4
    assert snap["series"][-1]["gone"] == 1
    assert snap["series"][-1]["ingest"] == 9
    monkeypatch.setattr(
        ops,
        "inventory",
        lambda force=False: {
            "listings": 0,
            "details": 0,
            "details_need": 0,
            "details_pct": 0.0,
            "llm_done": 0,
            "llm_partial": 0,
            "await_llm": 0,
            "llm_need": 0,
            "llm_pct": 0.0,
            "sources": [],
            "cities": [],
            "schema": 6,
        },
    )
    dash = ops.dashboard()
    assert dash["movement"]["new"] == 4
    assert dash["movement"]["gone"] == 1
    assert dash["movement"]["seen"] == 9
    assert dash["movement"]["new_24h"] == 4
    assert dash["movement"]["gone_24h"] == 1
    assert dash["movement"]["scope"] == "today"


def _empty_inv(**over):
    data = {
        "listings": 0,
        "details": 0,
        "details_need": 0,
        "details_pct": 0.0,
        "llm_done": 0,
        "llm_partial": 0,
        "await_llm": 0,
        "llm_need": 0,
        "llm_pct": 0.0,
        "sources": [],
        "cities": [],
        "schema": 6,
    }
    data.update(over)
    return data


def test_llm_and_details_survive_process_reset(tmp_path, monkeypatch):
    from app import store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "listings.sqlite")
    monkeypatch.setenv("PROPMAP_OPS_PERSIST", "1")
    store.init()
    ops.reset()
    ops.note("llm", outcome="ok")
    ops.note("llm", outcome="fail")
    ops.note("details", outcome="ok")
    ops.reset()
    snap = ops.snapshot()
    assert snap["series"][-1]["llm_by"]["ok"] == 1
    assert snap["series"][-1]["llm_by"]["fail"] == 1
    assert snap["series"][-1]["llm"] == 1
    assert snap["series"][-1]["details_by"]["ok"] == 1
    monkeypatch.setattr(ops, "inventory", lambda force=False: _empty_inv(llm_need=10, details_need=4))
    dash = ops.dashboard()
    assert dash["llm_pipe"]["this_min"] == 1
    assert dash["llm_pipe"]["ok_h"] == 1
    assert dash["llm_pipe"]["ok_d"] == 1
    assert dash["llm_pipe"]["ok_24"] == 1
    assert dash["llm_pipe"]["fail_h"] == 1
    assert dash["llm_pipe"]["per_hour"] == 1
    assert dash["llm_pipe"]["rate_scope"] == "hour"
    assert dash["details_pipe"]["this_min"] == 1
    assert dash["details_pipe"]["ok_h"] == 1


def test_outcome_pace_uses_hour_then_day_then_hold(monkeypatch):
    ops.reset()
    busy = [{"llm_by": {}} for _ in range(59)] + [{"llm_by": {"ok": 12}}]
    hour = ops._outcome_pace(busy, "llm_by", prefix="llm")
    assert hour["ok_h"] == 12
    assert hour["ok_24"] == 12
    assert hour["per_hour"] == 12
    assert hour["rate_scope"] == "hour"

    ops.reset()
    monkeypatch.setattr(ops, "_day_outcomes", lambda prefix: (48, 0, 2))
    monkeypatch.setattr(ops, "_local_day_hours", lambda: 8.0)
    quiet = [{"llm_by": {}} for _ in range(60)]
    day = ops._outcome_pace(quiet, "llm_by", prefix="llm")
    assert day["ok_d"] == 48
    assert day["per_hour"] == 6.0
    assert day["rate_scope"] == "day"

    ops.reset()
    monkeypatch.setattr(ops, "_day_outcomes", lambda prefix: (0, 0, 0))
    first = ops._outcome_pace(busy, "llm_by", prefix="llm")
    assert first["per_hour"] == 12
    held = ops._outcome_pace(quiet, "llm_by", prefix="llm")
    assert held["held"] is True
    assert held["per_hour"] == 12
    assert held["rate_scope"] == "hold"


def test_series_keeps_older_minutes_from_disk(tmp_path, monkeypatch):
    from app import store

    now_min = 2_500_000
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "listings.sqlite")
    monkeypatch.setenv("PROPMAP_OPS_PERSIST", "1")
    monkeypatch.setattr(ops, "_today_start_min", lambda: now_min - 400)
    monkeypatch.setattr(ops.time, "time", lambda: now_min * 60 + 8)
    store.init()
    ops.reset()
    old = now_min - 200
    store.ops_bump("new", 7, old)
    store.ops_bump("gone", 2, old)
    store.ops_bump("llm.ok", 3, old)
    ops.reset()
    snap = ops.snapshot()
    assert len(snap["series"]) == ops.SERIES_MIN
    row = next(item for item in snap["series"] if item["t"] == old * 60)
    assert row["new"] == 7
    assert row["gone"] == 2
    assert row["llm_by"]["ok"] == 3
    monkeypatch.setattr(ops, "inventory", lambda force=False: _empty_inv(llm_need=10))
    dash = ops.dashboard()
    assert dash["movement"]["new"] == 7
    assert dash["movement"]["gone"] == 2
    assert dash["movement"]["new_24h"] == 7
    assert dash["llm_pipe"]["ok_d"] == 3
    assert dash["llm_pipe"]["ok_24"] == 3


def test_dashboard_returns_cache_without_blocking(monkeypatch):
    import time

    ops.reset()
    ops.note("new", n=3)
    monkeypatch.setattr(ops, "inventory", lambda force=False: _empty_inv(llm_need=1))
    first = ops.dashboard()
    assert first["movement"]["new"] == 3
    started = []

    def stuck():
        started.append(1)
        time.sleep(2)
        return first

    monkeypatch.setattr(ops, "_build_dashboard", stuck)
    ops._dash_at = 0.0
    t0 = time.time()
    second = ops.dashboard()
    elapsed = time.time() - t0
    assert elapsed < 0.4
    assert second["movement"]["new"] == 3
    deadline = time.time() + 1
    while time.time() < deadline and not started:
        time.sleep(0.01)
    assert started

