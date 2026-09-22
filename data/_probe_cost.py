import os
import time

import httpx

PW = os.environ.get("SEARCH_PASSWORD") or ""
H = {"x-propmap-ops": PW}
WINDOW = 120.0


def metrics():
    out = {}
    text = httpx.get("http://propmap-llm:8080/metrics", timeout=4).text
    for line in text.splitlines():
        if line.startswith("#") or " " not in line:
            continue
        key, _, val = line.partition(" ")
        try:
            out[key] = float(val)
        except ValueError:
            pass
    return out


def done_total():
    d = httpx.get("http://127.0.0.1:8000/api/ops", headers=H, timeout=20).json()
    p = d.get("llm_pipe") or {}
    return int(p.get("done") or 0), int(p.get("partial") or 0), p


a = metrics()
d0, p0, _ = done_total()
t0 = time.time()
time.sleep(WINDOW)
b = metrics()
d1, p1, pipe = done_total()
dt = time.time() - t0

avisos = (d1 + p1) - (d0 + p0)
tok = b.get("llamacpp:tokens_predicted_total", 0) - a.get("llamacpp:tokens_predicted_total", 0)
gen_s = b.get("llamacpp:tokens_predicted_seconds_total", 0) - a.get("llamacpp:tokens_predicted_seconds_total", 0)
pro_tok = b.get("llamacpp:prompt_tokens_total", 0) - a.get("llamacpp:prompt_tokens_total", 0)
pro_s = b.get("llamacpp:prompt_seconds_total", 0) - a.get("llamacpp:prompt_seconds_total", 0)

print("ventana %.0fs · avisos terminados %s" % (dt, avisos))
if avisos > 0:
    print("por aviso: %.0f tokens generados · %.2fs de generacion · %.0f tokens de prompt · %.2fs de prompt"
          % (tok / avisos, gen_s / avisos, pro_tok / avisos, pro_s / avisos))
    print("gpu ocupada %.0f%% de la ventana" % (100.0 * (gen_s + pro_s) / dt))
    print("techo si la gpu no parara: %.0f/h" % (3600.0 / ((gen_s + pro_s) / avisos)))
print("ritmo actual: %.0f/h (%.1f/min)" % (avisos * 3600.0 / dt, avisos * 60.0 / dt))
print("cola=%s listos_para_gpu=%s trabajando=%s" % (pipe.get("queue"), pipe.get("ready"), pipe.get("working")))
