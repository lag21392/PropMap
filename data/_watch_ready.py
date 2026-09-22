"""Cuánto tiempo la GPU se queda sin prompt de reserva. Se corre desde el host."""
import json
import time
import urllib.request

H = {"x-propmap-ops": "PropMapMatomo1"}
N = 40

vacio = 0
vacio_con_gpu_libre = 0
hist: dict[int, int] = {}
for i in range(N):
    req = urllib.request.Request("http://127.0.0.1:8000/api/ops", headers=H)
    with urllib.request.urlopen(req, timeout=10) as res:
        p = json.load(res)["llm_pipe"]
    ready = int(p.get("ready") or 0)
    hist[ready] = hist.get(ready, 0) + 1
    if ready == 0:
        vacio += 1
        if not p.get("gpu"):
            vacio_con_gpu_libre += 1
    time.sleep(1.0)

print("muestras: %s" % N)
print("reparto de prompts en reserva: %s" % dict(sorted(hist.items())))
print("sin reserva: %s%% · de esas, con la GPU libre (perdiendo tiempo): %s" % (round(100 * vacio / N), vacio_con_gpu_libre))
