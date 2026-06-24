from unittest.mock import AsyncMock, Mock, patch

import pytest

from news.fetcher import (
    fetch_html_sources,
    fetch_rss_feeds,
    normalize_rss_entry,
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
