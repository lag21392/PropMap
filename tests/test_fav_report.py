from io import BytesIO

from pypdf import PdfReader

from app import accounts
from app.fav_report import MAX_FAVORITES, build_report
from tests.test_accounts import _ready, _req


def test_favorite_report_pdf_caps_at_ten(tmp_path, monkeypatch):
    _ready(tmp_path, monkeypatch)
    accounts.register("pdfok", "pdf@correo.com", "clave-segura-1", True, _req())
    accounts.verify_token(accounts.verify_url_for(accounts._row_to_account(accounts._get_by_username("pdfok")).id).split("token=", 1)[1])
    user, _token = accounts.login("pdfok", "clave-segura-1", _req())
    for i in range(12):
        accounts.save_pin(user, f"zonaprop:{i}", favorite=True, notes=f"llamar {i}")
    accounts.save_pin(user, "zonaprop:nota", notes="sin estrella")
    picked, total = accounts.favorite_entries(user, MAX_FAVORITES)
    assert total == 12
    assert len(picked) == 10
    rows = []
    for listing_id, pin in picked:
        rows.append(
            {
                "id": listing_id,
                "title": f"Depto {listing_id}",
                "url": "https://www.zonaprop.com.ar/x",
                "property_type": "departamento",
                "price_usd": 91000,
                "address": "Florida 600",
                "city": "caba",
                "covered_m2": 42,
                "notes": pin.get("notes"),
                "description": "Luz y balcón al frente.",
            }
        )
    pdf = build_report(rows, username=user.username, total=total)
    assert pdf.startswith(b"%PDF")
    text = "".join((page.extract_text() or "") for page in PdfReader(BytesIO(pdf)).pages)
    assert "Mostramos 10 de 12" in text
    assert "Florida 600" in text
    assert "Depto zonaprop:0" in text
    assert "llamar 0" in text


def test_favorite_report_splits_direccion_and_interseccion():
    pdf = build_report(
        [
            {
                "id": "zonaprop:1",
                "title": "Casa esquina",
                "property_type": "casa",
                "price_usd": 120000,
                "address": "Roca 240",
                "intersection": "Roca y Apeleg",
                "location_real": True,
                "has_exact_location": True,
                "location_kind": "intersection",
            },
            {
                "id": "zonaprop:2",
                "title": "Lote suelto",
                "property_type": "terreno",
                "price_usd": 40000,
                "location_missing": True,
            },
        ],
        username="pdfok",
        total=2,
    )
    text = "".join((page.extract_text() or "") for page in PdfReader(BytesIO(pdf)).pages)
    assert "Dirección" in text and "Roca 240" in text
    assert "Intersección" in text and "Roca y Apeleg" in text
    assert "Ubicación real" in text
    assert "Sin ubicación" in text
    assert "Punto en el mapa" in text
