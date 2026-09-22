from types import SimpleNamespace

from fastapi import HTTPException

from app import accounts, store
from app.models import Listing


def _ready(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AUTH_DEV_SHOW_LINK", "1")
    monkeypatch.setenv("AUTH_SECRET", "test-secret-for-accounts")
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "listings.sqlite")
    accounts.reset_crypto()
    store.init()


def _req(host="127.0.0.1"):
    return SimpleNamespace(client=SimpleNamespace(host=host), cookies={}, url=SimpleNamespace(scheme="http"))


def test_register_encrypts_email_and_verifies(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch)
    data = accounts.register("madryn_ok", "yo@correo.com", "clave-segura-1", True, _req())
    assert data["ok"] is True
    assert data["user"]["email_masked"].startswith("y***@")
    assert data["verify_url"]
    with store.connect() as conn:
        row = conn.execute("SELECT * FROM accounts").fetchone()
        blob = " ".join(str(row[k]) for k in row.keys())
    assert "yo@correo.com" not in blob
    assert "madryn_ok" not in blob
    assert row["password_hash"].startswith("$argon2")
    user = accounts.verify_token(data["verify_url"].split("token=", 1)[1])
    assert user.email_verified is True


def test_login_and_encrypted_pins(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch)
    accounts.register("pino", "pino@correo.com", "clave-segura-1", True, _req())
    try:
        accounts.login("pino", "clave-segura-1", _req())
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("expected 403")
    accounts.verify_token(accounts.verify_url_for(accounts._row_to_account(accounts._get_by_username("pino")).id).split("token=", 1)[1])
    user, token = accounts.login("pino", "clave-segura-1", _req())
    assert token
    saved = accounts.save_pin(user, "zonaprop:1", favorite=True, notes="llamar jueves")
    assert saved["favorite"] is True
    with store.connect() as conn:
        vault = conn.execute("SELECT cipher FROM account_vaults").fetchone()["cipher"]
    assert "llamar jueves" not in vault
    listings = [{"id": "zonaprop:1", "favorite": False, "notes": ""}]
    overlay = accounts.overlay_pins(listings, user)
    assert overlay[0]["favorite"] is True
    assert overlay[0]["notes"] == "llamar jueves"
    assert listings[0]["favorite"] is False


def test_delete_account_wipes_vault(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch)
    accounts.register("baja", "baja@correo.com", "clave-segura-1", True, _req())
    accounts.verify_token(accounts.verify_url_for(accounts._row_to_account(accounts._get_by_username("baja")).id).split("token=", 1)[1])
    user, _token = accounts.login("baja", "clave-segura-1", _req())
    accounts.save_pin(user, "x:1", favorite=True, notes="secreto")
    accounts.delete_account(user, "clave-segura-1", "ELIMINAR")
    with store.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM accounts").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM account_vaults").fetchone()["n"] == 0


def test_rejects_without_terms(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch)
    try:
        accounts.register("sinok", "sin@correo.com", "clave-segura-1", False, _req())
    except HTTPException as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError("expected 400")


def test_mail_address_from_env(monkeypatch):
    monkeypatch.setenv("MAIL_ADDRESS", "hola@estudio.com")
    monkeypatch.setenv("MAIL_UI_URL", "http://127.0.0.1:7500")
    monkeypatch.setenv("AUTH_DEV_SHOW_LINK", "1")
    assert accounts.mail_address() == "hola@estudio.com"
    assert accounts.mail_inbox_url() == "http://127.0.0.1:7500"


def test_mail_address_from_public_host(monkeypatch):
    monkeypatch.delenv("MAIL_ADDRESS", raising=False)
    monkeypatch.setenv("SMTP_FROM", "PropMap <cuenta@propmap.local>")
    monkeypatch.setenv("APP_PUBLIC_URL", "https://propmaplag.duckdns.org")
    monkeypatch.setenv("AUTH_DEV_SHOW_LINK", "0")
    assert accounts.mail_address() == "no-reply@propmaplag.duckdns.org"
    assert accounts.smtp_from().startswith("PropMap <no-reply@")
    assert accounts.mail_inbox_url() == ""


def test_register_pending_writes_outbox(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch)
    data = accounts.register("nuevo", "nuevo@correo.com", "clave-segura-1", True, _req())
    assert data["pending"] is True
    assert data["user"]["email_verified"] is False
    files = list((tmp_path / "mail").glob("*.txt"))
    assert files
    body = files[0].read_text(encoding="utf-8")
    assert "Confirmá tu cuenta" in body
    assert "verificar?token=" in (data.get("verify_url") or body)


