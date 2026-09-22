"""Por qué fallan las bajadas de fichas: códigos HTTP y portales."""
import json
import urllib.request

H = {"x-propmap-ops": "PropMapMatomo1"}
req = urllib.request.Request("http://127.0.0.1:8000/api/ops", headers=H)
with urllib.request.urlopen(req, timeout=15) as res:
    d = json.load(res)

fi = d.get("details_pipe") or {}
print("fichas: %s" % {k: fi.get(k) for k in ("done", "need", "queue", "working", "workers", "pct", "per_min", "per_hour", "this_min", "ok_h", "fail_h", "ok_d", "fail_d")})

http = (d.get("telemetry") or {}).get("http") or {}
print("\ncodigos HTTP de este proceso:")
for code, n in sorted((http.get("by_status") or {}).items(), key=lambda kv: -kv[1]):
    print("  %-6s %s" % (code, n))
print("\npor portal:")
for host, n in sorted((http.get("by_host") or {}).items(), key=lambda kv: -kv[1])[:12]:
    print("  %-34s %s" % (host, n))
print("\npor carril:")
for lane, n in sorted((http.get("by_lane") or {}).items(), key=lambda kv: -kv[1])[:12]:
    print("  %-22s %s" % (lane, n))

eg = d.get("egress") or {}
print("\ncarriles:")
for t in eg.get("tracks") or []:
    print("  %-22s caido_s=%s kind=%s" % (t.get("id"), t.get("down_s"), t.get("kind")))
