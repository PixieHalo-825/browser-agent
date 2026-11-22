"""MCP search server: web search as an independent process.

Run standalone (stdio):  python mcp_servers/search_server.py

Tools:
- web_search – DuckDuckGo text search (no API key required)
"""

from __future__ import annotations

import sys

try:  # mcp >= 2.0: FastMCP was renamed to MCPServer
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP

mcp = FastMCP("search")


@mcp.tool()
def web_search(query: str, max_results: int = 5) -> str:
    """联网搜索，返回标题、链接和摘要。用于查找网址、背景信息、竞品资料。"""
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        return "ERROR: duckduckgo-search 未安装"
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max(1, min(max_results, 10))))
    except Exception as exc:
        return f"ERROR: 搜索失败: {exc}"
    if not results:
        return f"未找到 '{query}' 的搜索结果"
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r.get('title', '')}\n   {r.get('href', '')}\n   {r.get('body', '')[:150]}")
    return "\n".join(lines)


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
