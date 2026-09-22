import os
import time

import httpx

PW = os.environ.get("SEARCH_PASSWORD") or ""
H = {"x-propmap-ops": PW}


def ops():
    return httpx.get("http://127.0.0.1:8000/api/ops", headers=H, timeout=20).json()


def slot():
    try:
        s = httpx.get("http://propmap-llm:8080/slots", timeout=2).json()[0]
        return bool(s.get("is_processing")), s.get("n_prompt_tokens_cache"), s.get("n_prompt_tokens")
    except Exception as exc:
        return False, str(exc), 0


d = ops()
p = d.get("llm_pipe") or {}
print("pace", {k: p.get(k) for k in ("per_min", "per_hour", "this_min", "ok_h", "queue", "working", "busy_s", "gpu")})
print("--- sample ---")
busy = 0
rounds = 16
for i in range(rounds):
    time.sleep(2.0)
    proc, cache, ptok = slot()
    busy += 1 if proc else 0
    d = ops()
    llm = (d.get("live") or {}).get("llm") or {}
    pipe = d.get("llm_pipe") or {}
    print(
        "t+%4.1f llama=%s cache=%s ptok=%s busy_s=%s pending=%s this_min=%s"
        % (
            (i + 1) * 2.0,
            proc,
            cache,
            ptok,
            llm.get("busy_s"),
            llm.get("pending"),
            pipe.get("this_min"),
        )
    )
print("llama ocupada %s/%s muestras" % (busy, rounds))
m = httpx.get("http://propmap-llm:8080/metrics", timeout=3).text
for line in m.splitlines():
    if line.startswith("#") or not line:
        continue
    if "predicted" in line and "spec_" not in line:
        print(line)
