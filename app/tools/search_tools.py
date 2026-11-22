"""Web search tool (no API key required via DuckDuckGo)."""

from __future__ import annotations

import logging
from typing import Any

from app.config import get_config
from app.tools.base import BaseTool

logger = logging.getLogger(__name__)


class WebSearch(BaseTool):
    name = "web_search"
    description = "联网搜索。返回标题、摘要和链接。用于查找网站地址、背景信息、竞品资料。"
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
        },
        "required": ["query"],
    }

    async def execute(self, query: str, **kwargs: Any) -> str:
        cfg = get_config().search
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            return "ERROR: duckduckgo-search 未安装，请 pip install duckduckgo-search"

        try:
            # duckduckgo_search's public API is sync; run it in a thread via
            # the sync client (fast enough for a handful of results).
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=cfg.max_results))
        except Exception as exc:
            return f"搜索失败: {exc}"

        if not results:
            return f"未找到 '{query}' 的搜索结果"
        lines = []
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r.get('title', '')}\n   {r.get('href', '')}\n   {r.get('body', '')[:150]}")
        return "\n".join(lines)
