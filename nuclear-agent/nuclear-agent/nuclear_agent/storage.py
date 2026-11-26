"""SQLite persistence for tasks, action history and checkpoints.

Everything is written immediately so a crash, a reboot or a closed panel
never loses task state. Reads are paginated (default 20 rows) to keep the
panel fast.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal TEXT NOT NULL,
    status TEXT NOT NULL,
    model TEXT DEFAULT '',
    start_url TEXT DEFAULT '',
    max_actions INTEGER NOT NULL,
    steps INTEGER NOT NULL DEFAULT 0,
    result TEXT DEFAULT '',
    error TEXT DEFAULT '',
    pending TEXT DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    step INTEGER NOT NULL,
    kind TEXT NOT NULL,
    params TEXT DEFAULT '{}',
    reasoning TEXT DEFAULT '',
    outcome TEXT DEFAULT '',
    ok INTEGER NOT NULL DEFAULT 1,
    url TEXT DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_actions_task ON actions(task_id, id);
CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    step INTEGER NOT NULL,
    url TEXT DEFAULT '',
    label TEXT DEFAULT '',
    state TEXT DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cp_task ON checkpoints(task_id, id);
"""

ACTIVE_STATUSES = ("running", "paused", "awaiting_confirmation", "needs_user")
FINAL_STATUSES = ("completed", "failed", "stopped")


class Storage:
    def __init__(self, path: Path | str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=15)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return cur

    def _all(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def _one(self, sql: str, args: tuple = ()) -> dict | None:
        with self._lock:
            row = self._conn.execute(sql, args).fetchone()
            return dict(row) if row else None

    # --------------------------------------------------------------- tasks
    def create_task(self, goal: str, max_actions: int, model: str = "", start_url: str = "") -> int:
        now = time.time()
        cur = self._exec(
            "INSERT INTO tasks (goal, status, model, start_url, max_actions, created_at, updated_at) "
            "VALUES (?, 'running', ?, ?, ?, ?, ?)",
            (goal, model, start_url, int(max_actions), now, now),
        )
        return int(cur.lastrowid)

    def update_task(self, task_id: int, **fields: Any) -> None:
        allowed = {"status", "model", "steps", "result", "error", "pending", "max_actions"}
        sets, args = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "pending" and not isinstance(v, str):
                v = json.dumps(v) if v else ""
            sets.append(f"{k} = ?")
            args.append(v)
        if not sets:
            return
        sets.append("updated_at = ?")
        args.append(time.time())
        args.append(task_id)
        self._exec(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", tuple(args))

    def get_task(self, task_id: int) -> dict | None:
        t = self._one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if t and t.get("pending"):
            try:
                t["pending"] = json.loads(t["pending"])
            except ValueError:
                t["pending"] = None
        elif t:
            t["pending"] = None
        return t

    def list_tasks(self, limit: int = 20, offset: int = 0) -> list[dict]:
        limit = max(1, min(int(limit), 200))
        return self._all(
            "SELECT id, goal, status, steps, max_actions, model, result, error, created_at, updated_at "
            "FROM tasks ORDER BY id DESC LIMIT ? OFFSET ?", (limit, max(0, int(offset))))

    def count_tasks(self) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM tasks")
        return int(row["n"]) if row else 0

    def mark_interrupted(self) -> list[int]:
        """Called on startup: tasks that were active when the app died become 'interrupted'."""
        rows = self._all(
            f"SELECT id FROM tasks WHERE status IN ({','.join('?' * len(ACTIVE_STATUSES))})",
            ACTIVE_STATUSES)
        ids = [r["id"] for r in rows]
        for tid in ids:
            self.update_task(tid, status="interrupted",
                             error="Nuclear Agent was closed while this task was active. Resume to continue from the last checkpoint.")
        return ids

    # ------------------------------------------------------------- actions
    def add_action(self, task_id: int, step: int, kind: str, params: dict | None = None,
                   reasoning: str = "", outcome: str = "", ok: bool = True, url: str = "") -> int:
        cur = self._exec(
            "INSERT INTO actions (task_id, step, kind, params, reasoning, outcome, ok, url, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, step, kind, json.dumps(params or {}), reasoning[:1000], outcome[:2000],
             1 if ok else 0, url[:500], time.time()))
        return int(cur.lastrowid)

    def list_actions(self, task_id: int, limit: int = 20, before_id: int | None = None) -> list[dict]:
        """Newest-first page of actions. Use before_id to page further back."""
        limit = max(1, min(int(limit), 200))
        if before_id:
            rows = self._all("SELECT * FROM actions WHERE task_id = ? AND id < ? ORDER BY id DESC LIMIT ?",
                             (task_id, int(before_id), limit))
        else:
            rows = self._all("SELECT * FROM actions WHERE task_id = ? ORDER BY id DESC LIMIT ?",
                             (task_id, limit))
        for r in rows:
            try:
                r["params"] = json.loads(r["params"] or "{}")
            except ValueError:
                r["params"] = {}
            r["ok"] = bool(r["ok"])
        return rows

    # ---------------------------------------------------------- checkpoints
    def save_checkpoint(self, task_id: int, step: int, url: str, label: str, state: dict) -> int:
        cur = self._exec(
            "INSERT INTO checkpoints (task_id, step, url, label, state, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, step, url[:500], label[:200], json.dumps(state), time.time()))
        # keep the table small: last 50 checkpoints per task
        self._exec(
            "DELETE FROM checkpoints WHERE task_id = ? AND id NOT IN "
            "(SELECT id FROM checkpoints WHERE task_id = ? ORDER BY id DESC LIMIT 50)",
            (task_id, task_id))
        return int(cur.lastrowid)

    def latest_checkpoint(self, task_id: int) -> dict | None:
        cp = self._one("SELECT * FROM checkpoints WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task_id,))
        if cp:
            cp["state"] = _loads(cp["state"])
        return cp

    def list_checkpoints(self, task_id: int, limit: int = 20) -> list[dict]:
        rows = self._all("SELECT * FROM checkpoints WHERE task_id = ? ORDER BY id DESC LIMIT ?",
                         (task_id, max(1, min(int(limit), 200))))
        for r in rows:
            r["state"] = _loads(r["state"])
        return rows


def _loads(text: str) -> dict:
    try:
        v = json.loads(text or "{}")
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}
