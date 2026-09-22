"""Fallas de ficha por minuto, para ver si dejó de reintentar en bucle."""
import json
import time
import urllib.request

H = {"x-propmap-ops": "PropMapMatomo1"}


def snap():
    req = urllib.request.Request("http://127.0.0.1:8000/api/ops", headers=H)
    with urllib.request.urlopen(req, timeout=15) as res:
        d = json.load(res)
    tel = d.get("telemetry") or {}
    serie = tel.get("series") or []
    ult = serie[-1] if serie else {}
    by = ult.get("details_by") or {}
    fi = d.get("details_pipe") or {}
    return {
        "min_fail": int(by.get("fail") or 0),
        "min_ok": int(by.get("ok") or 0),
        "cola": fi.get("queue"),
        "bajando": fi.get("working"),
        "esperando": fi.get("cooling"),
        "fail_h": fi.get("fail_h"),
    }


for i in range(7):
    print(snap(), flush=True)
    time.sleep(20)
