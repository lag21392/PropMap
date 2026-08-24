from app.market import _changed, _headline, _parse_dt, _trend


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
