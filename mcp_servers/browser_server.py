"""MCP browser server: exposes browser actions as an independent process.

Run standalone (stdio):  python mcp_servers/browser_server.py

This server demonstrates tool decoupling: the browser stack (Playwright +
GUI-Plus locator) lives in its own process and is hot-pluggable via config.
Note: it owns its own browser instance, so it is most useful when you want
browser access from *other* MCP clients too; by default the agent uses the
in-process browser tools for lower latency.

Tools:
- navigate_and_read – open a URL, return title, text and interactive elements
- screenshot        – capture the current page to the workspace
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

try:  # mcp >= 2.0: FastMCP was renamed to MCPServer
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP

# Allow running both as a module and as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

mcp = FastMCP("browser")

WORKSPACE = Path(__file__).resolve().parent.parent / "workspace"


@mcp.tool()
async def navigate_and_read(url: str) -> str:
    """打开 URL 并返回页面标题、正文文本摘要和可交互元素列表。"""
    from app.browser.browser import get_browser
    from app.browser.dom_extractor import extract_interactive_elements, format_elements

    browser = get_browser()
    page = await browser.start()
    try:
        await page.goto(url, wait_until="domcontentloaded")
    except Exception as exc:
        return f"ERROR: 导航失败: {exc}"
    title = await page.title()
    text = await page.evaluate("() => document.body ? document.body.innerText : ''")
    elements = await extract_interactive_elements(page)
    body = "\n".join(ln.strip() for ln in text.splitlines() if ln.strip())[:2500]
    return f"标题: {title}\n\n正文摘要:\n{body}\n\n可交互元素:\n{format_elements(elements)}"


@mcp.tool()
async def screenshot(name: str = "") -> str:
    """截图当前页面并保存到工作区，返回文件路径。"""
    from app.browser.browser import get_browser

    browser = get_browser()
    page = await browser.start()
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    path = WORKSPACE / f"{name or 'mcp-screenshot'}-{int(time.time())}.png"
    await page.screenshot(path=str(path))
    return f"截图已保存: {path}"


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
