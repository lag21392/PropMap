from app.llm_gemini import contents_from_messages, gemini_tools, payload_from_gemini


def test_gemini_tools_unwraps_ollama_functions():
    tools = gemini_tools(
        [
            {
                "type": "function",
                "function": {
                    "name": "catalogo_campo",
                    "description": "catálogo",
                    "parameters": {"type": "object", "properties": {"campo": {"type": "string"}}},
                },
            }
        ]
    )
    decls = tools[0]["functionDeclarations"]
    assert decls[0]["name"] == "catalogo_campo"
    assert "parameters" in decls[0]


def test_contents_keeps_thought_signature_and_tool_replies():
    system, contents = contents_from_messages(
        [
            {"role": "system", "content": "extractor"},
            {"role": "user", "content": "aviso"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "thought_signature": "sig",
                        "function": {"name": "validar_esquina", "arguments": '{"calle_a":"Roca","calle_b":"Mitre"}'},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "validar_esquina",
                "content": '{"ok": true}',
            },
        ]
    )
    assert system == "extractor"
    assert contents[0]["role"] == "user"
    model_part = contents[1]["parts"][0]
    assert model_part["functionCall"]["name"] == "validar_esquina"
    assert model_part["thoughtSignature"] == "sig"
    reply = contents[2]["parts"][0]["functionResponse"]
    assert reply["id"] == "call_1"
    assert reply["response"]["ok"] is True


def test_payload_from_gemini_maps_function_calls():
    payload = payload_from_gemini(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "functionCall": {"name": "catalogo_campo", "args": {"campo": "tipo"}, "id": "c1"},
                                "thoughtSignature": "abc",
                            }
                        ]
                    }
                }
            ]
        }
    )
    call = payload["message"]["tool_calls"][0]
    assert call["id"] == "c1"
    assert call["thought_signature"] == "abc"
    assert call["function"]["name"] == "catalogo_campo"
