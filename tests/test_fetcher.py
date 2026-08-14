import logging
from unittest.mock import AsyncMock, Mock, patch

import pytest

from news.fetcher import (
    fetch_all_sources,
    fetch_api_sources,
    fetch_html_sources,
    fetch_rss_feeds,
    normalize_rss_entry,
    parse_api_items,
    parse_html_listing,
    parse_rss_feed,
)

SAMPLE_RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>Test Feed</title>
  <item>
    <title>AI Agents Transform Banking</title>
    <link>https://example.com/ai-banking</link>
    <description>A story about AI agents in banking.</description>
    <pubDate>Sat, 05 Apr 2026 08:00:00 GMT</pubDate>
    <author>jane@example.com (Jane Doe)</author>
  </item>
  <item>
    <title>New MacOS Features</title>
    <link>https://example.com/macos-features</link>
    <description>macOS gets new features.</description>
    <pubDate>Sat, 05 Apr 2026 07:00:00 GMT</pubDate>
  </item>
</channel>
</rss>"""


# Mimics the-agent-daily.org card markup: <a class="card" href="/agentnews/.../slug">
# wrapping an <h3 class="card__title"> plus tag/date noise. Nav links share the
# /agentnews/ prefix but have no slug, so they must be filtered out.
SAMPLE_HTML_LISTING = """
<html><body>
  <a href="/agentnews/">Home</a>
  <a href="/agentnews/news">News</a>
  <a href="/agentnews/about">About</a>
  <a class="card" href="/agentnews/news/the-pope-is-into-ai-matthew-berman">
    <div class="meta"><span class="tag">AI-news</span></div>
    <div class="dates"><span class="date-value">2026-05-29</span></div>
    <h3 class="card__title">The Pope Is Into AI — Matthew Berman</h3>
  </a>
  <a class="card" href="/agentnews/deep-dives/harness-engineering">
    <h3 class="card__title">Harness Engineering</h3>
  </a>
  <a class="card" href="/agentnews/news/the-pope-is-into-ai-matthew-berman">
    <h3 class="card__title">The Pope Is Into AI — Matthew Berman</h3>
  </a>
