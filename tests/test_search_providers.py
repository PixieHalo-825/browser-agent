"""Search providers: result parsing and failover. No network unless the
environment variable BA_LIVE_SEARCH is set."""

from __future__ import annotations

import base64
import os
from types import SimpleNamespace

import pytest

from app.tools import search_providers as sp
from app.tools.search_tools import WebSearch

pytestmark = pytest.mark.asyncio

WRAPPED_TARGET = "https://example.org/a"
WRAPPED_URL = "https://www.bing.com/ck/a?!&amp;p=x&amp;u=a1" + base64.urlsafe_b64encode(
    WRAPPED_TARGET.encode()
).decode().rstrip("=")

BING_PAGE = f"""
<html><body><ol id="b_results">
<li class="b_algo"><h2><a href="https://playwright.dev/" h="ID">Playwright <b>docs</b></a></h2>
  <p>Reliable &amp; end-to-end testing.   Cross-browser.</p></li>
<li class="b_algo"><h2><a href="{WRAPPED_URL}">Wrapped link</a></h2><p>Snippet two</p></li>
<li class="b_algo"><h2><a href="https://third.example/page">Third</a></h2></li>
<li class="b_algo"><div>no link in this one</div></li>
</ol></body></html>
"""


class TestBingParsing:
    def test_extracts_title_url_and_snippet(self):
        hits = sp.parse_bing_results(BING_PAGE, 10)
        assert [hit.url for hit in hits] == ["https://playwright.dev/", WRAPPED_TARGET, "https://third.example/page"]
        assert hits[0].title == "Playwright docs"  # tags stripped
        assert hits[0].snippet == "Reliable & end-to-end testing. Cross-browser."  # entities, whitespace

    def test_respects_the_limit(self):
        assert len(sp.parse_bing_results(BING_PAGE, 2)) == 2

    def test_nothing_to_parse(self):
        assert sp.parse_bing_results("<html>verify you are human</html>", 5) == []

    def test_plain_urls_are_left_alone(self):
        assert sp.resolve_bing_url("https://playwright.dev/") == "https://playwright.dev/"

    def test_malformed_tracking_link_falls_back_to_the_original(self):
        url = "https://www.bing.com/ck/a?u=a1A"  # one data character: not valid base64
        assert sp.resolve_bing_url(url) == url


class _FakeProvider:
    """Duck-typed provider: 'ok', 'raise', 'unavailable'."""

    def __init__(self, name: str, outcome: str):
        self.name, self.outcome, self.calls = name, outcome, []

    @property
    def available(self) -> bool:
        return self.outcome != "unavailable"

    async def search(self, query: str, max_results: int):
        self.calls.append((query, max_results))
        if self.outcome == "raise":
            raise sp.SearchProviderError("blocked")
        return [sp.SearchHit(f"{self.name} title", f"https://{self.name}.example", f"result {i}") for i in range(max_results)]


def _configure(monkeypatch, providers: list[str], **fakes: _FakeProvider):
    for name, fake in fakes.items():
        monkeypatch.setitem(sp._PROVIDERS, name, lambda fake=fake: fake)
    config = SimpleNamespace(
        search=SimpleNamespace(providers=providers, max_results=5, timeout=5, dashscope_api_key="")
    )
    monkeypatch.setattr(sp, "get_config", lambda: config)


class TestFailover:
    async def test_moves_on_to_the_next_provider(self, monkeypatch):
        first, second = _FakeProvider("first", "raise"), _FakeProvider("second", "ok")
        _configure(monkeypatch, ["first", "second"], first=first, second=second)
        hits, used = await sp.SearchChain().search("query")
        assert used == "second"
        assert len(hits) == 5 and {hit.url for hit in hits} == {"https://second.example"}
        assert first.calls == [("query", 5)] and second.calls == [("query", 5)]

    async def test_stops_at_the_first_success(self, monkeypatch):
        first, second = _FakeProvider("first", "ok"), _FakeProvider("second", "ok")
        _configure(monkeypatch, ["first", "second"], first=first, second=second)
        _, used = await sp.SearchChain().search("query", 2)
        assert used == "first"
        assert second.calls == []  # never asked
        assert first.calls == [("query", 2)]

    async def test_unconfigured_and_unknown_providers_are_skipped(self, monkeypatch):
        last = _FakeProvider("last", "ok")
        _configure(monkeypatch, ["ghost", "nokey", "last"], nokey=_FakeProvider("nokey", "unavailable"), last=last)
        _, used = await sp.SearchChain().search("query")
        assert used == "last"

    async def test_every_failure_is_named_when_all_fail(self, monkeypatch):
        first, second = _FakeProvider("first", "raise"), _FakeProvider("second", "unavailable")
        _configure(monkeypatch, ["first", "second", "ghost"], first=first, second=second)
        with pytest.raises(sp.SearchError) as caught:
            await sp.SearchChain().search("query")
        message = str(caught.value)
        assert "first: SearchProviderError: blocked" in message
        assert "second: 未配置或依赖缺失" in message
        assert "ghost: 未知搜索源" in message

    async def test_tool_reports_when_every_provider_fails(self, monkeypatch):
        _configure(monkeypatch, ["first"], first=_FakeProvider("first", "raise"))
        output = await WebSearch().execute(query="query")
        assert output.startswith("搜索失败") and "blocked" in output

    def test_hits_are_numbered(self):
        text = sp.format_hits([sp.SearchHit("A", "https://a.example", "about a"), sp.SearchHit("B", "https://b.example")])
        assert text == "1. A\n   https://a.example\n   about a\n2. B\n   https://b.example\n   "


@pytest.mark.skipif(not os.environ.get("BA_LIVE_SEARCH"), reason="set BA_LIVE_SEARCH=1 to run live searches")
async def test_live_default_chain():
    hits, used = await sp.get_search_chain().search("Playwright python")
    assert hits and all(hit.url.startswith("http") for hit in hits)
