"""Sandbox tests. Docker-based isolation is exercised only when the Docker
daemon is reachable; otherwise the local backend is smoke-tested."""

from __future__ import annotations

import pytest

from app.sandbox.local_sandbox import LocalSubprocessSandbox

pytestmark = pytest.mark.asyncio


async def test_local_sandbox_runs_code():
    sandbox = LocalSubprocessSandbox()
    result = await sandbox.run("print(21 * 2)", timeout=20)
    assert "42" in result.stdout
    assert result.exit_code == 0
    assert not result.timed_out


async def test_local_sandbox_timeout():
    sandbox = LocalSubprocessSandbox()
    result = await sandbox.run("import time; time.sleep(60)", timeout=3)
    assert result.timed_out


async def test_docker_sandbox_isolation():
    docker = pytest.importorskip("docker")
    try:
        client = docker.from_env()
        client.ping()
    except Exception as exc:
        pytest.skip(f"Docker daemon not reachable: {exc}")

    from app.sandbox.docker_sandbox import DockerSandbox

    sandbox = DockerSandbox()
    # Network isolation: outbound sockets must fail inside the container.
    result = await sandbox.run(
        "import urllib.request, sys\n"
        "try:\n"
        "    urllib.request.urlopen('https://example.com', timeout=5)\n"
        "    print('NETWORK-REACHABLE')\n"
        "except Exception as e:\n"
        "    print('NETWORK-BLOCKED')\n",
        timeout=60,
    )
    assert "NETWORK-BLOCKED" in result.stdout, "sandbox container must not have network access"
    assert result.exit_code == 0
