"""Body enrichment for feeds that publish headlines without them.

The load-bearing property is that this is BOUNDED. processor.extract_content was
never wired precisely because an unbounded version would put thousands of blocking
fetches into a ~240s pipeline. Most of these tests pin the bounds.
"""

import httpx
import pytest

from news.content_enrich import (
    DEFAULT_MAX_FETCHES,
    candidates,
    enrich_thin_articles,
    sources_opting_in,
)
from news.models import Article


def _article(source, content, url="https://example.test/a"):
    return Article(
        url=url,
        title="t",
        source=source,
        content=content,
        categories=["releases"],
        language="en",
    )


SOURCES = {
    "rss_feeds": [
        {"name": "Thin Feed", "url": "https://x/f", "extract_content": True},
        {"name": "Fat Feed", "url": "https://x/g"},
        # Flagged but nameless: unmatchable against article.source, so it must be
        # skipped rather than added as an empty-string entry.
        {"url": "https://x/h", "extract_content": True},
    ]
}


def test_only_opted_in_sources_are_eligible():
    """Global enrichment is the thing that makes this unshippable; opt-in is the point."""
    assert sources_opting_in(SOURCES) == {"Thin Feed"}
    assert sources_opting_in({}) == set()


def test_candidates_are_opted_in_AND_actually_thin():
    arts = [
        _article("Thin Feed", "", url="https://x/1"),
        _article("Thin Feed", "word " * 50, url="https://x/2"),
        _article("Fat Feed", "", url="https://x/3"),
        _article("Thin Feed", "", url=""),
    ]
    got = candidates(arts, {"Thin Feed"}, min_words=10)
    assert [a.url for a in got] == ["https://x/1"]


def test_no_opted_in_sources_means_no_work():
    arts = [_article("Fat Feed", "")]
    assert candidates(arts, set(), min_words=10) == []


@pytest.mark.asyncio
async def test_enrich_fills_a_thin_body(monkeypatch):
    html = "<html><body><article><p>" + ("real body text " * 40) + "</p></article></body></html>"

    class FakeResponse:
        text = html

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    art = _article("Thin Feed", "")
    filled, tried = await enrich_thin_articles([art], SOURCES, min_words=10)

    assert (filled, tried) == (1, 1)
    assert len(art.content.split()) > 10


@pytest.mark.asyncio
async def test_a_dead_url_does_not_stop_the_pass_or_raise(monkeypatch):
    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    art = _article("Thin Feed", "")
    filled, tried = await enrich_thin_articles([art], SOURCES, min_words=10)

    assert (filled, tried) == (0, 1)
    assert art.content == ""  # unchanged, and the quality gate drops it as before


@pytest.mark.asyncio
async def test_extraction_never_shortens_an_existing_body(monkeypatch):
    """A paywall stub must not overwrite the feed body we already had."""

    class FakeResponse:
        text = "<html><body>tiny</body></html>"

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    art = _article("Thin Feed", "five words of feed body")
    filled, _ = await enrich_thin_articles([art], SOURCES, min_words=10)

    assert filled == 0
    assert art.content == "five words of feed body"


@pytest.mark.asyncio
async def test_the_fetch_cap_is_enforced_and_announced(monkeypatch, caplog):
    """A silent cap reads as 'covered everything'."""
    import logging

    calls = {"n": 0}

    class FakeResponse:
        text = "<html><body><article><p>" + ("body " * 40) + "</p></article></body></html>"

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            calls["n"] += 1
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    arts = [
        _article("Thin Feed", "", url=f"https://x/{i}") for i in range(DEFAULT_MAX_FETCHES + 15)
    ]

    with caplog.at_level(logging.WARNING, logger="news.content_enrich"):
        _, tried = await enrich_thin_articles(arts, SOURCES, min_words=10, max_fetches=5)

    assert tried == 5
    assert calls["n"] == 5
    assert any("cap" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_nothing_to_do_makes_no_network_calls():
    arts = [_article("Fat Feed", "")]
    assert await enrich_thin_articles(arts, SOURCES, min_words=10) == (0, 0)


@pytest.mark.asyncio
async def test_readability_fallback_when_trafilatura_finds_nothing(monkeypatch):
    """trafilatura returning None must fall through, not end the attempt."""
    import news.content_enrich as ce

    monkeypatch.setattr(ce, "_extract", ce._extract)  # keep the real one

    class FakeTrafilatura:
        @staticmethod
        def extract(html):
            return None

    monkeypatch.setitem(__import__("sys").modules, "trafilatura", FakeTrafilatura)
    html = "<html><body><div><p>" + ("fallback body " * 40) + "</p></div></body></html>"
    out = ce._extract(html)

    assert len(out.split()) > 10
    assert "fallback" in out


def test_extract_returns_empty_when_both_extractors_fail(monkeypatch):
    import sys

    import news.content_enrich as ce

    class Boom:
        @staticmethod
        def extract(html):
            raise RuntimeError("no")

    monkeypatch.setitem(sys.modules, "trafilatura", Boom)
    monkeypatch.setitem(sys.modules, "readability", Boom)
    assert ce._extract("<html></html>") == ""


@pytest.mark.asyncio
async def test_the_wall_clock_budget_stops_the_pass_and_says_so(monkeypatch, caplog):
    """A run that overspends on bodies is stealing from synthesis."""
    import logging

    import news.content_enrich as ce

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):  # pragma: no cover - budget expires first
            raise AssertionError("should not fetch once the budget is spent")

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    arts = [_article("Thin Feed", "", url=f"https://x/{i}") for i in range(3)]

    with caplog.at_level(logging.WARNING, logger="news.content_enrich"):
        filled, tried = await ce.enrich_thin_articles(
            arts, SOURCES, min_words=10, budget_seconds=-1
        )

    assert (filled, tried) == (0, 3)
    assert any("budget" in r.getMessage() for r in caplog.records)
