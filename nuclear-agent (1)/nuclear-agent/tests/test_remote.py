"""Remote access: API keys, scopes, tunnel rules, CORS, MCP (HTTP + stdio),
Claude Desktop setup, and the GitHub Pages panel in a real browser."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nuclear_agent.keys import KeyStore
from nuclear_agent.server import create_app

ROOT = Path(__file__).resolve().parent.parent


def act(action, **kw):
    return {"summary": kw.get("summary", "step"), "note": kw.get("note", ""), "action": action}


def page_brain(user):
    """Page-aware fake model used by several tests."""
    time.sleep(0.2)
    if "ORDER PLACED" in user:
        return act({"type": "done", "answer": "Order placed, 499 SEK."})
    if "Checkout" in user:
        return act({"type": "click", "element": 4})            # needs approval
    if "Opening hours" in user:
        return act({"type": "done", "answer": "Mon-Fri 09:00-17:00"})
    return act({"type": "click", "element": 4})                # playground -> page two


# ---------------------------------------------------------------- keys
def test_keystore(tmp_path):
    ks = KeyStore(tmp_path)
    key, rec = ks.create("ChatGPT", "ai")
    assert key.startswith("na_") and len(key) > 40
    assert key not in (tmp_path / "api_keys.json").read_text()          # only the hash is stored
    assert ks.verify(key)["scopes"] == ["read", "submit", "control"]
    assert ks.verify("na_wrong") is None and ks.verify("") is None
    assert KeyStore(tmp_path).verify(key)                               # persisted
    assert ks.revoke(rec["id"]) and ks.verify(key) is None
    with pytest.raises(ValueError):
        ks.create("x", "god-mode")
    local = ks.ensure_local_key()
    assert ks.verify(local)["local_only"] and ks.ensure_local_key() == local
    (tmp_path / "local_mcp_key.txt").unlink()
    assert ks.ensure_local_key() != local and ks.verify(local) is None   # old one revoked
    assert all(ks.allow_request("k", 3) for _ in range(3)) and not ks.allow_request("k", 3)


# ---------------------------------------------------------- REST + rules
@pytest.fixture
def remote_client(config, fake_llm_cls):
    config.update({"remote": {"allowed_origins": ["https://olle.github.io"]}}, persist=False)
    app = create_app(config, llm=fake_llm_cls([page_brain] * 30), extra_hosts={"testserver"})
    with TestClient(app) as c:
        c.na = app.state.na
        c.panel_token = app.state.na.token
        yield c


def B(key):
    return {"Authorization": "Bearer " + key}


def wait_status(c, key, statuses, timeout=40):
    statuses = {statuses} if isinstance(statuses, str) else set(statuses)
    end = time.time() + timeout
    while time.time() < end:
        st = c.get("/remote/v1/status", headers=B(key)).json()
        if st.get("task") and st["task"]["status"] in statuses:
            return st
        time.sleep(0.15)
    raise AssertionError(f"timeout, last={st}")


def test_scopes_and_approval_rules(remote_client, site):
    c = remote_client
    ai, _ = c.na.keys.create("AI", "ai")
    panel, _ = c.na.keys.create("Me", "panel")
    ro, _ = c.na.keys.create("RO", "readonly")
    assert c.get("/remote/v1/whoami").status_code == 401
    assert c.post("/remote/v1/tasks", headers=B(ro), json={"goal": "x"}).status_code == 403
    r = c.post("/remote/v1/tasks", headers=B(ai), json={"goal": "buy", "start_url": site + "/sensitive.html"})
    assert r.status_code == 200, r.text
    tid = r.json()["task_id"]
    st = wait_status(c, ai, "awaiting_confirmation")
    assert "Place order" in st["task"]["pending"]["reason"]
    deny = c.post("/remote/v1/task/confirm", headers=B(ai), json={"approve": True})
    assert deny.status_code == 403 and "person" in deny.json()["error"]      # AI can't approve itself
    info = c.get(f"/remote/v1/tasks/{tid}", headers=B(ai)).json()
    assert info["needs_approval"] and "cannot approve" in info["hint"]
    assert c.post("/remote/v1/task/confirm", headers=B(panel), json={"approve": True}).status_code == 200
    done = c.post(f"/remote/v1/tasks/{tid}/wait?seconds=30", headers=B(ai)).json()
    assert done["status"] == "completed" and done["result"].startswith("Order placed")
    assert done["source"] == "remote:AI"
    audit = (c.na.config.data_dir / "logs" / "remote_audit.jsonl").read_text()
    assert '"action": "start_task"' in audit and '"action": "approve"' in audit
    assert c.get("/remote/v1/screenshot", headers=B(ro)).content[:2] == b"\xff\xd8"


def test_tunnel_rules(remote_client):
    c = remote_client
    ai, _ = c.na.keys.create("AI", "ai")
    local = c.na.keys.ensure_local_key()
    tunnel = {"Host": "abc-def.trycloudflare.com"}
    r = c.get("/remote/v1/whoami", headers={**B(ai), **tunnel})
    assert r.status_code == 403 and "turned off" in r.json()["error"]       # remote off by default
    c.na.config.update({"remote": {"enabled": True}}, persist=False)
    assert c.get("/remote/v1/whoami", headers={**B(ai), **tunnel}).status_code == 200
    assert c.get("/remote/v1/whoami", headers={**B(local), **tunnel}).status_code == 403   # local-only key
    assert c.get("/remote/v1/whoami", headers=tunnel).status_code == 401
    # the local panel API is never reachable through the tunnel, even with its token
    assert c.get("/api/status", headers={"X-Nuclear-Token": c.panel_token, **tunnel}).status_code == 403
    assert c.get("/", headers=tunnel).status_code == 403


def test_cors(remote_client):
    c = remote_client
    ai, _ = c.na.keys.create("AI", "ai")
    good = {"Origin": "https://olle.github.io"}
    pre = c.options("/remote/v1/status", headers={**good, "Access-Control-Request-Method": "GET",
                                                    "Access-Control-Request-Private-Network": "true"})
    assert pre.status_code == 204
    assert pre.headers["access-control-allow-origin"] == "https://olle.github.io"
    assert pre.headers["access-control-allow-private-network"] == "true"
    assert "Authorization" in pre.headers["access-control-allow-headers"]
    r = c.get("/remote/v1/whoami", headers={**B(ai), **good})
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "https://olle.github.io"
    assert c.get("/remote/v1/whoami", headers={**B(ai), "Origin": "https://evil.example"}).status_code == 403
    assert c.get("/remote/v1/whoami", headers={**B(ai), "Origin": "https://olle.github.io.evil.example"}).status_code == 403


def test_rate_limit(remote_client):
    c = remote_client
    c.na.config.update({"remote": {"max_requests_per_minute": 10}}, persist=False)
    ai, _ = c.na.keys.create("AI", "ai")
    codes = [c.get("/remote/v1/whoami", headers=B(ai)).status_code for _ in range(12)]
    assert codes[:10] == [200] * 10 and codes[10:] == [429, 429]


def test_panel_key_management(remote_client):
    c = remote_client
    H = {"X-Nuclear-Token": c.panel_token}
    r = c.post("/api/keys", headers=H, json={"name": "Phone", "preset": "panel"}).json()
    assert r["key"].startswith("na_") and r["record"]["scopes"][-1] == "approve"
    info = c.get("/api/remote", headers=H).json()
    names = [k["name"] for k in info["keys"]]
    assert "Phone" in names and any("local-mcp" in n for n in names)
    assert all("hash" not in k for k in info["keys"])
    assert c.delete(f"/api/keys/{r['record']['id']}", headers=H).status_code == 200
    assert c.get("/remote/v1/whoami", headers=B(r["key"])).status_code == 401
    s = c.post("/api/settings", headers=H, json={"remote": {"allowed_origins": "https://a.github.io/\nhttps://b.github.io"}}).json()
    assert s["remote"]["allowed_origins"] == ["https://a.github.io", "https://b.github.io"]


# ------------------------------------------------- real server + MCP
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(config, fake_llm_cls):
    import uvicorn
    port = _free_port()
    config.update({"server": {"port": port}}, persist=False)
    config._data["server"]["port"] = port   # server section is read-only via update(); set directly in tests
    llm = fake_llm_cls([page_brain] * 50)
    app = create_app(config, llm=llm)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    end = time.time() + 20
    while not server.started and time.time() < end:
        time.sleep(0.05)
    assert server.started
    yield {"port": port, "url": f"http://127.0.0.1:{port}", "state": app.state.na, "config": config}
    server.should_exit = True
    th.join(20)


def test_mcp_over_http(live_server, site):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    state = live_server["state"]
    key, _ = state.keys.create("Cursor", "ai")

    async def scenario():
        async with streamablehttp_client(live_server["url"] + "/mcp",
                                         headers={"Authorization": "Bearer " + key}) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                names = {t.name for t in (await s.list_tools()).tools}
                assert {"run_web_task", "wait_for_task", "get_task", "stop_task", "list_tasks",
                        "get_screenshot", "reply_to_agent"} <= names
                res = await s.call_tool("run_web_task", {"task": "Find the opening hours",
                                                         "start_url": site + "/playground.html", "wait_seconds": 40})
                data = json.loads(res.content[0].text)
                assert data["status"] == "completed" and data["result"] == "Mon-Fri 09:00-17:00"
                img = await s.call_tool("get_screenshot", {})
                assert img.content[0].type == "image" and img.content[0].mimeType == "image/jpeg"
                lst = json.loads((await s.call_tool("list_tasks", {"limit": 5})).content[0].text)
                assert lst["tasks"][0]["source"] == "remote:Cursor"
        # key in the URL path (for AI apps that can't send headers)
        async with streamablehttp_client(live_server["url"] + "/mcp/k/" + key) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                info = json.loads((await s.call_tool("get_task", {"task_id": 0})).content[0].text)
                assert info["status"] == "completed"
    asyncio.run(scenario())


def _stdio_params(live, extra_env=None):
    from mcp import StdioServerParameters
    env = dict(os.environ)
    env.update({"NUCLEAR_AGENT_DATA": str(live["config"].data_dir), "NUCLEAR_AGENT_PORT": str(live["port"]),
                "NUCLEAR_MCP_NO_AUTOSTART": "1"})
    env.update(extra_env or {})
    return StdioServerParameters(command=sys.executable, args=[str(ROOT / "nuclear_mcp.py")], env=env)


def test_mcp_over_stdio_like_claude_desktop(live_server, site):
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    async def scenario():
        async with stdio_client(_stdio_params(live_server)) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                res = await s.call_tool("run_web_task", {"task": "buy the thing", "start_url": site + "/sensitive.html",
                                                         "wait_seconds": 30})
                data = json.loads(res.content[0].text)
                assert data["status"] == "awaiting_confirmation" and "cannot approve" in data["hint"]
                # the human approves in the panel (here: directly on the server)
                state = live_server["state"]
                await asyncio.wrap_future(state.engine.submit(state.agent.confirm(True)))
                data = json.loads((await s.call_tool("wait_for_task", {"task_id": data["task_id"], "seconds": 30})).content[0].text)
                assert data["status"] == "completed" and "499" in data["result"]
                stop = await s.call_tool("stop_task", {})
                assert not stop.isError
    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform == "win32", reason="test cleanup of the background agent is Linux/macOS-only")
def test_stdio_autostarts_agent(tmp_path, site):
    """Claude Desktop starts nuclear_mcp.py; if the agent isn't running it is started in the background."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    port = _free_port()
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "server": {"port": port},
        "browser": {"headless": True, "persistent_profile": False,
                    "executable_path": os.environ.get("NUCLEAR_TEST_CHROME", "")},
        "ollama": {"url": "http://127.0.0.1:9"},   # no model needed for this test
    }))
    env = dict(os.environ)
    env.update({"NUCLEAR_AGENT_DATA": str(tmp_path / "data"), "NUCLEAR_AGENT_CONFIG": str(cfg_path)})
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "nuclear_mcp.py")], env=env)

    async def scenario():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                res = await s.call_tool("list_tasks", {})
                assert not res.isError, res.content[0].text
                assert json.loads(res.content[0].text)["total"] == 0
                res = await s.call_tool("run_web_task", {"task": "x"})
                assert res.isError and "Ollama" in res.content[0].text   # honest error, no crash
    try:
        asyncio.run(scenario())
        assert (tmp_path / "data" / "logs" / "started_by_mcp.log").exists()
    finally:
        _kill_agents_using(cfg_path)


