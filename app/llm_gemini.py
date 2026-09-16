"""Cliente Gemini (Flash Lite, sin thinking) para la limpieza de avisos."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-3.5-flash-lite"


def api_key() -> str:
    return (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()


def model_name() -> str:
    raw = (os.environ.get("GEMINI_MODEL") or os.environ.get("LLM_MODEL") or DEFAULT_MODEL).strip()
    if not raw or raw == "qwen35-9b-iq2xxs":
        return DEFAULT_MODEL
    return raw


def gemini_tools(ollama_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    decls: list[dict[str, Any]] = []
    for item in ollama_tools:
        fn = (item.get("function") or {}) if isinstance(item, dict) else {}
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        decls.append(
            {
                "name": name,
                "description": str(fn.get("description") or ""),
                "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return [{"functionDeclarations": decls}] if decls else []


def contents_from_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    system = ""
    contents: list[dict[str, Any]] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        role = str(msg.get("role") or "")
        if role == "system":
            system = str(msg.get("content") or "")
            i += 1
            continue
        if role == "user":
            contents.append({"role": "user", "parts": [{"text": str(msg.get("content") or "")}]})
            i += 1
            continue
        if role == "assistant":
            calls = msg.get("tool_calls") or []
            if calls:
                parts: list[dict[str, Any]] = []
                for call in calls:
                    fn = (call.get("function") or {}) if isinstance(call, dict) else {}
                    args = fn.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    if not isinstance(args, dict):
                        args = {}
                    fc: dict[str, Any] = {"name": str(fn.get("name") or ""), "args": args}
                    cid = str(call.get("id") or "")
                    if cid:
                        fc["id"] = cid
                    part: dict[str, Any] = {"functionCall": fc}
                    sig = str(call.get("thought_signature") or "")
                    if sig:
                        part["thoughtSignature"] = sig
                    parts.append(part)
                contents.append({"role": "model", "parts": parts})
                i += 1
                replies: list[dict[str, Any]] = []
                while i < len(messages) and str(messages[i].get("role") or "") == "tool":
                    tool_msg = messages[i]
                    raw = tool_msg.get("content") or "{}"
                    try:
                        parsed = json.loads(raw) if isinstance(raw, str) else raw
                    except json.JSONDecodeError:
                        parsed = {"result": str(raw)}
                    if not isinstance(parsed, dict):
                        parsed = {"result": parsed}
                    fr: dict[str, Any] = {
                        "name": str(tool_msg.get("name") or ""),
                        "response": parsed,
                    }
                    tid = str(tool_msg.get("tool_call_id") or "")
                    if tid:
                        fr["id"] = tid
                    replies.append({"functionResponse": fr})
                    i += 1
                if replies:
                    contents.append({"role": "user", "parts": replies})
                continue
            contents.append({"role": "model", "parts": [{"text": str(msg.get("content") or "")}]})
            i += 1
            continue
        i += 1
    return system, contents


def payload_from_gemini(data: dict[str, Any]) -> dict[str, Any]:
    cand = (data.get("candidates") or [{}])[0]
    parts = ((cand.get("content") or {}).get("parts")) or []
    calls: list[dict[str, Any]] = []
    texts: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        fc = part.get("functionCall")
        if isinstance(fc, dict) and fc.get("name"):
            call: dict[str, Any] = {
                "id": str(fc.get("id") or fc.get("name")),
                "type": "function",
                "function": {
                    "name": str(fc.get("name")),
                    "arguments": json.dumps(fc.get("args") or {}, ensure_ascii=False),
                },
            }
            sig = str(part.get("thoughtSignature") or "")
            if sig:
                call["thought_signature"] = sig
            calls.append(call)
        text = part.get("text")
        if text:
            texts.append(str(text))
    return {"message": {"content": "\n".join(texts), "tool_calls": calls}}


def chat(messages: list[dict[str, Any]], ollama_tools: list[dict[str, Any]]) -> dict[str, Any]:
    key = api_key()
    if not key:
        return {}
    system, contents = contents_from_messages(messages)
    if not contents:
        return {}
    body: dict[str, Any] = {
        "contents": contents,
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 512},
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    tools = gemini_tools(ollama_tools)
    if tools:
        body["tools"] = tools
        body["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}
    url = f"{API_ROOT}/models/{model_name()}:generateContent"
    raw = json.dumps(body).encode("utf-8")
    last_err = b""
    for attempt in range(4):
        req = urllib.request.Request(
            url,
            data=raw,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_err = exc.read() or b""
            if exc.code in {429, 500, 503} and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            return {}
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
            if attempt < 3:
                time.sleep(1)
                continue
            return {}
        if isinstance(data, dict) and (data.get("candidates") or data.get("error")):
            if data.get("error"):
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                return {}
            return payload_from_gemini(data)
    _ = last_err
    return {}