</body></html>
"""

_HTML_SOURCE_CFG = {
    "name": "The Agent Daily",
    "url": "https://the-agent-daily.org/",
    "link_pattern": r"/agentnews/(?:news|deep-dives|articles)/.+",
    "category": "ai",
    "tier": 1,
    "language": "en",
}


def test_parse_html_listing_extracts_article_links_and_titles():
    articles = parse_html_listing(SAMPLE_HTML_LISTING, _HTML_SOURCE_CFG)

    # Two unique articles (the duplicate Pope link deduped; nav links excluded).
    assert len(articles) == 2
    titles = {a.title for a in articles}
    assert "The Pope Is Into AI — Matthew Berman" in titles
    assert "Harness Engineering" in titles
    # Title comes from the card heading, not the tag/date noise.
    assert not any("AI-news" in a.title or "2026-05-29" in a.title for a in articles)


def test_parse_html_listing_resolves_absolute_urls_and_metadata():
    articles = parse_html_listing(SAMPLE_HTML_LISTING, _HTML_SOURCE_CFG)
    pope = next(a for a in articles if a.title.startswith("The Pope"))

    assert pope.url == (
        "https://the-agent-daily.org/agentnews/news/the-pope-is-into-ai-matthew-berman"
    )
    assert pope.source == "The Agent Daily"
    assert pope.categories == ["ai"]
    assert pope.language == "en"


def test_parse_html_listing_ignores_non_matching_links():
    """Nav/section links without an article slug must never become articles."""
    articles = parse_html_listing(SAMPLE_HTML_LISTING, _HTML_SOURCE_CFG)
    urls = {a.url for a in articles}
    assert not any(u.rstrip("/").endswith("/agentnews") for u in urls)
    assert "https://the-agent-daily.org/agentnews/about" not in urls


@pytest.mark.asyncio
async def test_fetch_html_sources_returns_articles():
    with patch("news.fetcher.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        resp = Mock()
        resp.text = SAMPLE_HTML_LISTING
        resp.raise_for_status = Mock()
        mock_client.get = AsyncMock(return_value=resp)
        mock_client_cls.return_value = mock_client

        articles, errors = await fetch_html_sources([_HTML_SOURCE_CFG])

        assert errors == []
        assert {a.title for a in articles} == {
            "The Pope Is Into AI — Matthew Berman",
            "Harness Engineering",
        }


@pytest.mark.asyncio
async def test_fetch_html_sources_reports_errors():
    with patch("news.fetcher.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=Exception("Connection refused"))
        mock_client_cls.return_value = mock_client

        articles, errors = await fetch_html_sources([_HTML_SOURCE_CFG])

        assert articles == []
        assert len(errors) == 1
        assert "The Agent Daily" in errors[0]


def test_parse_rss_feed_extracts_entries():
    source_config = {
        "name": "TestFeed",
        "url": "https://example.com/feed",
        "category": "tech",
        "tier": 1,
        "language": "en",
    }
    articles = parse_rss_feed(SAMPLE_RSS_XML, source_config)
    assert len(articles) == 2
    assert articles[0].title == "AI Agents Transform Banking"
    assert articles[0].source == "TestFeed"
    assert articles[0].categories == ["tech"]
    assert articles[0].url == "https://example.com/ai-banking"


def test_normalize_rss_entry_handles_missing_fields():
    entry = {"title": "Minimal Article", "link": "https://example.com/minimal"}
    source_config = {"name": "Src", "category": "ai", "tier": 2, "language": "en"}
    article = normalize_rss_entry(entry, source_config)
    assert article.title == "Minimal Article"
    assert article.author == ""
    assert article.summary == ""
    assert article.categories == ["ai"]


@pytest.mark.asyncio
async def test_fetch_rss_feeds_handles_errors():
    sources = [
        {
            "name": "Bad Feed",
            "url": "https://bad.example.com/feed",
            "category": "tech",
            "tier": 1,
            "language": "en",
        }
    ]
    with patch("news.fetcher.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=Exception("Connection refused"))
        mock_client_cls.return_value = mock_client
        articles, errors = await fetch_rss_feeds(sources)
        assert len(articles) == 0
        assert len(errors) == 1
        assert "Bad Feed" in errors[0]


# --- API sources (the-agent-daily.org content platform) -----------------------
# Item shape is the platform's public /api/views/:v/sections/:s/items contract.
_API_SOURCE_CFG = {
    "name": "The Agent Daily",
    "base_url": "https://the-agent-daily.org",
    "category": "industry",
    "tier": 1,
    "language": "en",
}

_API_ITEM = {
    "slug": "claude-invisible-watermark",
    "title": "Claude's Invisible Watermark",
    "summary": "SimplyExplain breaks down Anthropic's statistical watermarking system.",
    "author": "SimplyExplain",
    "publishedAt": "2026-08-11T00:00:00.000Z",
    "sourceUrl": "https://youtu.be/QZ9MsAwKnLk",
}


def test_parse_api_items_maps_item_fields_to_article():
    articles = parse_api_items([_API_ITEM], _API_SOURCE_CFG, "agentnews", "news")

    assert len(articles) == 1
    article = articles[0]
    assert article.title == "Claude's Invisible Watermark"
    assert article.author == "SimplyExplain"
    assert article.source == "The Agent Daily"
    assert article.language == "en"
    assert article.categories == ["industry"]


def test_parse_api_items_builds_permalink_from_view_section_slug():
    articles = parse_api_items([_API_ITEM], _API_SOURCE_CFG, "agentnews", "news")

    assert articles[0].url == (
        "https://the-agent-daily.org/agentnews/news/claude-invisible-watermark"
    )


def test_parse_api_items_uses_summary_as_content_so_it_clears_the_word_gate():
    articles = parse_api_items([_API_ITEM], _API_SOURCE_CFG, "agentnews", "news")

    assert articles[0].content == _API_ITEM["summary"]
    assert articles[0].summary == _API_ITEM["summary"]


def test_parse_api_items_parses_published_at_as_utc_datetime():
    articles = parse_api_items([_API_ITEM], _API_SOURCE_CFG, "agentnews", "news")

    published = articles[0].published_at
    assert published is not None
    assert (published.year, published.month, published.day) == (2026, 8, 11)
    assert published.tzinfo is not None


def test_parse_api_items_skips_items_without_title_or_slug():
    items = [_API_ITEM, {"slug": "no-title", "summary": "x"}, {"title": "No slug"}]

    articles = parse_api_items(items, _API_SOURCE_CFG, "agentnews", "news")

    assert len(articles) == 1


def _api_response_for(url: str) -> Mock:
    """Dispatch a mocked platform API response by URL path."""
    if url.endswith("/api/views"):
        payload = {"views": [{"id": "agentnews"}, {"id": "the-sound-of-agent"}]}
    elif url.endswith("/api/views/agentnews"):
        payload = {
            "sections": [{"id": "news"}, {"id": "deep-dives"}, {"id": "about"}],
        }
    elif url.endswith("/api/views/the-sound-of-agent"):
        payload = {"sections": [{"id": "deep-dives"}]}
    elif "/sections/news/items" in url:
        payload = {"items": [_API_ITEM]}
    elif "/sections/deep-dives/items" in url:
        payload = {"items": [dict(_API_ITEM, slug="harness", title="Harness Engineering")]}
    elif "/sections/about/items" in url:
        payload = {"items": [dict(_API_ITEM, slug="about-us", title="About Us")]}
    else:
        payload = {}
    resp = Mock()
    resp.json = Mock(return_value=payload)
    resp.raise_for_status = Mock()
    return resp


def _mock_api_client(mock_client_cls, side_effect=None):
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    if side_effect is not None:
        mock_client.get = AsyncMock(side_effect=side_effect)
    else:
        mock_client.get = AsyncMock(side_effect=lambda url, **kw: _api_response_for(url))
    mock_client_cls.return_value = mock_client
    return mock_client


@pytest.mark.asyncio
async def test_fetch_api_sources_walks_every_view_and_section():
    with patch("news.fetcher.httpx.AsyncClient") as mock_client_cls:
        _mock_api_client(mock_client_cls)

        articles, errors = await fetch_api_sources([_API_SOURCE_CFG])

        assert errors == []
        # agentnews/news + agentnews/deep-dives + the-sound-of-agent/deep-dives
        assert len(articles) == 3
        assert {a.source for a in articles} == {"The Agent Daily"}


@pytest.mark.asyncio
async def test_fetch_api_sources_skips_about_sections():
    with patch("news.fetcher.httpx.AsyncClient") as mock_client_cls:
        _mock_api_client(mock_client_cls)

        articles, _ = await fetch_api_sources([_API_SOURCE_CFG])

        assert "About Us" not in {a.title for a in articles}


@pytest.mark.asyncio
async def test_fetch_api_sources_reports_errors():
    with patch("news.fetcher.httpx.AsyncClient") as mock_client_cls:
        _mock_api_client(mock_client_cls, side_effect=Exception("Connection refused"))

        articles, errors = await fetch_api_sources([_API_SOURCE_CFG])

        assert articles == []
        assert len(errors) == 1
        assert "The Agent Daily" in errors[0]


# --- Dead-source detection ----------------------------------------------------
# A source that returns nothing without erroring is indistinguishable from a
# quiet publishing day. the-agent-daily.org sat dead for weeks that way.
EMPTY_RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Empty</title></channel></rss>"""

