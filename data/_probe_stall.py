import time
import urllib.request

from app import listings_cache, pipeline
from app import llm_enrich


def took(fn):
    t = time.time()
    ok = fn()
    return ok, time.time() - t


def alive():
    t = time.time()
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/api/alive", timeout=1.5) as r:
            ok = 200 <= int(r.status) < 300
    except Exception:
        ok = False
    return ok, time.time() - t


print("t     pipe_lock   cache_lock  api_alive   building cities_loading  ready saving cola")
for i in range(30):
    p_ok, p_s = took(lambda: pipeline.ping_lock(1.2))
    c_ok, c_s = took(lambda: listings_cache.ping_lock(1.2))
    a_ok, a_s = took(alive)
    st = llm_enrich.queue_stats()
    print(
        "%4.0f  %s %5.2fs  %s %5.2fs  %s %5.2fs   %-8s %-14s %s %s %s"
        % (
            i * 2.0,
            "ok" if p_ok else "NO",
            p_s,
            "ok" if c_ok else "NO",
            c_s,
            "ok" if a_ok else "NO",
            a_s,
            listings_cache.busy_building(),
            listings_cache.cities_loading(),
            st.get("ready"),
            st.get("saving"),
            st.get("pending"),
        ),
        flush=True,
    )
    time.sleep(2.0)
