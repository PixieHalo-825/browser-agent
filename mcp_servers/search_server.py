"""MCP search server: web search as an independent process.

Run standalone (stdio):  python mcp_servers/search_server.py

Tools:
- web_search – web search through the failover chain in
  app/tools/search_providers.py (Bing -> DuckDuckGo -> Bailian search API)
"""

from __future__ import annotations

import sys
from pathlib import Path

try:  # mcp >= 2.0: FastMCP was renamed to MCPServer
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP

# Allow running both as a module and as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.tools.search_providers import SearchError, format_hits, get_search_chain  # noqa: E402

mcp = FastMCP("search")


@mcp.tool()
async def web_search(query: str, max_results: int = 5) -> str:
    """联网搜索，返回标题、链接和摘要。多个搜索源自动切换。用于查找网址、背景信息、竞品资料。"""
    try:
        hits, _provider = await get_search_chain().search(query, max(1, min(max_results, 10)))
    except SearchError as exc:
        return f"ERROR: 搜索失败: {exc}"
    return format_hits(hits)


def _main() -> int:
    """stdio by default; set MCP_TRANSPORT=http MCP_PORT=8xxx for containers."""
    import os

    if os.environ.get("MCP_TRANSPORT") == "http":
        mcp.run(
            transport="streamable-http",
            host="0.0.0.0",
            port=int(os.environ.get("MCP_PORT", "8000")),
        )
        return 0
    mcp.run()  # stdio
    return 0


if __name__ == "__main__":
    sys.exit(_main())
