"""Local control API + control panel.

Security model (see docs/REMOTE_ACCESS.md):
  * Bound to 127.0.0.1 only.
  * Every /api call needs the X-Nuclear-Token header. The token is random per
    start and only embedded in the panel page served by this process.
  * Host header must be a loopback name (blocks DNS-rebinding attacks).
  * Cross-site Origins are rejected (blocks other websites from driving the agent).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from . import __version__, ocr
from .agent import AgentManager
from .browser import BrowserError, BrowserManager
from .config import PROJECT_ROOT, Config
from .engine import Engine
from .llm import LLMError, OllamaClient
from .storage import Storage

log = logging.getLogger(__name__)
WEB_DIR = PROJECT_ROOT / "web"
LOOPBACK_NAMES = {"127.0.0.1", "localhost", "[::1]", "::1"}


# ------------------------------------------------------------------ models
class TaskIn(BaseModel):
    goal: str = Field(..., max_length=4000)
    start_url: str = ""
    max_actions: int | None = None
    model: str = ""


class UrlIn(BaseModel):
    url: str


class ConfirmIn(BaseModel):
    approve: bool


class MessageIn(BaseModel):
    text: str = Field(..., max_length=1000)


class ManualAction(BaseModel):
    type: str
    x: int | None = None
    y: int | None = None
    direction: str = "down"
    key: str = ""
    text: str = Field("", max_length=2000)


class AppState:
    def __init__(self, config: Config, llm: Any = None):
        self.config = config
        self.token = secrets.token_urlsafe(32)
        self.engine = Engine()
        self.storage = Storage(config.db_path)
        self.browser = BrowserManager(config)
        self.llm = llm or OllamaClient(config.get("ollama", "url"), config.get("ollama", "request_timeout_s"))
        self.agent = AgentManager(config, self.storage, self.browser, self.llm)


def create_app(config: Config | None = None, *, llm: Any = None, extra_hosts: set[str] | None = None) -> FastAPI:
    config = config or Config()
    state = AppState(config, llm)
    allowed_hosts = set(LOOPBACK_NAMES) | (extra_hosts or set())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state.engine.start()
        interrupted = state.storage.mark_interrupted()
        if interrupted:
            log.warning("Tasks interrupted by last shutdown: %s (resume them from the panel)", interrupted)
        yield
        try:
            await state.engine.call(state.agent.stop(), 15)
            await state.engine.call(state.browser.stop(), 20)
        except Exception as exc:
            log.warning("Shutdown cleanup: %s", exc)
        state.engine.stop()
        state.storage.close()

    app = FastAPI(title="Nuclear Agent", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.na = state

    # ------------------------------------------------------- middleware
    @app.middleware("http")
    async def guard(request: Request, call_next):
        if _hostname(request.headers.get("host") or "") not in allowed_hosts:
            return JSONResponse({"error": "Forbidden host"}, status_code=403)
        origin = request.headers.get("origin")
        if origin:
            if _hostname(origin.split("://", 1)[-1]) not in allowed_hosts:
                return JSONResponse({"error": "Cross-site request blocked"}, status_code=403)
        if request.url.path.startswith("/api/"):
            if not secrets.compare_digest(request.headers.get("x-nuclear-token", ""), state.token):
                return JSONResponse({"error": "Missing or invalid token. Reload the control panel."}, status_code=401)
        try:
            response = await call_next(request)
        except Exception as exc:  # never let one request crash the server
            log.exception("Unhandled error on %s", request.url.path)
            return JSONResponse({"error": f"Internal error: {exc.__class__.__name__}: {exc}"}, status_code=500)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        return response

    async def run(coro, timeout: float = 120):
        """Run on the engine; map known errors to 400 responses."""
        try:
            return await state.engine.call(coro, timeout)
        except (ValueError, RuntimeError, BrowserError, LLMError) as exc:
            raise _ApiError(str(exc), 400)
        except asyncio.TimeoutError:
            raise _ApiError("Operation timed out", 504)

    @app.exception_handler(_ApiError)
    async def _handle(request: Request, exc: _ApiError):
        return JSONResponse({"error": exc.message}, status_code=exc.status)

    def _manual_allowed() -> None:
        r = state.agent.runner
        if state.agent.busy and r and r.status == "running":
            raise _ApiError("A task is running. Pause it before controlling the browser manually.", 409)

    # ------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    async def index():
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("__NUCLEAR_TOKEN__", state.token).replace("__VERSION__", __version__))

    @app.get("/static/{name}")
    async def static(name: str):
        if name not in {"app.js", "styles.css", "logo.svg"}:
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(WEB_DIR / name)

    # ----------------------------------------------------------- status
    @app.get("/api/status")
    async def status():
        s = await run(state.agent.status(), 10)
        s["ocr_available"] = ocr.available()
        s["version"] = __version__
        return s

    @app.get("/api/models")
    async def models():
        health = await run(state.llm.health(), 10)
        if not health.get("ok"):
            return {"ok": False, "error": health.get("error"), "models": [], "selected": config.get("ollama", "model")}
        try:
            items = await run(state.llm.list_models(), 60)
        except _ApiError as exc:
            return {"ok": False, "error": exc.message, "models": [], "selected": config.get("ollama", "model")}
        return {"ok": True, "version": health.get("version"), "models": items,
                "selected": config.get("ollama", "model")}

    @app.get("/api/settings")
    async def get_settings():
        return config.as_dict()

    @app.post("/api/settings")
    async def set_settings(changes: dict):
        updated = config.update(changes)
        if "ollama" in changes:
            state.llm.base_url = config.get("ollama", "url").rstrip("/")
            state.llm.timeout_s = config.get("ollama", "request_timeout_s")
        return updated

    # ---------------------------------------------------------- browser
    @app.post("/api/browser/start")
    async def browser_start():
        return await run(state.browser.start(), 90)

    @app.post("/api/browser/stop")
    async def browser_stop():
        if state.agent.busy:
            raise _ApiError("Stop the running task first.", 409)
        return await run(state.browser.stop(), 30)

    @app.post("/api/browser/navigate")
    async def browser_navigate(body: UrlIn):
        _manual_allowed()

        async def go():
            await state.browser.ensure()
            return {"url": await state.browser.navigate(body.url)}
        return await run(go(), 60)

    @app.post("/api/browser/action")
    async def browser_action(a: ManualAction):
        """Manual control from the panel (the user, not the AI)."""
        _manual_allowed()
        b = state.browser

        async def act():
            await b.ensure()
            if a.type == "click":
                if a.x is None or a.y is None:
                    raise ValueError("click needs x and y")
                return {"url": await b.click(x=a.x, y=a.y)}
            if a.type == "scroll":
                return await b.scroll(a.direction)
            if a.type == "press":
                return {"url": await b.press(a.key)}
            if a.type == "type":
                await b.page.keyboard.type(a.text, delay=10)
                return {"ok": True}
            if a.type == "back":
                return {"url": await b.back()}
            if a.type == "forward":
                return {"url": await b.forward()}
            if a.type == "reload":
                return {"url": await b.reload()}
            raise ValueError(f"Unknown manual action '{a.type}'")
        return await run(act(), 60)

    @app.get("/api/browser/inspect")
    async def browser_inspect():
        async def ins():
            if not state.browser.running:
                raise RuntimeError("Browser is not running")
            o = await state.browser.observe(80, 6000)
            return o.to_dict()
        return await run(ins(), 30)

    @app.get("/api/screenshot")
    async def screenshot():
        async def shot():
            b = state.browser
            if not b.running:
                return b.last_screenshot
            if b.last_screenshot and time.time() - b.last_screenshot_at < 0.7:
                return b.last_screenshot
            try:
                return await b.screenshot()
            except BrowserError:
                return b.last_screenshot
        data = await run(shot(), 20)
        if not data:
            return Response(status_code=204)
        return Response(content=data, media_type="image/jpeg")

    # ------------------------------------------------------------ tasks
    @app.post("/api/task")
    async def task_start(body: TaskIn):
        return await run(state.agent.start_task(body.goal, body.start_url, body.max_actions, body.model), 60)

    @app.post("/api/task/pause")
    async def task_pause():
        return await run(state.agent.pause())

    @app.post("/api/task/resume")
    async def task_resume():
        return await run(state.agent.resume())

    @app.post("/api/task/stop")
    async def task_stop():
        return await run(state.agent.stop(), 30)

    @app.post("/api/task/confirm")
    async def task_confirm(body: ConfirmIn):
        return await run(state.agent.confirm(body.approve))

    @app.post("/api/task/message")
    async def task_message(body: MessageIn):
        return await run(state.agent.message(body.text))

    @app.get("/api/tasks")
    async def tasks(limit: int = 20, offset: int = 0):
        return {"tasks": state.storage.list_tasks(limit, offset), "total": state.storage.count_tasks()}

    @app.get("/api/tasks/{task_id}")
    async def task_get(task_id: int):
        t = state.storage.get_task(task_id)
        if not t:
            raise _ApiError("Task not found", 404)
        return t

    @app.post("/api/tasks/{task_id}/resume")
    async def task_restore(task_id: int):
        return await run(state.agent.resume_task(task_id), 60)

    @app.get("/api/tasks/{task_id}/actions")
    async def task_actions(task_id: int, limit: int = 20, before: int | None = None):
        return {"actions": state.storage.list_actions(task_id, limit, before)}

    @app.get("/api/tasks/{task_id}/checkpoints")
    async def task_checkpoints(task_id: int, limit: int = 20):
        return {"checkpoints": state.storage.list_checkpoints(task_id, limit)}

    return app


def _hostname(hostport: str) -> str:
    """'127.0.0.1:8765' -> '127.0.0.1', '[::1]:8765' -> '[::1]'."""
    hostport = hostport.strip().split("/", 1)[0]
    if hostport.startswith("["):
        return hostport.split("]", 1)[0] + "]"
    return hostport.rsplit(":", 1)[0] if ":" in hostport else hostport


class _ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status
