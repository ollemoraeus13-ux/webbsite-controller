"""The autonomous task loop.

  observe -> decide ONE action -> safety check -> execute -> verify -> repeat

Everything runs on the Engine loop. Control methods (pause/resume/stop/confirm)
are also called on that loop, so there are no cross-thread races.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from . import ocr, safety
from .browser import BrowserCrashed, BrowserError, BrowserManager, normalize_url
from .config import Config
from .llm import LLMError, OllamaClient
from .prompts import SYSTEM_PROMPT, build_user_message
from .storage import Storage

log = logging.getLogger(__name__)

ACTION_TYPES = {"navigate", "click", "type", "select", "press", "scroll", "back", "wait", "done", "ask_user"}
IMPORTANT_ACTIONS = {"navigate", "type", "select", "back"}


class TaskStopped(Exception):
    pass


def validate_action(raw: Any) -> dict:
    """Check the model's proposed action. Raises ValueError with a message the model can learn from."""
    if not isinstance(raw, dict):
        raise ValueError("'action' must be a JSON object")
    kind = str(raw.get("type", "")).strip().lower()
    if kind not in ACTION_TYPES:
        raise ValueError(f"Unknown action type '{kind}'. Use one of: {', '.join(sorted(ACTION_TYPES))}")
    a: dict[str, Any] = {"type": kind}
    if kind in ("click", "type", "select"):
        try:
            a["element"] = int(raw.get("element"))
        except (TypeError, ValueError):
            raise ValueError(f"'{kind}' needs a numeric 'element' from the element list")
    if kind == "navigate":
        a["url"] = normalize_url(str(raw.get("url", "")))
    elif kind == "type":
        if "text" not in raw:
            raise ValueError("'type' needs 'text'")
        a["text"] = str(raw.get("text"))[:2000]
        a["submit"] = bool(raw.get("submit", False))
    elif kind == "select":
        a["value"] = str(raw.get("value", ""))[:200]
    elif kind == "press":
        a["key"] = str(raw.get("key", "Enter"))
    elif kind == "scroll":
        a["direction"] = str(raw.get("direction", "down")).lower()
    elif kind == "wait":
        try:
            a["seconds"] = max(0.5, min(float(raw.get("seconds", 2)), 15))
        except (TypeError, ValueError):
            a["seconds"] = 2.0
    elif kind == "done":
        a["answer"] = str(raw.get("answer") or raw.get("summary") or "").strip()[:6000]
        if not a["answer"]:
            raise ValueError("'done' needs a non-empty 'answer'")
    elif kind == "ask_user":
        a["question"] = str(raw.get("question", "")).strip()[:1000] or "The agent needs your help."
    return a


