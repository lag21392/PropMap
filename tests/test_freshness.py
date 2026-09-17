from datetime import datetime, timedelta, timezone

from app.freshness import needs_detail_fetch, reset_known, same_local_day
from app.models import Listing
from app.scrapers import paginate
from app.scrapers.details import DETAILS_PARSER


def _item(source_id: str, **kwargs) -> Listing:
    data = {
        "source": "ml",
        "source_id": source_id,
        "url": f"https://example.com/{source_id}",
        "title": "Casa",
        "property_type": "casa",
        "price": 100000,
        "details_scraped": False,
        "extra": {},
    }
    data.update(kwargs)
    return Listing(**data)


def test_same_local_day_true_and_false():
    now = datetime(2026, 8, 24, 15, 0, tzinfo=timezone.utc)
    today = now.isoformat()
    yesterday = (now - timedelta(days=1)).isoformat()
    assert same_local_day(today, now=now) is True
    assert same_local_day(yesterday, now=now) is False
    assert same_local_day(None, now=now) is False


def test_never_downloaded_needs_fetch():
    assert needs_detail_fetch(_item("1")) is True


def test_unknown_city_after_llm_pass_skips_ficha():
    item = _item("lost", city="fuera", extra={"llm_city_ok": True, "llm_at": "2026-09-01T00:00:00+00:00"})
    assert needs_detail_fetch(item) is False
    still_new = _item("fresh", city="", extra={})
    assert needs_detail_fetch(still_new) is True
    skipped = _item("marked", city="", extra={"skip_details": True})
    assert needs_detail_fetch(skipped) is False
    placed = _item("ok", city="trelew", extra={"llm_city_ok": True})
    assert needs_detail_fetch(placed) is True


def test_already_downloaded_today_is_skipped():
    now = datetime(2026, 8, 24, 18, 0, tzinfo=timezone.utc)
    item = _item(
        "1",
        details_scraped=True,
        extra={"details_at": now.isoformat(), "details_parser": DETAILS_PARSER, "detail_price": 100000},
    )
    assert needs_detail_fetch(item, now=now) is False


def test_same_day_old_parser_needs_fetch():
    now = datetime(2026, 8, 24, 18, 0, tzinfo=timezone.utc)
    zp = _item(
        "1",
        source="zonaprop",
        details_scraped=True,
        extra={"details_at": now.isoformat(), "details_parser": "6", "detail_price": 100000},
    )
    assert needs_detail_fetch(zp, now=now) is False
    ap = _item(
        "2",
        source="argenprop",
        details_scraped=True,
        extra={"details_at": now.isoformat(), "details_parser": "6", "detail_price": 100000},
    )
    assert needs_detail_fetch(ap, now=now) is True
    ap_map = _item(
        "3",
        source="argenprop",
        details_scraped=True,
        extra={
            "details_at": now.isoformat(),
            "details_parser": "6",
            "detail_price": 100000,
            "portal_lat": -42.78,
            "portal_lon": -65.03,
        },
    )
    assert needs_detail_fetch(ap_map, now=now) is False


def test_same_day_even_if_price_changed_is_skipped():
    now = datetime(2026, 8, 24, 18, 0, tzinfo=timezone.utc)
    item = _item(
        "1",
        price=120000,
        details_scraped=True,
        extra={"details_at": now.isoformat(), "details_parser": DETAILS_PARSER, "detail_price": 100000},
    )
    assert needs_detail_fetch(item, now=now) is False


def test_next_day_same_price_is_skipped():
    now = datetime(2026, 8, 24, 18, 0, tzinfo=timezone.utc)
    item = _item(
        "1",
        price=100000,
        details_scraped=True,
        extra={
            "details_at": (now - timedelta(days=1)).isoformat(),
            "details_parser": DETAILS_PARSER,
            "detail_price": 100000,
        },
    )
    assert needs_detail_fetch(item, now=now) is False


def test_next_day_price_change_needs_fetch():
    now = datetime(2026, 8, 24, 18, 0, tzinfo=timezone.utc)
    item = _item(
        "1",
        price=130000,
        details_scraped=True,
        extra={
            "details_at": (now - timedelta(days=1)).isoformat(),
            "details_parser": DETAILS_PARSER,
            "detail_price": 100000,
        },
    )
    assert needs_detail_fetch(item, now=now) is True


def test_paginate_stops_when_page_is_already_known():
    reset_known({"ml:old1", "ml:old2", "ml:old3", "ml:old4", "ml:old5", "ml:old6", "ml:old7", "ml:old8"})
    calls = []

    def fetch_page(page: int):
        calls.append(page)
        if page == 1:
            return [_item(f"old{i}") for i in range(1, 9)]
        return [_item(f"later{i}") for i in range(1, 9)]

    items = paginate(fetch_page, max_pages=5)
    assert calls == [1]
    assert [x.source_id for x in items] == [f"old{i}" for i in range(1, 9)]


def test_paginate_keeps_going_while_there_are_new_ids():
    reset_known({"ml:old1"})
    calls = []
    first_page = [_item(f"n{i}") for i in range(1, 8)] + [_item("old1")]

    def fetch_page(page: int):
        calls.append(page)
        if page == 1:
            return first_page
        if page == 2:
            return first_page
        return [_item("never")]

    items = paginate(fetch_page, max_pages=5)
    assert calls == [1, 2]
    assert "n1" in {x.source_id for x in items}
    assert "never" not in {x.source_id for x in items}


def test_page_workers_stay_serial_in_tests():
    from app.scrapers import page_workers

    assert page_workers() == 1
