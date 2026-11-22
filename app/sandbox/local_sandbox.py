"""Un-isolated local fallback (subprocess + timeout + temp dir).

WARNING: this backend provides NO network or filesystem isolation. It exists
so the agent can still run in demo environments without Docker/Daytona, and
must be treated as a last resort. The manager only selects it with a loud
warning.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
from pathlib import Path

from app.sandbox.base import ExecutionResult, SandboxProvider

logger = logging.getLogger(__name__)


class LocalSubprocessSandbox(SandboxProvider):
    name = "local"

    async def run(self, code: str, timeout: float | None = None) -> ExecutionResult:
        timeout = timeout or 30.0
        with tempfile.TemporaryDirectory(prefix="ba-sandbox-", ignore_cleanup_errors=True) as tmp:
            script = Path(tmp) / "script.py"
            script.write_text(code, encoding="utf-8")
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-I", str(script),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=tmp,
                )
            except OSError as exc:
                return ExecutionResult(stdout="", stderr=f"spawn failed: {exc}", exit_code=-1, provider=self.name)
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await proc.wait()  # reap the process so Windows releases handles
                except Exception:
                    pass
                return ExecutionResult(
                    stdout="", stderr=f"execution exceeded {timeout}s and was killed",
                    exit_code=-1, timed_out=True, provider=self.name,
                )
        return ExecutionResult(
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            exit_code=proc.returncode or 0,
            provider=self.name,
            details={"warning": "un-isolated local fallback"},
        )
