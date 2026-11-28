"""Local Docker sandbox.

Each run gets a fresh container from a slim Python image with:

- ``network_mode="none"``      – no network access,
- ``mem_limit`` / ``nano_cpus``– memory and CPU caps,
- ``read_only=True`` + tmpfs   – immutable root filesystem, scratch space in /tmp,
- hard timeout                – container is killed and removed.

Code is passed base64-encoded on the command line to avoid quoting issues.
The docker SDK is synchronous, so all calls are wrapped in ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from app.config import get_config
from app.sandbox.base import ExecutionResult, SandboxProvider

logger = logging.getLogger(__name__)

PREFERRED_IMAGES = ("browser-agent-sandbox:latest", "python:3.11-slim")

DOCKER_DESKTOP_EXE = "Docker Desktop.exe"


def find_docker_desktop() -> Optional[Path]:
    """Path of the Docker Desktop launcher, or None where it is not installed."""
    if sys.platform != "win32":
        return None
    roots = []
    if os.environ.get("ProgramFiles"):  # machine-wide install
        roots.append(Path(os.environ["ProgramFiles"]) / "Docker" / "Docker")
    if os.environ.get("LOCALAPPDATA"):  # per-user install
        roots.append(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "DockerDesktop")
    for root in roots:
        launcher = root / DOCKER_DESKTOP_EXE
        if launcher.is_file():
            return launcher
    return None


def docker_engine_running() -> bool:
    """True when the local Docker engine answers a ping."""
    try:
        import docker

        return bool(docker.from_env().ping())
    except Exception:
        return False


def launch_docker_desktop(launcher: Optional[Path] = None) -> bool:
    """Start Docker Desktop in the background. False when it is not installed."""
    launcher = launcher or find_docker_desktop()
    if launcher is None:
        return False
    subprocess.Popen(
        [str(launcher)],
        cwd=str(launcher.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=(subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        if sys.platform == "win32"
        else 0,
    )
    logger.info("Starting Docker Desktop: %s", launcher)
    return True


async def wait_for_engine(timeout: float, poll: float = 2.0) -> bool:
    """Wait until the Docker engine answers, for at most ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            # A half-started engine can accept the pipe and still not answer,
            # so the ping itself is bounded too.
            if await asyncio.wait_for(asyncio.to_thread(docker_engine_running), timeout=10):
                return True
        except asyncio.TimeoutError:
            pass
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(poll)


async def ensure_docker_engine(timeout: float) -> bool:
    """Bring the engine up when Docker Desktop is installed but not running."""
    if await asyncio.to_thread(docker_engine_running):
        return True
    if not launch_docker_desktop():
        return False
    return await wait_for_engine(timeout)


class DockerSandbox(SandboxProvider):
    name = "docker"

    def __init__(self):
        self._client = None
        self._image: Optional[str] = None

    def _ensure_client(self):
        if self._client is None:
            import docker
            from docker.errors import DockerException

            try:
                self._client = docker.from_env()
                self._client.ping()
            except DockerException as exc:
                raise RuntimeError(
                    "Docker 不可用（未安装或 Docker Desktop 未运行）: " f"{exc}"
                ) from exc
        return self._client

    def _pick_image(self) -> str:
        if self._image:
            return self._image
        client = self._ensure_client()
        images = {tag for image in client.images.list() for tag in image.tags}
        for candidate in PREFERRED_IMAGES:
            if candidate in images:
                self._image = candidate
                return candidate
        # Fall back to the first available slim python image, else the default
        # (docker will pull it on first use).
        for tag in images:
            if tag.startswith("python:3.1"):
                self._image = tag
                return tag
        self._image = PREFERRED_IMAGES[-1]
        return self._image

    async def run(self, code: str, timeout: Optional[float] = None) -> ExecutionResult:
        cfg = get_config().sandbox
        timeout = timeout or cfg.timeout
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._run_sync, code, timeout, cfg),
                timeout=timeout + 30,  # docker overhead guard
            )
        except asyncio.TimeoutError:
            return ExecutionResult(
                stdout="", stderr="sandbox runner itself timed out",
                exit_code=-1, timed_out=True, provider=self.name,
            )
        except RuntimeError as exc:
            return ExecutionResult(stdout="", stderr=str(exc), exit_code=-1, provider=self.name)

    def _run_sync(self, code: str, timeout: float, cfg) -> ExecutionResult:
        client = self._ensure_client()
        image = self._pick_image()
        encoded = base64.b64encode(code.encode("utf-8")).decode("ascii")
        command = ["python", "-c", f"import base64;exec(base64.b64decode('{encoded}').decode('utf-8'))"]

        container = client.containers.run(
            image,
            command,
            detach=True,
            network_mode="none",
            mem_limit=cfg.memory_limit,
            # Without this Docker grants swap equal to the limit, doubling what
            # the code may actually consume; equal values mean "no swap".
            memswap_limit=cfg.memory_limit,
            nano_cpus=int(cfg.cpu_limit * 1e9),
            read_only=True,
            tmpfs={"/tmp": "size=64m"},
            environment={"PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"},
        )
        timed_out = False
        exit_code = -1
        try:
            result = container.wait(timeout=timeout)
            exit_code = result.get("StatusCode", -1)
        except Exception as exc:  # requests timeout / API error => kill it
            logger.warning("Docker wait failed (%s); killing container", exc)
            timed_out = True
            try:
                container.kill()
                container.wait(timeout=10)
            except Exception:  # pragma: no cover - best effort
                pass
        try:
            # Fetched separately: requesting both streams at once interleaves
            # them, which would bury tracebacks (stderr) in the stdout stream.
            stdout = container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
            stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")
        except Exception:
            stdout = stderr = ""
        try:
            container.remove(force=True)
        except Exception:  # pragma: no cover
            pass
        return ExecutionResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            timed_out=timed_out,
            provider=self.name,
            details={"image": image},
        )
