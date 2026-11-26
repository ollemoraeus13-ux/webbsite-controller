"""Add Nuclear Agent to Claude Desktop's MCP config (Windows/macOS).

    python -m nuclear_agent.setup_claude          add / update
    python -m nuclear_agent.setup_claude --remove  remove

A backup of the old config is written next to it first. Other MCP servers in
the file are left untouched.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

from .config import PROJECT_ROOT

SERVER_NAME = "nuclear-agent"


def claude_config_path() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Claude" / "claude_desktop_config.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def server_entry() -> dict:
    return {"command": sys.executable, "args": [str(PROJECT_ROOT / "nuclear_mcp.py")]}


def update(path: Path, remove: bool = False) -> str:
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except ValueError:
            raise SystemExit(f"{path} is not valid JSON. Fix or delete it, then run this again.")
        backup = path.with_name(f"claude_desktop_config.backup-{time.strftime('%Y%m%d-%H%M%S')}.json")
        shutil.copy2(path, backup)
    if not isinstance(data, dict):
        raise SystemExit(f"{path} has an unexpected format.")
    servers = data.setdefault("mcpServers", {})
    if remove:
        servers.pop(SERVER_NAME, None)
        msg = "Removed Nuclear Agent from Claude Desktop."
    else:
        servers[SERVER_NAME] = server_entry()
        msg = "Added Nuclear Agent to Claude Desktop."
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return msg


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--config", help="path to claude_desktop_config.json (default: auto)")
    a = ap.parse_args(argv)
    path = Path(a.config) if a.config else claude_config_path()
    print(update(path, a.remove))
    print(f"Config file: {path}")
    if not a.remove:
        e = server_entry()
        print("\nNext: fully quit Claude Desktop (tray icon > Quit) and open it again.")
        print("Then ask Claude e.g.: \"Use Nuclear Agent to find today's top headline on svt.se\".")
        print("\nClaude Code instead? Run:")
        print(f'  claude mcp add nuclear-agent -- "{e["command"]}" "{e["args"][0]}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
