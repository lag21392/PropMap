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
    user, token = accounts.login("pino", "clave-segura-1", _req())
    assert token
    accounts.verify_token(accounts.verify_url_for(user.id).split("token=", 1)[1])
    user = accounts._row_to_account(accounts._get_by_id(user.id))
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
    monkeypatch.setenv("MAIL_ADDRESS", "cuenta@propmap.local")
    monkeypatch.setenv("MAIL_UI_URL", "http://127.0.0.1:7500")
    assert accounts.mail_address() == "cuenta@propmap.local"
    assert accounts.mail_inbox_url() == "http://127.0.0.1:7500"


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