def test_public_listing_hides_pins():
    item = Listing(
        source="properati",
        source_id="abc",
        url="https://www.properati.com.ar/x",
        title="Depto",
        property_type="departamento",
        lat=-34.58812,
        lon=-58.43088,
        has_exact_location=True,
        favorite=True,
        notes="no debería salir",
    )
    public = item.to_public_dict()
    assert public["favorite"] is False
    assert public["notes"] == ""
    assert public["location_approx"] is True
    assert public["has_exact_location"] is False
    assert public["lat"] != -34.58812 or public["lon"] != -58.43088


def _verified(name: str, email: str, req):
    accounts.register(name, email, "clave-segura-1", True, req)
    row = accounts._get_by_username(name)
    accounts.verify_token(
        accounts.verify_url_for(accounts._row_to_account(row).id).split("token=", 1)[1]
    )
    user, _token = accounts.login(name, "clave-segura-1", req)
    return user


def test_listing_edits_stay_on_the_account(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch)
    item = Listing(
        source="zonaprop",
        source_id="ed1",
        url="https://example.com/ed1",
        title="Casa",
        property_type="casa",
        price=100000,
        currency="USD",
        price_usd=100000,
        city="caba",
        address="Mitre 100",
    )
    store.upsert_many([item])
    req = _req()
    mine = _verified("editor_ok", "editor@correo.com", req)
    other = _verified("otro_ok", "otro@correo.com", req)
    accounts.save_pin(
        mine,
        item.id,
        contacted=True,
        notes="llamar",
        edits={"price": 90000, "address": "Mitre 200"},
    )
    stored = store.get_listing(item.id)
    assert stored.price == 100000
    assert stored.address == "Mitre 100"
    assert not (stored.extra or {}).get("user_edits")
    public = stored.to_public_dict()
    mine_view = accounts.overlay_pins([dict(public)], mine)[0]
    other_view = accounts.overlay_pins([dict(public)], other)[0]
    guest_view = accounts.overlay_pins([dict(public)], None)[0]
    assert mine_view["price"] == 90000
    assert mine_view["price_usd"] == 90000
    assert mine_view["address"] == "Mitre 200"
    assert mine_view["notes"] == "llamar"
    assert other_view["price"] == 100000
    assert other_view["address"] == "Mitre 100"
    assert other_view["notes"] == ""
    assert guest_view["price"] == 100000
    assert guest_view["notes"] == ""


def test_api_listing_does_not_write_shared_user_edits(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main

    _ready(tmp_path, monkeypatch)
    monkeypatch.setenv("PROPMAP_TEST", "1")
    item = Listing(
        source="zonaprop",
        source_id="ed2",
        url="https://example.com/ed2",
        title="Depto",
        property_type="departamento",
        price=150000,
        currency="USD",
        price_usd=150000,
        city="caba",
        address="San Martín 50",
    )
    store.upsert_many([item])
    _verified("api_ok", "api@correo.com", _req())
    with TestClient(main.app) as client:
        login = client.post(
            "/api/auth/login",
            json={"username": "api_ok", "password": "clave-segura-1"},
        )
        assert login.status_code == 200
        patched = client.post(
            "/api/listing",
            json={
                "id": item.id,
                "price": 120000,
                "address": "San Martín 80",
                "notes": "solo mio",
                "contacted": True,
            },
        )
        assert patched.status_code == 200
        body = patched.json()["listing"]
        assert body["price"] == 120000
        assert body["address"] == "San Martín 80"
        assert body["notes"] == "solo mio"
    stored = store.get_listing(item.id)
    assert stored.price == 150000
    assert stored.address == "San Martín 50"
    assert not (stored.extra or {}).get("user_edits")


def test_api_listing_get_returns_description_and_rent(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main

    _ready(tmp_path, monkeypatch)
    monkeypatch.setenv("PROPMAP_TEST", "1")
    item = Listing(
        source="zonaprop",
        source_id="ficha-get",
        url="https://example.com/ficha-get",
        title="Depto",
        property_type="departamento",
        price=110000,
        currency="USD",
        price_usd=110000,
        city="caba",
        address="Mitre 10",
        description="Living comedor al frente con balcón.",
        extra={"monthly_rent_usd": 420, "monthly_yield_pct": 4.8},
    )
    store.upsert_many([item])
    with TestClient(main.app) as client:
        res = client.get("/api/listing", params={"id": item.id})
        missing = client.get("/api/listing", params={"id": "zonaprop:no-esta"})
    assert res.status_code == 200
    body = res.json()["listing"]
    assert "Living comedor" in (body.get("description") or "")
    assert body.get("monthly_yield_pct") == 4.8
    assert body.get("monthly_rent_usd") == 420
    assert missing.status_code == 404
