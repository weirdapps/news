from datetime import datetime, timezone
from unittest.mock import patch, AsyncMock, MagicMock
import pytest
from news.fetcher import parse_rss_feed, fetch_rss_feeds, normalize_rss_entry
from news.models import Article

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

def test_parse_rss_feed_extracts_entries():
    source_config = {"name": "TestFeed", "url": "https://example.com/feed",
        "category": "tech", "tier": 1, "language": "en"}
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
    sources = [{"name": "Bad Feed", "url": "https://bad.example.com/feed",
        "category": "tech", "tier": 1, "language": "en"}]
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
