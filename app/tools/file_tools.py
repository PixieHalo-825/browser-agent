"""Workspace-restricted file tools.

All paths are confined to the configured workspace directory (path traversal
is rejected), so the agent cannot scribble outside its own sandbox folder.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Union

from app.config import get_config
from app.tools.base import BaseTool

logger = logging.getLogger(__name__)


def _resolve(path: str) -> Path:
    """Resolve ``path`` inside the workspace root, refusing escapes."""
    root = get_config().workspace_root()
    candidate = (root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    if not str(candidate).startswith(str(root.resolve())):
        raise ValueError(f"path escapes workspace root: {path}")
    return candidate


class ReadFile(BaseTool):
    name = "read_file"
    description = "读取工作区内文本文件的内容。路径相对于工作区目录。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径（相对工作区）"},
        },
        "required": ["path"],
    }

    async def execute(self, path: str, **kwargs: Any) -> str:
        try:
            target = _resolve(path)
        except ValueError as exc:
            return f"ERROR: {exc}"
        if not target.exists():
            return f"ERROR: 文件不存在: {path}"
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"ERROR: 读取失败: {exc}"
        return content[:8000]


class WriteFile(BaseTool):
    name = "write_file"
    description = (
        "将文本内容写入工作区文件（覆盖或新建）。用于保存分析结果、"
        "比价报告、竞品清单等。路径相对于工作区目录。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径（相对工作区）"},
            "content": {"type": "string", "description": "要写入的文本内容"},
        },
        "required": ["path", "content"],
    }

    async def execute(self, path: str, content: str, **kwargs: Any) -> str:
        try:
            target = _resolve(path)
        except ValueError as exc:
            return f"ERROR: {exc}"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            return f"ERROR: 写入失败: {exc}"
        return f"已写入 {len(content)} 字符到 {path}"
