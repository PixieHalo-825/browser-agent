"""Web search providers with automatic failover.

The search tool asks the providers in the order configured (``[search]
providers``) and stops at the first one that returns results, so a single
blocked or rate-limited search engine no longer disables search:

- ``bing``       - cn.bing.com result pages, no key, reachable from mainland China
- ``duckduckgo`` - the ``ddgs`` / ``duckduckgo_search`` package, no key
- ``dashscope``  - Alibaba Cloud Bailian search API, needs ``DASHSCOPE_API_KEY``

Adding a source means subclassing :class:`SearchProvider` and decorating it
with :func:`register_provider`; nothing else has to change.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import html
import logging
import re
import time
import warnings
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import parse_qs, urlencode, urlparse

from app.config import get_config

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
BING_URL = "https://cn.bing.com/search"
DASHSCOPE_SEARCH_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation"

_BING_RESULT = re.compile(r'<li class="b_algo"[^>]*>(.*?)</li>', re.S)
_BING_LINK = re.compile(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_BING_SNIPPET = re.compile(r"<p[^>]*>(.*?)</p>", re.S)
_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")


class SearchProviderError(Exception):
    """A single provider could not deliver results."""


class SearchError(Exception):
    """Every provider failed."""


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str = ""


def _plain(fragment: str) -> str:
    """Strip tags, unescape entities and collapse whitespace."""
    return _WHITESPACE.sub(" ", html.unescape(_TAG.sub("", fragment))).strip()


def resolve_bing_url(url: str) -> str:
    """Unwrap Bing's click-tracking links to the destination URL."""
    parsed = urlparse(url)
    if parsed.hostname and parsed.hostname.endswith("bing.com") and parsed.path.startswith("/ck/a"):
        encoded = parse_qs(parsed.query).get("u", [""])[0]
        if encoded.startswith("a1"):  # "a1" + URL-safe base64 of the destination
            raw = encoded[2:]
            try:
                return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "replace")
            except (ValueError, binascii.Error):
                return url
    return url


def parse_bing_results(page: str, max_results: int) -> list[SearchHit]:
    """Extract organic results from a Bing search results page."""
    hits: list[SearchHit] = []
    for block in _BING_RESULT.findall(page):
        link = _BING_LINK.search(block)
        if not link:
            continue
        snippet = _BING_SNIPPET.search(block)
        hits.append(
            SearchHit(
                title=_plain(link.group(2)),
                url=resolve_bing_url(html.unescape(link.group(1))),
                snippet=_plain(snippet.group(1)) if snippet else "",
            )
        )
        if len(hits) >= max_results:
            break
    return hits


async def _get(url: str, timeout: float) -> str:
    import httpx

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.text


class SearchProvider(ABC):
    name: str = "abstract"

    @property
    def available(self) -> bool:
        """False when the provider cannot run at all (missing key / package)."""
        return True

    @abstractmethod
    async def search(self, query: str, max_results: int) -> list[SearchHit]:
        """Return results or raise SearchProviderError."""


_PROVIDERS: dict[str, type[SearchProvider]] = {}


def register_provider(cls: type[SearchProvider]) -> type[SearchProvider]:
    _PROVIDERS[cls.name] = cls
    return cls


@register_provider
class BingProvider(SearchProvider):
    """cn.bing.com result pages (no key required)."""

    name = "bing"

    async def search(self, query: str, max_results: int) -> list[SearchHit]:
        page = await _get(BING_URL + "?" + urlencode({"q": query, "ensearch": 0}), get_config().search.timeout)
        hits = parse_bing_results(page, max_results)
        if not hits:
            raise SearchProviderError("页面里没有解析到结果（可能触发了验证页或页面结构变了）")
        return hits


def _load_ddgs() -> Callable:
    try:  # the package was renamed upstream; the import announces it with a warning
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                from ddgs import DDGS
            except ImportError:
                from duckduckgo_search import DDGS  # noqa: F401 - older releases
    except ImportError:
        raise
    return DDGS


