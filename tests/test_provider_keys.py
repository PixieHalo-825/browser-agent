"""Per-endpoint API key resolution and coordinate-space tests (no network)."""

from __future__ import annotations

from app.config import _env_api_key, api_key_env_for
from app.llm.vision import (
    GRID_SPACE,
    PIXEL_SPACE,
    classify_coordinate_space,
    to_pixels,
)


class TestApiKeyEnvFor:
    def test_bailian(self):
        assert api_key_env_for("https://dashscope.aliyuncs.com/compatible-mode/v1") == "DASHSCOPE_API_KEY"

    def test_mimo(self):
        assert api_key_env_for("https://token-plan-cn.xiaomimimo.com/v1") == "MIMO_API_KEY"

    def test_unknown_provider(self):
        assert api_key_env_for("https://api.deepseek.com/v1") == ""

    def test_subdomain(self):
        assert api_key_env_for("https://api.xiaomimimo.com/v1") == "MIMO_API_KEY"


class TestEnvApiKey:
    def test_generic_key_wins(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "generic")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "dash")
        section = {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"}
        assert _env_api_key(section) == "generic"

    def test_provider_key_for_its_own_endpoint(self, monkeypatch):
        monkeypatch.setenv("DASHSCOPE_API_KEY", "dash")
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        section = {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"}
        assert _env_api_key(section) == "dash"

    def test_provider_key_not_sent_to_other_endpoint(self, monkeypatch):
        monkeypatch.setenv("DASHSCOPE_API_KEY", "dash")
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        monkeypatch.delenv("MIMO_API_KEY", raising=False)
        section = {"base_url": "https://api.deepseek.com/v1"}
        assert _env_api_key(section) == ""

    def test_no_keys(self, monkeypatch):
        for name in ("LLM_API_KEY", "DASHSCOPE_API_KEY", "MIMO_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        section = {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"}
        assert _env_api_key(section) == ""


class TestCoordinateSpace:
    # Calibration geometry: 400x200 image, box at (300,100)-(340,140).
    def test_pixel_answer(self):
        assert classify_coordinate_space([300, 100, 340, 140]) == PIXEL_SPACE

    def test_grid_answer(self):
        # The same box expressed on the 0-1000 grid: (750,500,850,700).
        assert classify_coordinate_space([750, 500, 850, 700]) == GRID_SPACE

    def test_garbage_answer(self):
        assert classify_coordinate_space([0, 0, 10, 10]) == ""
        assert classify_coordinate_space([1, 2, 3]) == ""

    def test_to_pixels_passthrough(self):
        assert to_pixels((10, 20, 30, 40), PIXEL_SPACE, 1280, 800) == (10, 20, 30, 40)

    def test_to_pixels_grid(self):
        # 0-1000 grid on a 1280x800 image.
        assert to_pixels((500, 250, 1000, 750), GRID_SPACE, 1280, 800) == (640.0, 200.0, 1280.0, 600.0)