def _kill_agents_using(cfg_path: Path) -> None:
    """Stop the background agent this test started (Linux/macOS test helper)."""
    import signal
    import subprocess
    if sys.platform == "win32":
        return
    pids = subprocess.run(["pgrep", "-f", "nuclear_agent --no-open"], capture_output=True, text=True).stdout.split()
    for pid in pids:
        try:
            if str(cfg_path).encode() in Path(f"/proc/{pid}/environ").read_bytes():
                os.kill(int(pid), signal.SIGTERM)
        except OSError:
            pass


def test_setup_claude_merges_config(tmp_path):
    from nuclear_agent import setup_claude
    p = tmp_path / "Claude" / "claude_desktop_config.json"
    p.parent.mkdir()
    p.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}))
    setup_claude.update(p)
    data = json.loads(p.read_text())
    assert data["theme"] == "dark" and "other" in data["mcpServers"]
    assert data["mcpServers"]["nuclear-agent"]["args"][0].endswith("nuclear_mcp.py")
    assert list(p.parent.glob("claude_desktop_config.backup-*.json"))
    setup_claude.update(p, remove=True)
    assert "nuclear-agent" not in json.loads(p.read_text())["mcpServers"]
    fresh = tmp_path / "new" / "claude_desktop_config.json"
    setup_claude.update(fresh)
    assert "nuclear-agent" in json.loads(fresh.read_text())["mcpServers"]


