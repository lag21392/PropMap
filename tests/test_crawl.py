import threading
import time

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
    assert full < 60


def test_wait_does_not_block_other_hosts():
    crawl.reset()
    crawl.set_slow(False)
    crawl.backoff(8, "www.properati.com.ar")
    started = time.time()
    crawl.wait(host="www.zonaprop.com.ar")
    assert time.time() - started < 1.6


def test_parallel_waits_do_not_multiply_cooldown():
    crawl.reset()
    crawl.set_slow(False)
    crawl.backoff(1.4, "www.zonaprop.com.ar")

    def go() -> None:
        crawl.wait(host="www.zonaprop.com.ar")

    started = time.time()
    threads = [threading.Thread(target=go) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.time() - started
    assert elapsed < 5.0
    assert elapsed >= 1.3


def test_note_http_403_on_tor_pauses_for_minutes():
    crawl.reset()
    try:
        crawl.note_http(403, "www.zonaprop.com.ar", lane="tor-0")
        assert crawl.host_paused("www.zonaprop.com.ar", lane="tor-0")
        assert crawl.snapshot()["wait_s"] >= 12 * 60 - 1
        assert not crawl.host_paused("www.argenprop.com")
    finally:
        crawl.reset()


def test_note_http_403_on_local_is_about_20s():
    crawl.reset()
    try:
        crawl.note_http(403, "www.zonaprop.com.ar", lane="direct")
        wait_s = crawl.snapshot()["wait_s"]
        assert 17 <= wait_s <= 23
        assert crawl.cooling("www.zonaprop.com.ar", "direct")
        assert not crawl.host_paused("www.argenprop.com")
    finally:
        crawl.reset()


def test_note_http_401_is_short_pause():
    crawl.reset()
    try:
        crawl.note_http(401, "www.properati.com.ar")
        assert crawl.snapshot()["wait_s"] < 40
        assert crawl.host_paused("www.properati.com.ar")
    finally:
        crawl.reset()


def test_host_paused_false_when_idle():
    crawl.reset()
    from app.http_client import reset_fetch_state

    reset_fetch_state()
    try:
        assert not crawl.host_paused("www.zonaprop.com.ar")
    finally:
        crawl.reset()


def test_wait_returns_during_a_block_cooldown():
    crawl.reset()
    try:
        crawl.note_http(403, "www.zonaprop.com.ar", lane="tor-0")
        started = time.time()
        crawl.wait(host="www.zonaprop.com.ar", lane="tor-0")
        assert time.time() - started < 0.5
    finally:
        crawl.reset()


def test_403_on_one_lane_does_not_pause_another():
    crawl.reset()
    try:
        crawl.note_http(403, "www.zonaprop.com.ar", lane="direct")
        assert crawl.host_paused("www.zonaprop.com.ar", lane="direct")
        assert crawl.cooling("www.zonaprop.com.ar", "direct")
        assert not crawl.host_paused("www.zonaprop.com.ar", lane="tor")
        assert not crawl.cooling("www.zonaprop.com.ar", "tor")
        started = time.time()
        crawl.wait(host="www.zonaprop.com.ar", lane="tor")
        assert time.time() - started < 1.6
    finally:
        crawl.reset()


def test_short_pacing_is_not_cooling():
    crawl.reset()
    try:
        crawl.backoff(0.5, "www.argenprop.com", lane="direct")
        assert crawl.host_paused("www.argenprop.com", lane="direct")
        assert not crawl.cooling("www.argenprop.com", "direct")
        rows = crawl.busy_rows()
        assert any(row["lane"] == "direct" and row["host"] == "www.argenprop.com" for row in rows)
        assert all(not row["cooling"] for row in rows if row["host"] == "www.argenprop.com")
        assert not crawl.host_cooling("www.argenprop.com", "direct")
    finally:
        crawl.reset()


def test_host_cooling_matches_related_subdomain():
    crawl.reset()
    try:
        crawl.note_http(403, "inmueble.mercadolibre.com.ar", lane="direct")
        assert crawl.host_cooling("mercadolibre.com.ar", "direct")
        assert not crawl.host_cooling("www.zonaprop.com.ar", "direct")
    finally:
        crawl.reset()
    try:
        crawl.backoff(0.4, "terreno.mercadolibre.com.ar", lane="direct")
        assert crawl.host_paused("mercadolibre.com.ar", "direct")
        assert not crawl.host_cooling("mercadolibre.com.ar", "direct")
    finally:
        crawl.reset()
