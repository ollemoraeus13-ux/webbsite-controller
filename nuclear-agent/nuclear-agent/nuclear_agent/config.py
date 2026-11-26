"""Configuration loading.

Settings come from (lowest to highest priority):
  1. DEFAULTS below
  2. config.json in the project root (copy config.example.json to create it)
  3. Changes made in the control panel (saved back to config.json)

config.json is git-ignored so personal settings never end up on GitHub.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict[str, Any] = {
    "server": {
        # Keep this on 127.0.0.1. See docs/REMOTE_ACCESS.md before changing.
        "host": "127.0.0.1",
        "port": 8765,
    },
    "browser": {
        "headless": False,
        "viewport_width": 1280,
        "viewport_height": 800,
        # Keep logins between runs (stored in data/profile, never committed).
        "persistent_profile": True,
        # Optional: path to an existing Chrome/Edge executable. Empty = Playwright's Chromium.
        "executable_path": "",
        "navigation_timeout_ms": 30000,
        "action_timeout_ms": 8000,
    },
    "ollama": {
        "url": "http://127.0.0.1:11434",
        "model": "",           # empty = pick first installed model
        "vision_model": "",    # optional second model that describes screenshots
        "use_vision": False,   # only works if the chosen model really supports images
        "num_ctx": 8192,       # context window; smaller = less VRAM
        "temperature": 0.2,
        "keep_alive": "5m",    # how long Ollama keeps the model in VRAM after a call
        "request_timeout_s": 180,
    },
    "agent": {
        "max_actions": 30,
        "step_timeout_s": 240,
        "max_consecutive_errors": 3,
        "max_browser_restarts": 2,
        "history_in_prompt": 8,
        "max_elements_in_prompt": 60,
        "max_page_chars": 3500,
        "checkpoint_every": 3,
        "use_ocr_fallback": True,
        "confirm_sensitive_actions": True,
    },
    "ui": {
        "history_page_size": 20,
        "screenshot_refresh_ms": 1500,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    """Thread-safe settings holder backed by a JSON file."""

    def __init__(self, path: Path | None = None, data_dir: Path | None = None):
        self.path = Path(path) if path else PROJECT_ROOT / "config.json"
        env_data = os.environ.get("NUCLEAR_AGENT_DATA")
        self.data_dir = Path(data_dir or env_data or PROJECT_ROOT / "data")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._data = copy.deepcopy(DEFAULTS)
        self.load()

    # ------------------------------------------------------------------ io
    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("config root must be an object")
            with self._lock:
                self._data = _deep_merge(DEFAULTS, raw)
        except Exception as exc:  # broken config must not crash startup
            log.error("Could not read %s (%s). Using defaults.", self.path, exc)

    def save(self) -> None:
        with self._lock:
            text = json.dumps(self._data, indent=2)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.path)

    # -------------------------------------------------------------- access
    def get(self, section: str, key: str | None = None, default: Any = None) -> Any:
        with self._lock:
            sec = self._data.get(section, {})
            if key is None:
                return copy.deepcopy(sec)
            return copy.deepcopy(sec.get(key, default))

    def as_dict(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)

    def update(self, changes: dict, persist: bool = True) -> dict:
        """Apply validated changes. Unknown sections/keys are ignored.

        server.host is deliberately not editable from the panel.
        """
        clean: dict[str, dict] = {}
        for section, values in (changes or {}).items():
            if section not in DEFAULTS or not isinstance(values, dict):
                continue
            for key, value in values.items():
                if key not in DEFAULTS[section]:
                    continue
                if section == "server":
                    continue
                default = DEFAULTS[section][key]
                try:
                    if isinstance(default, bool):
                        value = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
                    elif isinstance(default, int):
                        value = int(value)
                    elif isinstance(default, float):
                        value = float(value)
                    else:
                        value = str(value)
                except (TypeError, ValueError):
                    continue
                clean.setdefault(section, {})[key] = value
        _clamp(clean)
        with self._lock:
            self._data = _deep_merge(self._data, clean)
        if persist:
            try:
                self.save()
            except OSError as exc:
                log.error("Could not save config: %s", exc)
        return self.as_dict()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "agent.db"

    @property
    def profile_dir(self) -> Path:
        return self.data_dir / "profile"

    @property
    def screenshot_dir(self) -> Path:
        p = self.data_dir / "screenshots"
        p.mkdir(parents=True, exist_ok=True)
        return p


def _clamp(clean: dict) -> None:
    """Keep numeric settings inside sane ranges."""
    limits = {
        ("agent", "max_actions"): (1, 500),
        ("agent", "step_timeout_s"): (10, 1800),
        ("agent", "max_consecutive_errors"): (1, 20),
        ("agent", "max_browser_restarts"): (0, 10),
        ("agent", "history_in_prompt"): (1, 30),
        ("agent", "max_elements_in_prompt"): (5, 200),
        ("agent", "max_page_chars"): (200, 20000),
        ("agent", "checkpoint_every"): (1, 50),
        ("ollama", "num_ctx"): (1024, 131072),
        ("ollama", "temperature"): (0.0, 2.0),
        ("ollama", "request_timeout_s"): (10, 1800),
        ("browser", "navigation_timeout_ms"): (1000, 180000),
        ("browser", "action_timeout_ms"): (500, 60000),
        ("browser", "viewport_width"): (400, 3840),
        ("browser", "viewport_height"): (300, 2160),
        ("ui", "history_page_size"): (5, 200),
        ("ui", "screenshot_refresh_ms"): (300, 60000),
    }
    for (section, key), (lo, hi) in limits.items():
        if key in clean.get(section, {}):
            clean[section][key] = max(lo, min(hi, clean[section][key]))
