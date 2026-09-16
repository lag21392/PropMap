from app import cache_keep


def test_cache_keep_does_not_start_in_tests():
    cache_keep.stop()
    cache_keep.start()
    assert cache_keep._thread is None or not cache_keep._thread.is_alive()
