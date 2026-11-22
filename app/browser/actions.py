"""Action execution on top of LocateResult.

DOM-channel results first try a Playwright element click (full actionability
checks); if the handle went stale (dynamic DOM re-render) they fall back to
viewport-coordinate clicks. Vision-channel results act on coordinates directly.
"""

from __future__ import annotations

import logging
from typing import Optional

from playwright.async_api import Page

from app.browser.dom_extractor import find_by_marker
from app.browser.locator import LocateResult

logger = logging.getLogger(__name__)


async def _resolve_point(page: Page, result: LocateResult) -> Optional[tuple[int, int]]:
    """Best-effort refresh of the click point right before acting."""
    if result.element_index is not None and result.candidates:
        try:
            elements = result.candidates
            element = elements[result.element_index]
            box = await page.evaluate(
                """(idx) => {
                    const find = (root) => {
                        for (const el of root.querySelectorAll('*')) {
                            if (el.getAttribute && el.getAttribute('data-ba-idx') === String(idx)) {
                                const r = el.getBoundingClientRect();
                                return {x: r.x, y: r.y, w: r.width, h: r.height};
                            }
                            if (el.shadowRoot) { const hit = find(el.shadowRoot); if (hit) return hit; }
                        }
                        return null;
                    };
                    return find(document);
                }""",
                result.element_index,
            )
            if box:
                return (round(box["x"] + box["w"] / 2), round(box["y"] + box["h"] / 2))
        except Exception as exc:
            logger.debug("point refresh failed, using stale point: %s", exc)
    return result.point


async def click(page: Page, result: LocateResult) -> str:
    """Click the located element."""
    if not result.success or not result.point:
        return f"click failed: {result.reason}"

    # Prefer a real element click for DOM-channel hits.
    if result.element_index is not None:
        handle = await find_by_marker(page, result.element_index)
        if handle:
            try:
                await handle.click(timeout=5000)
                return f"clicked element [{result.element_index}] via {result.channel}"
            except Exception as exc:
                logger.debug("handle click failed (%s); falling back to coordinates", exc)

    point = await _resolve_point(page, result)
    if point:
        await page.mouse.click(point[0], point[1])
        return f"clicked at {point} via {result.channel}"
    return "click failed: no usable coordinates"


async def type_text(page: Page, result: LocateResult, text: str) -> str:
    """Click the located element then type ``text`` into it."""
    if not result.success or not result.point:
        return f"type failed: {result.reason}"

    if result.element_index is not None:
        handle = await find_by_marker(page, result.element_index)
        if handle:
            try:
                await handle.click(timeout=5000)
                await handle.fill(text, timeout=5000)
                return f"typed {text!r} into element [{result.element_index}] via {result.channel}"
            except Exception as exc:
                logger.debug("handle fill failed (%s); falling back to keyboard", exc)

    point = await _resolve_point(page, result)
    if point:
        await page.mouse.click(point[0], point[1])
        await page.keyboard.type(text)
        return f"typed {text!r} at {point} via {result.channel}"
    return "type failed: no usable coordinates"


async def hover(page: Page, result: LocateResult) -> str:
    if not result.success or not result.point:
        return f"hover failed: {result.reason}"
    point = await _resolve_point(page, result)
    if point:
        await page.mouse.move(point[0], point[1])
        return f"hovered {point} via {result.channel}"
    return "hover failed: no usable coordinates"
