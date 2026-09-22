"""Ritmo del LLM y ocupación del buffer, medido desde el host contra /api/ops."""
import json
import sys
import time
import urllib.request

H = {"x-propmap-ops": "PropMapMatomo1"}
WINDOW = float(sys.argv[1]) if len(sys.argv) > 1 else 120.0


def pipe():
    req = urllib.request.Request("http://127.0.0.1:8000/api/ops", headers=H)
    with urllib.request.urlopen(req, timeout=10) as res:
        return json.load(res)["llm_pipe"]


def hechos(p):
    return int(p.get("done") or 0) + int(p.get("partial") or 0)


p0 = pipe()
t0 = time.time()
hist: dict[int, int] = {}
sin_reserva_gpu_libre = 0
n = 0
while time.time() - t0 < WINDOW:
    time.sleep(2.0)
    p = pipe()
    n += 1
    ready = int(p.get("ready") or 0)
    hist[ready] = hist.get(ready, 0) + 1
    if ready == 0 and not p.get("gpu"):
        sin_reserva_gpu_libre += 1
p1 = pipe()
dt = time.time() - t0
avisos = hechos(p1) - hechos(p0)

print("ventana %.0fs · avisos terminados %s" % (dt, avisos))
print("ritmo: %.0f/h (%.1f/min)" % (avisos * 3600.0 / dt, avisos * 60.0 / dt))
print("prompts en reserva: %s" % dict(sorted(hist.items())))
print("GPU libre sin prompt listo: %s de %s muestras" % (sin_reserva_gpu_libre, n))
print("cola=%s esperando guardado=%s" % (p1.get("queue"), p1.get("saving")))
