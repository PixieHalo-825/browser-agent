"""Python code execution tool backed by the isolated sandbox."""

from __future__ import annotations

import logging
from typing import Any

from app.sandbox.manager import run_code
from app.tools.base import BaseTool

logger = logging.getLogger(__name__)


class PythonExecute(BaseTool):
    name = "python_execute"
    description = (
        "在隔离沙箱中执行 Python 代码（网络隔离、资源受限、超时保护）。"
        "用于数据处理、价格计算、格式转换等。代码的 print 输出会作为结果返回。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "要执行的 Python 代码"},
            "timeout": {"type": "number", "description": "可选，超时秒数（默认 30）"},
        },
        "required": ["code"],
    }

    async def execute(self, code: str, timeout: float | None = None, **kwargs: Any) -> str:
        logger.info("Executing python in sandbox (%d chars)", len(code))
        result = await run_code(code, timeout=timeout)
        return result.render()
