"""One place for everything remote clients (MCP tools, remote API, GitHub Pages
panel) can do. Every method checks the caller's scopes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

log = logging.getLogger(__name__)

# Statuses where waiting is pointless: the task is finished or needs a human.
SETTLED = {"completed", "failed", "stopped", "interrupted", "needs_user", "awaiting_confirmation", "paused"}


class ScopeError(PermissionError):
    pass


class RemoteService:
    def __init__(self, state: Any):
        self.s = state  # server.AppState
        self.audit_path = state.config.data_dir / "logs" / "remote_audit.jsonl"
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ helpers
    @staticmethod
    def require(caller: dict, scope: str) -> None:
        if scope not in (caller or {}).get("scopes", []):
            raise ScopeError(f"This key is not allowed to '{scope}'. "
                             + ("Approvals must be done by a person in the Nuclear Agent panel."
                                if scope == "approve" else ""))

    def audit(self, caller: dict, action: str, detail: Any = "") -> None:
        try:
            line = json.dumps({"t": time.time(), "key": caller.get("name"), "key_id": caller.get("id"),
                               "action": action, "detail": str(detail)[:300]})
            with self.audit_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    async def _engine(self, coro, timeout: float = 60):
        return await self.s.engine.call(coro, timeout)

    # ------------------------------------------------------------- reads
    async def task_info(self, caller: dict, task_id: int | None = None, actions: int = 5) -> dict:
        self.require(caller, "read")
        status = await self._engine(self.s.agent.status(), 10)
        live = status.get("task")
        if not task_id:
            if live:
                task_id = live["task_id"]
            else:
                latest = self.s.storage.list_tasks(1)
                if not latest:
                    return {"task": None, "message": "No tasks yet."}
                task_id = latest[0]["id"]
        t = self.s.storage.get_task(int(task_id))
        if not t:
            raise ValueError(f"Task {task_id} not found")
        info = {
            "task_id": t["id"], "goal": t["goal"], "status": t["status"], "step": t["steps"],
            "max_actions": t["max_actions"], "source": t.get("source", ""), "result": t.get("result") or "",
            "error": t.get("error") or "", "created_at": t["created_at"],
        }
        if live and live["task_id"] == t["id"] and status.get("busy"):
            info.update(status=live["status"], step=live["step"], max_actions=live["max_actions"],
                        summary=live.get("summary", ""), message=live.get("message", ""),
                        notes=live.get("memory", []), last_error=live.get("last_error", ""))
            if live.get("pending"):
                info["needs_approval"] = live["pending"].get("reason", "")
        info["recent_actions"] = [
            {"step": a["step"], "kind": a["kind"], "params": a["params"], "outcome": a["outcome"][:300],
             "ok": a["ok"], "reason": a["reasoning"][:200]}
            for a in self.s.storage.list_actions(int(t["id"]), max(1, min(actions, 50)))
        ]
        info["hint"] = _hint(info)
        return info

    async def wait(self, caller: dict, task_id: int, seconds: float) -> dict:
        self.require(caller, "read")
        end = time.time() + max(1.0, min(float(seconds or 50), 600))
        info = await self.task_info(caller, task_id)
        while info.get("status") not in SETTLED and time.time() < end:
            await asyncio.sleep(0.7)
            info = await self.task_info(caller, task_id)
        return info

    async def list_tasks(self, caller: dict, limit: int = 10, offset: int = 0) -> dict:
        self.require(caller, "read")
        return {"tasks": self.s.storage.list_tasks(max(1, min(int(limit), 50)), offset),
                "total": self.s.storage.count_tasks()}

    async def actions(self, caller: dict, task_id: int, limit: int = 20, before: int | None = None) -> dict:
        self.require(caller, "read")
        return {"actions": self.s.storage.list_actions(task_id, limit, before)}

    async def screenshot(self, caller: dict) -> bytes | None:
        self.require(caller, "read")
        b = self.s.browser

        async def shot():
            if not b.running:
                return b.last_screenshot
            if b.last_screenshot and time.time() - b.last_screenshot_at < 0.7:
                return b.last_screenshot
            try:
                return await b.screenshot()
            except Exception:
                return b.last_screenshot
        return await self._engine(shot(), 20)

    async def status(self, caller: dict) -> dict:
        self.require(caller, "read")
        st = await self._engine(self.s.agent.status(), 10)
        return {"busy": st["busy"], "task": st["task"], "browser": st["browser"],
                "caller": {"name": caller.get("name"), "scopes": caller.get("scopes")}}

    # ------------------------------------------------------------ writes
    async def start(self, caller: dict, goal: str, start_url: str = "", max_actions: int | None = None) -> dict:
        self.require(caller, "submit")
        per_hour = self.s.config.get("remote", "max_task_starts_per_hour")
        if caller.get("id") and not self.s.keys.allow_task_start(caller["id"], per_hour):
            raise ValueError(f"Too many tasks started with this key (limit {per_hour} per hour).")
        snap = await self._engine(self.s.agent.start_task(
            goal, start_url, max_actions or None, "", source=f"remote:{caller.get('name', '?')}"[:80]), 60)
        self.audit(caller, "start_task", goal)
        return snap

    async def stop(self, caller: dict) -> dict:
        self.require(caller, "control")
        self.audit(caller, "stop")
        return await self._engine(self.s.agent.stop(), 30)

    async def pause(self, caller: dict) -> dict:
        self.require(caller, "control")
        self.audit(caller, "pause")
        return await self._engine(self.s.agent.pause())

    async def resume(self, caller: dict) -> dict:
        self.require(caller, "control")
        self.audit(caller, "resume")
        return await self._engine(self.s.agent.resume())

    async def reply(self, caller: dict, text: str, resume: bool = True) -> dict:
        self.require(caller, "control")
        self.audit(caller, "reply", text)
        snap = await self._engine(self.s.agent.message(text[:1000]))
        if resume and snap.get("status") in ("needs_user", "paused"):
            snap = await self._engine(self.s.agent.resume())
        return snap

    async def confirm(self, caller: dict, approve: bool) -> dict:
        self.require(caller, "approve")
        self.audit(caller, "approve" if approve else "deny")
        return await self._engine(self.s.agent.confirm(bool(approve)))


def _hint(info: dict) -> str:
    st = info.get("status")
    if st == "completed":
        return "Finished. The answer is in 'result'."
    if st == "awaiting_confirmation":
        return ("The agent wants to do something sensitive and is waiting for the USER to approve or deny it "
                "in the Nuclear Agent panel. Tell the user; you cannot approve it yourself.")
    if st == "needs_user":
        return ("The agent needs help: see 'message'. If it asked a question you can answer, use reply_to_agent. "
                "Logins, passwords and CAPTCHAs must be done by the user in the browser window.")
    if st in ("running",):
        return "Still working. Call wait_for_task to wait for more progress."
    if st == "paused":
        return "Paused by the user."
    if st in ("failed", "stopped", "interrupted"):
        return "Not finished; see 'error'. The user can resume it from the panel's Tasks tab."
    return ""
