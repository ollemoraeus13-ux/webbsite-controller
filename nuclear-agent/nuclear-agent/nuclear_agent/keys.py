"""API keys for AI clients (MCP) and the remote/GitHub Pages panel.

* Keys look like  na_<43 random chars>  and are shown ONCE when created.
* Only a SHA-256 hash is stored (data/api_keys.json), never the key itself.
* Each key has scopes:
    read     - see status, tasks, history, screenshots
    submit   - start tasks
    control  - pause / resume / stop / answer the agent's questions
    approve  - approve or deny sensitive actions (purchases, messages ...)
  The "ai" preset deliberately has NO approve scope: an AI must never be able
  to approve its own purchase. Approvals come from a human (panel key or the
  local panel).
* A special local key (data/local_mcp_key.txt) is created for the MCP server
  that Claude Desktop starts on this PC. It has the "ai" scopes and only works
  from localhost.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from collections import deque
from pathlib import Path

SCOPES = ("read", "submit", "control", "approve")
PRESETS = {
    "ai": ["read", "submit", "control"],
    "panel": ["read", "submit", "control", "approve"],
    "readonly": ["read"],
}
LOCAL_KEY_NAME = "local-mcp (this PC)"


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class KeyStore:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "api_keys.json"
        self.local_key_path = Path(data_dir) / "local_mcp_key.txt"
        self._lock = threading.Lock()
        self._keys: list[dict] = []
        self._hits: dict[str, deque] = {}
        self._starts: dict[str, deque] = {}
        self._load()

    # ------------------------------------------------------------------ io
    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._keys = [k for k in data.get("keys", []) if isinstance(k, dict) and k.get("hash")]
        except (OSError, ValueError):
            self._keys = []

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"keys": self._keys}, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    # ------------------------------------------------------------ manage
    def create(self, name: str, preset: str = "ai", local_only: bool = False) -> tuple[str, dict]:
        if preset not in PRESETS:
            raise ValueError(f"Unknown key type '{preset}'. Use one of: {', '.join(PRESETS)}")
        name = (name or "").strip()[:60] or preset
        key = "na_" + secrets.token_urlsafe(32)
        rec = {
            "id": secrets.token_hex(4), "name": name, "hash": _hash(key), "prefix": key[:7],
            "scopes": list(PRESETS[preset]), "preset": preset, "local_only": bool(local_only),
            "created": time.time(), "last_used": None, "revoked": False,
        }
        with self._lock:
            self._keys.append(rec)
            self._save()
        return key, self.public(rec)

    def revoke(self, key_id: str) -> bool:
        with self._lock:
            for k in self._keys:
                if k["id"] == key_id and not k["revoked"]:
                    k["revoked"] = True
                    self._save()
                    return True
        return False

    def list(self) -> list[dict]:
        with self._lock:
            return [self.public(k) for k in self._keys if not k["revoked"]]

    @staticmethod
    def public(rec: dict) -> dict:
        return {k: rec.get(k) for k in ("id", "name", "prefix", "scopes", "preset", "local_only", "created", "last_used")}

    def ensure_local_key(self) -> str:
        """Key file used by the stdio MCP server on this PC. Re-created if lost."""
        try:
            key = self.local_key_path.read_text(encoding="utf-8").strip()
            if key and self.verify(key, touch=False):
                return key
        except OSError:
            pass
        with self._lock:
            for k in self._keys:
                if k.get("name") == LOCAL_KEY_NAME:
                    k["revoked"] = True
        key, _ = self.create(LOCAL_KEY_NAME, "ai", local_only=True)
        self.local_key_path.write_text(key, encoding="utf-8")
        try:
            os.chmod(self.local_key_path, 0o600)
        except OSError:
            pass
        return key

    # ------------------------------------------------------------ verify
    def verify(self, key: str, touch: bool = True) -> dict | None:
        if not key or not key.startswith("na_"):
            return None
        h = _hash(key)
        with self._lock:
            for k in self._keys:
                if not k["revoked"] and secrets.compare_digest(k["hash"], h):
                    if touch and (not k["last_used"] or time.time() - k["last_used"] > 60):
                        k["last_used"] = time.time()
                        try:
                            self._save()
                        except OSError:
                            pass
                    return dict(k)
        return None

    def allow_request(self, key_id: str, per_minute: int) -> bool:
        return self._window(self._hits, key_id, 60, per_minute)

    def allow_task_start(self, key_id: str, per_hour: int) -> bool:
        return self._window(self._starts, key_id, 3600, per_hour)

    def _window(self, store: dict, key_id: str, seconds: int, limit: int) -> bool:
        now = time.time()
        with self._lock:
            q = store.setdefault(key_id, deque())
            while q and now - q[0] > seconds:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True
