import json
import re

from fastapi.testclient import TestClient

from app import main

ORIGIN = "https://propmaplag.duckdns.org"


def _client(monkeypatch):
    monkeypatch.setenv("APP_PUBLIC_URL", ORIGIN)
    return TestClient(main.app)


def test_robots_points_at_the_sitemap(monkeypatch):
    with _client(monkeypatch) as client:
        text = client.get("/robots.txt").text
    assert "Allow: /" in text
    assert "Disallow: /static" not in text
    assert "Disallow: /api" not in text
    assert "Disallow: /stats" in text
    assert "Disallow: /tablero" in text
    assert "Disallow: /flujo" in text
    assert "Disallow: /admin" in text
    assert "Disallow: /verificar" in text
    assert f"Sitemap: {ORIGIN}/sitemap.xml" in text
    assert f"Sitemap: {ORIGIN}/sitemap-cities.xml" in text


def test_sitemap_lists_only_public_pages(monkeypatch):
    with _client(monkeypatch) as client:
        res = client.get("/sitemap.xml")
    assert res.status_code == 200
    assert "xml" in res.headers["content-type"]
    text = res.text
    assert f"<loc>{ORIGIN}/</loc>" in text
    assert f"<loc>{ORIGIN}/legal</loc>" in text
    assert "/ciudad/" not in text
    assert "/stats" not in text
    assert "/tablero" not in text
    assert "/admin" not in text
    assert re.search(r"<lastmod>\d{4}-\d{2}-\d{2}</lastmod>", text)


def test_home_and_legal_expose_indexable_metadata(monkeypatch):
    with _client(monkeypatch) as client:
        home = client.get("/")
        legal = client.get("/legal")
        alias = client.get("/privacidad", follow_redirects=False)
        api = client.get("/api/alive")
    assert home.status_code == 200
    assert "__ORIGIN__" not in home.text
    assert f'<link rel="canonical" href="{ORIGIN}/"' in home.text
    assert 'name="description"' in home.text
    assert "score de ganga" in home.text
    assert "Casas, departamentos y terrenos en venta" in home.text
    assert 'rel="icon"' in home.text
    raw = re.search(r'<script type="application/ld\+json">\s*(.*?)\s*</script>', home.text, re.S).group(1)
    payload = json.loads(raw)
    types = {node["@type"] for node in payload["@graph"]}
    assert types == {"WebSite", "WebApplication"}
    website = next(node for node in payload["@graph"] if node["@type"] == "WebSite")
    assert website["potentialAction"]["@type"] == "SearchAction"
    assert website["potentialAction"]["target"]["urlTemplate"].endswith("/ciudades?q={search_term_string}")
    assert f"{ORIGIN}/static/og.png" in home.text
    assert "Casas en venta en Argentina" in home.text
    assert home.headers.get("x-robots-tag") is None
    assert f"{ORIGIN}/static/og.png" in legal.text
    assert legal.status_code == 200
    assert f'<link rel="canonical" href="{ORIGIN}/legal"' in legal.text
    assert "Ley 25.326" in legal.text
    assert alias.status_code == 301
    assert alias.headers["location"].endswith("/legal")
    assert "noindex" in (api.headers.get("x-robots-tag") or "")


def test_private_html_stays_out_of_the_index(monkeypatch):
    with _client(monkeypatch) as client:
        admin = client.get("/admin")
        query = client.get("/?tab=map")
    assert "noindex" in (admin.headers.get("x-robots-tag") or "")
    assert "nofollow" in (admin.headers.get("x-robots-tag") or "")
    assert query.headers.get("x-robots-tag") == "noindex, follow"
    assert 'content="noindex, follow"' in query.text
    assert f'<link rel="canonical" href="{ORIGIN}/"' in query.text


def _seed_trelew(tmp_path, monkeypatch):
    from app import store
    from app.models import Listing

    monkeypatch.setenv("APP_PUBLIC_URL", ORIGIN)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "seo.sqlite")
    store.init()
    rows = []
    for i in range(4):
        rows.append(
            Listing(
                source="zonaprop",
                source_id=f"casa-{i}",
                url=f"https://example.com/casa-{i}",
                title=f"Casa {i} en Centro",
                property_type="casa",
                city="trelew",
                barrio="Centro",
                price_usd=100000 + i * 1000,
                price_m2=1000 + i * 50,
                score=80 - i,
            )
        )
    for i in range(4):
        rows.append(
            Listing(
                source="zonaprop",
                source_id=f"depto-{i}",
                url=f"https://example.com/depto-{i}",
                title=f"Departamento {i} en Norte",
                property_type="departamento",
                city="trelew",
                barrio="Norte",
                price_usd=80000 + i * 1000,
                price_m2=1800 + i * 40,
                score=70 - i,
            )
        )
    rows.append(
        Listing(
            source="zonaprop",
            source_id="suelto",
            url="https://example.com/suelto",
            title="Aviso sin barrio",
            property_type="casa",
            city="trelew",
            barrio="Sin clasificar",
            price_usd=90000,
            price_m2=1100,
            score=10,
        )
    )
    store.upsert_many(rows)


