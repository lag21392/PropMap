from __future__ import annotations
import json
from pathlib import Path
DATA = Path(__file__).parent.parent / "data" / "alerts.json"
def load(): 
    if not DATA.exists(): return []
    try: return json.loads(DATA.read_text())
    except: return []
def save(lst): DATA.parent.mkdir(parents=True, exist_ok=True); DATA.write_text(json.dumps(lst))
def add_alert(user_id, filters):
    lst = load()
    lst.append({"user_id": user_id, "filters": filters, "ts": __import__('time').time()})
    save(lst)
    return {"ok": True}
def list_alerts(user_id): return [a for a in load() if a["user_id"]==user_id]