# --------------------------------------------- GitHub Pages panel (real Chrome)
def test_github_pages_panel_cross_origin(live_server, site):
    """Serve docs/index.html from a DIFFERENT origin (127.0.0.2) and drive the real agent through it."""
    import functools
    import http.server

    from playwright.sync_api import sync_playwright

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ROOT / "docs"))
    handler.log_message = lambda *a: None
    pages = http.server.ThreadingHTTPServer(("127.0.0.2", 0), handler)
    threading.Thread(target=pages.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.2:{pages.server_address[1]}"
    state = live_server["state"]
    panel_key, _ = state.keys.create("Phone", "panel")

    with sync_playwright() as p:
        exe = os.environ.get("NUCLEAR_TEST_CHROME") or None
        b = p.chromium.launch(executable_path=exe, headless=True)
        pg = b.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(origin + "/index.html")
        pg.fill("#c-url", live_server["url"])
        pg.fill("#c-key", panel_key)

        # 1) origin NOT allowed yet -> browser blocks it
        pg.click("#c-connect")
        pg.wait_for_function("document.getElementById('c-msg').textContent.length > 12", timeout=10000)
        assert "Kunde inte" in pg.inner_text("#c-msg") or "not in" in pg.inner_text("#c-msg")
        assert pg.is_hidden("#task-card")

        # 2) allow the origin -> works
        state.config.update({"remote": {"allowed_origins": [origin]}}, persist=False)
        pg.click("#c-connect")
        pg.wait_for_selector("#task-card:not(.hidden)", timeout=10000)
        assert "Phone" in pg.inner_text("#c-msg")
        pg.fill("#t-goal", "Buy it")
        pg.fill("#t-url", site + "/sensitive.html")
        pg.click("#t-start")
        pg.wait_for_selector("#approve-card:not(.hidden)", timeout=30000)
        assert "Place order" in pg.inner_text("#a-reason")
        pg.wait_for_selector("#shot:not(.hidden)", timeout=10000)
        pg.screenshot(path=str(Path(os.environ.get("NUCLEAR_SHOT_DIR", "/tmp")) / "pages_panel.png"))
        pg.click("#a-yes")
        pg.wait_for_selector("#s-res:not(.hidden)", timeout=30000)
        assert "Order placed" in pg.inner_text("#s-res")
        assert errors == []
        b.close()
    pages.shutdown()
