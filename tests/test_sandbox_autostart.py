"""Docker Desktop on-demand start: the sandbox must not quietly degrade when
the engine is merely stopped. Everything here is faked; no Docker needed."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.sandbox import manager
from app.sandbox.base import ExecutionResult
from app.sandbox.docker_sandbox import find_docker_desktop
from app.sandbox.local_sandbox import LocalSubprocessSandbox

pytestmark = pytest.mark.asyncio


class _FakeDockerSandbox:
    """Probe fails for as long as the (fake) engine is down."""

    name = "docker"
    _image = "browser-agent-sandbox:latest"
    engine_up = False
    probes = 0

    async def run(self, code, timeout=None):
        type(self).probes += 1
        if type(self).engine_up:
            return ExecutionResult(stdout="sandbox-ready\n", exit_code=0, provider="docker")
        return ExecutionResult(
            stdout="", stderr="Error while fetching server API version", exit_code=-1, provider="docker"
        )


@pytest.fixture
def world(monkeypatch):
    """Fakes for the Docker sandbox, the launcher lookup and the engine start."""
    calls: dict = {"start": 0}
    _FakeDockerSandbox.engine_up = False
    _FakeDockerSandbox.probes = 0

    def install(*, installed=True, auto_start=True, start_succeeds=True):
        async def ensure_docker_engine(timeout):
            calls["start"] += 1
            calls["timeout"] = timeout
            if start_succeeds:
                _FakeDockerSandbox.engine_up = True
            return start_succeeds

        monkeypatch.setattr(manager, "DockerSandbox", _FakeDockerSandbox)
        monkeypatch.setattr(manager, "find_docker_desktop", lambda: Path("C:/fake/Docker Desktop.exe") if installed else None)
        monkeypatch.setattr(manager, "ensure_docker_engine", ensure_docker_engine)
        monkeypatch.setattr(
            manager, "get_config",
            lambda: SimpleNamespace(sandbox=SimpleNamespace(
                provider="auto", auto_start_docker=auto_start, docker_start_timeout=7)),
        )

    return SimpleNamespace(install=install, calls=calls)


async def test_running_engine_is_used_directly(world):
    world.install()
    _FakeDockerSandbox.engine_up = True
    sandbox = await manager.create_sandbox()
    assert sandbox.name == "docker"
    assert world.calls["start"] == 0 and _FakeDockerSandbox.probes == 1


async def test_stopped_engine_is_started_then_used(world):
    world.install()
    sandbox = await manager.create_sandbox()
    assert sandbox.name == "docker"  # not the unisolated fallback
    assert world.calls["start"] == 1 and world.calls["timeout"] == 7
    assert _FakeDockerSandbox.probes == 2  # before and after the start


async def test_start_timeout_falls_back_to_local(world):
    world.install(start_succeeds=False)
    sandbox = await manager.create_sandbox()
    assert isinstance(sandbox, LocalSubprocessSandbox)
    assert world.calls["start"] == 1


async def test_auto_start_can_be_switched_off(world):
    world.install(auto_start=False)
    assert isinstance(await manager.create_sandbox(), LocalSubprocessSandbox)
    assert world.calls["start"] == 0


async def test_nothing_is_started_when_docker_desktop_is_not_installed(world):
    world.install(installed=False)
    assert isinstance(await manager.create_sandbox(), LocalSubprocessSandbox)
    assert world.calls["start"] == 0


async def test_forcing_docker_raises_instead_of_degrading(world):
    world.install(start_succeeds=False)
    with pytest.raises(RuntimeError, match="7 秒内未就绪"):
        await manager._create_docker(strict=True)


@pytest.mark.skipif(sys.platform != "win32", reason="Docker Desktop launcher lookup is Windows only")
def test_launcher_lookup_on_this_machine():
    launcher = find_docker_desktop()
    if launcher is None:
        pytest.skip("Docker Desktop is not installed here")
    assert launcher.name == "Docker Desktop.exe" and launcher.is_file()