_DEAD_FEED_CFG = {
    "name": "Dead Feed",
    "url": "https://dead.example.com/feed",
    "category": "tech",
    "tier": 2,
    "language": "en",
}


@pytest.mark.asyncio
async def test_fetch_all_sources_warns_when_a_source_yields_nothing_without_erroring(caplog):
    with patch("news.fetcher.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        resp = Mock()
        resp.text = EMPTY_RSS_XML
        resp.raise_for_status = Mock()
        mock_client.get = AsyncMock(return_value=resp)
        mock_client_cls.return_value = mock_client

        with caplog.at_level(logging.WARNING, logger="news.fetcher"):
            articles, errors = await fetch_all_sources({"rss_feeds": [_DEAD_FEED_CFG]})

    assert articles == []
    assert errors == []
    assert "Dead Feed" in caplog.text


@pytest.mark.asyncio
async def test_fetch_all_sources_does_not_flag_a_source_that_already_reported_an_error(caplog):
    """An errored source is already visible in errors; don't double-report it."""
    with patch("news.fetcher.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=Exception("Connection refused"))
        mock_client_cls.return_value = mock_client

        with caplog.at_level(logging.WARNING, logger="news.fetcher"):
            _, errors = await fetch_all_sources({"rss_feeds": [_DEAD_FEED_CFG]})

    assert len(errors) == 1
    assert "returned no articles" not in caplog.text


@pytest.mark.asyncio
async def test_fetch_all_sources_does_not_warn_about_sources_that_produced_articles(caplog):
    with patch("news.fetcher.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        resp = Mock()
        resp.text = SAMPLE_RSS_XML
        resp.raise_for_status = Mock()
        mock_client.get = AsyncMock(return_value=resp)
        mock_client_cls.return_value = mock_client

        with caplog.at_level(logging.WARNING, logger="news.fetcher"):
            articles, _ = await fetch_all_sources({"rss_feeds": [_DEAD_FEED_CFG]})

    assert len(articles) == 2
    assert "returned no articles" not in caplog.text


@pytest.mark.asyncio
async def test_fetch_all_sources_includes_api_sources():
    """A profile gains the platform by adding api_sources to its YAML."""
    with patch("news.fetcher.httpx.AsyncClient") as mock_client_cls:
        _mock_api_client(mock_client_cls)

        articles, errors = await fetch_all_sources({"api_sources": [_API_SOURCE_CFG]})

    assert errors == []
    assert len(articles) == 3
    assert {a.source for a in articles} == {"The Agent Daily"}
