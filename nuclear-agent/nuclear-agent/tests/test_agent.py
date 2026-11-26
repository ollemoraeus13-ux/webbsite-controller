"""Agent loop tests with a scripted fake model and a real headless browser."""

import time

import pytest

from nuclear_agent.agent import AgentManager, validate_action
from nuclear_agent.browser import BrowserManager
from nuclear_agent.storage import Storage


def act(action, summary="", note=""):
    return {"summary": summary or f"doing {action['type']}", "note": note, "action": action}


@pytest.fixture
def make_manager(config, engine):
    made = []

    def _make(script, llm=None, cls=None):
        from tests.conftest import FakeLLM
        storage = Storage(config.db_path)
        browser = BrowserManager(config)
        mgr = AgentManager(config, storage, browser, llm or FakeLLM(script))
        made.append(mgr)
        return mgr

    yield _make
    for m in made:
        try:
            engine.run(m.stop(), 20)
            engine.run(m.browser.stop(), 20)
        except Exception:
            pass
        m.storage.close()


def wait_for(engine, mgr, statuses, timeout=40):
    statuses = {statuses} if isinstance(statuses, str) else set(statuses)
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = engine.run(mgr.status())
        if last["task"] and last["task"]["status"] in statuses:
            return last["task"]
        time.sleep(0.1)
    raise AssertionError(f"timeout waiting for {statuses}; last={last}")


def test_validate_action():
    assert validate_action({"type": "click", "element": "3"}) == {"type": "click", "element": 3}
    assert validate_action({"type": "navigate", "url": "example.com"})["url"] == "https://example.com"
    for bad in ({"type": "fly"}, {"type": "click"}, {"type": "done", "answer": ""},
                {"type": "navigate", "url": "file:///etc/passwd"}, "click"):
        with pytest.raises(Exception):
            validate_action(bad)


def test_full_task_completes(make_manager, engine, site):
    script = [
        act({"type": "click", "element": 1}, "Clicking the test button"),
        act({"type": "type", "element": 2, "text": "fission", "submit": True}, "Searching"),
        act({"type": "scroll", "direction": "down"}),
        act({"type": "scroll", "direction": "top"}),
        act({"type": "click", "element": 4}, note="Fact: uranium-235 is fissile"),
        act({"type": "done", "answer": "Hours are Mon-Fri 09:00-17:00; U-235 is fissile."}),
    ]
    mgr = make_manager(script)
    engine.run(mgr.start_task("Find the facts", start_url=site + "/playground.html"))
    t = wait_for(engine, mgr, "completed")
    task = mgr.storage.get_task(t["task_id"])
    assert task["status"] == "completed" and "09:00-17:00" in task["result"]
    assert task["steps"] == 6
    kinds = [a["kind"] for a in reversed(mgr.storage.list_actions(t["task_id"], 50))]
    assert kinds == ["click", "type", "scroll", "scroll", "click", "done"]
    assert all(a["ok"] for a in mgr.storage.list_actions(t["task_id"], 50))
    assert mgr.browser.page.url.endswith("/page2.html")
    assert t["memory"] == ["Fact: uranium-235 is fissile"]
    cps = mgr.storage.list_checkpoints(t["task_id"])
    assert cps[0]["label"] == "done" and cps[-1]["label"] == "start"
    # the model saw the real page
    assert "uranium-235 is fissile" in mgr.llm.calls[0]["user"]
    assert "[1] button \"Click me\"" in mgr.llm.calls[0]["user"]
    assert "can NOT see the screenshot" in mgr.llm.calls[0]["user"]


def test_pause_and_resume(make_manager, engine, site):
    def slow(action):
        def f(_):
            time.sleep(0.6)  # runs in engine thread: simulates model latency
            return act(action)
        return f
    script = [slow({"type": "scroll", "direction": "down"}), slow({"type": "scroll", "direction": "up"}),
              act({"type": "done", "answer": "ok"})]
    mgr = make_manager(script)
    engine.run(mgr.start_task("scroll around", start_url=site + "/playground.html"))
    wait_for(engine, mgr, "running")
    engine.run(mgr.pause())
    t = wait_for(engine, mgr, "paused")
    steps_at_pause = t["step"]
    time.sleep(1.0)
    assert engine.run(mgr.status())["task"]["step"] == steps_at_pause  # nothing happens while paused
    assert mgr.storage.get_task(t["task_id"])["status"] == "paused"
    engine.run(mgr.resume())
    t = wait_for(engine, mgr, "completed")
    assert t["step"] == 3


def test_sensitive_action_approved(make_manager, engine, site):
    mgr = make_manager([act({"type": "click", "element": 4}), act({"type": "done", "answer": "bought"})])
    engine.run(mgr.start_task("buy it", start_url=site + "/sensitive.html"))
    t = wait_for(engine, mgr, "awaiting_confirmation")
    assert "Place order" in t["pending"]["reason"]
    assert engine.run(mgr.browser.page.inner_text("#done")) == ""  # not clicked yet
    engine.run(mgr.confirm(True))
    wait_for(engine, mgr, "completed")
    assert engine.run(mgr.browser.page.inner_text("#done")) == "ORDER PLACED"


