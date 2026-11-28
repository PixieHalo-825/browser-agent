"""Configuration loader.

Reads ``config.toml`` from the project root (falls back to safe defaults when
the file is missing). Secrets such as API keys can also be provided through
environment variables, which take precedence over the file. The LLM key comes
from ``LLM_API_KEY`` or from the variable of the provider ``base_url`` points
at, so keys of several providers never get mixed up.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.toml"


class LLMConfig(BaseModel):
    """Text/vision model settings (OpenAI-compatible endpoints)."""

    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key: str = ""
    model: str = "qwen-max"
    # Vision model used by the GUI-Plus locator fallback channel. It must accept
    # images together with forced tool calls (the structured-output protocol).
    # qwen-vl-max is no longer listed on Bailian; qwen3-vl-plus is.
    vision_base_url: str = ""  # empty -> reuse base_url
    vision_api_key: str = ""  # empty -> reuse api_key
    vision_model: str = "qwen3-vl-plus"
    temperature: float = 0.0
    max_tokens: int = 4096
    timeout: float = 120.0


class ModelPreset(BaseModel):
    """A switchable model profile (declared as [[model_presets]] in config.toml).

    Empty fields inherit from the active ``[llm]`` settings, so a preset on the
    same endpoint only needs ``model``/``vision_model``. A preset pointing at a
    different provider should set ``base_url`` plus ``api_key`` or
    ``api_key_env`` (an environment variable read at switch time).
    """

    id: str
    label: str = ""
    base_url: str = ""
    api_key: str = ""
    api_key_env: str = ""
    model: str = ""
    vision_model: str = ""


class AgentConfig(BaseModel):
    max_plan_steps: int = 10
    max_replans: int = 3
    react_max_iterations: int = 8
    # The same tool call with the same result this many times in one step
    # triggers a hint to the model to change approach.
    repeat_call_limit: int = 3


class BrowserConfig(BaseModel):
    headless: bool = True
    viewport_width: int = 1280
    viewport_height: int = 800
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    locale: str = "zh-CN"
    navigation_timeout_ms: int = 30000
    action_timeout_ms: int = 10000


class DOMConfig(BaseModel):
    """DOM extraction tuning for the first locator channel."""

    max_elements: int = 80  # cap interactive elements sent to the LLM
    max_text_length: int = 60
    include_invisible: bool = False


class VisionConfig(BaseModel):
    """GUI-Plus vision channel settings."""

    enabled: bool = True
    # DOM matches below this score trigger the vision fallback.
    min_confidence: float = 0.55
    max_image_width: int = 1280


class SandboxConfig(BaseModel):
    provider: str = "auto"  # auto | docker | daytona
    timeout: float = 30.0
    memory_limit: str = "512m"
    cpu_limit: float = 1.0
    network_enabled: bool = False
    daytona_api_key: str = ""
    daytona_server_url: str = ""
    daytona_snapshot: str = "python-runner"
    # When Docker Desktop is installed but its engine is not running, start it
    # and wait up to docker_start_timeout seconds instead of degrading.
    auto_start_docker: bool = True
    docker_start_timeout: float = 90.0


class MCPConfig(BaseModel):
    enabled: bool = True
    # Names map to [mcp_servers.<name>] sections: add/remove entries to
    # hot-plug servers without touching agent code.
    servers: list[str] = Field(default_factory=lambda: ["filesystem", "search"])
    request_timeout: float = 60.0


class SearchConfig(BaseModel):
    """Web search: providers are tried in order until one returns results."""

    # bing (cn.bing.com page, no key), duckduckgo (needs reachable Bing/DuckDuckGo),
    # dashscope (Alibaba Cloud Bailian search API, needs DASHSCOPE_API_KEY).
    providers: list[str] = Field(default_factory=lambda: ["bing", "duckduckgo", "dashscope"])
    max_results: int = 5
    timeout: float = 15.0
    dashscope_api_key: str = ""


class WorkspaceConfig(BaseModel):
    root: str = "./workspace"


class AppConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    dom: DOMConfig = Field(default_factory=DOMConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    # Raw [mcp_servers.*] launch definitions (command/args/env).
    mcp_servers: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # Switchable model profiles ([[model_presets]] tables).
    model_presets: list[ModelPreset] = Field(default_factory=list)

    def workspace_root(self) -> Path:
        """Resolve (and create) the sandboxed workspace directory."""
        root = Path(self.workspace.root)
        if not root.is_absolute():
            root = PROJECT_ROOT / root
        root.mkdir(parents=True, exist_ok=True)
        return root


_GENERIC_KEY_ENV = "LLM_API_KEY"
# Dedicated API-key variable of each known OpenAI-compatible provider host.
_PROVIDER_KEY_ENV = {
    "dashscope.aliyuncs.com": "DASHSCOPE_API_KEY",  # 阿里云百炼（默认端点）
    "xiaomimimo.com": "MIMO_API_KEY",
}

_ENV_OVERRIDES = {
    "llm.base_url": "LLM_BASE_URL",
    "llm.model": "LLM_MODEL",
    "llm.vision_model": "VISION_MODEL",
    "search.dashscope_api_key": "DASHSCOPE_API_KEY",
    "sandbox.daytona_api_key": "DAYTONA_API_KEY",
    "sandbox.daytona_server_url": "DAYTONA_SERVER_URL",
}


def api_key_env_for(base_url: str) -> str:
    """Dedicated key variable of the provider ``base_url`` points at.

    Returns ``""`` for unknown providers, which then rely on ``LLM_API_KEY``
    or an explicit ``api_key``.
    """
    host = urlparse(base_url).hostname or ""
    for provider_host, env_name in _PROVIDER_KEY_ENV.items():
        if host == provider_host or host.endswith("." + provider_host):
            return env_name
    return ""


def _env_api_key(llm_section: dict[str, Any]) -> str:
    """API key from the environment for the endpoint the config points at.

    ``LLM_API_KEY`` always wins (endpoint-agnostic, explicit user intent).
    Otherwise only the variable of the *matching* provider is used, so a
    machine holding several providers' keys never sends one provider's key to
    another provider's endpoint.
    """
    if os.environ.get(_GENERIC_KEY_ENV):
        return os.environ[_GENERIC_KEY_ENV]
    env_name = api_key_env_for(llm_section.get("base_url", ""))
    return os.environ.get(env_name, "") if env_name else ""


def _apply_env_overrides(data: dict[str, Any]) -> None:
    for path, env_name in _ENV_OVERRIDES.items():
        value = os.environ.get(env_name)
        if not value:
            continue
        section, key = path.split(".")
        data.setdefault(section, {})[key] = value
    api_key = _env_api_key(data.get("llm", {}))
    if api_key:
        data.setdefault("llm", {})["api_key"] = api_key


def load_config(path: Optional[Path] = None) -> AppConfig:
    """Load configuration from ``config.toml`` (created from example if absent)."""
    config_path = path or CONFIG_PATH
    data: dict[str, Any] = {}
    if config_path.exists():
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
    else:
        example = config_path.with_name("config.example.toml")
        if example.exists():
            with open(example, "rb") as f:
                data = tomllib.load(f)
    _apply_env_overrides(data)
    return AppConfig(**data)


_config: Optional[AppConfig] = None


def get_config() -> AppConfig:
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reset_config() -> None:  # for tests
    global _config
    _config = None