class TaskRunner:
    def __init__(self, manager: "AgentManager", task_id: int, goal: str, max_actions: int,
                 model: str, start_url: str = "", restored: dict | None = None):
        self.m = manager
        self.task_id = task_id
        self.goal = goal
        self.max_actions = max_actions
        self.model = model
        self.start_url = start_url
        self.step = 0
        self.memory: list[str] = []
        self.history: list[dict] = []
        self.user_messages: list[str] = []
        self.last_error = ""
        self.summary = ""
        self.status = "running"
        self.message = ""
        self.pending: dict | None = None
        self.consecutive_errors = 0
        self.restarts = 0
        self.resume_url = ""
        self.last_url = ""
        self._resume = asyncio.Event()
        self._decision: bool | None = None
        self._pause_requested = False
        self.task: asyncio.Task | None = None
        if restored:
            self.step = int(restored.get("step", 0))
            self.memory = list(restored.get("memory", []))
            self.history = list(restored.get("history", []))
            self.user_messages = list(restored.get("user_messages", []))
            self.resume_url = restored.get("url", "") or ""

    # ------------------------------------------------------------ status
    def snapshot(self) -> dict:
        return {
            "task_id": self.task_id, "goal": self.goal, "status": self.status, "step": self.step,
            "max_actions": self.max_actions, "model": self.model, "summary": self.summary,
            "message": self.message, "pending": self.pending, "memory": self.memory[-15:],
            "last_error": self.last_error,
        }

    def _set_status(self, status: str, message: str = "", **extra: Any) -> None:
        self.status = status
        self.message = message
        fields: dict[str, Any] = {"status": status, "steps": self.step, **extra}
        if status in ("needs_user", "failed", "interrupted"):
            fields.setdefault("error", message)
        self.m.storage.update_task(self.task_id, **fields)
        log.info("Task %s -> %s %s", self.task_id, status, message)

    def _checkpoint(self, label: str) -> None:
        url = self.m.browser.status().get("url", "")
        if not url or url == "about:blank":
            url = self.last_url or self.resume_url
        state = {
            "goal": self.goal, "step": self.step, "memory": self.memory[-30:],
            "history": self.history[-20:], "user_messages": self.user_messages[-10:],
            "url": url, "start_url": self.start_url, "max_actions": self.max_actions, "model": self.model,
        }
        self.m.storage.save_checkpoint(self.task_id, self.step, url, label, state)

    # ------------------------------------------------------- user control
    async def _wait_for_user(self, status: str, message: str) -> None:
        self._resume.clear()
        self._set_status(status, message)
        self._checkpoint(f"waiting: {status}")
        await self._resume.wait()
        self._set_status("running")

    def resume(self) -> None:
        if self.step >= self.max_actions:
            extra = int(self.m.config.get("agent", "max_actions"))
            self.max_actions = self.step + extra
            self.m.storage.update_task(self.task_id, max_actions=self.max_actions)
        self._resume.set()

    def request_pause(self) -> None:
        if self.status == "running":
            self._pause_requested = True
            self.message = "Pausing after the current step..."

    def confirm(self, approve: bool) -> None:
        self._decision = bool(approve)
        self._resume.set()

    def add_user_message(self, text: str) -> None:
        text = (text or "").strip()[:1000]
        if text:
            self.user_messages.append(text)

    async def _gate(self) -> bool:
        """Honour pause requests between steps. Returns True if the task was paused."""
        if self._pause_requested:
            self._pause_requested = False
            await self._wait_for_user("paused", "Paused by user. Press Resume to continue.")
            return True
        return False

    # --------------------------------------------------------------- loop
    async def run(self) -> None:
        cfg = self.m.config
        try:
            await self._recoverable(self.m.browser.ensure)
            first_url = self.resume_url or self.start_url
            if first_url:
                try:
                    await self.m.browser.navigate(first_url)
                except BrowserError as exc:
                    self.last_error = f"Could not open {first_url}: {exc}"
            self._checkpoint("start" if not self.resume_url else "resumed")

            while True:
                await self._gate()
                acfg = cfg.get("agent")
                if self.step >= self.max_actions:
                    await self._wait_for_user(
                        "needs_user",
                        f"Reached the action limit ({self.max_actions}) before finishing. "
                        f"Press Resume to allow {acfg['max_actions']} more actions, or Stop.")
                    continue

                obs = await self._recoverable(lambda: self._observe(acfg))
                if obs.get("captcha", {}).get("detected"):
                    await self._wait_for_user(
                        "needs_user",
                        f"This page shows a CAPTCHA / bot check ({obs['captcha'].get('reason')}). "
                        "Nuclear Agent will not solve it. Please complete it yourself in the browser window, then press Resume.")
                    continue

                try:
                    decision = await asyncio.wait_for(self._decide(obs, acfg), acfg["step_timeout_s"])
                except asyncio.TimeoutError:
                    decision = {"error": f"The model did not answer within {acfg['step_timeout_s']}s."}
                if "error" in decision:
                    await self._count_error(decision["error"], acfg, llm=True)
                    continue

                action = decision["action"]
                self.summary = decision.get("summary", "")[:400]
                note = (decision.get("note") or "").strip()
                if note and note not in self.memory:
                    self.memory.append(note[:500])

                if action["type"] == "done":
                    self.step += 1
                    self._record(action, decision, "Task finished", True, obs["url"])
                    self._checkpoint("done")
                    self._set_status("completed", "Task completed.", result=action["answer"])
                    return

                if action["type"] == "ask_user":
                    self.step += 1
                    self._record(action, decision, "Asked the user", True, obs["url"])
                    await self._wait_for_user("needs_user", "The agent asks: " + action["question"] +
                                              "  (Reply in the message box, then press Resume.)")
                    continue

                # ----- safety gate
                focused = None
                if action["type"] == "press":
                    focused = await self._recoverable(self.m.browser.focused_element)
                verdict = safety.assess(action, self.m.browser.last_elements, focused)
                if verdict.level == "user":
                    self._record(action, decision, "Blocked: " + verdict.reason, False, obs["url"])
                    self.last_error = "That action is for the user only. " + verdict.reason
                    await self._wait_for_user("needs_user", verdict.reason)
                    continue
                if verdict.level == "confirm" and acfg["confirm_sensitive_actions"]:
                    approved = await self._ask_confirmation(action, verdict.reason)
                    if not approved:
                        self.step += 1
                        self._record(action, decision, "User declined this action", False, obs["url"])
                        self.last_error = "The user declined that action. Do not repeat it; find another way or use ask_user/done."
                        continue
                    # The page may have changed while waiting: the approved element must still be the same one.
                    if "element" in action and not await self._same_element(action["element"]):
                        self.last_error = "The page changed while waiting for approval, so the action was not run. Choose again."
                        continue

                if await self._gate():
                    # The user may have changed the page while paused: never run a stale action.
                    self.last_error = ""
                    continue
                await self._execute(action, decision, obs, acfg)
        except TaskStopped:
            self._set_status("stopped", "Stopped.")
        except asyncio.CancelledError:
            try:
                self._checkpoint("stopped")
                self._set_status("stopped", "Stopped by user.")
            finally:
                raise
        except Exception as exc:  # explain failures clearly instead of crashing
            log.exception("Task %s failed", self.task_id)
            try:
                self._checkpoint("failed")
            except Exception:
                pass
            self._set_status("failed", f"{exc.__class__.__name__}: {exc}")
        finally:
            self.pending = None

    async def _ask_confirmation(self, action: dict, reason: str) -> bool:
        self.pending = {"action": action, "reason": reason, "element": self.m.browser.last_elements.get(action.get("element", -1))}
        self._decision = None
        self.m.storage.update_task(self.task_id, pending=self.pending)
        await self._wait_for_user("awaiting_confirmation", "Approval needed: " + reason)
        decision = bool(self._decision)
        self.pending = None
        self.m.storage.update_task(self.task_id, pending="")
        return decision

    async def _same_element(self, element_id: int) -> bool:
        before = dict(self.m.browser.last_elements.get(element_id) or {})
        try:
            await self._recoverable(lambda: self.m.browser.elements(self.m.config.get("agent", "max_elements_in_prompt") + 20))
        except BrowserError:
            return False
        after = self.m.browser.last_elements.get(element_id) or {}
        keys = ("tag", "type", "text", "label", "name", "href")
        return bool(before) and all(before.get(k) == after.get(k) for k in keys)

    async def _count_error(self, message: str, acfg: dict, llm: bool = False) -> None:
        self.consecutive_errors += 1
        self.last_error = message
        self.m.storage.add_action(self.task_id, self.step, "error", {}, "", message, False,
                                  self.m.browser.status().get("url", ""))
        if self.consecutive_errors >= acfg["max_consecutive_errors"]:
            hint = " Check that Ollama is running and the model is installed." if llm else ""
            await self._wait_for_user(
                "needs_user", f"Stopped after {self.consecutive_errors} errors in a row. Last error: {message}.{hint} "
                              "Press Resume to try again.")
            self.consecutive_errors = 0
        elif llm:
            await asyncio.sleep(2)

    async def _recoverable(self, fn):
        """Run a browser call; if the browser crashed, restart it at the last known URL."""
        while True:
            try:
                return await fn()
            except BrowserCrashed as exc:
                limit = self.m.config.get("agent", "max_browser_restarts")
                if self.restarts >= limit:
                    raise RuntimeError(f"Browser crashed and could not be recovered after {self.restarts} restart(s): {exc}")
                self.restarts += 1
                cp = self.m.storage.latest_checkpoint(self.task_id)
                url = (self.m.browser.last_known_url or self.last_url or (cp or {}).get("url")
                       or self.resume_url or self.start_url)
                self.m.storage.add_action(self.task_id, self.step, "recover", {"url": url}, "",
                                          f"Browser crashed ({exc}); restarting", True, url or "")
                await self.m.browser.restart(url)

    async def _observe(self, acfg: dict) -> dict:
        b = self.m.browser
        o = await b.observe(acfg["max_elements_in_prompt"] + 20, acfg["max_page_chars"])
        try:
            shot = await b.screenshot()
        except BrowserError:
            shot = None
        obs = o.to_dict()
        obs["_shot"] = shot
        if obs["url"] and obs["url"] != "about:blank":
            self.last_url = obs["url"]
        if acfg["use_ocr_fallback"] and shot and len(obs["text"]) < 80 and ocr.available():
            obs["ocr_text"] = await asyncio.to_thread(ocr.image_to_text, shot, 1500)
        return obs

    async def _decide(self, obs: dict, acfg: dict) -> dict:
        ocfg = self.m.config.get("ollama")
        llm = self.m.llm
        images = None
        vision_note = "You can NOT see the screenshot. Rely on the element list and page text."
        shot = obs.get("_shot")
        try:
            if shot and ocfg["use_vision"] and await llm.supports_vision(self.model):
                images = [shot]
                vision_note = "A screenshot of the current viewport is attached."
            elif shot and ocfg["vision_model"] and ocfg["use_vision"]:
                if await llm.supports_vision(ocfg["vision_model"]):
                    desc = await llm.describe_image(
                        ocfg["vision_model"], shot,
                        "Describe this web page screenshot briefly: main content, dialogs, buttons, errors. Max 6 sentences.",
                        keep_alive=ocfg["keep_alive"])
                    vision_note = f"SCREENSHOT DESCRIPTION (from vision model {ocfg['vision_model']}, may be imperfect):\n{desc}"
        except LLMError as exc:
            vision_note += f" (Vision unavailable: {exc})"

        user_msg = build_user_message(
            goal=self.goal, step=self.step, max_actions=self.max_actions, obs=obs, memory=self.memory,
            history=self.history, last_error=self.last_error,
            limits={"history": acfg["history_in_prompt"], "elements": acfg["max_elements_in_prompt"]},
            vision_note=vision_note, user_messages=self.user_messages)
        try:
            reply = await llm.chat_json(
                self.model,
                [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_msg}],
                images=images, num_ctx=ocfg["num_ctx"], temperature=ocfg["temperature"],
                keep_alive=ocfg["keep_alive"])
        except LLMError as exc:
            return {"error": str(exc)}
        try:
            action = validate_action(reply.get("action"))
        except (ValueError, BrowserError) as exc:
            return {"error": f"Invalid action from model: {exc}"}
        return {"action": action, "summary": str(reply.get("summary", ""))[:400],
                "note": str(reply.get("note", "") or "")[:500]}

    async def _execute(self, action: dict, decision: dict, obs: dict, acfg: dict) -> None:
        b = self.m.browser
        kind = action["type"]
        before_url = obs.get("url", "")
        try:
            if kind == "navigate":
                outcome = f"Opened {await b.navigate(action['url'])}"
            elif kind == "click":
                url = await b.click(action["element"])
                outcome = "Clicked" + (f"; page changed to {url}" if url != before_url else "")
            elif kind == "type":
                url = await b.type_text(action["element"], action["text"], submit=action["submit"])
                outcome = "Typed text" + (" and submitted" if action["submit"] else "") + \
                          (f"; page changed to {url}" if url != before_url else "")
            elif kind == "select":
                await b.select_option(action["element"], action["value"])
                outcome = f"Selected {action['value']}"
            elif kind == "press":
                url = await b.press(action["key"])
                outcome = f"Pressed {action['key']}" + (f"; page changed to {url}" if url != before_url else "")
            elif kind == "scroll":
                info = await b.scroll(action["direction"])
                outcome = f"Scrolled {action['direction']} (at {info.get('y')}/{info.get('height')} px)"
            elif kind == "back":
                outcome = f"Went back to {await b.back()}"
            elif kind == "wait":
                await b.wait(action["seconds"])
                outcome = f"Waited {action['seconds']}s"
            else:  # pragma: no cover - validated earlier
                raise BrowserError(f"Unsupported action {kind}")
            ok = True
            self.consecutive_errors = 0
            self.last_error = ""
        except BrowserCrashed:
            raise
        except BrowserError as exc:
            ok = False
            outcome = f"Error: {exc}"
        self.step += 1
        after_url = b.status().get("url", "")
        if after_url and after_url != "about:blank":
            self.last_url = after_url
        logged = dict(action)
        if kind == "type" and safety.is_personal_field(b.last_elements.get(action["element"], {})):
            logged["text"] = "***"  # don't keep personal data in history
        self._record(logged, decision, outcome, ok, b.status().get("url", ""))
        if not ok:
            await self._count_error(outcome, acfg)
        if kind in IMPORTANT_ACTIONS or after_url != before_url or self.step % acfg["checkpoint_every"] == 0:
            self._checkpoint(f"after {kind}")

    def _record(self, action: dict, decision: dict, outcome: str, ok: bool, url: str) -> None:
        self.history.append({"step": self.step, "action": action, "outcome": outcome, "ok": ok})
        self.history = self.history[-40:]
        self.m.storage.add_action(self.task_id, self.step, action["type"], action,
                                  decision.get("summary", ""), outcome, ok, url or "")
        self.m.storage.update_task(self.task_id, steps=self.step)


