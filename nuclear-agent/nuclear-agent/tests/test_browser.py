"""Real browser tests (headless Chromium) against local pages."""

import io

import pytest
from PIL import Image

from nuclear_agent.browser import BrowserCrashed, BrowserError, BrowserManager, normalize_url


@pytest.fixture
def browser(config, engine):
    b = BrowserManager(config)
    engine.run(b.start())
    yield b
    engine.run(b.stop())


def el_by(elements, **kw):
    for e in elements:
        if all(str(e.get(k, "")) == str(v) for k, v in kw.items()):
            return e["id"]
    raise AssertionError(f"no element {kw} in {elements}")


def test_start_and_stop(config, engine):
    b = BrowserManager(config)
    assert b.running is False
    st = engine.run(b.start())
    assert st["running"] is True
    engine.run(b.start())  # idempotent
    st = engine.run(b.stop())
    assert st["running"] is False


def test_open_and_read(browser, engine, site):
    url = engine.run(browser.navigate(site + "/playground.html"))
    assert url.endswith("/playground.html")
    obs = engine.run(browser.observe())
    assert obs.title == "Nuclear Test Playground"
    assert "uranium-235 is fissile" in obs.text
    assert obs.captcha["detected"] is False
    kinds = {(e["tag"], e["text"] or e["label"]) for e in obs.elements}
    assert ("button", "Click me") in kinds and ("a", "Go to page two") in kinds


def test_screenshot_is_valid_jpeg(browser, engine, site):
    engine.run(browser.navigate(site + "/playground.html"))
    data = engine.run(browser.screenshot())
    img = Image.open(io.BytesIO(data))
    assert img.format == "JPEG" and img.size == (1280, 800)
    assert browser.last_screenshot == data


def test_click_type_select_press(browser, engine, site):
    engine.run(browser.navigate(site + "/playground.html"))
    els = engine.run(browser.elements())
    engine.run(browser.click(el_by(els, text="Click me")))
    engine.run(browser.click(el_by(els, text="Click me")))
    assert engine.run(browser.page.inner_text("#status")) == "clicked 2"

    engine.run(browser.type_text(el_by(els, tag="input"), "neutron", submit=True))
    assert engine.run(browser.page.inner_text("#result")) == "searched: neutron"

    engine.run(browser.select_option(el_by(els, tag="select"), "Green"))
    assert engine.run(browser.page.eval_on_selector("#color", "e => e.value")) == "Green"

    engine.run(browser.type_text(el_by(els, tag="input"), "abc", submit=False))
    engine.run(browser.press("Enter"))
    assert engine.run(browser.page.inner_text("#result")) == "searched: abc"
    with pytest.raises(BrowserError):
        engine.run(browser.press("Control+W"))


def test_coordinate_click(browser, engine, site):
    engine.run(browser.navigate(site + "/playground.html"))
    els = engine.run(browser.elements())
    btn = browser.last_elements[el_by(els, text="Click me")]
    engine.run(browser.click(x=btn["x"], y=btn["y"]))
    assert engine.run(browser.page.inner_text("#status")) == "clicked 1"


def test_scroll_reveals_elements(browser, engine, site):
    engine.run(browser.navigate(site + "/playground.html"))
    els = engine.run(browser.elements())
    assert not any(e["text"] == "Bottom button" for e in els)  # off-screen
    info = engine.run(browser.scroll("bottom"))
    assert info["y"] > 1000
    els = engine.run(browser.elements())
    engine.run(browser.click(el_by(els, text="Bottom button")))
    assert engine.run(browser.page.inner_text("#late")) == "late clicked"
    info = engine.run(browser.scroll("up", 500))
    assert info["y"] < 2000
    info = engine.run(browser.scroll("top"))
    assert info["y"] == 0


def test_link_navigation_and_back(browser, engine, site):
    engine.run(browser.navigate(site + "/playground.html"))
    els = engine.run(browser.elements())
    url = engine.run(browser.click(el_by(els, text="Go to page two")))
    assert url.endswith("/page2.html")
    assert "Opening hours" in engine.run(browser.page_text())
    assert engine.run(browser.back()).endswith("/playground.html")
    assert engine.run(browser.forward()).endswith("/page2.html")


def test_stale_and_invalid_element(browser, engine, site):
    engine.run(browser.navigate(site + "/playground.html"))
    engine.run(browser.elements())
    with pytest.raises(BrowserError, match="not on the current element list"):
        engine.run(browser.click(999))
    with pytest.raises(BrowserError):
        engine.run(browser.click("abc"))


def test_url_rules():
    assert normalize_url("example.com") == "https://example.com"
    for bad in ("file:///C:/Windows/win.ini", "javascript:alert(1)", "chrome://settings", ""):
        with pytest.raises(BrowserError):
            normalize_url(bad)


def test_captcha_detection(browser, engine, site):
    engine.run(browser.navigate(site + "/captcha.html"))
    assert engine.run(browser.captcha_check())["detected"] is True


def test_crash_detection_and_recovery(browser, engine, site):
    engine.run(browser.navigate(site + "/page2.html"))
    engine.run(browser.page.close())  # simulate the page/browser dying
    assert browser.running is False
    with pytest.raises(BrowserCrashed):
        engine.run(browser.page_text())
    st = engine.run(browser.restart(site + "/page2.html"))
    assert st["running"] and st["url"].endswith("/page2.html") and st["restarts"] == 1
    assert "Opening hours" in engine.run(browser.page_text())


def test_persistent_profile_keeps_storage(config, engine, site):
    config.update({"browser": {"persistent_profile": True}}, persist=False)
    b = BrowserManager(config)
    engine.run(b.start())
    engine.run(b.navigate(site + "/page2.html"))
    engine.run(b.page.evaluate("localStorage.setItem('nuclear', 'kept')"))
    engine.run(b.stop())
    engine.run(b.start())
    engine.run(b.navigate(site + "/page2.html"))
    assert engine.run(b.page.evaluate("localStorage.getItem('nuclear')")) == "kept"
    engine.run(b.stop())
    assert (config.profile_dir).exists()


def test_no_blind_coordinate_click_on_changed_page(browser, engine, site):
    engine.run(browser.navigate(site + "/playground.html"))
    els = engine.run(browser.elements())
    link = el_by(els, text="Go to page two")
    # page content is replaced behind the agent's back; old id no longer exists in the DOM
    engine.run(browser.page.set_content('<button style="width:100%;height:100vh" '
                                        'onclick="this.innerText=\'BAD\'">Place order</button>'))
    with pytest.raises(BrowserError):
        engine.run(browser.click(link))
    assert engine.run(browser.page.inner_text("button")) == "Place order"
