"""Async OpenAI-compatible LLM client.

Messages are plain OpenAI-format dicts (``{"role": ..., "content": ...}``)
so the agent layer can pass them straight through without message-class
conversion. Supports tool calling and image inputs (for the vision channel).
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
from typing import Any, Optional, Sequence, Union

from openai import AsyncOpenAI
from PIL import Image

from app.config import LLMConfig, get_config

logger = logging.getLogger(__name__)

# JSON schema handed to the model when we need a structured reply.
JSON_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "submit_result",
        "description": "Submit the structured result.",
        "parameters": {
            "type": "object",
            "properties": {"result": {"type": "object"}},
            "required": ["result"],
        },
    },
}


class LLM:
    """Thin wrapper around an OpenAI-compatible chat endpoint."""

    def __init__(self, config: Optional[LLMConfig] = None):
        self.config = config or get_config().llm
        if not self.config.api_key:
            logger.warning(
                "LLM_API_KEY / llm.api_key 未配置，调用模型将失败。"
                "请复制 config.example.toml 为 config.toml 并填写。"
            )
        self._client = AsyncOpenAI(
            base_url=self.config.base_url,
            api_key=self.config.api_key or "EMPTY",
            timeout=self.config.timeout,
        )

    @property
    def client(self) -> AsyncOpenAI:
        return self._client

    async def ask(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Optional[Sequence[dict[str, Any]]] = None,
        tool_choice: Union[str, dict[str, Any], None] = None,
        **kwargs: Any,
    ) -> Any:
        """Chat completion, optionally with tools. Returns the full response."""
        response = await self._client.chat.completions.create(
            model=self.config.model,
            messages=list(messages),
            tools=list(tools) if tools else None,
            tool_choice=tool_choice if tools else None,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            **kwargs,
        )
        logger.debug("LLM response: %s", response)
        return response

    async def ask_text(self, messages: Sequence[dict[str, Any]], **kwargs: Any) -> str:
        """Chat completion returning plain text content."""
        response = await self.ask(messages, **kwargs)
        return response.choices[0].message.content or ""

    async def ask_json(
        self,
        messages: Sequence[dict[str, Any]],
        images: Optional[Sequence[Union[Image.Image, bytes, str]]] = None,
        **kwargs: Any,
    ) -> Any:
        """Ask the model to return a JSON object (via forced tool call).

        ``images`` (PIL images / PNG bytes / data URLs) are attached to the
        last user message — used by the vision-grounding channel.
        """
        if images:
            messages = _attach_images(messages, images)
        response = await self.ask(
            messages,
            tools=[JSON_TOOL_SCHEMA],
            tool_choice={"type": "function", "function": {"name": "submit_result"}},
        )
        message = response.choices[0].message
        if message.tool_calls:
            raw = message.tool_calls[0].function.arguments
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Failed to parse structured output: %s", raw)
            else:
                # Models are asked to wrap the payload in {"result": ...}; some
                # (e.g. Bailian gui-plus) submit the payload itself.
                if isinstance(parsed, dict):
                    return parsed.get("result", parsed)
                logger.warning("Unexpected structured output: %s", raw)
        # Fallback: try to parse raw content as JSON.
        content = message.content or ""
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            start, end = content.find("{"), content.rfind("}")
            if 0 <= start < end:
                try:
                    return json.loads(content[start : end + 1])
                except json.JSONDecodeError:
                    pass
        raise ValueError(f"Model did not return valid JSON: {content[:200]!r}")

    async def ask_with_images(
        self,
        messages: Sequence[dict[str, Any]],
        images: Sequence[Union[Image.Image, bytes, str]],
        **kwargs: Any,
    ) -> Any:
        """Chat completion with images attached to the last user message.

        ``images`` items may be PIL images, raw PNG bytes, or already-encoded
        data URLs.
        """
        new_messages = _attach_images(messages, images)
        response = await self._client.chat.completions.create(
            model=self.config.model,
            messages=new_messages,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            **kwargs,
        )
        return response


def _to_data_url(image: Union[Image.Image, bytes, str]) -> str:
    if isinstance(image, str):
        return image  # assume already a data URL or http(s) URL
    if isinstance(image, bytes):
        b64 = base64.b64encode(image).decode()
        return f"data:image/png;base64,{b64}"
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    b64 = base64.b64encode(buffer.getvalue()).decode()
    return f"data:image/png;base64,{b64}"


def _attach_images(
    messages: Sequence[dict[str, Any]],
    images: Sequence[Union[Image.Image, bytes, str]],
) -> list[dict[str, Any]]:
    """Return a copy of ``messages`` with images appended to the last user turn."""
    content: list[dict[str, Any]] = []
    last_user = messages[-1]
    if isinstance(last_user.get("content"), str) and last_user["content"]:
        content.append({"type": "text", "text": last_user["content"]})
    elif isinstance(last_user.get("content"), list):
        content.extend(last_user["content"])
    for image in images:
        content.append({"type": "image_url", "image_url": {"url": _to_data_url(image)}})
    return list(messages[:-1]) + [{"role": "user", "content": content}]


class VisionLLM(LLM):
    """LLM bound to the vision model endpoint (may differ from the text one)."""

    def __init__(self, config: Optional[LLMConfig] = None):
        cfg = config or get_config().llm
        vision_cfg = cfg.model_copy()
        if cfg.vision_base_url:
            vision_cfg.base_url = cfg.vision_base_url
        if cfg.vision_api_key:
            vision_cfg.api_key = cfg.vision_api_key
        vision_cfg.model = cfg.vision_model
        super().__init__(vision_cfg)


_llm: Optional[LLM] = None
_vision_llm: Optional[VisionLLM] = None
_active_preset_id: str = ""  # "" means "config.toml [llm] defaults"


def get_llm() -> LLM:
    global _llm
    if _llm is None:
        _llm = LLM()
    return _llm


def get_vision_llm() -> VisionLLM:
    global _vision_llm
    if _vision_llm is None:
        _vision_llm = VisionLLM()
    return _vision_llm


def reset_llm(config: Optional[LLMConfig] = None) -> LLMConfig:
    """Swap the cached LLM instances.

    ``config=None`` drops the caches so the next ``get_llm()`` rebuilds from
    ``config.toml [llm]`` (i.e. back to defaults). Returns the config in effect.
    """
    global _llm, _vision_llm, _active_preset_id
    cfg = config or get_config().llm
    _llm = LLM(cfg)
    _vision_llm = VisionLLM(cfg)
    _active_preset_id = "" if config is None else _active_preset_id
    if config is None:
        logger.info("LLM reset to config.toml defaults (model=%s)", cfg.model)
    return cfg


def build_config_from_preset(preset, base: Optional[LLMConfig] = None) -> LLMConfig:
    """Merge a ModelPreset onto the base [llm] config (empty fields inherit)."""
    from app.config import ModelPreset  # local import: config defines the type

    assert isinstance(preset, ModelPreset)
    merged = (base or get_config().llm).model_copy()
    if preset.base_url:
        merged.base_url = preset.base_url
    key = preset.api_key
    if not key and preset.api_key_env:
        key = os.environ.get(preset.api_key_env, "")
    if key:
        merged.api_key = key
    if preset.model:
        merged.model = preset.model
    if preset.vision_model:
        merged.vision_model = preset.vision_model
    return merged


def apply_model_preset(preset) -> LLMConfig:
    """Switch the active model at runtime (affects every get_llm() caller)."""
    global _active_preset_id
    cfg = build_config_from_preset(preset)
    reset_llm(cfg)
    _active_preset_id = preset.id
    logger.info(
        "Switched model preset '%s': model=%s vision=%s endpoint=%s",
        preset.id, cfg.model, cfg.vision_model, cfg.base_url,
    )
    return cfg


def active_preset_id() -> str:
    return _active_preset_id
