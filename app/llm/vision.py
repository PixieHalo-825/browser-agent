"""Vision channel of the GUI-Plus locator.

Two modes, both backed by an OpenAI-compatible VLM (e.g. qwen-vl-max):

- **Set-of-Mark selection**: DOM candidates are drawn as numbered boxes on the
  screenshot and the model picks the number matching the target description.
- **Free grounding**: no DOM candidates exist (canvas, obfuscated DOM); the
  model returns a bounding box for the target directly.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from PIL import Image, ImageDraw

from app.browser.dom_extractor import DomElement
from app.config import get_config
from app.llm.llm import get_vision_llm

logger = logging.getLogger(__name__)

Box = Tuple[int, int, int, int]  # x1, y1, x2, y2


@dataclass
class VisionResult:
    success: bool
    element_index: Optional[int] = None  # SoM mode: matching candidate number
    bbox: Optional[Box] = None  # grounding mode: box in original screenshot px
    confidence: float = 0.0
    reason: str = ""


def load_screenshot(png_bytes: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png_bytes)).convert("RGB")


def scale_to_width(image: Image.Image, max_width: int) -> Tuple[Image.Image, float]:
    """Downscale so width <= max_width. Returns (image, scale_factor applied)."""
    if image.width <= max_width:
        return image, 1.0
    scale = max_width / image.width
    new_size = (max_width, round(image.height * scale))
    return image.resize(new_size, Image.LANCZOS), scale


def annotate_boxes(
    image: Image.Image,
    boxes: Sequence[Tuple[Box, str]],
) -> Image.Image:
    """Draw numbered Set-of-Mark boxes (semi-transparent fill + label)."""
    annotated = image.copy()
    draw = ImageDraw.Draw(annotated)
    for (x1, y1, x2, y2), label in boxes:
        draw.rectangle([x1, y1, x2, y2], outline=(255, 60, 60), width=3)
        # Label chip in the top-left corner of the box.
        text = str(label)
        text_w = draw.textlength(text)
        chip_h = 18
        draw.rectangle([x1, y1 - chip_h, x1 + text_w + 8, y1], fill=(255, 60, 60))
        draw.text((x1 + 4, y1 - chip_h + 2), text, fill=(255, 255, 255))
    return annotated


_SOM_PROMPT = """You are a GUI grounding expert. The screenshot shows a web page with numbered red boxes marking interactive candidate elements.

Target element to find: {target}

Reply via the submit_result tool with a JSON object:
{{"index": <the number of the box that matches the target>, "confidence": <0.0-1.0>}}

Rules:
- Choose the element a human would most likely mean by the target description.
- If none of the numbered boxes matches, set index to -1 and confidence to 0.0."""

_GROUND_PROMPT = """You are a GUI grounding expert. Locate the described element in the screenshot and return its bounding box.

Target element: {target}

Reply via the submit_result tool with a JSON object:
{{"bbox": [x1, y1, x2, y2], "confidence": <0.0-1.0>}}

Rules:
- Coordinates are pixels from the top-left corner of this image.
- The box must tightly cover the target element.
- If the target is not present, set bbox to [0, 0, 0, 0] and confidence to 0.0."""


class VisionLocator:
    """VLM-based element location (fallback channel of GUI-Plus)."""

    async def select_by_som(
        self,
        screenshot: Image.Image,
        candidates: Sequence[DomElement],
        target: str,
    ) -> VisionResult:
        """SoM mode: pick which numbered candidate matches ``target``."""
        cfg = get_config().vision
        scaled, scale = scale_to_width(screenshot, cfg.max_image_width)
        boxes = [
            (
                (round(e.x * scale), round(e.y * scale), round((e.x + e.w) * scale), round((e.y + e.h) * scale)),
                str(e.index),
            )
            for e in candidates
            if e.w > 2 and e.h > 2
        ]
        if not boxes:
            return VisionResult(success=False, reason="no drawable candidates")
        annotated = annotate_boxes(scaled, boxes)
        llm = get_vision_llm()
        try:
            result = await llm.ask_json(
                [{"role": "user", "content": _SOM_PROMPT.format(target=target)}],
                images=[annotated],
            )
        except Exception as exc:
            logger.warning("Vision SoM call failed: %s", exc)
            return VisionResult(success=False, reason=f"vlm error: {exc}")
        index = int(result.get("index", -1))
        confidence = float(result.get("confidence", 0.0))
        if index < 0 or index >= len(candidates) or confidence < 0.5:
            return VisionResult(
                success=False,
                reason=f"vision rejected (index={index}, conf={confidence:.2f})",
                confidence=confidence,
            )
        return VisionResult(success=True, element_index=index, confidence=confidence)

    async def ground_bbox(self, screenshot: Image.Image, target: str) -> VisionResult:
        """Grounding mode: return a bbox for ``target`` without candidates."""
        cfg = get_config().vision
        scaled, scale = scale_to_width(screenshot, cfg.max_image_width)
        llm = get_vision_llm()
        try:
            result = await llm.ask_json(
                [{"role": "user", "content": _GROUND_PROMPT.format(target=target)}],
                images=[scaled],
            )
        except Exception as exc:
            logger.warning("Vision grounding call failed: %s", exc)
            return VisionResult(success=False, reason=f"vlm error: {exc}")
        raw_box = result.get("bbox") or [0, 0, 0, 0]
        confidence = float(result.get("confidence", 0.0))
        try:
            x1, y1, x2, y2 = [float(v) for v in raw_box]
        except (TypeError, ValueError):
            return VisionResult(success=False, reason=f"bad bbox: {raw_box!r}")
        if x2 <= x1 or y2 <= y1 or confidence < 0.5:
            return VisionResult(
                success=False,
                reason=f"vision rejected (conf={confidence:.2f})",
                confidence=confidence,
            )
        # Map back from the scaled image to original screenshot coordinates.
        inv = 1.0 / scale
        bbox = (
            round(x1 * inv),
            round(y1 * inv),
            round(x2 * inv),
            round(y2 * inv),
        )
        return VisionResult(success=True, bbox=bbox, confidence=confidence)


_vision: Optional[VisionLocator] = None


def get_vision_locator() -> VisionLocator:
    global _vision
    if _vision is None:
        _vision = VisionLocator()
    return _vision
