"""The locator's click-point guard, against real Chromium with faked models.

No LLM is called: the text matcher always declines and the vision model
returns whatever bounding box a test asks for.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PIL import Image
from playwright.async_api import Page

from app.browser import locator as locator_module
from app.browser.actions import click
from app.browser.browser import get_browser
from app.browser.locator import get_locator
from app.llm import vision as vision_module
from app.llm.vision import VisionResult

pytestmark = pytest.mark.asyncio

VIEWPORT = (1280, 800)


@pytest.fixture
async def page():
    browser = get_browser()
    yield await browser.start()
    await browser.stop()


class _NoTextMatch:
    async def ask_json(self, messages, **kwargs):
        return {"index": -1, "confidence": 0.0}


class _FakeVision:
    """Set-of-Mark is never offered; grounding returns a fixed box."""

    def __init__(self, bbox=None):
        self.bbox = bbox

    async def select_by_som(self, screenshot, candidates, target):
        return VisionResult(success=False, reason="not offered")

    async def ground_bbox(self, screenshot, target):
        if self.bbox is None:
            return VisionResult(success=False, reason="not found")
        return VisionResult(success=True, bbox=self.bbox, confidence=0.9)


def _fake_models(monkeypatch, bbox=None):
    monkeypatch.setattr(locator_module, "get_llm", _NoTextMatch)
    monkeypatch.setattr(locator_module, "get_vision_locator", lambda: _FakeVision(bbox))


BUTTON_PAGE = """
<body style="margin:0">
  <button onclick="window.__clicked='save'"
          style="position:absolute;left:100px;top:100px;width:220px;height:60px">保存设置</button>
</body>
"""


async def test_dom_target_comes_back_at_its_centre(page: Page):
    await page.set_content(BUTTON_PAGE)
    result = await get_locator().locate(page, "保存设置", mode="dom-only")
    assert result.success and result.channel == "dom-rule"
    assert result.point == (210, 130) and result.warning == ""
    await click(page, result)
    assert await page.evaluate("window.__clicked") == "save"


async def test_target_below_the_fold_is_scrolled_into_view(page: Page):
    await page.set_content(
        '<body style="margin:0"><div style="height:2400px">tall</div>'
        '<button onclick="window.__clicked=\'bottom\'" style="display:block;margin:40px;width:220px;height:60px">底部按钮</button></body>'
    )
    result = await get_locator().locate(page, "底部按钮", mode="dom-only")
    assert result.success, result.reason
    x, y = result.point
    assert 0 <= x < VIEWPORT[0] and 0 <= y < VIEWPORT[1]  # the guard scrolled it in and used the fresh centre
    await click(page, result)
    assert await page.evaluate("window.__clicked") == "bottom"


async def test_vision_point_on_empty_background_is_refused(page: Page, monkeypatch):
    _fake_models(monkeypatch, bbox=(600, 500, 640, 540))
    await page.set_content(BUTTON_PAGE)
    result = await get_locator().locate(page, "某个控件", mode="gui-plus")
    assert not result.success
    assert "empty page background" in result.reason


async def test_empty_background_is_fine_when_that_is_what_was_asked(page: Page, monkeypatch):
    _fake_models(monkeypatch, bbox=(600, 500, 640, 540))
    await page.set_content(BUTTON_PAGE)
    result = await get_locator().locate(page, "点击页面空白处", mode="gui-plus")
    assert result.success and result.channel == "vision-ground"


async def test_vision_point_on_a_real_element_is_accepted(page: Page, monkeypatch):
    _fake_models(monkeypatch, bbox=(104, 102, 316, 158))
    await page.set_content(BUTTON_PAGE)
    result = await get_locator().locate(page, "某个控件", mode="gui-plus")
    assert result.success and result.channel == "vision-ground"
    await click(page, result)
    assert await page.evaluate("window.__clicked") == "save"


async def test_vision_point_outside_the_viewport_is_refused(page: Page, monkeypatch):
    _fake_models(monkeypatch, bbox=(1300, 900, 1340, 940))
    await page.set_content(BUTTON_PAGE)
    result = await get_locator().locate(page, "某个控件", mode="gui-plus")
    assert not result.success and "outside the viewport" in result.reason


COVERED_PAGE = """
<body style="margin:0">
  <button onclick="window.__clicked='button'"
          style="position:absolute;left:100px;top:100px;width:220px;height:60px">被遮挡的按钮</button>
  <div class="veil shield" onclick="window.__clicked='overlay'"
       style="position:absolute;left:90px;top:90px;width:240px;height:80px;background:rgba(255,0,0,.15)"></div>
</body>
"""


async def test_covered_target_is_still_clicked_but_flagged(page: Page):
    await page.set_content(COVERED_PAGE)
    result = await get_locator().locate(page, "被遮挡的按钮", mode="dom-only")
    assert result.success  # not refused: decorative layers are how custom widgets work
    assert "覆盖" in result.warning and "div.veil" in result.warning
    await click(page, result)
    assert await page.evaluate("window.__clicked") == "overlay"  # and the agent was told to double-check


class _FakeLLM:
    def __init__(self, answer):
        self.config = SimpleNamespace(model="fake-vision")
        self.answer = answer

    async def ask_json(self, messages, images=None, **kwargs):
        return self.answer


async def _ground(monkeypatch, answer):
    async def pixels(llm, model):
        return vision_module.PIXEL_SPACE

    monkeypatch.setattr(vision_module, "get_vision_llm", lambda: _FakeLLM(answer))
    monkeypatch.setattr(vision_module, "coordinate_space", pixels)
    return await vision_module.VisionLocator().ground_bbox(Image.new("RGB", VIEWPORT, "white"), "target")


async def test_grounding_refuses_a_box_whose_centre_left_the_screenshot(monkeypatch):
    result = await _ground(monkeypatch, {"bbox": [0, 520, 3200, 5800], "confidence": 0.9})
    assert not result.success and "outside the screenshot" in result.reason


async def test_grounding_keeps_a_box_inside_the_screenshot(monkeypatch):
    result = await _ground(monkeypatch, {"bbox": [100, 100, 300, 160], "confidence": 0.9})
    assert result.success and result.bbox == (100, 100, 300, 160)
