from fastapi.testclient import TestClient

from app import main
from app.lineage import blueprint, entity_diagram, graph, module_graph
from tests.conftest import post_ops_login


def test_graph_is_a_connected_pipeline():
    data = graph()
    ids = {node["id"] for node in data["nodes"]}
    assert data["engine"] == "elkjs"
    assert data["algorithm"] == "layered"
    assert len(ids) == len(data["nodes"])
    assert {"zonaprop", "store", "api", "ui", "matomo"} <= ids
    for edge in data["edges"]:
        assert edge["source"] in ids
        assert edge["target"] in ids


def test_er_reads_sqlite_and_emits_mermaid():
    er = entity_diagram()
    names = {table["id"] for table in er["tables"]}
    assert {"listings", "pins", "price_history", "accounts"} <= names
    assert er["engine"] == "mermaid"
    assert er["mermaid"].startswith("erDiagram")
    assert "listings {" in er["mermaid"]
    assert "PK FK" not in er["mermaid"]
    assert "PK, FK" in er["mermaid"]
    assert any(table["id"] == "meta" for table in er["tables"])
    assert all("rows" in table for table in er["tables"])
    assert entity_diagram() is entity_diagram()
    assert any(
        edge["source"] == "price_history" and edge["target"] == "listings"
        for edge in er["edges"]
    )
    assert any(
        edge["source"] == "pins" and edge["target"] == "listings" for edge in er["edges"]
    )


def test_modules_follow_internal_imports():
    mods = module_graph()
    ids = {node["id"] for node in mods["nodes"]}
    assert {"store", "pipeline", "main", "place_api"} <= ids
    assert "scrapers" in ids
    assert "scrapers.zonaprop" not in ids
    assert any(edge["source"] == "pipeline" and edge["target"] == "store" for edge in mods["edges"])
    assert any(edge["source"] == "main" and edge["target"] == "store" for edge in mods["edges"])
    assert all(node["id"] in {e["source"] for e in mods["edges"]} | {e["target"] for e in mods["edges"]} for node in mods["nodes"])


def test_blueprint_bundles_the_three_views():
    data = blueprint()
    assert data["flow"]["nodes"]
    assert data["er"]["mermaid"]
    assert data["modules"]["nodes"]
    assert data["nodes"] == data["flow"]["nodes"]


def test_lineage_requires_admin_password(monkeypatch):
    monkeypatch.setenv("SEARCH_PASSWORD", "test-secret")
    with TestClient(main.app) as client:
        denied = client.post("/api/lineage", json={"password": "nope"})
        assert denied.status_code == 401
        ok = client.post("/api/lineage", json={"password": "test-secret"})
        assert ok.status_code == 200
        body = ok.json()
        assert body["er"]["tables"]
        assert body["modules"]["edges"]
        assert body["flow"]["nodes"]


def test_flujo_page_is_served(monkeypatch):
    monkeypatch.setenv("SEARCH_PASSWORD", "test-secret")
    with TestClient(main.app) as client:
        gate = client.get("/flujo")
        assert gate.status_code == 200
        assert "Contraseña" in gate.text
        assert b"flujo.js" not in gate.content
        login = post_ops_login(client, "test-secret", next_url="/flujo")
        assert login.status_code == 303
        assert login.headers["location"] == "/flujo"
        page = client.get("/flujo")
        assert page.status_code == 200
        assert b"elkjs" in page.content
        assert b"mermaid" in page.content
        assert b"flujo.js" in page.content
