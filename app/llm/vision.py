"""Vision channel of the GUI-Plus locator.

Two modes, both backed by an OpenAI-compatible VLM (e.g. qwen3-vl-plus on
Alibaba Cloud Bailian):

- **Set-of-Mark selection**: DOM candidates are drawn as numbered boxes on the
  screenshot and the model picks the number matching the target description.
- **Free grounding**: no DOM candidates exist (canvas, obfuscated DOM); the
  model returns a bounding box for the target directly.
"""

from __future__ import annotations

import io
import logging
import math
from dataclasses import dataclass
from typing import Any, Optional, Sequence, Tuple

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


# --- coordinate space -------------------------------------------------------
# Models disagree on what the numbers in ``bbox`` mean even when the prompt asks
# for pixels: qwen3-vl answers on a 0-1000 grid relative to the image, while
# others (MiMo, gui-plus) answer in image pixels. Rather than hardcoding model
# names, the convention is probed once per model with a synthetic image of known
# geometry. The two possible answers are then far apart:
#   image pixels -> roughly (300, 100, 340, 140)
#   0-1000 grid  -> roughly (750, 500, 850, 700)
_CALIB_SIZE = (400, 200)
_CALIB_BOX = (300, 100, 340, 140)
_CALIB_TOLERANCE = 30  # max distance in px between reported and true centre

PIXEL_SPACE = "pixel"
GRID_SPACE = "grid-1000"
_coordinate_spaces: dict[str, str] = {}  # model name -> detected space


def _calibration_image() -> Image.Image:
    image = Image.new("RGB", _CALIB_SIZE, (255, 255, 255))
    ImageDraw.Draw(image).rectangle(_CALIB_BOX, fill=(0, 0, 0))
    return image


def classify_coordinate_space(box: Sequence[float]) -> str:
    """Which convention a calibration answer uses; ``""`` when neither fits."""
    if len(box) != 4:
        return ""
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    true_cx = (_CALIB_BOX[0] + _CALIB_BOX[2]) / 2
    true_cy = (_CALIB_BOX[1] + _CALIB_BOX[3]) / 2
    width, height = _CALIB_SIZE
    errors = {
        PIXEL_SPACE: math.hypot(cx - true_cx, cy - true_cy),
        GRID_SPACE: math.hypot(cx / 1000 * width - true_cx, cy / 1000 * height - true_cy),
    }
    best = min(errors, key=errors.get)
    return best if errors[best] <= _CALIB_TOLERANCE else ""


def to_pixels(box: Sequence[float], space: str, width: int, height: int) -> tuple[float, float, float, float]:
    """Convert a model-reported box to pixel coordinates of a ``width x height`` image."""
    x1, y1, x2, y2 = box
    if space == GRID_SPACE:
        return (x1 / 1000 * width, y1 / 1000 * height, x2 / 1000 * width, y2 / 1000 * height)
    return (x1, y1, x2, y2)


async def coordinate_space(llm: Any, model: str) -> str:
    """Coordinate convention of ``model`` (probed once per process)."""
    if model in _coordinate_spaces:
        return _coordinate_spaces[model]
    try:
        result = await llm.ask_json(
            [{"role": "user", "content": _GROUND_PROMPT.format(target="the solid black square")}],
            images=[_calibration_image()],
        )
        detected = classify_coordinate_space([float(v) for v in (result.get("bbox") or [])])
    except Exception as exc:
        logger.warning("Coordinate-space probe for %s failed (%s); assuming pixels", model, exc)
        return PIXEL_SPACE  # transient failure: do not cache
    space = detected or PIXEL_SPACE
    if not detected:
        logger.warning("Could not determine the coordinate space of %s (answer: %s); assuming pixels", model, result)
    _coordinate_spaces[model] = space
    logger.info("Coordinate space of %s: %s", model, space)
    return space


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
        # Bring the reported box into pixels of the *sent* image (models differ
        # in coordinate convention, see coordinate_space), then map it back to
        # the original screenshot.
        space = await coordinate_space(llm, llm.config.model)
        x1, y1, x2, y2 = to_pixels((x1, y1, x2, y2), space, scaled.width, scaled.height)
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
