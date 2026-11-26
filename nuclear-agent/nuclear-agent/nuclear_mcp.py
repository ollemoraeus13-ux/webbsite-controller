"""Start Nuclear Agent's MCP server (stdio) for Claude Desktop / Claude Code.

Point your AI app at this file with the Python from .venv, e.g.
    C:\\path\\to\\webbsite-controller\\.venv\\Scripts\\python.exe C:\\path\\to\\webbsite-controller\\nuclear_mcp.py
connect_claude.bat does this for Claude Desktop automatically.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nuclear_agent.mcp_stdio import main  # noqa: E402

if __name__ == "__main__":
    main()
