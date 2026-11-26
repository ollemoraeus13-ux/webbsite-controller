"""Remote access: /remote/v1/* (REST, used by the GitHub Pages panel) and /mcp
(MCP over HTTP, used by AI clients through localhost or a tunnel).

Rules (enforced in server.guard + here):
  * A valid API key is always required (Authorization: Bearer na_...,
    or for AI apps that can't send headers: /mcp/k/na_...).
  * Requests whose Host is not localhost (i.e. arriving through a tunnel)
    are refused unless remote.enabled is true, and local-only keys never
    work through a tunnel.
  * Browsers may only call from origins listed in remote.allowed_origins.
  * Rate limits per key; every write is written to data/logs/remote_audit.jsonl.
"""

from __future__ import annotations

import contextvars
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from .browser import BrowserError
from .llm import LLMError
from .service import RemoteService, ScopeError

log = logging.getLogger(__name__)
caller_var: contextvars.ContextVar[dict | None] = contextvars.ContextVar("nuclear_caller", default=None)


class RemoteTaskIn(BaseModel):
    goal: str = Field(..., max_length=4000)
    start_url: str = ""
    max_actions: int | None = None


class ReplyIn(BaseModel):
    text: str = Field(..., max_length=1000)
    resume: bool = True


class ApproveIn(BaseModel):
    approve: bool


class RemoteError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


def is_remote_path(path: str) -> bool:
    return path.startswith("/remote/") or path == "/mcp" or path.startswith("/mcp/")


def authenticate(state: Any, request_headers: dict, path_key: str | None, host_is_local: bool) -> dict:
    """Return the key record for this request or raise RemoteError."""
    auth = request_headers.get("authorization", "")
    key = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    key = key or (path_key or "")
    rec = state.keys.verify(key)
    if not rec:
        raise RemoteError("Missing or invalid API key", 401)
    if not host_is_local:
        if not state.config.get("remote", "enabled"):
            raise RemoteError("Remote access is turned off in Nuclear Agent (Settings > AI connections).", 403)
        if rec.get("local_only"):
            raise RemoteError("This key only works on this PC (localhost).", 403)
    per_min = state.config.get("remote", "max_requests_per_minute")
    if not state.keys.allow_request(rec["id"], per_min):
        raise RemoteError(f"Rate limit: max {per_min} requests per minute for this key", 429)
    return rec


def build_router(state: Any, service: RemoteService) -> APIRouter:
    r = APIRouter(prefix="/remote/v1")

    async def caller(request: Request) -> dict:
        return authenticate(state, dict(request.headers), None, request.state.host_is_local)

    async def run(request: Request, fn):
        try:
            c = await caller(request)
            return await fn(c)
        except RemoteError as e:
            return JSONResponse({"error": e.message}, status_code=e.status)
        except ScopeError as e:
            return JSONResponse({"error": str(e)}, status_code=403)
        except (ValueError, RuntimeError, BrowserError, LLMError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except TimeoutError:
            return JSONResponse({"error": "Timed out"}, status_code=504)

    @r.get("/whoami")
    async def whoami(request: Request):
        return await run(request, lambda c: _done({"name": c["name"], "scopes": c["scopes"]}))

    @r.get("/status")
    async def status(request: Request):
        return await run(request, lambda c: service.status(c))

    @r.post("/tasks")
    async def start(request: Request, body: RemoteTaskIn):
        return await run(request, lambda c: service.start(c, body.goal, body.start_url, body.max_actions))

    @r.get("/tasks")
    async def tasks(request: Request, limit: int = 10, offset: int = 0):
        return await run(request, lambda c: service.list_tasks(c, limit, offset))

    @r.get("/tasks/{task_id}")
    async def task(request: Request, task_id: int):
        return await run(request, lambda c: service.task_info(c, task_id))

    @r.post("/tasks/{task_id}/wait")
    async def wait(request: Request, task_id: int, seconds: float = 30):
        return await run(request, lambda c: service.wait(c, task_id, seconds))

    @r.get("/tasks/{task_id}/actions")
    async def actions(request: Request, task_id: int, limit: int = 20, before: int | None = None):
        return await run(request, lambda c: service.actions(c, task_id, limit, before))

    @r.post("/task/stop")
    async def stop(request: Request):
        return await run(request, lambda c: service.stop(c))

    @r.post("/task/pause")
    async def pause(request: Request):
        return await run(request, lambda c: service.pause(c))

    @r.post("/task/resume")
    async def resume(request: Request):
        return await run(request, lambda c: service.resume(c))

    @r.post("/task/reply")
    async def reply(request: Request, body: ReplyIn):
        return await run(request, lambda c: service.reply(c, body.text, body.resume))

    @r.post("/task/confirm")
    async def confirm(request: Request, body: ApproveIn):
        return await run(request, lambda c: service.confirm(c, body.approve))

    @r.get("/screenshot")
    async def screenshot(request: Request):
        async def shot(c):
            data = await service.screenshot(c)
            return Response(content=data, media_type="image/jpeg") if data else Response(status_code=204)
        return await run(request, shot)

    return r


async def _done(value):
    return value


class McpEndpoint:
    """ASGI app for /mcp and /mcp/k/{key}: checks the key, then hands over to FastMCP."""

    def __init__(self, state: Any, mcp_app):
        self.state = state
        self.mcp_app = mcp_app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.mcp_app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        path_key = (scope.get("path_params") or {}).get("key")
        host_is_local = bool((scope.get("state") or {}).get("host_is_local"))
        try:
            rec = authenticate(self.state, headers, path_key, host_is_local)
        except RemoteError as e:
            resp = JSONResponse({"error": e.message}, status_code=e.status)
            return await resp(scope, receive, send)
        inner = dict(scope)
        inner["path"] = "/mcp"
        inner["raw_path"] = b"/mcp"
        token = caller_var.set(rec)
        try:
            await self.mcp_app(inner, receive, send)
        finally:
            caller_var.reset(token)
