"""Sandbox factory with capability-based selection and graceful degradation.

Selection order (``provider = "auto"``):

1. Daytona cloud sandbox – if ``daytona-sdk`` is installed AND an API key is set,
2. local Docker sandbox  – if the Docker engine answers; when Docker Desktop is
   installed but stopped it is started and waited for (``auto_start_docker``),
3. local subprocess      – last resort, prints an explicit isolation warning.

Explicit ``provider`` values short-circuit the chain.
"""

from __future__ import annotations

import logging
from typing import Optional

from app.config import get_config
from app.sandbox.base import ExecutionResult, SandboxProvider
from app.sandbox.docker_sandbox import (
    DockerSandbox,
    ensure_docker_engine,
    find_docker_desktop,
)
from app.sandbox.local_sandbox import LocalSubprocessSandbox

logger = logging.getLogger(__name__)


async def create_sandbox() -> SandboxProvider:
    cfg = get_config().sandbox
    provider = cfg.provider.lower()

    if provider == "daytona":
        return _create_daytona(strict=True)
    if provider == "docker":
        return await _create_docker(strict=True)
    if provider == "local":
        logger.warning("沙箱 provider=local：代码将在无隔离的本地子进程中执行！")
        return LocalSubprocessSandbox()

    # auto: try daytona -> docker -> local
    if _daytona_ready():
        return _create_daytona(strict=False)
    try:
        return await _create_docker(strict=False)
    except Exception as exc:
        logger.warning(
            "Docker 沙箱不可用（%s），降级为本地子进程沙箱（无网络/文件系统隔离）。"
            "安装并启动 Docker Desktop 或配置 DAYTONA_API_KEY 可恢复完整隔离。",
            exc,
        )
        return LocalSubprocessSandbox()


def _daytona_ready() -> bool:
    try:
        from app.sandbox.daytona_sandbox import daytona_available

        return daytona_available()
    except Exception:
        return False


def _create_daytona(strict: bool) -> SandboxProvider:
    from app.sandbox.daytona_sandbox import DaytonaSandbox

    if strict and not _daytona_ready():
        raise RuntimeError("provider=daytona 但 daytona-sdk 未安装或缺少 API key")
    logger.info("使用 Daytona 云沙箱")
    return DaytonaSandbox()


async def _create_docker(strict: bool) -> SandboxProvider:
    """Return a probed Docker sandbox; raise when Docker cannot be used.

    DockerSandbox.run() reports daemon failures inside ExecutionResult rather
    than raising, so the probe inspects the result. When the engine is merely
    not running, Docker Desktop is started first instead of degrading.
    """
    sandbox = DockerSandbox()
    reason = await _probe(sandbox)
    if reason is None:
        logger.info("使用本地 Docker 沙箱 (镜像: %s)", sandbox._image)
        return sandbox

    cfg = get_config().sandbox
    if cfg.auto_start_docker and find_docker_desktop() is not None:
        logger.warning("Docker 引擎未响应（%s），正在启动 Docker Desktop，最多等待 %.0f 秒…",
                       reason, cfg.docker_start_timeout)
        if await ensure_docker_engine(cfg.docker_start_timeout):
            reason = await _probe(sandbox)
            if reason is None:
                logger.info("Docker Desktop 已启动，使用 Docker 沙箱 (镜像: %s)", sandbox._image)
                return sandbox
            reason = f"引擎启动后探测仍失败: {reason}"
        else:
            reason = f"Docker Desktop 在 {cfg.docker_start_timeout:.0f} 秒内未就绪"

    if strict:
        raise RuntimeError(f"provider=docker 但 Docker 不可用: {reason}")
    raise RuntimeError(reason)


async def _probe(sandbox: DockerSandbox) -> Optional[str]:
    """None when a container ran fine, otherwise why it did not."""
    probe = await sandbox.run("print('sandbox-ready')", timeout=15)
    if probe.exit_code == 0 and "sandbox-ready" in probe.stdout:
        return None
    return probe.stderr or probe.stdout.strip() or f"exit_code={probe.exit_code}"


_sandbox: Optional[SandboxProvider] = None


async def get_sandbox() -> SandboxProvider:
    """Get (and cache) the active sandbox provider instance."""
    global _sandbox
    if _sandbox is None:
        _sandbox = await create_sandbox()
    return _sandbox


async def run_code(code: str, timeout: Optional[float] = None) -> ExecutionResult:
    sandbox = await get_sandbox()
    return await sandbox.run(code, timeout=timeout)


async def shutdown_sandbox() -> None:
    global _sandbox
    if _sandbox is not None:
        await _sandbox.close()
        _sandbox = None
