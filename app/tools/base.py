"""BaseTool: OpenManus-style tool abstraction.

Every tool declares a name, description, and JSON-schema parameters, and is
exposed to the LLM as an OpenAI function-calling schema. ``execute`` always
returns a string observation for the ReAct loop.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolResponse:
    """Structured execution outcome (string-serialised for the LLM)."""

    output: str = ""
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:  # what the agent sees in tool messages
        if self.error:
            return f"ERROR: {self.error}"
        return self.output


class BaseTool(ABC):
    name: str = "base_tool"
    description: str = "Abstract base tool."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    @abstractmethod
    async def execute(self, **kwargs: Any) -> str:
        """Run the tool and return a string observation."""

    def to_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name!r}>"
