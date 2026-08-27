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