def test_sensitive_action_denied(make_manager, engine, site):
    mgr = make_manager([act({"type": "click", "element": 5}), act({"type": "done", "answer": "skipped"})])
    engine.run(mgr.start_task("send it", start_url=site + "/sensitive.html"))
    wait_for(engine, mgr, "awaiting_confirmation")
    engine.run(mgr.confirm(False))
    t = wait_for(engine, mgr, "completed")
    assert engine.run(mgr.browser.page.inner_text("#done")) == ""
    declined = [a for a in mgr.storage.list_actions(t["task_id"]) if a["outcome"] == "User declined this action"]
    assert declined and "declined" in mgr.llm.calls[1]["user"]


def test_password_field_is_user_only(make_manager, engine, site):
    mgr = make_manager([act({"type": "type", "element": 2, "text": "hunter2"}), act({"type": "done", "answer": "x"})])
    engine.run(mgr.start_task("log in", start_url=site + "/sensitive.html"))
    t = wait_for(engine, mgr, "needs_user")
    assert "never fills these in" in t["message"]
    assert engine.run(mgr.browser.page.eval_on_selector("#pw", "e => e.value")) == ""
    engine.run(mgr.resume())
    wait_for(engine, mgr, "completed")
    assert engine.run(mgr.browser.page.eval_on_selector("#pw", "e => e.value")) == ""


def test_personal_data_needs_approval_and_is_masked(make_manager, engine, site):
    mgr = make_manager([act({"type": "type", "element": 1, "text": "olle@example.com"}), act({"type": "done", "answer": "x"})])
    engine.run(mgr.start_task("fill email", start_url=site + "/sensitive.html"))
    wait_for(engine, mgr, "awaiting_confirmation")
    engine.run(mgr.confirm(True))
    t = wait_for(engine, mgr, "completed")
    assert engine.run(mgr.browser.page.eval_on_selector("#email", "e => e.value")) == "olle@example.com"
    typed = [a for a in mgr.storage.list_actions(t["task_id"]) if a["kind"] == "type"][0]
    assert typed["params"]["text"] == "***"


def test_captcha_hands_over_to_user(make_manager, engine, site):
    mgr = make_manager([act({"type": "done", "answer": "x"})])
    engine.run(mgr.start_task("get past it", start_url=site + "/captcha.html"))
    t = wait_for(engine, mgr, "needs_user")
    assert "CAPTCHA" in t["message"] and mgr.llm.calls == []  # model was never asked to solve it
    engine.run(mgr.stop())
    assert mgr.storage.get_task(t["task_id"])["status"] == "stopped"


def test_action_limit_then_extend(make_manager, engine, site, config):
    scroll = act({"type": "scroll", "direction": "down"})
    mgr = make_manager([scroll, scroll, scroll, act({"type": "done", "answer": "fin"})])
    config.update({"agent": {"max_actions": 2}}, persist=False)
    engine.run(mgr.start_task("loop", start_url=site + "/playground.html"))
    t = wait_for(engine, mgr, "needs_user")
    assert "action limit (2)" in t["message"] and t["step"] == 2
    engine.run(mgr.resume())
    t = wait_for(engine, mgr, "completed")
    assert t["max_actions"] == 4 and t["step"] == 4


def test_bad_model_output_recovers(make_manager, engine, site):
    from nuclear_agent.llm import LLMError
    script = [{"summary": "?", "action": {"type": "teleport"}},
              {"summary": "?", "action": {"type": "click", "element": 999}},
              act({"type": "done", "answer": "recovered"})]
    mgr = make_manager(script)
    engine.run(mgr.start_task("x", start_url=site + "/playground.html"))
    t = wait_for(engine, mgr, "completed")
    acts = mgr.storage.list_actions(t["task_id"], 50)
    assert any("Unknown action type" in a["outcome"] for a in acts)
    assert any("not on the current element list" in a["outcome"] for a in acts)
    assert "not on the current element list" in mgr.llm.calls[2]["user"]  # error fed back


def test_ollama_down_asks_user(make_manager, engine, site):
    from nuclear_agent.llm import LLMError
    mgr = make_manager([LLMError("Ollama request failed")] * 3 + [act({"type": "done", "answer": "back"})])
    engine.run(mgr.start_task("x", start_url=site + "/page2.html"))
    t = wait_for(engine, mgr, "needs_user", timeout=60)
    assert "3 errors in a row" in t["message"] and "Ollama" in t["message"]
    engine.run(mgr.resume())
    assert wait_for(engine, mgr, "completed")


