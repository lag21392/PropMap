from app import crawl


def test_fast_and_slow_page_limits():
    crawl.set_slow(False)
    assert crawl.list_page_limit() == crawl.FAST_PAGES
    crawl.set_slow(True)
    assert crawl.list_page_limit() == crawl.SLOW_PAGES
    crawl.set_slow(False)


def test_rest_soon_is_shorter_than_full_break():
    soon = crawl.rest_seconds(soon=True)
    full = crawl.rest_seconds(soon=False)
    assert crawl.SOON_MIN <= soon <= crawl.SOON_MAX
    assert crawl.REST_MIN <= full <= crawl.REST_MAX
    assert soon < full