def _words(html_text: str) -> int:
    plain = re.sub(r"<script[\s\S]*?</script>", " ", html_text)
    plain = re.sub(r"<[^>]+>", " ", plain)
    return len(re.findall(r"[0-9A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+", plain))


def test_city_landing_is_indexable_and_specific(tmp_path, monkeypatch):
    _seed_trelew(tmp_path, monkeypatch)
    with _client(monkeypatch) as client:
        page = client.get("/ciudad/trelew")
        kind = client.get("/ciudad/trelew/casa")
        missing = client.get("/ciudad/trelew/local")
        unknown = client.get("/ciudad/ciudad-que-no-existe")
        directory = client.get("/ciudades")
        found = client.get("/ciudades?q=trelew")
        sitemap = client.get("/sitemap-cities.xml")
        queried = client.get("/ciudad/trelew?utm=1")
    assert page.status_code == 200
    assert page.headers.get("x-robots-tag") is None
    assert f'<link rel="canonical" href="{ORIGIN}/ciudad/trelew"' in page.text
    assert "<h1>Casas y departamentos en venta en Trelew</h1>" in page.text
    assert "Centro" in page.text
    assert "Sin clasificar" not in page.text
    assert 'href="/ciudad/trelew/casa"' in page.text
    assert 'id="barrio-centro"' in page.text
    assert "Cómo se calcula el score" in page.text
    assert "dato raro" in page.text
    assert _words(page.text) >= 200
    assert "ItemList" in page.text
    assert "BreadcrumbList" in page.text
    assert "RealEstateListing" in page.text
    raw = re.search(r'<script type="application/ld\+json">\s*(.*?)\s*</script>', page.text, re.S).group(1)
    graph = json.loads(raw)["@graph"]
    assert {node["@type"] for node in graph} >= {"WebPage", "BreadcrumbList", "ItemList"}
    assert kind.status_code == 200
    assert "<h1>Casas en venta en Trelew</h1>" in kind.text
    assert f'<link rel="canonical" href="{ORIGIN}/ciudad/trelew/casa"' in kind.text
    assert missing.status_code == 404
    assert unknown.status_code == 404
    assert "Trelew" in directory.text
    assert 'href="/ciudad/trelew/departamento"' in directory.text
    assert 'href="/ciudad/trelew#barrio-centro"' in directory.text
    assert found.headers.get("x-robots-tag") == "noindex, follow"
    assert "Trelew" in found.text
    assert f"<loc>{ORIGIN}/ciudad/trelew</loc>" in sitemap.text
    assert f"<loc>{ORIGIN}/ciudad/trelew/casa</loc>" in sitemap.text
    assert f"<loc>{ORIGIN}/ciudad/trelew/departamento</loc>" in sitemap.text
    assert "/local" not in sitemap.text
    assert re.search(r"<lastmod>\d{4}-\d{2}-\d{2}</lastmod>", sitemap.text)
    assert queried.headers.get("x-robots-tag") == "noindex, follow"
    assert f'<link rel="canonical" href="{ORIGIN}/ciudad/trelew"' in queried.text


def test_city_slug_redirects_to_the_id(tmp_path, monkeypatch):
    from app import seo

    _seed_trelew(tmp_path, monkeypatch)
    real = seo._place_row

    def wrapped(cid):
        row = real(cid)
        if cid == "trelew":
            row = dict(row)
            row["slug"] = "trelew-chubut"
        return row

    monkeypatch.setattr(seo, "_place_row", wrapped)
    with _client(monkeypatch) as client:
        res = client.get("/ciudad/trelew-chubut", follow_redirects=False)
        kind = client.get("/ciudad/trelew-chubut/casa", follow_redirects=False)
    assert res.status_code == 301
    assert res.headers["location"].endswith("/ciudad/trelew")
    assert kind.status_code == 301
    assert kind.headers["location"].endswith("/ciudad/trelew/casa")


def test_brotli_when_the_library_is_present(monkeypatch):
    from app import compress

    monkeypatch.setattr(compress, "brotli_ready", lambda: True)
    monkeypatch.setattr(compress, "compress_br", lambda data: b"br-ok")
    with _client(monkeypatch) as client:
        res = client.get("/", headers={"Accept-Encoding": "br"})
    assert res.headers.get("content-encoding") == "br"
    assert res.content == b"br-ok"
