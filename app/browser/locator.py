"""GUI-Plus unified locator: the heart of the multimodal interaction design.

Resolution pipeline (channels, tried in order):

1. ``dom-rule``  – deterministic text/attribute matching on the extracted
   interactive-element list (fast, free, no LLM round-trip).
2. ``dom-llm``   – the text LLM picks the best candidate from the numbered
   DOM list (handles fuzzy / synonym descriptions).
3. ``vision-som``– VLM picks a numbered box on a Set-of-Mark annotated
   screenshot (handles misleading DOM text, popup overlays, anti-bot walls).
4. ``vision-ground`` – VLM returns a bounding box directly (handles canvas /
   shadow-obfuscated UI where no DOM candidates exist at all).

Every result records its channel and confidence so the evaluation harness can
report per-channel success rates.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from playwright.async_api import Page

from app.browser.dom_extractor import (
    MARKER_ATTR,
    DomElement,
    extract_interactive_elements,
    format_elements,
)
from app.config import get_config
from app.llm.llm import get_llm
from app.llm.vision import load_screenshot, get_vision_locator

logger = logging.getLogger(__name__)

Point = Tuple[int, int]

# Attribute weights used by the deterministic scorer.
_ATTR_WEIGHTS = (
    ("text", 1.0),
    ("aria_label", 0.95),
    ("placeholder", 0.95),
    ("value", 0.85),
    ("name", 0.80),
    ("id", 0.75),
    ("href", 0.60),
)


@dataclass
class LocateResult:
    success: bool
    channel: str  # dom-rule | dom-llm | vision-som | vision-ground | failed
    element_index: Optional[int] = None
    point: Optional[Point] = None  # viewport coordinates to act on
    confidence: float = 0.0
    reason: str = ""
    candidates: Sequence[DomElement] = ()
    warning: str = ""  # non-fatal note about where the click will land

    def describe(self) -> str:
        if not self.success:
            return f"定位失败 ({self.channel}): {self.reason}"
        text = f"定位成功 [{self.channel}] 置信度={self.confidence:.2f} point={self.point}"
        return text + (f" ⚠ {self.warning}" if self.warning else "")


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def rule_score(element: DomElement, target: str) -> float:
    """Deterministic match score in [0, 1] between an element and a target.

    Coverage semantics (prevents weak partial hits from auto-winning):
    - exact equality            → full attribute weight
    - target inside attr        → strong: the element fully contains what was
                                  asked; only lightly discounted for extra text
    - attr inside target        → weak: the element covers only part of a rich
                                  description — decays with coverage ratio, so
                                  e.g. text '查看价格' vs target '无线耳机 Pro
                                  商品的查看价格按钮' scores low and falls
                                  through to the LLM/vision channels
    """
    t = _normalize(target)
    if not t:
        return 0.0
    best = 0.0
    for attr, weight in _ATTR_WEIGHTS:
        a = _normalize(getattr(element, attr))
        if not a:
            continue
        if a == t:
            return max(best, weight)
        if t in a:
            # Target fully contained in the attribute: strong evidence.
            length_factor = max(0.7, len(t) / len(a))
            best = max(best, (weight - 0.10) * length_factor)
        elif a in t:
            # Attribute covers only part of the target: scale by how much of
            # the target it accounts for (≥60% coverage keeps full score).
            coverage = min(1.0, (len(a) / len(t)) / 0.6)
            best = max(best, (weight - 0.15) * coverage)
    return best


# Clicking empty page background is only legitimate when that was the request.
_BLANK_AREA = re.compile(r"空白|背景|任意位置|blank|background|empty area|anywhere", re.I)

_HIT_TEST_JS = """
([x, y, idx, marker]) => {
  let target = null;
  if (idx !== null && idx !== undefined) {
    target = document.querySelector('[' + marker + '="' + idx + '"]');
    if (target) {
      // Playwright's element click scrolls its target into view first; do the
      // same, so the check looks exactly where the click is going to land.
      const before = target.getBoundingClientRect();
      const inView = before.top >= 0 && before.left >= 0 && before.bottom <= innerHeight && before.right <= innerWidth;
      if (!inView) target.scrollIntoView({ block: "center", inline: "center" });
      const rect = target.getBoundingClientRect();
      x = rect.x + rect.width / 2;
      y = rect.y + rect.height / 2;
    }
  }
  if (!(x >= 0 && y >= 0 && x < innerWidth && y < innerHeight)) {
    return { ok: false, reason: "outside the viewport" };
  }
  const hit = document.elementFromPoint(x, y);
  if (!hit) return { ok: false, reason: "nothing at that point" };
  const tag = hit.tagName.toLowerCase();
  if (tag === "html" || tag === "body") return { ok: false, reason: "empty page background" };
  if (!target) return { ok: true, x: x, y: y };  // coordinates only (canvas and the like)
  const label = tag + (hit.className && typeof hit.className === "string" ? "." + hit.className.trim().split(/\\s+/)[0] : "");
  if (target.getRootNode() !== document) return { ok: true, x: x, y: y };  // shadow DOM: cannot compare
  if (target === hit || target.contains(hit)) return { ok: true, x: x, y: y };
  return { ok: false, reason: "covered", by: label, x: x, y: y };
}
"""


async def hit_test(
    page: Page,
    point: Point,
    element_index: Optional[int],
    description: str = "",
) -> Tuple[bool, str, Optional[Point], str]:
    """Check what a click at ``point`` would really land on.

    Returns (ok, reason, point to use, warning). A DOM target is scrolled into
    view first and its fresh centre becomes the point to use. Three situations
    are refused: the point is outside the viewport, nothing is there, or a
    vision-only point lands on empty page background (unless the description
    asked for exactly that). A DOM target covered by another element is only
    reported: custom widgets often place a decorative layer over the real
    control, and clicking that layer is what makes them work.
    """
    try:
        verdict = await page.evaluate(_HIT_TEST_JS, [point[0], point[1], element_index, MARKER_ATTR])
    except Exception as exc:  # the guard itself must never break locating
        return True, "", None, ""
    if verdict.get("ok"):
        return True, "", (round(verdict["x"]), round(verdict["y"])), ""
    reason = verdict.get("reason", "unknown")
    if reason == "empty page background" and element_index is None and _BLANK_AREA.search(description or ""):
        return True, "", None, ""
    if reason == "covered":
        return True, "", (round(verdict["x"]), round(verdict["y"])), f"点击位置被 <{verdict.get('by')}> 覆盖，请核对点击效果"
    return False, reason, None, ""


class GuiPlusLocator:
    """Dual-channel (DOM text + vision) element locator."""

    async def locate(
        self,
        page: Page,
        description: str,
        mode: str = "gui-plus",  # "gui-plus" | "dom-only"
    ) -> LocateResult:
        result = await self._locate(page, description, mode)
        if not result.success:
            return result
        ok, reason, fresh, warning = await hit_test(page, result.point, result.element_index, description)
        if not ok:
            # A point that cannot be the target is worse than no result: the
            # agent would act on it. Report a miss so it can try differently.
            logger.info("[GUI-Plus] %s result rejected: %s", result.channel, reason)
            return LocateResult(
                success=False,
                channel=result.channel,
                confidence=result.confidence,
                reason=f"{reason}（{result.channel} 给出的位置落在了这里）",
                candidates=result.candidates,
            )
        if fresh is not None:
            result.point = fresh
        result.warning = warning
        return result

    async def _locate(
        self,
        page: Page,
        description: str,
        mode: str = "gui-plus",  # "gui-plus" | "dom-only"
    ) -> LocateResult:
        cfg = get_config()
        elements = await extract_interactive_elements(page)
        threshold = cfg.vision.min_confidence

        # ---- Channel 1a: deterministic rule matching --------------------
        if elements:
            scored = [(rule_score(e, description), e) for e in elements]
            best_score, best = max(scored, key=lambda pair: pair[0])
            if best_score >= threshold:
                logger.debug("dom-rule hit: [%s] score=%.2f", best.index, best_score)
                return LocateResult(
                    success=True,
                    channel="dom-rule",
                    element_index=best.index,
                    point=best.center,
                    confidence=best_score,
                    candidates=elements,
                )

        # ---- Channel 1b: LLM text matching over the DOM list ------------
        if elements:
            llm_pick = await self._llm_match(elements, description)
            if llm_pick is not None:
                idx, confidence = llm_pick
                element = elements[idx]
                return LocateResult(
                    success=True,
                    channel="dom-llm",
                    element_index=idx,
                    point=element.center,
                    confidence=confidence,
                    candidates=elements,
                )

        if mode == "dom-only" or not cfg.vision.enabled:
            return LocateResult(
                success=False,
                channel="failed",
                reason="dom channels exhausted (vision disabled for this run)",
                candidates=elements,
            )

        # ---- Channel 2: Set-of-Mark vision selection --------------------
        screenshot = load_screenshot(await page.screenshot())
        if elements:
            som = await get_vision_locator().select_by_som(screenshot, elements, description)
            if som.success:
                element = elements[som.element_index]
                return LocateResult(
                    success=True,
                    channel="vision-som",
                    element_index=som.element_index,
                    point=element.center,
                    confidence=som.confidence,
                    candidates=elements,
                )
            logger.debug("vision-som miss: %s", som.reason)

        # ---- Channel 3: free-form vision grounding ----------------------
        ground = await get_vision_locator().ground_bbox(screenshot, description)
        if ground.success:
            x1, y1, x2, y2 = ground.bbox
            return LocateResult(
                success=True,
                channel="vision-ground",
                point=((x1 + x2) // 2, (y1 + y2) // 2),
                confidence=ground.confidence,
                candidates=elements,
            )

        return LocateResult(
            success=False,
            channel="failed",
            reason=ground.reason or "all channels failed",
            candidates=elements,
        )

    async def _llm_match(
        self, elements: Sequence[DomElement], description: str
    ) -> Optional[Tuple[int, float]]:
        """Ask the text LLM which numbered DOM element matches ``description``."""
        prompt = (
            "You are locating a web page element for an automation agent.\n"
            f"Target element: {description}\n\n"
            "Candidate interactive elements on the current page:\n"
            f"{format_elements(elements)}\n\n"
            "Reply via the submit_result tool with JSON: "
            '{"index": <best candidate number>, "confidence": <0.0-1.0>}. '
            "If none matches, use index -1 and confidence 0.0."
        )
        try:
            result = await get_llm().ask_json([{"role": "user", "content": prompt}])
        except Exception as exc:
            logger.warning("dom-llm match failed: %s", exc)
            return None
        try:
            index = int(result.get("index", -1))
            confidence = float(result.get("confidence", 0.0))
        except (TypeError, ValueError, AttributeError):
            return None
        if 0 <= index < len(elements) and confidence >= 0.6:
            return index, confidence
        return None


_locator: Optional[GuiPlusLocator] = None


def get_locator() -> GuiPlusLocator:
    global _locator
    if _locator is None:
        _locator = GuiPlusLocator()
    return _locator
