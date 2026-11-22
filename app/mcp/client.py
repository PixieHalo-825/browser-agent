"""MCP stdio client connection.

The official SDK requires ``stdio_client``/``ClientSession`` context managers
to be entered and exited within the *same* task (anyio task-group cancel
scopes), so a manual ``__aenter__`` pattern is not viable. Instead every
connection owns a dedicated lifecycle task that holds the context managers
open for the whole agent run, while tool calls are forwarded to it through a
queue. Calls are serialized per server, which matches how the executor runs
tools.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _flatten_content(result: Any) -> str:
    """Flatten a CallToolResult into a string."""
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    output = "\n".join(parts)
    if getattr(result, "isError", False):
        return f"ERROR: {output or 'tool call failed'}"
    return output


class MCPConnection:
    """A long-lived MCP server connection managed inside its own task.

    ``url`` selects streamable-HTTP transport (containerised servers);
    otherwise a stdio subprocess is launched from ``command``/``args``.
    """

    def __init__(
        self,
        name: str,
        command: str = "",
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        url: str = "",
    ):
        self.name = name
        self.command = command or "python"
        self.args = args or []
        self.env = env or {}
        self.url = url
        self.tools: list[dict[str, Any]] = []
        self._task: Optional[asyncio.Task] = None
        self._requests: asyncio.Queue[Optional[tuple[str, dict[str, Any]]]] = asyncio.Queue()
        self._replies: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    async def connect(self, timeout: float = 30.0) -> list[dict[str, Any]]:
        """Spawn the lifecycle task and wait until the server is ready."""
        if self._task is not None:
            return self.tools
        self._task = asyncio.create_task(self._lifecycle(), name=f"mcp-{self.name}")
        try:
            kind, payload = await asyncio.wait_for(self._replies.get(), timeout=timeout)
        except asyncio.TimeoutError:
            await self.close()
            raise RuntimeError(f"MCP server '{self.name}' did not become ready in {timeout}s")
        if kind == "error":
            await self.close()
            raise RuntimeError(f"MCP server '{self.name}' failed to start: {payload}")
        self.tools = payload
        logger.info("MCP server '%s' ready with %d tools: %s",
                    self.name, len(self.tools), [t["name"] for t in self.tools])
        return self.tools

    async def _lifecycle(self) -> None:
        from mcp import ClientSession

        try:
            if self.url:
                from mcp.client.streamable_http import streamable_http_client

                logger.info("Connecting MCP server '%s' over HTTP: %s", self.name, self.url)
                # streamable_http_client yields (read, write, get_session_id).
                async with streamable_http_client(self.url) as streams:
                    read, write = streams[0], streams[1]
                    await self._session_loop(ClientSession, read, write)
            else:
                from mcp import StdioServerParameters
                from mcp.client.stdio import stdio_client

                resolved_args = [
                    str(Path(a).resolve()) if not a.startswith("-") and Path(a).suffix in (".py", ".js") else a
                    for a in self.args
                ]
                # A bare "python" may resolve to another interpreter (e.g. the
                # WindowsApps stub); pin to the interpreter running the agent.
                command = sys.executable if self.command in ("python", "python3") else self.command
                params = StdioServerParameters(
                    command=command,
                    args=resolved_args,
                    env={**self.env} if self.env else None,
                )
                logger.info("Starting MCP server '%s': %s %s", self.name, command, " ".join(resolved_args))
                async with stdio_client(params) as (read, write):
                    await self._session_loop(ClientSession, read, write)
        except Exception as exc:
            logger.error("MCP server '%s' lifecycle crashed: %s", self.name, exc)
            logger.debug("lifecycle traceback:", exc_info=exc)
            await self._replies.put(("error", exc))
        finally:
            logger.info("MCP server '%s' stopped", self.name)

    async def _session_loop(self, ClientSession, read, write) -> None:
        """Shared session body: initialize, publish tools, serve call requests."""
        async with ClientSession(read, write) as session:
            await session.initialize()
            listing = await session.list_tools()
            tool_defs = []
            for tool in listing.tools:
                # mcp 2.x uses snake_case, 1.x used camelCase.
                schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None)
                tool_defs.append(
                    {
                        "name": tool.name,
                        "description": tool.description or "",
                        "inputSchema": schema or {"type": "object", "properties": {}},
                    }
                )
            await self._replies.put(("ready", tool_defs))
            while True:
                request = await self._requests.get()
                if request is None:
                    break
                tool_name, arguments = request
                try:
                    result = await session.call_tool(tool_name, arguments=arguments or {})
                    await self._replies.put(("result", _flatten_content(result)))
                except Exception as exc:
                    await self._replies.put(("result", f"ERROR: {exc}"))

    async def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None) -> str:
        """Invoke a tool (serialized per connection) and return its text."""
        if self._task is None or self._task.done():
            return f"ERROR: MCP server '{self.name}' is not connected"
        await self._requests.put((tool_name, arguments or {}))
        kind, payload = await self._replies.get()
        if kind == "error":
            return f"ERROR: MCP server '{self.name}' died: {payload}"
        return payload

    async def close(self) -> None:
        if self._task is not None:
            self._requests.put_nowait(None)
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (asyncio.TimeoutError, Exception):  # noqa: B014 - best effort
                self._task.cancel()
        self._task = None
