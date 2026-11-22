"""Terminate tool: lets the agent end the task explicitly."""

from __future__ import annotations

from typing import Any

from app.tools.base import BaseTool


class Terminate(BaseTool):
    name = "terminate"
    description = (
        "任务完成时调用。summary 里给出最终答案/结论，"
        "会作为任务的最终结果呈现给用户。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "任务最终结果摘要"},
        },
        "required": ["summary"],
    }

    async def execute(self, summary: str = "", **kwargs: Any) -> str:
        return summary or "任务已结束。"