class AgentManager:
    """Owns the single active task. All methods run on the Engine loop."""

    def __init__(self, config: Config, storage: Storage, browser: BrowserManager, llm: OllamaClient | Any):
        self.config = config
        self.storage = storage
        self.browser = browser
        self.llm = llm
        self.runner: TaskRunner | None = None

    @property
    def busy(self) -> bool:
        return bool(self.runner and self.runner.task and not self.runner.task.done())

    async def pick_model(self, requested: str = "") -> str:
        model = requested or self.config.get("ollama", "model")
        try:
            installed = [m["name"] for m in await self.llm.list_models()]
        except LLMError as exc:
            raise RuntimeError(str(exc))
        if not installed:
            raise RuntimeError("No Ollama models installed. Run: ollama pull qwen2.5:7b")
        if model and model not in installed:
            # allow "qwen2.5" to match "qwen2.5:latest"
            match = [n for n in installed if n.split(":")[0] == model or n == f"{model}:latest"]
            if not match:
                raise RuntimeError(f"Model '{model}' is not installed. Installed: {', '.join(installed)}")
            model = match[0]
        return model or installed[0]

    async def start_task(self, goal: str, start_url: str = "", max_actions: int | None = None, model: str = "") -> dict:
        goal = (goal or "").strip()
        if not goal:
            raise ValueError("Task description is empty")
        if self.busy:
            raise RuntimeError("A task is already active. Stop it first.")
        if start_url:
            start_url = normalize_url(start_url)
        model = await self.pick_model(model)
        max_actions = int(max_actions or self.config.get("agent", "max_actions"))
        tid = self.storage.create_task(goal[:4000], max_actions, model, start_url)
        self.runner = TaskRunner(self, tid, goal, max_actions, model, start_url)
        self.runner.task = asyncio.get_running_loop().create_task(self.runner.run())
        return self.runner.snapshot()

    async def resume_task(self, task_id: int) -> dict:
        """Continue an interrupted/stopped/failed task from its last checkpoint."""
        if self.busy:
            raise RuntimeError("A task is already active. Stop it first.")
        t = self.storage.get_task(task_id)
        if not t:
            raise ValueError(f"Task {task_id} not found")
        if t["status"] == "completed":
            raise ValueError("Task already completed")
        cp = self.storage.latest_checkpoint(task_id) or {"state": {}}
        state = cp["state"] or {}
        model = await self.pick_model(t.get("model") or "")
        max_actions = max(int(t["max_actions"]), int(state.get("step", 0)) + int(self.config.get("agent", "max_actions")))
        self.storage.update_task(task_id, status="running", error="", max_actions=max_actions)
        self.runner = TaskRunner(self, task_id, t["goal"], max_actions, model, t.get("start_url") or "", restored=state)
        self.storage.add_action(task_id, self.runner.step, "restore", {"checkpoint": cp.get("id")}, "",
                                f"Restored from checkpoint at step {self.runner.step}", True, state.get("url", ""))
        self.runner.task = asyncio.get_running_loop().create_task(self.runner.run())
        return self.runner.snapshot()

    async def pause(self) -> dict:
        r = self._require()
        r.request_pause()
        return r.snapshot()

    async def resume(self) -> dict:
        r = self._require()
        if r.status == "awaiting_confirmation":
            raise RuntimeError("Approve or deny the pending action instead.")
        r._pause_requested = False
        r.resume()
        return r.snapshot()

    async def confirm(self, approve: bool) -> dict:
        r = self._require()
        if r.status != "awaiting_confirmation":
            raise RuntimeError("Nothing is waiting for approval.")
        r.confirm(approve)
        return r.snapshot()

    async def message(self, text: str) -> dict:
        r = self._require()
        r.add_user_message(text)
        self.storage.add_action(r.task_id, r.step, "user_message", {"text": text[:300]}, "", "Message from user", True, "")
        return r.snapshot()

    async def stop(self) -> dict:
        r = self.runner
        if not r or not r.task or r.task.done():
            return {"status": "idle"}
        r.task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(r.task), 10)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            pass
        return r.snapshot()

    def _require(self) -> TaskRunner:
        if not self.busy or not self.runner:
            raise RuntimeError("No active task.")
        return self.runner

    async def status(self) -> dict:
        snap = self.runner.snapshot() if self.runner else None
        return {"busy": self.busy, "task": snap, "browser": self.browser.status(), "time": time.time()}
