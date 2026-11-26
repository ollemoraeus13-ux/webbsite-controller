"""HTTP API tests: security checks, browser control, task control, history."""

import time

import pytest
from fastapi.testclient import TestClient

from nuclear_agent.server import create_app


def act(action, **kw):
    return {"summary": kw.get("summary", "step"), "note": "", "action": action}


@pytest.fixture
def client_factory(config, fake_llm_cls):
    clients = []

    def make(script=None):
        llm = fake_llm_cls(script or [])
        app = create_app(config, llm=llm, extra_hosts={"testserver"})
        c = TestClient(app)
        c.__enter__()
        token = app.state.na.token
        c.headers.update({"X-Nuclear-Token": token})
        c.llm = llm
        clients.append(c)
        return c

    yield make
    for c in clients:
        c.__exit__(None, None, None)


def wait_status(c, statuses, timeout=40):
    statuses = {statuses} if isinstance(statuses, str) else set(statuses)
    end = time.time() + timeout
    while time.time() < end:
        s = c.get("/api/status").json()
        if s["task"] and s["task"]["status"] in statuses:
            return s
        time.sleep(0.15)
    raise AssertionError(f"timeout; last={s}")


def test_security_checks(client_factory):
    c = client_factory()
    assert c.get("/api/status").status_code == 200
    assert c.get("/api/status", headers={"X-Nuclear-Token": "wrong"}).status_code == 401
    assert c.get("/api/status", headers={"Host": "evil.example.com"}).status_code == 403          # DNS rebinding
    assert c.post("/api/task", json={"goal": "x"}, headers={"Origin": "https://evil.example.com"}).status_code == 403
    assert c.get("/api/status", headers={"Host": "127.0.0.1:8765"}).status_code == 200
    page = c.get("/")
    assert page.status_code == 200 and c.headers["X-Nuclear-Token"] in page.text
    assert "__NUCLEAR_TOKEN__" not in page.text
    assert c.get("/static/app.js").status_code == 200
    assert c.get("/static/../config.json").status_code == 404


def test_settings_roundtrip(client_factory):
    c = client_factory()
    s = c.post("/api/settings", json={"agent": {"max_actions": 7}, "server": {"host": "0.0.0.0"}}).json()
    assert s["agent"]["max_actions"] == 7 and s["server"]["host"] == "127.0.0.1"
    assert c.get("/api/settings").json()["agent"]["max_actions"] == 7
    m = c.get("/api/models").json()
    assert m["ok"] and m["models"][0]["name"] == "fake-model"


def test_browser_endpoints(client_factory, site):
    c = client_factory()
    assert c.get("/api/screenshot").status_code == 204            # no browser yet
    assert c.post("/api/browser/start").json()["running"] is True
    r = c.post("/api/browser/navigate", json={"url": site + "/playground.html"})
    assert r.status_code == 200 and r.json()["url"].endswith("playground.html")
    shot = c.get("/api/screenshot")
    assert shot.status_code == 200 and shot.headers["content-type"] == "image/jpeg" and shot.content[:2] == b"\xff\xd8"
    ins = c.get("/api/browser/inspect").json()
    assert "uranium-235" in ins["text"] and ins["elements"]
    btn = next(e for e in ins["elements"] if e["text"] == "Click me")
    assert c.post("/api/browser/action", json={"type": "click", "x": btn["x"], "y": btn["y"]}).status_code == 200
    assert "clicked 1" in c.get("/api/browser/inspect").json()["text"]
    assert c.post("/api/browser/action", json={"type": "scroll", "direction": "down"}).json()["y"] > 0
    bad = c.post("/api/browser/navigate", json={"url": "file:///C:/secret.txt"})
    assert bad.status_code == 400 and "not allowed" in bad.json()["error"]
    assert c.post("/api/browser/stop").json()["running"] is False


def test_task_flow_via_api(client_factory, site):
    def brain(user):
        """Page-aware fake model: order on the checkout page, then finish."""
        time.sleep(0.3)
        if "ORDER PLACED" in user:
            return act({"type": "done", "answer": "All good"})
        if "Checkout" in user:
            return act({"type": "click", "element": 4})  # "Place order" -> needs approval
        return act({"type": "wait", "seconds": 0.5})
    c = client_factory([brain] * 20)
    r = c.post("/api/task", json={"goal": "Test the flow", "start_url": site + "/playground.html", "max_actions": 10})
    assert r.status_code == 200, r.text
    tid = r.json()["task_id"]
    assert c.post("/api/task", json={"goal": "second"}).status_code == 400     # one at a time
    # manual control blocked while the agent is running
    assert c.post("/api/browser/action", json={"type": "scroll"}).status_code == 409

    c.post("/api/task/pause")
    s = wait_status(c, "paused")
    assert c.post("/api/browser/action", json={"type": "scroll"}).status_code == 200  # allowed while paused
    c.post("/api/browser/navigate", json={"url": site + "/sensitive.html"})
    c.post("/api/task/resume")
    s = wait_status(c, "awaiting_confirmation")
    assert "Place order" in s["task"]["pending"]["reason"]
    r = c.post("/api/task/confirm", json={"approve": True})
    assert r.status_code == 200, r.text
    s = wait_status(c, "completed")
    task = c.get(f"/api/tasks/{tid}").json()
    assert task["result"] == "All good"
    acts = c.get(f"/api/tasks/{tid}/actions?limit=1").json()["actions"]
    assert len(acts) == 1 and acts[0]["kind"] == "done"
    older = c.get(f"/api/tasks/{tid}/actions?limit=20&before={acts[-1]['id']}").json()["actions"]
    assert older and older[0]["id"] < acts[-1]["id"]
    assert c.get(f"/api/tasks/{tid}/checkpoints").json()["checkpoints"]
    assert c.get("/api/tasks").json()["total"] == 1
    assert c.post(f"/api/tasks/{tid}/resume").status_code == 400  # completed tasks can't resume


def test_stop_and_resume_from_tasks_list(client_factory, site):
    c = client_factory([lambda _: (time.sleep(1), act({"type": "scroll", "direction": "down"}))[1]] * 3
                       + [act({"type": "done", "answer": "resumed fine"})])
    tid = c.post("/api/task", json={"goal": "long", "start_url": site + "/playground.html"}).json()["task_id"]
    time.sleep(1.5)
    c.post("/api/task/stop")
    assert c.get(f"/api/tasks/{tid}").json()["status"] == "stopped"
    c.llm.script = [act({"type": "done", "answer": "resumed fine"})]
    assert c.post(f"/api/tasks/{tid}/resume").status_code == 200
    wait_status(c, "completed")
    assert c.get(f"/api/tasks/{tid}").json()["result"] == "resumed fine"


def test_errors_are_json_not_crashes(client_factory):
    c = client_factory()
    assert c.post("/api/task/pause").status_code == 400
    assert c.post("/api/task", json={"goal": "  "}).status_code == 400
    assert c.get("/api/tasks/9999").status_code == 404
    assert c.get("/api/browser/inspect").status_code == 400
    assert c.get("/api/status").status_code == 200  # still alive
