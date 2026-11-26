"""MCP tool definitions, shared by:
  * the HTTP MCP endpoint inside Nuclear Agent (/mcp)  - for tunnels, Cursor, etc.
  * the stdio MCP server (nuclear_mcp.py)              - for Claude Desktop / Claude Code on this PC

Both give the same tools; only the "backend" that carries out the call differs.
"""

from __future__ import annotations

from typing import Any, Protocol

from mcp.server.fastmcp import FastMCP, Image

INSTRUCTIONS = """Nuclear Agent is a browser agent running on the user's own Windows PC, in a real Chromium
browser that may be logged in to the user's accounts. Use run_web_task to give it a goal in plain language
(it plans and clicks by itself, one step at a time, with a local AI model). Tasks can take minutes:
if run_web_task returns while the task is still running, call wait_for_task with the task_id.
Only one task runs at a time. Sensitive actions (purchases, sending messages, account changes, entering
personal data) pause for the USER to approve in the Nuclear Agent panel - you cannot approve them.
Passwords, card numbers and CAPTCHAs are always left to the user."""


class Backend(Protocol):
    async def start(self, goal: str, start_url: str, max_actions: int) -> dict: ...
    async def info(self, task_id: int) -> dict: ...
    async def wait(self, task_id: int, seconds: float) -> dict: ...
    async def stop(self) -> dict: ...
    async def reply(self, text: str) -> dict: ...
    async def list(self, limit: int) -> dict: ...
    async def screenshot(self) -> bytes | None: ...


def build_mcp(backend: Backend, **fastmcp_kwargs: Any) -> FastMCP:
    mcp = FastMCP("nuclear-agent", instructions=INSTRUCTIONS, **fastmcp_kwargs)

    @mcp.tool()
    async def run_web_task(task: str, start_url: str = "", max_actions: int = 0, wait_seconds: int = 50) -> dict:
        """Give Nuclear Agent a task to do in a real web browser on the user's PC, e.g.
        "Go to sv.wikipedia.org and find the population of Umeå". Optional start_url opens a page first.
        Waits up to wait_seconds (max 600) and returns the result if finished, otherwise the task_id and
        status - then call wait_for_task."""
        snap = await backend.start(task, start_url, max_actions)
        return await backend.wait(int(snap["task_id"]), max(1, min(int(wait_seconds or 50), 600)))

    @mcp.tool()
    async def wait_for_task(task_id: int, seconds: int = 50) -> dict:
        """Wait (max 600 s) until the task finishes or needs the user, then return its status and result."""
        return await backend.wait(int(task_id), max(1, min(int(seconds or 50), 600)))

    @mcp.tool()
    async def get_task(task_id: int = 0) -> dict:
        """Status, result, notes and recent actions of a task. task_id 0 = the current or latest task."""
        return await backend.info(int(task_id or 0))

    @mcp.tool()
    async def reply_to_agent(text: str) -> dict:
        """Answer a question the agent asked (status needs_user) and let it continue."""
        return await backend.reply(text)

    @mcp.tool()
    async def stop_task() -> dict:
        """Stop the running task."""
        return await backend.stop()

    @mcp.tool()
    async def list_tasks(limit: int = 10) -> dict:
        """List recent tasks (newest first)."""
        return await backend.list(max(1, min(int(limit or 10), 50)))

    @mcp.tool()
    async def get_screenshot() -> Image:
        """A screenshot of what the agent's browser shows right now (JPEG)."""
        data = await backend.screenshot()
        if not data:
            raise ValueError("The browser is not running, so there is no screenshot.")
        return Image(data=data, format="jpeg")

    return mcp


class LocalBackend:
    """Runs inside the Nuclear Agent process. The caller (key) comes from a contextvar
    set by the /mcp endpoint after it checked the API key."""

    def __init__(self, service, caller_var):
        self.svc = service
        self.caller_var = caller_var

    def _c(self) -> dict:
        caller = self.caller_var.get(None)
        if not caller:
            raise PermissionError("Not authenticated")
        return caller

    async def start(self, goal, start_url, max_actions):
        return await self.svc.start(self._c(), goal, start_url, max_actions or None)

    async def info(self, task_id):
        return await self.svc.task_info(self._c(), task_id or None)

    async def wait(self, task_id, seconds):
        return await self.svc.wait(self._c(), task_id, seconds)

    async def stop(self):
        return await self.svc.stop(self._c())

    async def reply(self, text):
        return await self.svc.reply(self._c(), text)

    async def list(self, limit):
        return await self.svc.list_tasks(self._c(), limit)

    async def screenshot(self):
        return await self.svc.screenshot(self._c())
