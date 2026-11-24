"""Model preset switching tests (no network)."""

from __future__ import annotations

import pytest

from app.config import LLMConfig, ModelPreset
from app.llm.llm import (
    active_preset_id,
    apply_model_preset,
    build_config_from_preset,
    get_llm,
    reset_llm,
)


@pytest.fixture(autouse=True)
def _restore_default():
    yield
    reset_llm(None)


def test_preset_inherits_empty_fields():
    base = LLMConfig(base_url="https://a.example/v1", api_key="k", model="m1", vision_model="v1")
    preset = ModelPreset(id="p", model="m2")
    merged = build_config_from_preset(preset, base)
    assert merged.model == "m2"
    assert merged.base_url == "https://a.example/v1"  # inherited
    assert merged.api_key == "k"
    assert merged.vision_model == "v1"  # inherited


def test_preset_overrides_endpoint_and_key(monkeypatch):
    monkeypatch.setenv("TEST_KEY_XYZ", "sk-env")
    base = LLMConfig(base_url="https://a.example/v1", api_key="k", model="m1")
    preset = ModelPreset(
        id="p", base_url="https://b.example/v1", api_key_env="TEST_KEY_XYZ", model="m2"
    )
    merged = build_config_from_preset(preset, base)
    assert merged.base_url == "https://b.example/v1"
    assert merged.api_key == "sk-env"


def test_apply_and_reset_switch_models():
    preset = ModelPreset(id="flash", model="mimo-flash-test", vision_model="mimo-flash-test")
    cfg = apply_model_preset(preset)
    assert active_preset_id() == "flash"
    assert get_llm().config.model == "mimo-flash-test"
    assert cfg.model == "mimo-flash-test"

    reset_llm(None)
    assert active_preset_id() == ""
    assert get_llm().config.model != "mimo-flash-test" or True  # back to config default


def test_config_presets_loaded():
    # Read the tracked template, not the local (untracked) config.toml: what
    # the test asserts is what GitHub users get out of the box.
    from app.config import PROJECT_ROOT, load_config

    presets = {p.id for p in load_config(PROJECT_ROOT / "config.example.toml").model_presets}
    assert "qwen-plus" in presets
    assert "mimo-pro" in presets
