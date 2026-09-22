import time

from app.ops import _today_start_min
from app.store import connect

now_min = int(time.time() // 60)
start = _today_start_min()
print("ahora=%s inicio_dia=%s (%s min de hoy)" % (now_min, start, now_min - start))
with connect() as c:
    print("--- ultimos 90 min ---")
    for row in c.execute(
        "SELECT metric, SUM(n), MIN(minute), MAX(minute) FROM ops_minute WHERE minute >= ? GROUP BY metric ORDER BY 2 DESC",
        (now_min - 90,),
    ):
        print(tuple(row))
    print("--- desde el inicio del dia ---")
    for row in c.execute(
        "SELECT metric, SUM(n) FROM ops_minute WHERE minute >= ? GROUP BY metric ORDER BY 2 DESC",
        (start,),
    ):
        print(tuple(row))
