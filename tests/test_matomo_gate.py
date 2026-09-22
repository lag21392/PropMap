from fastapi.testclient import TestClient

from app import main
from app.matomo import public_url
from app.matomo_gate import _rewrite_cookie, _rewrite_location, _rewrite_text, rewrite_asset
from tests.conftest import post_ops_login, solve_ops_captcha


def test_rewrites_root_refs_to_stats_prefix(monkeypatch):
    monkeypatch.setenv("APP_PUBLIC_URL", "https://propmaplag.duckdns.org")
    monkeypatch.setenv("MATOMO_PUBLIC_URL", "https://propmaplag.duckdns.org/stats")
    html = '<a href="/index.php">x</a><img src="/plugins/a.png">'
    out = _rewrite_text(html)
    assert 'href="/stats/index.php"' in out
    assert 'src="/stats/plugins/a.png"' in out


def test_rewrite_asset_cache_reuses_bytes():
    raw = b'var u="/index.php"; function x(e){return "/"+e.type}'
    first = rewrite_asset(raw, "application/javascript", extra="h", cache_key="core-js")
    second = rewrite_asset(raw, "application/javascript", extra="h", cache_key="core-js")
    assert first is second
    assert b"/stats/index.php" in first
    assert b'"/"+e.type' in first


def test_enables_matomo_login_submit():
    html = '<input class="submit" id="login_form_submit" type="submit" value="Sign in" disabled="disabled"/>'
    out = _rewrite_text(html)
    assert 'id="login_form_submit"' in out
    assert "disabled" not in out


def test_does_not_break_self_closing_tags_or_js_slashes():
    html = (
        '<input type="hidden" name="form_nonce" value="abc123"/>'
        '<input type="hidden" name="form_redirect" value=""/>'
        'function x(e){return "/"+e.type}'
        'd.split("/")'
    )
    out = _rewrite_text(html)
    assert 'value="abc123"/>' in out
    assert 'value=""/>' in out
    assert '"/"+e.type' in out
    assert 'split("/")' in out
    assert "/stats/" not in out


def test_keeps_public_tracker_and_existing_prefix(monkeypatch):
    monkeypatch.setenv("APP_PUBLIC_URL", "https://propmaplag.duckdns.org")
    text = 'src="/matomo.js" src="/q/l.js" action="/stats/login" href="/static/styles.css"'
    out = _rewrite_text(text)
    assert 'src="/matomo.js"' in out
    assert 'src="/q/l.js"' in out
    assert 'action="/stats/login"' in out
    assert 'href="/static/styles.css"' in out
    assert "/stats/matomo.js" not in out
    assert "/stats/q/l.js" not in out
    assert "/stats/static/" not in out


def test_rewrites_internal_and_public_absolute_urls(monkeypatch):
    monkeypatch.setenv("APP_PUBLIC_URL", "https://propmaplag.duckdns.org")
    monkeypatch.setenv("MATOMO_PUBLIC_URL", "https://propmaplag.duckdns.org/stats")
    assert _rewrite_text("http://matomo/index.php") == "/stats/index.php"
    assert _rewrite_text("https://propmaplag.duckdns.org/index.php") == "/stats/index.php"
    assert _rewrite_text("https://propmaplag.duckdns.org/matomo.js") == (
        "https://propmaplag.duckdns.org/matomo.js"
    )
    assert _rewrite_text('<a href="https://matomo.org" title="x">') == (
        '<a href="https://matomo.org" title="x">'
    )
    assert _rewrite_text("https://127.0.0.1:8000/plugins/logo.svg") == "/stats/plugins/logo.svg"


def test_cookie_drops_internal_domain_and_scopes_path():
    out = _rewrite_cookie("MATOMO_SESSID=abc; Path=/; Domain=matomo")
    assert "Path=/stats/" in out
    assert "Domain" not in out
    assert "MATOMO_SESSID=abc" in out


def test_location_stays_under_stats(monkeypatch):
    monkeypatch.setenv("APP_PUBLIC_URL", "https://propmaplag.duckdns.org")
    assert _rewrite_location("/index.php") == "/stats/index.php"
    assert _rewrite_location("/stats/") == "/stats/"
    assert _rewrite_location("http://matomo/index.php?module=CoreHome") == (
        "/stats/index.php?module=CoreHome"
    )


def test_public_url_defaults_to_stats_path(monkeypatch):
    monkeypatch.delenv("MATOMO_PUBLIC_URL", raising=False)
    monkeypatch.setenv("APP_PUBLIC_URL", "https://propmaplag.duckdns.org")
    assert public_url() == "https://propmaplag.duckdns.org/stats"


def test_stats_gate_asks_for_password(monkeypatch):
    monkeypatch.setenv("SEARCH_PASSWORD", "test-secret")
    with TestClient(main.app) as client:
        page = client.get("/stats")
        assert page.status_code == 200
        assert "Contraseña" in page.text
        assert "Cuánto es" in page.text
        assert page.headers.get("cache-control") == "no-store"
        assert "noindex" in (page.headers.get("x-robots-tag") or "")
        captcha = solve_ops_captcha(page.text)
        bad = client.post("/stats/login", data={"password": "nope", **captcha})
        assert "incorrecta" in bad.text
        good = post_ops_login(client, "test-secret")
        assert good.status_code == 303
        assert good.headers["location"].endswith("/stats/")
        assert "propmap_ops" in good.cookies


def test_stats_login_needs_captcha(monkeypatch):
    monkeypatch.setenv("SEARCH_PASSWORD", "test-secret")
    with TestClient(main.app) as client:
        miss = client.post("/stats/login", data={"password": "test-secret"})
        assert "cuenta no cierra" in miss.text
        assert "propmap_ops" not in miss.cookies
        page = client.get("/stats")
        trap = client.post(
            "/stats/login",
            data={"password": "test-secret", "website": "http://spam.example", **solve_ops_captcha(page.text)},
        )
        assert "incorrecta" in trap.text
        assert "propmap_ops" not in trap.cookies


def test_stats_login_locks_after_too_many_tries(monkeypatch):
    from app import matomo_gate

    monkeypatch.setenv("SEARCH_PASSWORD", "test-secret")
    monkeypatch.setattr(matomo_gate, "LOGIN_MAX", 3)
    matomo_gate.reset_login_guard()
    with TestClient(main.app) as client:
        for _ in range(3):
            page = client.get("/stats")
            client.post("/stats/login", data={"password": "nope", **solve_ops_captcha(page.text)})
        locked = client.post("/stats/login", data={"password": "test-secret"})
        assert "Demasiados intentos" in locked.text
        assert "propmap_ops" not in locked.cookies


def test_robots_hides_admin_paths():
    with TestClient(main.app) as client:
        text = client.get("/robots.txt").text
        assert "Disallow: /stats" in text
        assert "Disallow: /tablero" in text
        assert "Allow: /" in text


def test_compose_binds_matomo_to_localhost():
    text = (main.ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "127.0.0.1:3102:80" in text
    assert '"3102:80"' not in text
