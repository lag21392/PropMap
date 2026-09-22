import json

import httpx

from app import llm_enrich
from app.llm_fields import EXTRACT_SYSTEM

MSGS = [
    {"role": "system", "content": EXTRACT_SYSTEM},
    {"role": "user", "content": "lugar_buscado: caba\ntitulo: Depto 2 ambientes en Palermo\ndireccion: Thames 1200"},
]


def body(**over):
    out = {
        "model": llm_enrich.llm_model(),
        "stream": False,
        "think": False,
        "chat_template_kwargs": {"enable_thinking": False},
        "cache_prompt": True,
        "options": {"temperature": 0.1, "num_predict": llm_enrich.MAX_TOKENS, "num_ctx": llm_enrich.llm_ctx()},
        "max_tokens": llm_enrich.MAX_TOKENS,
        "temperature": 0.1,
        "stop": ["```", "<|im_end|>", "<end_of_turn>"],
        "messages": MSGS,
    }
    out.update(over)
    return out


url = "%s/v1/chat/completions" % llm_enrich.llm_url()
print("url:", url, "| max_tokens:", llm_enrich.MAX_TOKENS)

for label, extra in (
    ("con response_format json_object", {"response_format": {"type": "json_object"}}),
    ("sin response_format", {}),
):
    print("=" * 70)
    print(label)
    try:
        res = httpx.post(url, json=body(**extra), timeout=60.0, trust_env=False)
        print("HTTP", res.status_code)
        txt = res.text
        try:
            data = res.json()
        except Exception:
            data = None
        if not isinstance(data, dict) or not (data.get("choices") or data.get("message")):
            print("cuerpo:", txt[:600])
            continue
        ch = (data.get("choices") or [{}])[0]
        msg = ch.get("message") or {}
        print("finish_reason:", ch.get("finish_reason"))
        print("usage:", json.dumps(data.get("usage") or {}))
        print("content:", repr(str(msg.get("content") or ""))[:600])
    except Exception as exc:
        print("fallo:", type(exc).__name__, exc)