def test_browser_crash_mid_task_recovers(make_manager, engine, site):
    mgr = make_manager([])
    calls = {"n": 0}

    def scripted(user):
        calls["n"] += 1
        if calls["n"] == 1:
            return act({"type": "click", "element": 4})  # go to page two
        if calls["n"] == 2:
            time.sleep(1.0)  # model "thinking" while the browser dies
            return act({"type": "wait", "seconds": 0.5})
        return act({"type": "done", "answer": "survived"})

    mgr.llm.script = [scripted] * 5
    engine.run(mgr.start_task("x", start_url=site + "/playground.html"))
    # wait until on page two, then kill the page
    end = time.time() + 20
    while time.time() < end and not (mgr.browser.page and mgr.browser.page.url.endswith("page2.html")):
        time.sleep(0.05)
    engine.run(mgr.browser.page.close())
    t = wait_for(engine, mgr, "completed")
    kinds = [a["kind"] for a in mgr.storage.list_actions(t["task_id"], 50)]
    assert "recover" in kinds
    assert mgr.browser.running and mgr.browser.page.url.endswith("/page2.html")


def test_resume_interrupted_task_from_checkpoint(make_manager, engine, site, config):
    # first "session": do some work, then simulate the app dying
    mgr = make_manager([act({"type": "click", "element": 4}, note="Opening hours found"),
                        lambda _: (time.sleep(30), act({"type": "done", "answer": "never"}))[1]])
    engine.run(mgr.start_task("collect info", start_url=site + "/playground.html"))
    end = time.time() + 20
    while time.time() < end:
        cp = mgr.storage.latest_checkpoint(mgr.runner.task_id)
        if cp and cp["step"] >= 1:
            break
        time.sleep(0.1)
    tid = mgr.runner.task_id
    mgr.runner.task.cancel()
    time.sleep(0.5)
    engine.run(mgr.browser.stop())
    mgr.storage.update_task(tid, status="running")      # as if the process died mid-task
    assert mgr.storage.mark_interrupted() == [tid]       # what startup does

    # second "session": new manager, same database
    mgr2 = make_manager([act({"type": "done", "answer": "Hours: Mon-Fri 09:00-17:00"})])
    snap = engine.run(mgr2.resume_task(tid))
    assert snap["step"] == 1 and snap["memory"] == ["Opening hours found"]
    t = wait_for(engine, mgr2, "completed")
    assert mgr2.storage.get_task(tid)["result"].startswith("Hours")
    # the browser was brought back to the checkpointed page
    assert "CURRENT PAGE: Page Two" in mgr2.llm.calls[0]["user"]
    assert "Opening hours found" in mgr2.llm.calls[0]["user"]


def test_user_message_reaches_model(make_manager, engine, site):
    mgr = make_manager([act({"type": "ask_user", "question": "Which city?"}), act({"type": "done", "answer": "ok"})])
    engine.run(mgr.start_task("weather", start_url=site + "/page2.html"))
    t = wait_for(engine, mgr, "needs_user")
    assert "Which city?" in t["message"]
    engine.run(mgr.message("Stockholm"))
    engine.run(mgr.resume())
    wait_for(engine, mgr, "completed")
    assert "- Stockholm" in mgr.llm.calls[1]["user"]


def test_only_one_task_at_a_time(make_manager, engine, site):
    mgr = make_manager([lambda _: (time.sleep(2), act({"type": "done", "answer": "x"}))[1]])
    engine.run(mgr.start_task("one", start_url=site + "/page2.html"))
    with pytest.raises(RuntimeError, match="already active"):
        engine.run(mgr.start_task("two"))
    engine.run(mgr.stop())


def test_missing_model_is_explained(make_manager, engine, config):
    config.update({"ollama": {"model": "qwen2.5:7b"}}, persist=False)
    mgr = make_manager([])
    with pytest.raises(RuntimeError, match="not installed"):
        engine.run(mgr.start_task("x"))


def test_stale_action_after_pause_is_discarded(make_manager, engine, site):
    """If the user pauses and changes the page, the action planned for the old page must not run."""
    def brain(user):
        time.sleep(0.8)
        if "Checkout" in user:
            return act({"type": "done", "answer": "saw checkout, did nothing"})
        return act({"type": "click", "element": 1})  # 'Click me' on playground; [1] is the email field on checkout
    mgr = make_manager([brain] * 10)
    engine.run(mgr.start_task("x", start_url=site + "/playground.html"))
    end = time.time() + 20
    while time.time() < end and not mgr.llm.calls:
        time.sleep(0.05)
    engine.run(mgr.pause())                                       # pause while the model is "thinking"
    wait_for(engine, mgr, "paused")
    engine.run(mgr.browser.navigate(site + "/sensitive.html"))     # user changes the page
    engine.run(mgr.resume())
    t = wait_for(engine, mgr, "completed")
    assert engine.run(mgr.browser.page.inner_text("#done")) == ""
    kinds = [a["kind"] for a in mgr.storage.list_actions(t["task_id"], 50)]
    assert "click" not in kinds


def test_ocr_fallback_reads_screenshot(engine, config, site):
    from nuclear_agent import ocr
    if not ocr.available():
        pytest.skip("Tesseract not installed")
    b = BrowserManager(config)
    engine.run(b.start())
    engine.run(b.navigate(site + "/page2.html"))
    text = ocr.image_to_text(engine.run(b.screenshot()))
    engine.run(b.stop())
    assert "Opening hours" in text
