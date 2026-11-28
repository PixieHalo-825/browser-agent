"""Sandbox tests. Docker-based isolation is exercised only when the Docker
daemon is reachable; otherwise the local backend is smoke-tested."""

from __future__ import annotations

import pytest

from app.config import get_config
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


@pytest.fixture
def docker_sandbox():
    """DockerSandbox against the local daemon; skips when Docker is absent."""
    docker = pytest.importorskip("docker")
    try:
        client = docker.from_env()
        client.ping()
    except Exception as exc:
        pytest.skip(f"Docker daemon not reachable: {exc}")
    from app.sandbox.docker_sandbox import DockerSandbox

    return DockerSandbox()


async def test_docker_sandbox_isolation(docker_sandbox):
    # Network isolation: outbound sockets must fail inside the container.
    result = await docker_sandbox.run(
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


async def test_docker_sandbox_streams_are_separated(docker_sandbox):
    result = await docker_sandbox.run(
        "import sys\n"
        "print('to-stdout')\n"
        "print('to-stderr', file=sys.stderr)\n",
        timeout=30,
    )
    assert result.exit_code == 0
    assert "to-stdout" in result.stdout and "to-stderr" not in result.stdout
    assert "to-stderr" in result.stderr and "to-stdout" not in result.stderr


async def test_docker_sandbox_traceback_goes_to_stderr(docker_sandbox):
    result = await docker_sandbox.run("raise ValueError('boom')", timeout=30)
    assert result.exit_code != 0
    assert result.stdout == ""
    assert "ValueError: boom" in result.stderr


async def test_docker_sandbox_read_only_rootfs(docker_sandbox):
    result = await docker_sandbox.run(
        "try:\n"
        "    open('/usr/evil.txt', 'w').write('x')\n"
        "    print('ROOTFS-WRITABLE')\n"
        "except OSError:\n"
        "    print('ROOTFS-READ-ONLY')\n"
        "open('/tmp/scratch.txt', 'w').write('ok')\n"
        "print('TMP-WRITABLE')\n",
        timeout=30,
    )
    assert "ROOTFS-READ-ONLY" in result.stdout, "root filesystem must be immutable"
    assert "TMP-WRITABLE" in result.stdout, "scratch space in /tmp must stay writable"


async def test_docker_sandbox_memory_limit(docker_sandbox):
    # Ask for 1.5x the configured cap: must fail even though Docker would hand
    # out matching swap unless memswap_limit pins the limit shut.
    limit_mb = int(float(get_config().sandbox.memory_limit.lower().rstrip("kmg")) or 512)
    result = await docker_sandbox.run(
        f"b = bytearray({limit_mb * 3 // 2} * 1024 * 1024)\nprint('ALLOCATED')\n",
        timeout=60,
    )
    assert "ALLOCATED" not in result.stdout
    assert result.exit_code != 0


async def test_docker_sandbox_timeout_removes_container(docker_sandbox):
    import docker

    client = docker.from_env()
    before = {c.id for c in client.containers.list(all=True)}
    result = await docker_sandbox.run("import time; time.sleep(60)", timeout=3)
    assert result.timed_out
    after = {c.id for c in client.containers.list(all=True)}
    assert after == before, "a timed-out run must not leave its container behind"
