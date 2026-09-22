#!/bin/sh
# Mide ritmo del LLM desde el host, sin abrir otro proceso python dentro del contenedor.
H="x-propmap-ops: ${OPS_PASS:-PropMapMatomo1}"
URL="http://127.0.0.1:8000/api/ops"
pick() { python3 -c "import json,sys;d=json.load(sys.stdin)['llm_pipe'];print(d.get('done',0)+d.get('partial',0),d.get('ready',0),d.get('saving',0),d.get('queue',0),d.get('per_min',0))"; }
set -- $(curl -s -H "$H" "$URL" | pick)
A=$1
T0=$(date +%s)
sleep "${1:-120}" 2>/dev/null || sleep 120
set -- $(curl -s -H "$H" "$URL" | pick)
B=$1
T1=$(date +%s)
DT=$((T1 - T0))
python3 - "$A" "$B" "$DT" "$2" "$3" "$4" <<'PY'
import sys
a, b, dt, ready, saving, cola = (int(float(x)) for x in sys.argv[1:7])
n = b - a
print("ventana %ss · avisos terminados %s" % (dt, n))
print("ritmo: %.0f/h (%.1f/min)" % (n * 3600.0 / dt, n * 60.0 / dt))
print("prompts listos=%s esperando guardado=%s cola=%s" % (ready, saving, cola))
PY
