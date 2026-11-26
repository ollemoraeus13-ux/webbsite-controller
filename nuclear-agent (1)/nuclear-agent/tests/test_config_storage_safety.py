import json

from nuclear_agent import safety
from nuclear_agent.config import DEFAULTS, Config
from nuclear_agent.storage import Storage


# ------------------------------------------------------------------ config
def test_config_defaults_and_update(tmp_path):
    cfg = Config(path=tmp_path / "c.json", data_dir=tmp_path / "d")
    assert cfg.get("server", "host") == "127.0.0.1"
    out = cfg.update({"agent": {"max_actions": "9999", "bogus": 1}, "server": {"host": "0.0.0.0"},
                      "browser": {"headless": "true"}, "nope": {"x": 1}})
    assert out["agent"]["max_actions"] == 500          # clamped
    assert "bogus" not in out["agent"]
    assert out["server"]["host"] == "127.0.0.1"        # not editable from the panel
    assert out["browser"]["headless"] is True
    saved = json.loads((tmp_path / "c.json").read_text())
    assert saved["agent"]["max_actions"] == 500


def test_config_survives_broken_file(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{not json")
    cfg = Config(path=p, data_dir=tmp_path / "d")
    assert cfg.get("agent", "max_actions") == DEFAULTS["agent"]["max_actions"]


# ----------------------------------------------------------------- storage
def test_storage_tasks_actions_checkpoints(tmp_path):
    s = Storage(tmp_path / "a.db")
    tid = s.create_task("goal", 10, "m", "https://x.test")
    for i in range(45):
        s.add_action(tid, i, "click", {"element": i}, "why", "ok", True, "u")
    page1 = s.list_actions(tid, limit=20)
    assert len(page1) == 20 and page1[0]["step"] == 44
    page2 = s.list_actions(tid, limit=20, before_id=page1[-1]["id"])
    assert page2[0]["step"] == 24
    s.save_checkpoint(tid, 3, "https://x.test/a", "after nav", {"memory": ["fact"], "step": 3})
    cp = s.latest_checkpoint(tid)
    assert cp["state"]["memory"] == ["fact"] and cp["url"].endswith("/a")
    s.update_task(tid, pending={"action": {"type": "click"}})
    assert s.get_task(tid)["pending"]["action"]["type"] == "click"
    assert s.mark_interrupted() == [tid]
    assert s.get_task(tid)["status"] == "interrupted"
    s.close()

    # reopen: state persisted
    s2 = Storage(tmp_path / "a.db")
    assert s2.get_task(tid)["status"] == "interrupted"
    assert s2.latest_checkpoint(tid)["step"] == 3
    s2.close()


def test_checkpoint_retention(tmp_path):
    s = Storage(tmp_path / "a.db")
    tid = s.create_task("g", 5)
    for i in range(60):
        s.save_checkpoint(tid, i, "", "x", {})
    assert len(s.list_checkpoints(tid, limit=200)) == 50
    s.close()


# ------------------------------------------------------------------ safety
def E(**kw):
    base = {"id": 1, "tag": "button", "type": "", "text": "", "label": "", "name": "", "placeholder": "",
            "autocomplete": "", "in_form": False, "role": ""}
    base.update(kw)
    return {1: base}


def test_safety_click_rules():
    assert safety.assess({"type": "click", "element": 1}, E(text="Read more")).level == "ok"
    assert safety.assess({"type": "click", "element": 1}, E(text="Place order")).level == "confirm"
    assert safety.assess({"type": "click", "element": 1}, E(text="Köp nu")).level == "confirm"
    assert safety.assess({"type": "click", "element": 1}, E(text="Skicka")).level == "confirm"
    assert safety.assess({"type": "click", "element": 1}, E(text="Delete account")).level == "confirm"
    assert safety.assess({"type": "click", "element": 1}, E(text="Facebook")).level == "ok"
    assert safety.assess({"type": "click", "element": 1}, E(text="OK", type="submit", in_form=True)).level == "confirm"
    assert safety.assess({"type": "click", "element": 1}, E(text="Search", type="submit", in_form=True)).level == "ok"


def test_safety_typing_rules():
    t = {"type": "type", "element": 1, "text": "x", "submit": False}
    assert safety.assess(t, E(tag="input", type="password")).level == "user"
    assert safety.assess(t, E(tag="input", autocomplete="cc-number")).level == "user"
    assert safety.assess(t, E(tag="input", name="cardnumber")).level == "user"
    assert safety.assess(t, E(tag="input", label="Lösenord")).level == "user"
    assert safety.assess(t, E(tag="input", type="email")).level == "confirm"
    assert safety.assess(t, E(tag="input", label="Phone number")).level == "confirm"
    assert safety.assess(t, E(tag="input", name="q", placeholder="Search")).level == "ok"
    assert safety.assess({**t, "submit": True}, E(tag="input", name="q", placeholder="Search")).level == "ok"
    assert safety.assess({**t, "submit": True}, E(tag="textarea", name="message")).level == "confirm"


def test_safety_enter_key_rules():
    press = {"type": "press", "key": "Enter"}
    assert safety.assess(press, {}, {"tag": "input", "name": "q", "type": "search"}).level == "ok"
    assert safety.assess(press, {}, {"tag": "textarea", "name": "chat"}).level == "confirm"
    assert safety.assess(press, {}, {"tag": "input", "type": "password"}).level == "user"
    assert safety.assess(press, {}, None).level == "ok"
