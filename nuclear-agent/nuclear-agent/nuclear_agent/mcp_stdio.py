"""MCP server over stdio, for AI apps on THIS PC (Claude Desktop, Claude Code, ...).

The AI app starts this script itself. It forwards tool calls to the running
Nuclear Agent on http://127.0.0.1:<port> using the local key in
data/local_mcp_key.txt. If Nuclear Agent isn't running, it starts it.

Nothing may be printed to stdout here (stdout is the MCP channel); logs go to stderr.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time

import httpx

from .config import PROJECT_ROOT, Config
from .mcp_tools import build_mcp

log = logging.getLogger("nuclear_mcp")


class HttpBackend:
    def __init__(self, base_url: str, key_path, start_server: bool = True):
        self.base = base_url.rstrip("/") + "/remote/v1"
        self.key_path = key_path
        self.start_server = start_server
        self._started_server = False

    def _headers(self) -> dict:
        try:
            key = self.key_path.read_text(encoding="utf-8").strip()
        except OSError:
            key = ""
        return {"Authorization": f"Bearer {key}"}

    async def _ensure_server(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=3) as c:
                r = await c.get(self.base + "/whoami", headers=self._headers())
            if r.status_code == 200:
                return
            if r.status_code != 401:
                return  # running; let the real call report the problem
        except httpx.TransportError:
            pass
        if not self.start_server or self._started_server:
            raise RuntimeError("Nuclear Agent is not running. Start it with start.bat.")
        self._started_server = True
        self._launch()
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                async with httpx.AsyncClient(timeout=3) as c:
                    r = await c.get(self.base + "/whoami", headers=self._headers())
                if r.status_code == 200:
                    return
            except httpx.TransportError:
                pass
            await asyncio.sleep(1)
        raise RuntimeError("Started Nuclear Agent but it did not answer within 40 s. Check data/logs/agent.log.")

    @staticmethod
    def _launch() -> None:
        log_dir = Config().data_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        out = open(log_dir / "started_by_mcp.log", "ab")
        kwargs: dict = {"cwd": str(PROJECT_ROOT), "stdout": out, "stderr": out, "stdin": subprocess.DEVNULL}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([sys.executable, "-m", "nuclear_agent", "--no-open"], **kwargs)
        log.info("Started Nuclear Agent in the background")

    async def _call(self, method: str, path: str, *, json: dict | None = None, params: dict | None = None,
                    timeout: float = 60, raw: bool = False):
        await self._ensure_server()
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.request(method, self.base + path, headers=self._headers(), json=json, params=params)
        if raw:
            return r.content if r.status_code == 200 else None
        try:
            data = r.json()
        except ValueError:
            data = {"error": r.text[:300]}
        if r.status_code >= 400:
            raise RuntimeError(data.get("error") or f"HTTP {r.status_code}")
        return data

    async def start(self, goal, start_url, max_actions):
        return await self._call("POST", "/tasks", json={"goal": goal, "start_url": start_url or "",
                                                         "max_actions": max_actions or None})

    async def info(self, task_id):
        if not task_id:
            st = await self._call("GET", "/status")
            if st.get("task"):
                task_id = st["task"]["task_id"]
            else:
                tasks = (await self._call("GET", "/tasks", params={"limit": 1}))["tasks"]
                if not tasks:
                    return {"task": None, "message": "No tasks yet."}
                task_id = tasks[0]["id"]
        return await self._call("GET", f"/tasks/{int(task_id)}")

    async def wait(self, task_id, seconds):
        return await self._call("POST", f"/tasks/{int(task_id)}/wait", params={"seconds": seconds},
                                timeout=float(seconds) + 30)

    async def stop(self):
        return await self._call("POST", "/task/stop")

    async def reply(self, text):
        return await self._call("POST", "/task/reply", json={"text": text, "resume": True})

    async def list(self, limit):
        return await self._call("GET", "/tasks", params={"limit": limit})

    async def screenshot(self):
        return await self._call("GET", "/screenshot", raw=True, timeout=30)


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="nuclear-mcp: %(message)s")
    cfg = Config()
    port = int(os.environ.get("NUCLEAR_AGENT_PORT") or cfg.get("server", "port"))
    backend = HttpBackend(f"http://127.0.0.1:{port}", cfg.data_dir / "local_mcp_key.txt",
                          start_server=os.environ.get("NUCLEAR_MCP_NO_AUTOSTART") != "1")
    build_mcp(backend).run("stdio")


if __name__ == "__main__":
    main()
