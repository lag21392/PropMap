from datetime import datetime, timedelta, timezone

from app.market import (
    _changed,
    _drop_from_points,
    _headline,
    _parse_dt,
    _trend,
    history_payload,
    price_drops,
    relevant_deals,
)
from app.models import Listing


def test_tiny_price_move_is_ignored():
    assert _changed(60000, 60050) is False
    assert _changed(60000, 59000) is True
    assert _changed(None, 60000) is True


def test_trend_down_is_cheaper_market():
    trend, delta = _trend(1600, 1700)
    assert trend == "down"
    assert delta is not None and delta < 0


def test_headline_mentions_city():
    text = _headline("Puerto Madryn", "departamento", "down", -2.4, {"median_m2": 1650}, 12, "cohort")
    assert "Puerto Madryn" in text
    assert "bajó" in text
    assert "12" in text


def test_parse_portal_datetime():
    when = _parse_dt("2026-08-19T21:31:57-0400")
    assert when is not None
    assert when.year == 2026


def _sale(**kwargs) -> Listing:
    data = {
        "source": "zonaprop",
        "source_id": "deal-1",
        "url": "",
        "title": "Depto 2 amb",
        "property_type": "departamento",
        "price_usd": 68000,
        "price_m2": 1400,
        "city": "test-city",
        "barrio": "Centro",
        "deal_label": "oportunidad",
        "vs_barrio_pct": 12.0,
        "extra": {"deal_score": 72},
    }
    data.update(kwargs)
    return Listing(**data)


def test_relevant_deals_keep_oportunidad_without_extra_vs_cut():
    item = _sale(vs_barrio_pct=12.0, deal_label="oportunidad")
    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    rows = relevant_deals([item], {}, cutoff)
    assert len(rows) == 1
    assert rows[0]["id"] == item.id


def test_relevant_deals_include_bueno_if_cheaper_than_zone():
    item = _sale(source_id="bueno-1", deal_label="bueno", vs_barrio_pct=10.0)
    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    rows = relevant_deals([item], {}, cutoff)
    assert len(rows) == 1


def test_drop_uses_peak_price_not_only_last_two_ticks():
    points = [
        {"price_usd": 100000, "seen_at": "2026-01-01"},
        {"price_usd": 90000, "seen_at": "2026-02-01"},
        {"price_usd": 91000, "seen_at": "2026-03-01"},
    ]
    found = _drop_from_points(points)
    assert found is not None
    assert found["old_usd"] == 100000
    assert found["new_usd"] == 91000
    item = _sale(price_usd=91000)
    rows = price_drops([item], {item.id: points})
    assert len(rows) == 1
    assert rows[0]["change_pct"] == found["change_pct"]


def test_typo_extra_zeros_is_not_a_price_drop():
    points = [
        {"price_usd": 21_500_000, "seen_at": "2026-08-24"},
        {"price_usd": 21_500, "seen_at": "2026-08-24"},
    ]
    assert _drop_from_points(points, 21_500, "departamento") is None
    payload = history_payload("x", points, current_usd=21_500, property_type="departamento")
    assert payload["change_pct"] is None
    assert payload["outlier_n"] == 1
    assert payload["points"][0]["outlier"] is True
    assert payload["points"][1]["outlier"] is False


def test_real_drop_still_counts_when_both_prices_are_sane():
    points = [
        {"price_usd": 120_000, "seen_at": "2026-01-01"},
        {"price_usd": 96_000, "seen_at": "2026-02-01"},
    ]
    found = _drop_from_points(points, 96_000, "departamento")
    assert found is not None
    assert found["old_usd"] == 120_000
    assert found["new_usd"] == 96_000
    payload = history_payload("y", points, current_usd=96_000, property_type="departamento")
    assert payload["change_pct"] == -20.0
    assert payload["outlier_n"] == 0
