"""MCP filesystem server: workspace-scoped file operations.

Run standalone (stdio):  python mcp_servers/filesystem_server.py

Tools:
- list_files   – list the workspace tree
- read_file    – read a text file
- write_file   – write a text file
"""

from __future__ import annotations

import sys
from pathlib import Path

try:  # mcp >= 2.0: FastMCP was renamed to MCPServer
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP

WORKSPACE = Path(__file__).resolve().parent.parent / "workspace"
WORKSPACE.mkdir(parents=True, exist_ok=True)

mcp = FastMCP("filesystem")


def _resolve(path: str) -> Path:
    candidate = (WORKSPACE / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    if not str(candidate).startswith(str(WORKSPACE.resolve())):
        raise ValueError(f"path escapes workspace: {path}")
    return candidate


@mcp.tool()
def list_files(subdir: str = "") -> str:
    """列出工作区（或其子目录）下的文件与目录树。"""
    try:
        base = _resolve(subdir) if subdir else WORKSPACE
    except ValueError as exc:
        return f"ERROR: {exc}"
    if not base.exists():
        return f"ERROR: 目录不存在: {subdir}"
    lines = []
    for item in sorted(base.rglob("*")):
        rel = item.relative_to(WORKSPACE)
        lines.append(("[DIR]  " if item.is_dir() else "[FILE] ") + str(rel))
    return "\n".join(lines[:200]) or "(empty)"


@mcp.tool()
def read_file(path: str) -> str:
    """读取工作区内文本文件的内容。"""
    try:
        target = _resolve(path)
    except ValueError as exc:
        return f"ERROR: {exc}"
    if not target.exists():
        return f"ERROR: 文件不存在: {path}"
    return target.read_text(encoding="utf-8", errors="replace")[:8000]


@mcp.tool()
def write_file(path: str, content: str) -> str:
    """将文本内容写入工作区文件（覆盖或新建）。"""
    try:
        target = _resolve(path)
    except ValueError as exc:
        return f"ERROR: {exc}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"已写入 {len(content)} 字符到 {path}"


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
