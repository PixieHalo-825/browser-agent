"""Sandbox provider abstraction.

A sandbox executes LLM-generated Python code with:

- **network isolation** – no outbound access by default,
- **resource limits** – memory / CPU caps,
- **time limits** – hard timeout kills the run,
- **filesystem isolation** – read-only rootfs with a scratch tmpfs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ExecutionResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = -1
    timed_out: bool = False
    provider: str = "unknown"
    details: dict[str, Any] = field(default_factory=dict)

    def render(self) -> str:
        parts = [f"[sandbox:{self.provider}] exit_code={self.exit_code}"]
        if self.timed_out:
            parts.append("(执行超时被强制终止)")
        if self.stdout:
            parts.append(f"--- stdout ---\n{self.stdout[-4000:]}")
        if self.stderr:
            parts.append(f"--- stderr ---\n{self.stderr[-2000:]}")
        return "\n".join(parts)


class SandboxProvider(ABC):
    """Interface for all sandbox backends (Docker, Daytona, local)."""

    name: str = "abstract"

    @abstractmethod
    async def run(self, code: str, timeout: Optional[float] = None) -> ExecutionResult:
        """Execute Python ``code`` and return its result."""

    async def close(self) -> None:
        """Release sandbox resources (default: nothing to do)."""

    async def __aenter__(self) -> "SandboxProvider":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()
