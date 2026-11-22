"""Daytona cloud sandbox backend.

Requires ``pip install daytona-sdk`` and a Daytona API key (env
``DAYTONA_API_KEY`` / ``DAYTONA_SERVER_URL`` or ``[sandbox]`` config). One
sandbox is created lazily and reused across runs, then deleted on close().
"""

from __future__ import annotations

import logging
from typing import Optional

from app.config import get_config
from app.sandbox.base import ExecutionResult, SandboxProvider

logger = logging.getLogger(__name__)


def daytona_available() -> bool:
    try:
        import daytona_sdk  # noqa: F401
    except ImportError:
        return False
    cfg = get_config().sandbox
    return bool(cfg.daytona_api_key)


class DaytonaSandbox(SandboxProvider):
    name = "daytona"

    def __init__(self):
        self._daytona = None
        self._sandbox = None

    def _ensure_sandbox(self):
        if self._sandbox is not None:
            return self._sandbox
        from daytona_sdk import Daytona, DaytonaConfig, Snapshot  # type: ignore

        cfg = get_config().sandbox
        config = DaytonaConfig(
            api_key=cfg.daytona_api_key,
            server_url=cfg.daytona_server_url or None,
        )
        self._daytona = Daytona(config)
        snapshot = Snapshot(cfg.daytona_snapshot) if cfg.daytona_snapshot else None
        logger.info("Creating Daytona sandbox (snapshot=%s)...", cfg.daytona_snapshot)
        self._sandbox = self._daytona.create(snapshot=snapshot)
        return self._sandbox

    async def run(self, code: str, timeout: Optional[float] = None) -> ExecutionResult:
        cfg = get_config().sandbox
        timeout = timeout or cfg.timeout
        import asyncio

        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._run_sync, code, timeout),
                timeout=timeout + 60,  # creation/round-trip overhead guard
            )
        except asyncio.TimeoutError:
            return ExecutionResult(
                stdout="", stderr="daytona run timed out",
                exit_code=-1, timed_out=True, provider=self.name,
            )
        except Exception as exc:
            return ExecutionResult(stdout="", stderr=f"daytona error: {exc}", exit_code=-1, provider=self.name)

    def _run_sync(self, code: str, timeout: float) -> ExecutionResult:
        sandbox = self._ensure_sandbox()
        response = sandbox.code_run(code, language="python", timeout=timeout)
        return ExecutionResult(
            stdout=(response.stdout or ""),
            stderr=(response.stderr or ""),
            exit_code=(response.exit_code if response.exit_code is not None else 0),
            timed_out=False,
            provider=self.name,
        )

    async def close(self) -> None:
        if self._sandbox is not None and self._daytona is not None:
            try:
                import asyncio

                await asyncio.to_thread(self._daytona.delete, self._sandbox)
            except Exception as exc:  # pragma: no cover - best effort
                logger.warning("Failed to delete Daytona sandbox: %s", exc)
        self._daytona = None
        self._sandbox = None
