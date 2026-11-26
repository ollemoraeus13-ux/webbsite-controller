"""Ollama client tests against a fake Ollama HTTP server (real HTTP, fake model)."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from nuclear_agent.llm import LLMError, OllamaClient, parse_json_reply


class FakeOllama(BaseHTTPRequestHandler):
    last_chat: dict = {}

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/version":
            return self._send(200, {"version": "0.9.9"})
        if self.path == "/api/tags":
            return self._send(200, {"models": [
                {"name": "qwen2.5:7b", "size": 4_700_000_000, "details": {"parameter_size": "7.6B", "quantization_level": "Q4_K_M"}},
                {"name": "qwen2.5vl:3b", "size": 3_200_000_000, "details": {"parameter_size": "3B"}},
                {"name": "oldllava:latest", "size": 1, "details": {}},
            ]})
        self._send(404, {"error": "nf"})

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        if self.path == "/api/show":
            if data["model"] == "qwen2.5vl:3b":
                return self._send(200, {"capabilities": ["completion", "vision"]})
            if data["model"] == "oldllava:latest":  # old Ollama: no capabilities field
                return self._send(200, {"details": {"families": ["llama", "clip"]}})
            return self._send(200, {"capabilities": ["completion", "tools"]})
        if self.path == "/api/chat":
            FakeOllama.last_chat = data
            if data["model"] == "missing":
                return self._send(404, {"error": "model not found"})
            reply = '```json\n{"summary": "s", "action": {"type": "done", "answer": "42"}}\n```'
            return self._send(200, {"message": {"role": "assistant", "content": reply}})
        self._send(404, {"error": "nf"})


@pytest.fixture(scope="module")
def ollama_url():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_list_models_and_vision_detection(ollama_url):
    c = OllamaClient(ollama_url)
    models = asyncio.run(c.list_models())
    by = {m["name"]: m for m in models}
    assert by["qwen2.5:7b"]["vision"] is False
    assert by["qwen2.5vl:3b"]["vision"] is True
    assert by["oldllava:latest"]["vision"] is True
    assert by["qwen2.5:7b"]["size_gb"] == 4.7
    assert asyncio.run(c.health())["ok"]


def test_chat_json_payload_and_parsing(ollama_url):
    c = OllamaClient(ollama_url)
    out = asyncio.run(c.chat_json("qwen2.5:7b", [{"role": "user", "content": "hi"}], num_ctx=4096, keep_alive="1m"))
    assert out["action"]["answer"] == "42"
    sent = FakeOllama.last_chat
    assert sent["format"] == "json" and sent["stream"] is False
    assert sent["options"]["num_ctx"] == 4096 and sent["keep_alive"] == "1m"


def test_text_model_never_gets_images(ollama_url):
    c = OllamaClient(ollama_url)
    with pytest.raises(LLMError):
        asyncio.run(c.chat_json("qwen2.5:7b", [{"role": "user", "content": "x"}], images=[b"\xff\xd8"]))
    out = asyncio.run(c.chat_json("qwen2.5vl:3b", [{"role": "user", "content": "x"}], images=[b"\xff\xd8"]))
    assert out["summary"] == "s"
    assert FakeOllama.last_chat["messages"][-1]["images"]


def test_missing_model_and_offline():
    with pytest.raises(LLMError):
        asyncio.run(OllamaClient("http://127.0.0.1:9").list_models())
    assert asyncio.run(OllamaClient("http://127.0.0.1:9").health())["ok"] is False


def test_missing_model_message(ollama_url):
    with pytest.raises(LLMError, match="ollama pull missing"):
        asyncio.run(OllamaClient(ollama_url).chat_json("missing", [{"role": "user", "content": "x"}]))


def test_parse_json_reply_variants():
    assert parse_json_reply('{"a": 1}') == {"a": 1}
    assert parse_json_reply('Sure! {"a": 2} hope that helps') == {"a": 2}
    assert parse_json_reply('```json\n{"a": 3}\n```') == {"a": 3}
    for bad in ("", "no json here", "[1,2]"):
        with pytest.raises(LLMError):
            parse_json_reply(bad)
