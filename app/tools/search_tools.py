"""Web search tool backed by the failover chain (see search_providers.py)."""

from __future__ import annotations

import logging
from typing import Any

from app.tools.base import BaseTool
from app.tools.search_providers import SearchError, format_hits, get_search_chain

logger = logging.getLogger(__name__)


class WebSearch(BaseTool):
    name = "web_search"
    description = (
        "联网搜索。返回标题、摘要和链接。多个搜索源自动切换。"
        "用于查找网站地址、背景信息、竞品资料。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
        },
        "required": ["query"],
    }

    async def execute(self, query: str, **kwargs: Any) -> str:
        try:
            hits, _provider = await get_search_chain().search(query)
        except SearchError as exc:
            return f"搜索失败（所有搜索源均不可用）: {exc}"
        return format_hits(hits)
