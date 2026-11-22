"""Unified tool registry: local BaseTools + MCP-server tools under one roof.

The executor only talks to this registry; whether a tool runs in-process or in
a separate MCP server process is invisible to it. Adding/removing a server in
``config.toml [mcp] servers`` is all it takes to hot-plug tools.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from app.config import get_config
from app.mcp.client import MCPConnection
from app.tools.base import BaseTool

logger = logging.getLogger(__name__)


class MCPToolAdapter(BaseTool):
    """Adapts an MCP server tool to the local BaseTool interface."""

    def __init__(self, connection: MCPConnection, tool_def: dict[str, Any]):
        self.connection = connection
        self.name = f"mcp_{connection.name}_{tool_def['name']}"
        self.description = f"[MCP:{connection.name}] {tool_def['description']}"
        self.parameters = tool_def["inputSchema"]

    async def execute(self, **kwargs: Any) -> str:
        # Strip the prefix to get the real tool name on the server.
        server_tool = self.name.removeprefix(f"mcp_{self.connection.name}_")
        return await self.connection.call_tool(server_tool, kwargs)


class ToolRegistry:
    def __init__(self):
        self._local: dict[str, BaseTool] = {}
        self._mcp: dict[str, BaseTool] = {}
        self._connections: dict[str, MCPConnection] = {}

    # -- registration ----------------------------------------------------
    def register(self, tool: BaseTool) -> None:
        if tool.name in self.all_names:
            logger.warning("Tool name conflict, overwriting: %s", tool.name)
        self._local[tool.name] = tool

    def register_many(self, tools: list[BaseTool]) -> None:
        for tool in tools:
            self.register(tool)

    # -- MCP lifecycle ----------------------------------------------------
    @property
    def mcp_connected(self) -> bool:
        return bool(self._connections)

    async def connect_mcp_servers(self) -> list[str]:
        """Start all configured MCP servers; returns names of connected ones."""
        cfg = get_config()
        connected: list[str] = []
        if not cfg.mcp.enabled:
            logger.info("MCP disabled in config")
            return connected
        for name in cfg.mcp.servers:
            definition = cfg.mcp_servers.get(name)
            if not definition:
                logger.warning("MCP server '%s' listed but no [mcp_servers.%s] definition", name, name)
                continue
            connection = MCPConnection(
                name=name,
                command=definition.get("command", "python"),
                args=definition.get("args", []),
                env=definition.get("env"),
                url=definition.get("url", ""),  # http transport (e.g. dockerized)
            )
            try:
                await connection.connect()
            except Exception as exc:
                logger.error("Failed to start MCP server '%s': %s", name, exc)
                await connection.close()
                continue
            self._connections[name] = connection
            for tool_def in connection.tools:
                adapter = MCPToolAdapter(connection, tool_def)
                self._mcp[adapter.name] = adapter
            connected.append(name)
        return connected

    async def shutdown(self) -> None:
        for connection in self._connections.values():
            await connection.close()
        self._connections.clear()
        self._mcp.clear()

    # -- lookup / execution -------------------------------------------------
    @property
    def all_names(self) -> list[str]:
        return list(self._local) + list(self._mcp)

    def get(self, name: str) -> Optional[BaseTool]:
        return self._local.get(name) or self._mcp.get(name)

    async def execute(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        tool = self.get(name)
        if tool is None:
            available = ", ".join(self.all_names)
            return f"ERROR: unknown tool '{name}'. Available: {available}"
        try:
            return await tool.execute(**(arguments or {}))
        except TypeError as exc:
            return f"ERROR: bad arguments for {name}: {exc}"
        except Exception as exc:
            logger.exception("Tool %s crashed", name)
            return f"ERROR: tool {name} failed: {exc}"

    def openai_schemas(self) -> list[dict[str, Any]]:
        return [tool.to_openai_schema() for tool in (*self._local.values(), *self._mcp.values())]


_registry: Optional[ToolRegistry] = None


def get_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry
