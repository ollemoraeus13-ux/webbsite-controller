"""Ollama client: model discovery, capability detection and JSON chat.

Only talks to the local Ollama server (default http://127.0.0.1:11434).
No paid APIs, no cloud calls.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Model families known to accept images, used only when Ollama is too old to
# report "capabilities". A name match alone is never trusted if Ollama says otherwise.
VISION_NAME_HINTS = ("llava", "bakllava", "moondream", "vision", "qwen2.5vl", "qwen2-vl", "qwen2.5-vl",
                     "minicpm-v", "gemma3", "llama3.2-vision", "granite3.2-vision", "mistral-small3.1")


class LLMError(Exception):
    pass


class OllamaClient:
    def __init__(self, base_url: str = "http://127.0.0.1:11434", timeout_s: float = 180):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._caps_cache: dict[str, list[str]] = {}

    def _client(self, timeout: float | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, timeout=timeout or self.timeout_s)

    async def health(self) -> dict:
        try:
            async with self._client(5) as c:
                r = await c.get("/api/version")
                r.raise_for_status()
                return {"ok": True, "version": r.json().get("version", "?")}
        except Exception as exc:
            return {"ok": False, "error": f"Ollama not reachable at {self.base_url}: {exc.__class__.__name__}"}

    async def list_models(self) -> list[dict]:
        """Installed models with size and whether they accept images."""
        try:
            async with self._client(10) as c:
                r = await c.get("/api/tags")
                r.raise_for_status()
                models = r.json().get("models", [])
        except Exception as exc:
            raise LLMError(f"Could not list Ollama models ({exc.__class__.__name__}). Is Ollama running?") from exc
        out = []
        for m in models:
            name = m.get("name") or m.get("model")
            if not name:
                continue
            caps = await self.capabilities(name)
            details = m.get("details") or {}
            out.append({
                "name": name,
                "size_gb": round((m.get("size") or 0) / 1e9, 2),
                "parameters": details.get("parameter_size", ""),
                "quantization": details.get("quantization_level", ""),
                "vision": "vision" in caps,
                "capabilities": caps,
            })
        out.sort(key=lambda x: x["name"])
        return out

    async def capabilities(self, model: str) -> list[str]:
        if model in self._caps_cache:
            return self._caps_cache[model]
        caps: list[str] = []
        try:
            async with self._client(15) as c:
                r = await c.post("/api/show", json={"model": model})
                r.raise_for_status()
                data = r.json()
            if isinstance(data.get("capabilities"), list):
                caps = [str(x) for x in data["capabilities"]]
            else:
                # Older Ollama: infer from projector info / family names.
                fams = " ".join((data.get("details") or {}).get("families") or []).lower()
                if data.get("projector_info") or "clip" in fams or any(h in model.lower() for h in VISION_NAME_HINTS):
                    caps = ["completion", "vision"]
                else:
                    caps = ["completion"]
        except Exception as exc:
            log.warning("Could not read capabilities for %s: %s", model, exc)
            caps = ["completion"]  # unknown -> assume text only; never pretend to see
        self._caps_cache[model] = caps
        return caps

    async def supports_vision(self, model: str) -> bool:
        return "vision" in await self.capabilities(model)

    async def chat_json(self, model: str, messages: list[dict], *, images: list[bytes] | None = None,
                        num_ctx: int = 8192, temperature: float = 0.2, keep_alive: str = "5m") -> dict:
        """Send a chat and return the parsed JSON object the model replied with."""
        msgs = [dict(m) for m in messages]
        if images:
            if not await self.supports_vision(model):
                raise LLMError(f"Model '{model}' does not support images")
            msgs[-1]["images"] = [base64.b64encode(b).decode() for b in images]
        payload = {
            "model": model,
            "messages": msgs,
            "stream": False,
            "format": "json",
            "keep_alive": keep_alive,
            "options": {"num_ctx": int(num_ctx), "temperature": float(temperature)},
        }
        try:
            async with self._client() as c:
                r = await c.post("/api/chat", json=payload)
                if r.status_code == 404:
                    raise LLMError(f"Model '{model}' is not installed. Run: ollama pull {model}")
                r.raise_for_status()
                content = (r.json().get("message") or {}).get("content", "")
        except LLMError:
            raise
        except httpx.TimeoutException as exc:
            raise LLMError(f"Ollama took longer than {self.timeout_s}s to answer. Try a smaller model.") from exc
        except Exception as exc:
            raise LLMError(f"Ollama request failed: {exc.__class__.__name__}: {exc}") from exc
        return parse_json_reply(content)

    async def describe_image(self, model: str, image: bytes, question: str, *, num_ctx: int = 4096,
                             keep_alive: str = "5m") -> str:
        """Ask a vision model for a short plain-text description of a screenshot."""
        if not await self.supports_vision(model):
            raise LLMError(f"Model '{model}' cannot read images")
        payload = {
            "model": model, "stream": False, "keep_alive": keep_alive,
            "options": {"num_ctx": int(num_ctx), "temperature": 0.1},
            "messages": [{"role": "user", "content": question,
                          "images": [base64.b64encode(image).decode()]}],
        }
        try:
            async with self._client() as c:
                r = await c.post("/api/chat", json=payload)
                r.raise_for_status()
                return ((r.json().get("message") or {}).get("content") or "").strip()[:1500]
        except Exception as exc:
            raise LLMError(f"Vision request failed: {exc.__class__.__name__}") from exc


def parse_json_reply(content: str) -> dict:
    """Extract a JSON object from a model reply, tolerating code fences and chatter."""
    content = (content or "").strip()
    if not content:
        raise LLMError("Model returned an empty reply")
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.IGNORECASE | re.MULTILINE).strip()
    try:
        obj: Any = json.loads(content)
    except ValueError:
        start, end = content.find("{"), content.rfind("}")
        if start == -1 or end <= start:
            raise LLMError(f"Model reply was not JSON: {content[:200]}")
        try:
            obj = json.loads(content[start:end + 1])
        except ValueError as exc:
            raise LLMError(f"Model reply was not valid JSON: {content[:200]}") from exc
    if not isinstance(obj, dict):
        raise LLMError("Model reply JSON was not an object")
    return obj
