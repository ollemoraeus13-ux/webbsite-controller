"""A dedicated background thread with its own asyncio event loop.

Playwright and the agent loop live here, separate from the web server.
Why: on Windows Playwright needs the Proactor event loop to launch the browser
process, and web servers sometimes run a different loop type. Keeping the
browser in its own thread also means a slow page or a slow model never freezes
the control panel, and a panel refresh never touches the running task.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import sys
import threading
from typing import Any, Awaitable

log = logging.getLogger(__name__)


class Engine:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="nuclear-engine", daemon=True)
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("Engine thread did not start")

    def _run(self) -> None:
        if sys.platform == "win32":
            loop: asyncio.AbstractEventLoop = asyncio.ProactorEventLoop()  # type: ignore[attr-defined]
        else:
            loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.set_exception_handler(self._on_loop_error)
        self.loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:  # pragma: no cover - shutdown best effort
                pass
            loop.close()

    @staticmethod
    def _on_loop_error(loop: asyncio.AbstractEventLoop, context: dict) -> None:
        exc = context.get("exception")
        # Never let a stray callback error kill the engine.
        log.error("Engine loop error: %s", context.get("message"), exc_info=exc)

    def submit(self, coro: Awaitable[Any]) -> concurrent.futures.Future:
        if not self.loop:
            raise RuntimeError("Engine not started")
        return asyncio.run_coroutine_threadsafe(coro, self.loop)  # type: ignore[arg-type]

    def run(self, coro: Awaitable[Any], timeout: float | None = 120) -> Any:
        """Blocking helper (for tests and scripts)."""
        return self.submit(coro).result(timeout)

    async def call(self, coro: Awaitable[Any], timeout: float | None = 120) -> Any:
        """Await an engine coroutine from another event loop (the web server)."""
        fut = self.submit(coro)
        return await asyncio.wait_for(asyncio.wrap_future(fut), timeout)

    def stop(self) -> None:
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self._thread:
            self._thread.join(10)
