from app import llm_enrich, store
from app.llm_fields import extract_json_obj

items = store.fetch_llm_backlog(3, schema=llm_enrich.LLM_SCHEMA)
print("backlog traido:", len(items))
for item in items[:2]:
    prep = llm_enrich._build_job(item)
    if not prep:
        print("sin prompt para", item.id)
        continue
    print("=" * 70)
    print("id:", item.id, "| prompt", len(prep["prompt"]), "chars")
    raw = llm_enrich._chat_json(prep["prompt"])
    print("--- raw (%d chars) ---" % len(raw or ""))
    print(repr(raw))
    data = extract_json_obj(raw)
    print("--- parseado ---")
    print(data)
