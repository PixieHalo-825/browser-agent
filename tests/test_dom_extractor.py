"""DOM extractor tests against real Chromium (no LLM required)."""

from __future__ import annotations

import pytest

from app.browser.browser import get_browser
from app.browser.dom_extractor import extract_interactive_elements, format_elements
from eval.benchmark import case_url

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def browser():
    b = get_browser()
    await b.start()
    yield b
    await b.stop()


async def test_icon_toolbar_extracts_buttons_without_text(browser):
    page = await browser.new_page()
    try:
        await page.goto(case_url("icon_toolbar"))
        elements = await extract_interactive_elements(page)
        assert len(elements) == 8, f"expected 8 toolbar buttons, got {len(elements)}"
        # The whole point of this scenario: no textual/ARIA signals at all.
        assert all(not e.text and not e.aria_label and not e.name for e in elements)
        assert all(e.tag == "button" for e in elements)
        # Markers must be sequential and unique.
        indexes = sorted(e.index for e in elements)
        assert indexes == list(range(8))
    finally:
        await page.close()


async def test_canvas_scenario_has_no_dom_candidates(browser):
    page = await browser.new_page()
    try:
        await page.goto(case_url("canvas_shapes"))
        elements = await extract_interactive_elements(page)
        assert elements == [], "canvas scenario must yield zero interactive elements"
        assert format_elements(elements) == "(no interactive elements found)"
    finally:
        await page.close()


async def test_duplicate_labels_finds_both_buttons(browser):
    page = await browser.new_page()
    try:
        await page.goto(case_url("duplicate_labels"))
        elements = await extract_interactive_elements(page)
        price_buttons = [e for e in elements if e.text == "查看价格"]
        assert len(price_buttons) == 2, "both identical buttons must be extracted"
        # Cards are laid out side by side: first card's button is leftmost.
        assert price_buttons[0].x < price_buttons[1].x
    finally:
        await page.close()


async def test_popup_overlay_buttons(browser):
    page = await browser.new_page()
    try:
        await page.goto(case_url("popup_overlay"))
        await page.click('[data-test="claim"]')  # open the dialog
        await page.wait_for_timeout(200)
        elements = await extract_interactive_elements(page)
        # claim + close(SVG, no text) + use
        labels = [e.text for e in elements if e.tag == "button"]
        assert "领取优惠券" in labels
        assert "去使用" in labels
        # The SVG close button carries no text at all.
        close_btn = [e for e in elements if not e.text]
        assert any(e for e in close_btn), "the SVG close button has no DOM text"
    finally:
        await page.close()
