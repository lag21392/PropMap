#!/usr/bin/env bash
# Uso: ./check_status.sh
# Verifica el estado del pipeline de detail fetch y del sistema de egress.
set -euo pipefail

URL="${1:-http://127.0.0.1:8000/api/ops}"
PASS="${OPS_PASSWORD:-PropMapMatomo1}"

json=$(curl -s -H "x-propmap-ops: $PASS" "$URL" 2>/dev/null)
if [ -z "$json" ]; then
  echo "❌ No se puede conectar a $URL"
  exit 1
fi

echo "$json" | python3 -c "
import sys, json
d = json.loads(sys.stdin.read())
dp = d.get('details_pipe', {})
eg = d.get('egress', {})
live = d.get('live', {})

# Alertas
problems = []

# 1. Workers estancados?
if dp.get('working', 0) == 0 and dp.get('queue', 0) > 0:
    problems.append('⚠️  Sin workers activos pero hay cola')

# 2. Capa diaria de IP local agotada?
if eg.get('local_today', 0) >= eg.get('local_cap', 1) * 0.95:
    problems.append(f'⚠️  Capa diaria casi al tope: {eg.get(\"local_today\")}/{eg.get(\"local_cap\")}')

# 3. Tasa de fallo alta?
fail_h = dp.get('fail_h', 0)
ok_h = dp.get('ok_h', 1)
if fail_h > ok_h * 0.5:
    problems.append(f'⚠️  Tasa de fallo alta: {fail_h}/{ok_h+fail_h} ({100*fail_h/(ok_h+fail_h):.0f}%)')

# 4. Tor con pocos circuitos?
if eg.get('tor_circuits', 0) < 8:
    problems.append(f'⚠️  Pocos circuitos Tor: {eg.get(\"tor_circuits\")}')

# 5. ETA muy lejana?
eta = dp.get('eta_h', 0)
if eta > 48:
    problems.append(f'⚠️  ETA muy lejana: {eta:.0f}h')

print('=== PIPELINE DETALLE ===')
print(f'  Hechos:  {dp.get(\"done\",0):>8}  Necesitan: {dp.get(\"need\",0):>6}  {dp.get(\"pct\",0):>5.1f}%')
print(f'  Cola:    {dp.get(\"queue\",0):>8}  Trabajando: {dp.get(\"working\",0):>3}  En frio: {dp.get(\"cooling\",0):>3}')
print(f'  OK/h:    {dp.get(\"ok_h\",0):>6}  Fail/h: {dp.get(\"fail_h\",0):>4}  Rate: {dp.get(\"per_min\",0):>5.1f}/min')
print(f'  ETA:     {eta:>8.1f}h')
print()
print('=== EGRESS ===')
print(f'  Lanes:   {eg.get(\"lanes\",0):>4}  Tor circuits: {eg.get(\"tor_circuits\",0):>3}')
print(f'  Local:   {eg.get(\"local_today\",0):>5}/{eg.get(\"local_cap\",0)}')
print()
print('=== CRAWL ===')
print(f'  Estado:  {live.get(\"message\",\"\")}')
print(f'  Modo:    {live.get(\"mode\",\"\")}')
print(f'  Ciudades: {len(live.get(\"queue\",[]))}  ETA: {live.get(\"eta_min\",0)}min')
print()

if problems:
    print('=== ALERTAS ===')
    for p in problems:
        print(p)
else:
    print('✅ Todo en orden')
" <<< "$json"