@register_provider
class DuckDuckGoProvider(SearchProvider):
    """The ddgs / duckduckgo_search package (no key required)."""

    name = "duckduckgo"

    @property
    def available(self) -> bool:
        try:
            _load_ddgs()
        except ImportError:
            return False
        return True

    async def search(self, query: str, max_results: int) -> list[SearchHit]:
        def run() -> list[dict]:
            ddgs_type = _load_ddgs()
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # upstream rename notice
                with ddgs_type() as ddgs:
                    return list(ddgs.text(query, max_results=max_results))

        rows = await asyncio.to_thread(run)
        hits = [SearchHit(r.get("title", ""), r.get("href", ""), r.get("body", "")) for r in rows if r.get("href")]
        if not hits:
            raise SearchProviderError("没有返回结果")
        return hits


@register_provider
class DashScopeSearchProvider(SearchProvider):
    """Alibaba Cloud Bailian search API (``DASHSCOPE_API_KEY``).

    The answer text is cut to a few tokens: only the structured search
    results that come with it are used.
    """

    name = "dashscope"

    @property
    def available(self) -> bool:
        return bool(get_config().search.dashscope_api_key)

    async def search(self, query: str, max_results: int) -> list[SearchHit]:
        import httpx

        payload = {
            "model": "qwen-plus",
            "input": {"messages": [{"role": "user", "content": f"搜索：{query}"}]},
            "parameters": {
                "enable_search": True,
                "result_format": "message",
                "max_tokens": 16,
                "search_options": {"forced_search": True, "enable_source": True, "search_strategy": "standard"},
            },
        }
        headers = {"Authorization": f"Bearer {get_config().search.dashscope_api_key}"}
        async with httpx.AsyncClient(timeout=get_config().search.timeout * 4) as client:
            response = await client.post(DASHSCOPE_SEARCH_URL, headers=headers, json=payload)
        if response.status_code != 200:
            # Surface the API's own code (e.g. "Arrearage"), not just the HTTP status.
            try:
                error = response.json()
                detail = f"{error.get('code', '')}: {str(error.get('message', ''))[:160]}"
            except ValueError:
                detail = response.text[:160]
            raise SearchProviderError(f"HTTP {response.status_code} {detail}")
        data = response.json()
        results = ((data.get("output") or {}).get("search_info") or {}).get("search_results") or []
        hits = [
            SearchHit(str(r.get("title", "")), str(r.get("url", "")), str(r.get("snippet") or ""))
            for r in results
            if r.get("url")
        ]
        if not hits:
            raise SearchProviderError("接口没有返回搜索结果")
        return hits[:max_results]


def format_hits(hits: list[SearchHit]) -> str:
    return "\n".join(
        f"{i}. {hit.title}\n   {hit.url}\n   {hit.snippet[:150]}" for i, hit in enumerate(hits, 1)
    )


class SearchChain:
    """Tries the configured providers in order and returns the first results."""

    def __init__(self, providers: Optional[list[str]] = None):
        self.providers = providers

    async def search(self, query: str, max_results: Optional[int] = None) -> tuple[list[SearchHit], str]:
        """Return (hits, provider name); raise SearchError when all fail."""
        cfg = get_config().search
        wanted = max_results or cfg.max_results
        failures: list[str] = []
        for name in self.providers or cfg.providers:
            provider_type = _PROVIDERS.get(name)
            if provider_type is None:
                failures.append(f"{name}: 未知搜索源")
                continue
            provider = provider_type()
            if not provider.available:
                failures.append(f"{name}: 未配置或依赖缺失")
                continue
            started = time.perf_counter()
            try:
                hits = await provider.search(query, wanted)
            except Exception as exc:  # any provider failure just moves on to the next
                failures.append(f"{name}: {type(exc).__name__}: {str(exc)[:120]}")
                logger.info("search provider %s failed (%s)", name, exc)
                continue
            logger.info("search via %s -> %d hits in %.1fs", name, len(hits), time.perf_counter() - started)
            return hits, name
        raise SearchError(" | ".join(failures))


_chain: Optional[SearchChain] = None


def get_search_chain() -> SearchChain:
    global _chain
    if _chain is None:
        _chain = SearchChain()
    return _chain
