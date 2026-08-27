import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock, patch

import pytest

from news.fetcher import (
    fetch_all_sources,
    fetch_api_sources,
    fetch_changelog_sources,
    fetch_html_sources,
    fetch_rss_feeds,
    normalize_rss_entry,
    parse_api_items,
    parse_changelog_sections,
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


# --- Changelog sources (vendor release notes) ---------------------------------
# Vendor release notes publish no feed, but their docs serve a clean markdown
# twin at the same path plus ".md". It is one long document whose dated sections
# are the actual news items, so splitting on the date headings turns one document
# into one article per entry. The anchor per entry is what lets the existing url
# PRIMARY KEY dedupe across runs without any new state to persist.

SAMPLE_CHANGELOG_HEADINGS = """---
title: Claude Platform release notes
url: https://platform.claude.com/docs/en/release-notes/overview
---

<Tip>
  For updates to Claude Code, see the complete CHANGELOG.md in the claude-code repository.
</Tip>

### August 11, 2026

* The Compliance API now returns transcripts of Cowork and Claude Code sessions.
* We've added the `anthropic-workspace-id` response header to the Claude API.

### August 10, 2026

* The introductory pricing for **Claude Sonnet 5** is now the standard price.

### April 9th, 2025

* Ordinal dates ship on 33 of the live page's headings.
"""

# Two models carry an entry dated January 18, 2026. That collision is real: the
# live page ships one under Claude Opus 4.5 and another under Claude Haiku 4.5.
SAMPLE_CHANGELOG_ACCORDIONS = """---
title: System Prompts
url: https://platform.claude.com/docs/en/release-notes/system-prompts
---

Claude's web interface uses a system prompt at the start of every conversation.

## Claude Opus 4.5

<AccordionGroup>
  <Accordion title="January 18, 2026">
    The assistant is Claude, made by Anthropic.
    Claude is accessible via an API and Claude Platform.
    Claude keeps its responses concise.
  </Accordion>

  <Accordion title="November 24, 2025">
    An older Opus 4.5 prompt.
    Claude is accessible via an API and Claude Platform.
    Claude keeps its responses concise.
  </Accordion>
</AccordionGroup>

## Claude Haiku 4.5

<AccordionGroup>
  <Accordion title="January 18, 2026">
    The Haiku variant of the same dated update.
  </Accordion>
</AccordionGroup>
"""

_CHANGELOG_HEADINGS_CFG = {
    "name": "Anthropic Platform Release Notes",
    "url": "https://platform.claude.com/docs/en/release-notes/overview.md",
    "layout": "headings",
    "category": "releases",
    "tier": 1,
    "language": "en",
}

_CHANGELOG_ACCORDIONS_CFG = {
    "name": "Claude System Prompts",
    "url": "https://platform.claude.com/docs/en/release-notes/system-prompts.md",
    "layout": "accordions",
    "category": "releases",
    "tier": 1,
    "language": "en",
}


def test_parse_changelog_sections_splits_dated_headings_into_articles():
    articles = parse_changelog_sections(SAMPLE_CHANGELOG_HEADINGS, _CHANGELOG_HEADINGS_CFG)

    assert [a.title for a in articles] == [
        "Anthropic Platform Release Notes: August 11, 2026",
        "Anthropic Platform Release Notes: August 10, 2026",
        "Anthropic Platform Release Notes: April 9th, 2025",
    ]


def test_parse_changelog_sections_dates_each_entry_from_its_heading():
    """The heading date is the publication date, so the age window can filter it."""
    articles = parse_changelog_sections(SAMPLE_CHANGELOG_HEADINGS, _CHANGELOG_HEADINGS_CFG)

    assert articles[0].published_at == datetime(2026, 8, 11, tzinfo=UTC)
    assert articles[1].published_at == datetime(2026, 8, 10, tzinfo=UTC)


def test_parse_changelog_sections_reads_an_ordinal_date_heading():
    """33 of the live page's 130 dated headings write the day as an ordinal.

    A pattern that misses them does not skip those entries, it welds them into
    the body of the entry above: the live page produced one 12,537-char section
    dated "May 1, 2025" holding 33 orphaned headings.
    """
    articles = parse_changelog_sections(SAMPLE_CHANGELOG_HEADINGS, _CHANGELOG_HEADINGS_CFG)

    ordinal = articles[-1]
    assert ordinal.published_at == datetime(2025, 4, 9, tzinfo=UTC)
    assert "Ordinal dates ship" in ordinal.content
    # The tell of the franken-entry: a heading swallowed into someone's body.
    assert not any("### " in a.content for a in articles)


def test_parse_changelog_sections_anchors_each_entry_url_for_stable_dedup():
    """One document, many articles: without an anchor they collapse to one row."""
    articles = parse_changelog_sections(SAMPLE_CHANGELOG_HEADINGS, _CHANGELOG_HEADINGS_CFG)

    assert [a.url for a in articles] == [
        "https://platform.claude.com/docs/en/release-notes/overview#august-11-2026",
        "https://platform.claude.com/docs/en/release-notes/overview#august-10-2026",
        "https://platform.claude.com/docs/en/release-notes/overview#april-9th-2025",
    ]


def test_parse_changelog_sections_keeps_the_section_body_as_content():
    articles = parse_changelog_sections(SAMPLE_CHANGELOG_HEADINGS, _CHANGELOG_HEADINGS_CFG)
    newest = articles[0]

    assert "anthropic-workspace-id" in newest.content
    # The next entry's bullets belong to the next article, not this one.
    assert "Claude Sonnet 5" not in newest.content
    # Front matter and the Tip banner are page chrome, not news.
    assert "title: Claude Platform release notes" not in newest.content
    assert "CHANGELOG.md" not in newest.content


def test_parse_changelog_sections_reads_accordion_entries_under_their_model_heading():
    articles = parse_changelog_sections(SAMPLE_CHANGELOG_ACCORDIONS, _CHANGELOG_ACCORDIONS_CFG)

    assert {a.title for a in articles} == {
        "Claude Opus 4.5 system prompt (January 18, 2026)",
        "Claude Opus 4.5 system prompt (November 24, 2025)",
        "Claude Haiku 4.5 system prompt (January 18, 2026)",
    }


def test_parse_changelog_sections_keeps_same_date_entries_apart_by_model():
    """Jan 18 2026 ships under two models; a date-only anchor would lose one
    of them to INSERT OR IGNORE on the url PRIMARY KEY."""
    articles = parse_changelog_sections(SAMPLE_CHANGELOG_ACCORDIONS, _CHANGELOG_ACCORDIONS_CFG)

    assert len({a.url for a in articles}) == 3
    opus = next(a for a in articles if a.title.startswith("Claude Opus 4.5 system prompt (January"))
    haiku = next(a for a in articles if a.title.startswith("Claude Haiku"))
    assert opus.url.endswith("/release-notes/system-prompts#claude-opus-4-5-january-18-2026")
    assert haiku.url.endswith("/release-notes/system-prompts#claude-haiku-4-5-january-18-2026")
    assert "Haiku variant" in haiku.content


def test_parse_changelog_sections_gives_a_new_dated_entry_a_distinct_content_hash():
    """A revision surfaces because the page appends a new dated entry, not
    because the url carries a body digest.

    The two Opus 4.5 accordions are the real shape: a new date means a new
    title, so the pair clears ``processor.deduplicate()`` on the hash alone.
    """
    articles = parse_changelog_sections(SAMPLE_CHANGELOG_ACCORDIONS, _CHANGELOG_ACCORDIONS_CFG)

    opus = [a for a in articles if a.title.startswith("Claude Opus 4.5 system prompt")]
    assert len(opus) == 2
    for article in opus:
        article.compute_hash()
    assert len({a.content_hash for a in opus}) == 2


def test_parse_changelog_sections_sets_a_deterministic_digest_on_every_entry():
    """Both layouts, or the stack email keeps quoting 300 chars of boilerplate."""
    for markdown, config in (
        (SAMPLE_CHANGELOG_HEADINGS, _CHANGELOG_HEADINGS_CFG),
        (SAMPLE_CHANGELOG_ACCORDIONS, _CHANGELOG_ACCORDIONS_CFG),
    ):
        articles = parse_changelog_sections(markdown, config)

        assert articles
        for article in articles:
            assert article.changelog_digest
            assert article.changelog_digest_source == "deterministic"


def test_parse_changelog_sections_diffs_an_accordion_against_its_same_model_predecessor():
    """The baseline is same-model scope, never document position."""
    articles = parse_changelog_sections(SAMPLE_CHANGELOG_ACCORDIONS, _CHANGELOG_ACCORDIONS_CFG)
    by_title = {a.title: a for a in articles}

    newer = by_title["Claude Opus 4.5 system prompt (January 18, 2026)"]
    older = by_title["Claude Opus 4.5 system prompt (November 24, 2025)"]

    assert "DELTA vs Claude Opus 4.5 / November 24, 2025:" in newer.changelog_digest
    assert older.changelog_digest.startswith("NEW MODEL ENTRY:")


def test_parse_changelog_sections_falls_back_to_a_profile_on_a_reordered_chunk(caplog):
    """A vendor reordering the page must not produce a confidently inverted diff."""
    reordered = """## Claude Opus 4.5

<AccordionGroup>
  <Accordion title="November 24, 2025">
    An older Opus 4.5 prompt.
  </Accordion>

  <Accordion title="January 18, 2026">
    The assistant is Claude, made by Anthropic.
  </Accordion>
</AccordionGroup>
"""

    with caplog.at_level(logging.WARNING, logger="news.changelog_delta"):
        articles = parse_changelog_sections(reordered, _CHANGELOG_ACCORDIONS_CFG)

    opus = [a for a in articles if a.title.startswith("Claude Opus 4.5")]
    assert all("DELTA vs" not in a.changelog_digest for a in opus)
    assert "Claude Opus 4.5" in caplog.text


def test_parse_changelog_sections_reuses_the_same_url_for_an_unchanged_entry():
    """The anchor is keyed on model and date alone.

    Folding a body digest back into it would mint 29 brand-new urls the first
    time the vendor re-escapes the 471 KB page, and re-report the whole thing.
    """
    before = parse_changelog_sections(SAMPLE_CHANGELOG_ACCORDIONS, _CHANGELOG_ACCORDIONS_CFG)
    after = parse_changelog_sections(
        SAMPLE_CHANGELOG_ACCORDIONS.replace(
            "The assistant is Claude, made by Anthropic.",
            "The assistant is Claude, made by Anthropic\\. Claude is concise.",
        ),
        _CHANGELOG_ACCORDIONS_CFG,
    )

    assert [a.url for a in before] == [a.url for a in after]


def _mock_text_client(mock_client_cls, text=None, side_effect=None):
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    if side_effect is not None:
        mock_client.get = AsyncMock(side_effect=side_effect)
    else:
        resp = Mock()
        resp.text = text
        resp.raise_for_status = Mock()
        mock_client.get = AsyncMock(return_value=resp)
    mock_client_cls.return_value = mock_client
    return mock_client


@pytest.mark.asyncio
async def test_fetch_changelog_sources_returns_articles():
    with patch("news.fetcher.httpx.AsyncClient") as mock_client_cls:
        _mock_text_client(mock_client_cls, text=SAMPLE_CHANGELOG_HEADINGS)

        articles, errors = await fetch_changelog_sources([_CHANGELOG_HEADINGS_CFG])

    assert errors == []
    assert len(articles) == 3
    assert {a.source for a in articles} == {"Anthropic Platform Release Notes"}


@pytest.mark.asyncio
async def test_fetch_changelog_sources_survives_an_ordinal_date():
    """An unparseable date must not take the whole source down with it.

    ``_fetch_single_changelog`` catches bare ``Exception``, so a strptime that
    cannot read "April 9th, 2025" degrades the entire page to zero articles and
    one error string rather than losing the single entry.
    """
    with patch("news.fetcher.httpx.AsyncClient") as mock_client_cls:
        _mock_text_client(mock_client_cls, text=SAMPLE_CHANGELOG_HEADINGS)

        articles, errors = await fetch_changelog_sources([_CHANGELOG_HEADINGS_CFG])

    assert errors == []
    assert datetime(2025, 4, 9, tzinfo=UTC) in {a.published_at for a in articles}


@pytest.mark.asyncio
async def test_fetch_changelog_sources_reports_errors():
    with patch("news.fetcher.httpx.AsyncClient") as mock_client_cls:
        _mock_text_client(mock_client_cls, side_effect=Exception("Connection refused"))

        articles, errors = await fetch_changelog_sources([_CHANGELOG_HEADINGS_CFG])

    assert articles == []
    assert len(errors) == 1
    assert "Anthropic Platform Release Notes" in errors[0]


@pytest.mark.asyncio
async def test_fetch_all_sources_includes_changelog_sources():
    """A profile gains a vendor changelog by adding changelog_sources to its YAML."""
    with patch("news.fetcher.httpx.AsyncClient") as mock_client_cls:
        _mock_text_client(mock_client_cls, text=SAMPLE_CHANGELOG_HEADINGS)

        articles, errors = await fetch_all_sources({"changelog_sources": [_CHANGELOG_HEADINGS_CFG]})

    assert errors == []
    assert len(articles) == 3


@pytest.mark.asyncio
async def test_fetch_all_sources_flags_a_silent_changelog_source(caplog):
    """A docs page that stops serving dated sections must not read as a quiet week."""
    undated = "---\ntitle: Claude Platform release notes\n---\n\nNothing dated here.\n"
    with patch("news.fetcher.httpx.AsyncClient") as mock_client_cls:
        _mock_text_client(mock_client_cls, text=undated)

        with caplog.at_level(logging.WARNING, logger="news.fetcher"):
            articles, errors = await fetch_all_sources(
                {"changelog_sources": [_CHANGELOG_HEADINGS_CFG]}
            )

    assert articles == []
    assert errors == []
    assert "Anthropic Platform Release Notes" in caplog.text


def test_changelog_model_heading_capture_is_not_super_linear():
    """A lazy capture followed by an optional whitespace run backtracks
    super-linearly. The input is a 471 KB third-party document, so a
    whitespace-heavy heading is their edit to make, not ours to hang on."""
    import time

    from news.fetcher import _CHANGELOG_MODEL_SPLIT_RE

    pathological = "## " + " " * 20000 + "\n"
    started = time.monotonic()
    parts = _CHANGELOG_MODEL_SPLIT_RE.split(pathological)
    assert time.monotonic() - started < 1.0
    # A heading with no text is not a model heading, so it does not split.
    assert len(parts) == 1

    real = "## Claude Opus 5\nbody\n"
    assert _CHANGELOG_MODEL_SPLIT_RE.split(real)[1] == "Claude Opus 5"


def test_changelog_model_headings_are_stripped_of_trailing_whitespace():
    """The capture is greedy to end of line now, so the strip moved to the
    caller; an unstripped model name would leak into every title and anchor."""
    markdown = (
        '## Claude Opus 5   \n\n<AccordionGroup>\n  <Accordion title="July 24, 2026">\n'
        "    The prompt body.\n  </Accordion>\n</AccordionGroup>\n"
    )
    articles = parse_changelog_sections(markdown, _CHANGELOG_ACCORDIONS_CFG)

    assert articles[0].title == "Claude Opus 5 system prompt (July 24, 2026)"
    assert articles[0].url.endswith("#claude-opus-5-july-24-2026")


# --- dateless feeds ---------------------------------------------------------


def test_normalize_rss_entry_falls_back_to_first_seen_when_the_feed_has_no_date():
    """A dateless entry must still get a published_at.

    None survived to the INSERT, where published_at is NOT NULL, and the resulting
    IntegrityError was booked as a duplicate URL. Every article from every dateless
    feed was dropped in silence. The four GitHubTrendingRSS feeds are the live case.
    """
    from datetime import UTC, datetime

    from news.fetcher import normalize_rss_entry

    source = {"name": "GitHub Trending All", "category": "showcase", "language": "en"}
    before = datetime.now(UTC)
    article = normalize_rss_entry({"title": "some/repo", "link": "https://x/1"}, source)

    assert article.published_at is not None
    assert before <= article.published_at <= datetime.now(UTC)


def test_dateless_entry_actually_persists():
    """End-to-end guard on the bug: parse -> insert -> row exists."""
    import sqlite3

    from news.fetcher import normalize_rss_entry
    from news.storage import init_db, insert_article

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    source = {"name": "GitHub Trending All", "category": "showcase", "language": "en"}
    article = normalize_rss_entry(
        {"title": "some/repo", "link": "https://example.test/repo"}, source
    )
    article.pipeline = "stack"
    article.compute_hash()

    assert insert_article(conn, article) is True
    assert conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 1
