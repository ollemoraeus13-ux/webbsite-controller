"""Playwright browser control. All methods must run on the Engine loop."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from . import dom
from .config import Config

log = logging.getLogger(__name__)

ALLOWED_SCHEMES = {"http", "https", "about"}
SCROLL_DIRECTIONS = {"down", "up", "top", "bottom"}
ALLOWED_KEYS = {
    "Enter", "Tab", "Escape", "Backspace", "Delete", "ArrowUp", "ArrowDown", "ArrowLeft",
    "ArrowRight", "PageUp", "PageDown", "Home", "End", "Space",
}


class BrowserError(Exception):
    """An action failed in a way the agent can recover from."""


class BrowserCrashed(BrowserError):
    """The browser or page is gone and must be restarted."""


@dataclass
class Observation:
    url: str = ""
    title: str = ""
    text: str = ""
    elements: list[dict] = field(default_factory=list)
    scroll: dict = field(default_factory=dict)
    captcha: dict = field(default_factory=dict)
    ocr_text: str = ""

    def to_dict(self) -> dict:
        return {
            "url": self.url, "title": self.title, "text": self.text,
            "elements": self.elements, "scroll": self.scroll,
            "captcha": self.captcha, "ocr_text": self.ocr_text,
        }


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise BrowserError("Empty URL")
    if url == "about:blank":
        return url
    # "javascript:..." / "mailto:..." / "data:..." have a scheme but no "://".
    # ("localhost:8080" is a host + port, so a digit after the colon is allowed.)
    m = re.match(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):(?!\d)", url)
    if m and "://" not in url:
        raise BrowserError(f"URL scheme '{m.group(1)}' is not allowed (only http/https)")
    if "://" not in url:
        local = re.match(r"^(localhost|127\.0\.0\.1|\[::1\])(:\d+)?(/|$)", url)
        url = ("http://" if local else "https://") + url
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise BrowserError(f"URL scheme '{parsed.scheme}' is not allowed (only http/https)")
    if not parsed.netloc:
        raise BrowserError(f"Invalid URL: {url}")
    return url


def _is_crash(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(s in msg for s in (
        "target closed", "target page, context or browser has been closed",
        "browser has been closed", "browser closed", "connection closed",
        "page closed", "has been closed", "browser has disconnected",
    ))


class BrowserManager:
    def __init__(self, config: Config):
        self.config = config
        self._pw = None
        self._browser = None
        self._context = None
        self.page = None
        self.last_elements: dict[int, dict] = {}
        self.last_screenshot: bytes | None = None
        self.last_screenshot_at: float = 0.0
        self.restarts = 0
        self.last_known_url = ""  # survives crashes; used for recovery
        self._lock = asyncio.Lock()

    # ----------------------------------------------------------- lifecycle
    @property
    def running(self) -> bool:
        try:
            if self._context is None or self.page is None or self.page.is_closed():
                return False
            if self._browser is not None and not self._browser.is_connected():
                return False
            return True
        except Exception:
            return False

    async def start(self) -> dict:
        async with self._lock:
            if self.running:
                return self.status()
            await self._close_quietly()
            from playwright.async_api import async_playwright

            cfg = self.config.get("browser")
            self._pw = await async_playwright().start()
            # No anti-detection flags on purpose: the agent does not hide that it is automated.
            launch_args: dict[str, Any] = {"headless": bool(cfg["headless"])}
            if cfg.get("executable_path"):
                launch_args["executable_path"] = cfg["executable_path"]
            ctx_args = {
                "viewport": {"width": cfg["viewport_width"], "height": cfg["viewport_height"]},
                "accept_downloads": False,  # downloads need explicit user action
            }
            try:
                if cfg.get("persistent_profile"):
                    self.config.profile_dir.mkdir(parents=True, exist_ok=True)
                    self._context = await self._pw.chromium.launch_persistent_context(
                        str(self.config.profile_dir), **launch_args, **ctx_args)
                    self._browser = None
                else:
                    self._browser = await self._pw.chromium.launch(**launch_args)
                    self._context = await self._browser.new_context(**ctx_args)
            except Exception as exc:
                await self._close_quietly()
                hint = ""
                if "Executable doesn't exist" in str(exc):
                    hint = " Run install.bat (or: python -m playwright install chromium)."
                raise BrowserError(f"Could not start browser: {exc}{hint}") from exc

            self._context.set_default_timeout(cfg["action_timeout_ms"])
            self._context.set_default_navigation_timeout(cfg["navigation_timeout_ms"])
            self._context.on("page", self._on_new_page)
            pages = self._context.pages
            self.page = pages[0] if pages else await self._context.new_page()
            self._watch(self.page)
            log.info("Browser started")
            return self.status()

    def _on_new_page(self, page) -> None:
        # Follow new tabs/popups so the agent keeps seeing what it opened.
        self.page = page
        self._watch(page)
        log.info("Switched to new tab")

    def _watch(self, page) -> None:
        def on_nav(frame):
            try:
                if frame == page.main_frame and frame.url and frame.url != "about:blank":
                    self.last_known_url = frame.url
            except Exception:
                pass
        page.on("framenavigated", on_nav)

    async def stop(self) -> dict:
        async with self._lock:
            await self._close_quietly()
            return self.status()

    async def _close_quietly(self) -> None:
        for obj, meth in ((self._context, "close"), (self._browser, "close"), (self._pw, "stop")):
            if obj is None:
                continue
            try:
                await asyncio.wait_for(getattr(obj, meth)(), 15)
            except Exception:
                pass
        self._pw = self._browser = self._context = self.page = None
        self.last_elements = {}

    async def restart(self, url: str | None = None) -> dict:
        log.warning("Restarting browser (recover to %s)", url)
        await self.stop()
        await self.start()
        self.restarts += 1
        if url and url != "about:blank":
            try:
                await self.navigate(url)
            except BrowserError as exc:
                log.warning("Could not reopen %s after restart: %s", url, exc)
        return self.status()

    async def ensure(self) -> None:
        if not self.running:
            await self.start()

    def status(self) -> dict:
        url = ""
        try:
            if self.running:
                url = self.page.url
        except Exception:
            pass
        return {"running": self.running, "url": url, "restarts": self.restarts,
                "last_known_url": self.last_known_url}

    # ------------------------------------------------------------ helpers
    async def _guard(self, coro):
        """Run a page operation and translate failures into agent-friendly errors."""
        if not self.running:
            if hasattr(coro, "close"):
                coro.close()  # avoid "coroutine was never awaited"
            raise BrowserCrashed("Browser is not running")
        try:
            return await coro
        except BrowserError:
            raise
        except Exception as exc:
            if _is_crash(exc) or not self.running:
                raise BrowserCrashed(str(exc).splitlines()[0]) from exc
            raise BrowserError(str(exc).splitlines()[0][:300]) from exc

    def _locator(self, element_id: int):
        try:
            element_id = int(element_id)
        except (TypeError, ValueError):
            raise BrowserError(f"Invalid element id: {element_id!r}")
        if element_id not in self.last_elements:
            raise BrowserError(f"Element [{element_id}] is not on the current element list; inspect the page again")
        return self.page.locator(f'[data-nuclear-id="{element_id}"]').first

    # ------------------------------------------------------------ actions
    async def navigate(self, url: str) -> str:
        url = normalize_url(url)

        async def _go():
            await self.page.goto(url, wait_until="domcontentloaded")
            await self._settle()
            return self.page.url

        return await self._guard(_go())

    async def _settle(self, timeout_ms: int = 3000) -> None:
        try:
            await self.page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:
            pass  # many sites never go fully idle; that's fine

    async def back(self) -> str:
        async def _b():
            await self.page.go_back(wait_until="domcontentloaded")
            await self._settle()
            return self.page.url
        return await self._guard(_b())

    async def forward(self) -> str:
        async def _f():
            await self.page.go_forward(wait_until="domcontentloaded")
            await self._settle()
            return self.page.url
        return await self._guard(_f())

    async def reload(self) -> str:
        async def _r():
            await self.page.reload(wait_until="domcontentloaded")
            await self._settle()
            return self.page.url
        return await self._guard(_r())

    async def click(self, element_id: int | None = None, x: int | None = None, y: int | None = None) -> str:
        async def _c():
            if element_id is not None:
                loc = self._locator(element_id)
                try:
                    await loc.scroll_into_view_if_needed(timeout=3000)
                    await loc.click()
                except Exception as exc:
                    if _is_crash(exc):
                        raise
                    info = self.last_elements.get(int(element_id), {})
                    if "x" not in info:
                        raise
                    # Only fall back to coordinates if that exact element is really at that point
                    # (e.g. covered by a transparent overlay). Never click blindly on a changed page.
                    hit = await self.page.evaluate(
                        "([x, y]) => { const e = document.elementFromPoint(x, y);"
                        " const t = e && e.closest('[data-nuclear-id]'); return t ? t.getAttribute('data-nuclear-id') : null; }",
                        [info["x"], info["y"]])
                    if hit != str(int(element_id)):
                        raise BrowserError(f"Element [{element_id}] could not be clicked and is no longer at its position; "
                                           "inspect the page again") from exc
                    log.info("Locator click failed (%s); falling back to coordinates", str(exc)[:120])
                    await self.page.mouse.click(info["x"], info["y"])
            elif x is not None and y is not None:
                await self.page.mouse.click(int(x), int(y))
            else:
                raise BrowserError("click needs an element id or x/y coordinates")
            await self._settle(2000)
            return self.page.url
        return await self._guard(_c())

    async def type_text(self, element_id: int, text: str, submit: bool = False, clear: bool = True) -> str:
        text = "" if text is None else str(text)

        async def _t():
            loc = self._locator(element_id)
            info = self.last_elements.get(int(element_id), {})
            await loc.scroll_into_view_if_needed(timeout=3000)
            fillable = info.get("tag") in ("input", "textarea") or info.get("role") == "textbox"
            if fillable:
                if clear:
                    await loc.fill(text)
                else:
                    await loc.click()
                    await self.page.keyboard.type(text, delay=15)
            else:
                await loc.click()
                if clear:
                    await self.page.keyboard.press("Control+A")
                    await self.page.keyboard.press("Backspace")
                await self.page.keyboard.type(text, delay=15)
            if submit:
                await loc.press("Enter")
                await self._settle(3000)
            return self.page.url
        return await self._guard(_t())

    async def select_option(self, element_id: int, value: str) -> str:
        async def _s():
            loc = self._locator(element_id)
            try:
                await loc.select_option(label=value)
            except Exception:
                await loc.select_option(value=value)
            return self.page.url
        return await self._guard(_s())

    async def press(self, key: str) -> str:
        key = (key or "").strip()
        if key not in ALLOWED_KEYS:
            raise BrowserError(f"Key '{key}' not allowed. Allowed: {', '.join(sorted(ALLOWED_KEYS))}")

        async def _p():
            await self.page.keyboard.press(" " if key == "Space" else key)
            await self._settle(1500)
            return self.page.url
        return await self._guard(_p())

    async def scroll(self, direction: str = "down", amount: int = 600) -> dict:
        direction = (direction or "down").lower()
        if direction not in SCROLL_DIRECTIONS:
            raise BrowserError(f"Scroll direction must be one of {sorted(SCROLL_DIRECTIONS)}")
        amount = max(50, min(int(amount or 600), 5000))

        async def _s():
            if direction == "top":
                await self.page.evaluate("window.scrollTo(0, 0)")
            elif direction == "bottom":
                await self.page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
            else:
                await self.page.mouse.wheel(0, amount if direction == "down" else -amount)
            await asyncio.sleep(0.4)
            return await self.page.evaluate(dom.SCROLL_INFO_JS)
        return await self._guard(_s())

    async def wait(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, min(float(seconds or 1), 30.0)))

    # ----------------------------------------------------------- inspect
    async def screenshot(self, full_page: bool = False) -> bytes:
        async def _shot():
            data = await self.page.screenshot(type="jpeg", quality=70, full_page=full_page)
            self.last_screenshot = data
            self.last_screenshot_at = time.time()
            return data
        return await self._guard(_shot())

    async def elements(self, max_items: int = 60) -> list[dict]:
        async def _e():
            items = await self.page.evaluate(dom.COLLECT_ELEMENTS_JS, int(max_items))
            self.last_elements = {int(i["id"]): i for i in items}
            return items
        return await self._guard(_e())

    async def page_text(self, max_chars: int = 3500) -> str:
        return await self._guard(self.page.evaluate(dom.PAGE_TEXT_JS, int(max_chars)))

    async def focused_element(self) -> dict | None:
        try:
            return await self._guard(self.page.evaluate(dom.FOCUSED_JS))
        except BrowserCrashed:
            raise
        except BrowserError:
            return None

    async def captcha_check(self) -> dict:
        try:
            return await self._guard(self.page.evaluate(dom.CAPTCHA_JS))
        except BrowserCrashed:
            raise
        except BrowserError:
            return {"detected": False, "reason": ""}

    async def observe(self, max_elements: int = 60, max_chars: int = 3500) -> Observation:
        # Deliberately no auto-start here: a dead browser must surface as BrowserCrashed
        # so the agent can restart it at the last checkpoint URL.
        if not self.running:
            raise BrowserCrashed("Browser is not running")
        obs = Observation()
        obs.url = self.page.url
        try:
            obs.title = await self._guard(self.page.title())
        except BrowserCrashed:
            raise
        except BrowserError:
            obs.title = ""
        obs.text = await self.page_text(max_chars)
        obs.elements = await self.elements(max_elements)
        try:
            obs.scroll = await self._guard(self.page.evaluate(dom.SCROLL_INFO_JS))
        except BrowserCrashed:
            raise
        except BrowserError:
            obs.scroll = {}
        obs.captcha = await self.captcha_check()
        return obs
