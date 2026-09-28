"""Filtros de alertas alineados con el mapa, y dueño de cada alerta."""

from app.alerts import add_alert, delete_alert
from app.models import Listing
from app.search_auth import apply_filters


def _item(**kwargs) -> Listing:
    base = dict(
        source="manual",
        source_id="1",
        url="https://example.test/1",
        title="Casa",
        property_type="casa",
        city="caba",
    )
    base.update(kwargs)
    return Listing(**base)


def test_price_and_beds_accept_frontend_and_legacy_keys():
    items = [
        _item(source_id="1", city="caba", price_usd=100000, bedrooms=3),
        _item(source_id="2", city="mdq", price_usd=50000, bedrooms=1),
    ]
    found = apply_filters(items, {"city": "caba", "price_max": 120000, "minBeds": "2"})
    assert [item.id for item in found] == ["manual:1"]


def test_deal_bar_is_minimum_score():
    bargain = _item(source_id="1", deal_label="oportunidad")
    plain = _item(source_id="2", deal_label="")
    assert len(apply_filters([bargain, plain], {"dealBar": "40"})) == 1


def test_city_scoped_keeps_related_listings():
    items = [_item(source_id="1", city="caba"), _item(source_id="2", city="vicente-lopez")]
    found = apply_filters(items, {"cityFilter": "caba"}, city_scoped=True)
    assert len(found) == 2


def test_patio_trait_reads_saved_signal():
    with_patio = _item(source_id="1", extra={"signals_ver": 1, "has_patio": True})
    without = _item(source_id="2", extra={"signals_ver": 1, "has_patio": False})
    found = apply_filters([with_patio, without], {"traits": ["patio"]})
    assert [item.id for item in found] == ["manual:1"]


def test_alert_requires_city_and_owner(tmp_path, monkeypatch):
    monkeypatch.setattr("app.alerts.DATA", tmp_path / "alerts.json")
    missing = add_alert("user-a", {"maxPrice": 100000})
    assert missing["ok"] is False

    created = add_alert("user-a", {"cityFilter": "caba", "maxPrice": "200000"})
    assert created["ok"] is True
    alert_id = created["alert"]["id"]
    assert delete_alert(alert_id, "user-b")["ok"] is False
    assert delete_alert(alert_id, "user-a")["ok"] is True
