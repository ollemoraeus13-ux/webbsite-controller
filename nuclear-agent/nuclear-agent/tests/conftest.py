"""Shared fixtures.

Tests run the real browser (headless) against local test pages, so no
internet is needed. Set NUCLEAR_TEST_CHROME to use a specific Chrome/Edge
executable instead of Playwright's bundled Chromium.
"""

from __future__ import annotations

import functools
import http.server
import os
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nuclear_agent.config import Config  # noqa: E402
from nuclear_agent.engine import Engine  # noqa: E402

PAGES = Path(__file__).parent / "pages"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # keep test output clean
        pass


@pytest.fixture(scope="session")
def site():
    """Serve tests/pages on a random localhost port."""
    handler = functools.partial(_QuietHandler, directory=str(PAGES))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def config(tmp_path):
    cfg = Config(path=tmp_path / "config.json", data_dir=tmp_path / "data")
    cfg.update({
        "browser": {"headless": True, "persistent_profile": False,
                    "executable_path": os.environ.get("NUCLEAR_TEST_CHROME", ""),
                    "navigation_timeout_ms": 15000, "action_timeout_ms": 4000},
        "agent": {"max_actions": 20, "use_ocr_fallback": False, "max_consecutive_errors": 3,
                  "step_timeout_s": 30},
        "ollama": {"model": "fake-model"},
    }, persist=False)
    return cfg


@pytest.fixture
def engine():
    e = Engine()
    e.start()
    yield e
    e.stop()


class FakeLLM:
    """Stands in for OllamaClient. `script` is a list of replies or callables(user_msg)->reply."""

    def __init__(self, script=None, models=None, vision_models=()):
        self.script = list(script or [])
        self.models = models or ["fake-model"]
        self.vision_models = set(vision_models)
        self.calls: list[dict] = []
        self.base_url = "http://fake"
        self.timeout_s = 30

    async def health(self):
        return {"ok": True, "version": "fake"}

    async def list_models(self):
        return [{"name": m, "size_gb": 1.0, "parameters": "7B", "quantization": "Q4",
                 "vision": m in self.vision_models, "capabilities": ["completion"]} for m in self.models]

    async def capabilities(self, model):
        return ["completion", "vision"] if model in self.vision_models else ["completion"]

    async def supports_vision(self, model):
        return model in self.vision_models

    async def describe_image(self, model, image, question, **kw):
        return "A test page."

    async def chat_json(self, model, messages, images=None, **kw):
        user = messages[-1]["content"]
        self.calls.append({"model": model, "user": user, "images": bool(images)})
        if not self.script:
            return {"summary": "nothing left", "action": {"type": "done", "answer": "script exhausted"}}
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            import asyncio
            return await asyncio.to_thread(item, user)  # slow scripted steps must not block the loop
        return item


@pytest.fixture
def fake_llm_cls():
    return FakeLLM
