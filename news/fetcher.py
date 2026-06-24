"""RSS feed fetcher with parallel async fetching.

Also supports feed-less sites via ``parse_html_listing`` / ``fetch_html_sources``:
some sources publish no RSS/Atom and are not indexed by Google News, so we scrape
their server-rendered listing pages for article links + titles instead.
"""

import asyncio
import logging
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin

import feedparser
import httpx
import lxml.etree
import lxml.html

from news.models import Article

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 30
_MAX_CONCURRENT = 10


def normalize_rss_entry(entry: dict[str, Any], source_config: dict[str, Any]) -> Article:
    """Convert feedparser entry to Article model.

    Args:
        entry: feedparser entry dictionary
        source_config: source configuration with name, category, tier, language

    Returns:
        Article instance with normalized fields
    """
    # Extract title and url (required)
    title = entry.get("title", "")
    url = entry.get("link", "")

    # Extract author (optional)
    author = entry.get("author", "")

    # Extract summary/description (optional)
    summary = ""
    if "summary" in entry:
        summary = entry["summary"]
    elif "description" in entry:
        summary = entry["description"]

    # Extract content (optional)
    content = ""
    if "content" in entry and entry["content"]:
        # feedparser stores content as list of dicts with 'value' key
        content = entry["content"][0].get("value", "")
    elif summary:
        content = summary

    # Extract published date (optional)
    published_at = None
    if "published_parsed" in entry and entry["published_parsed"]:
        parsed = entry["published_parsed"]
        published_at = datetime(
            parsed[0],
            parsed[1],
            parsed[2],
            parsed[3],
            parsed[4],
            parsed[5],
            tzinfo=UTC,
        )
    elif "updated_parsed" in entry and entry["updated_parsed"]:
        parsed = entry["updated_parsed"]
        published_at = datetime(
            parsed[0],
            parsed[1],
            parsed[2],
            parsed[3],
            parsed[4],
            parsed[5],
            tzinfo=UTC,
        )
    elif "published" in entry:
        try:
            published_at = parsedate_to_datetime(entry["published"])
        except (TypeError, ValueError):
            pass

    # Create article with source metadata
    return Article(
        url=url,
        title=title,
        source=source_config["name"],
        content=content,
        categories=[source_config["category"]],
        language=source_config["language"],
        author=author,
        published_at=published_at,
        summary=summary,
    )


def parse_rss_feed(xml_content: str, source_config: dict[str, Any]) -> list[Article]:
    """Parse RSS/Atom feed XML and extract articles.

    Args:
        xml_content: Raw XML content of RSS/Atom feed
        source_config: source configuration with name, category, tier, language

    Returns:
        List of Article instances
    """
    feed = feedparser.parse(xml_content)
    articles = []

    for entry in feed.entries:
        # Skip entries without required fields
        if not entry.get("link") or not entry.get("title"):
            continue

        article = normalize_rss_entry(entry, source_config)
        articles.append(article)

    return articles


async def _fetch_single_feed(
    source: dict[str, Any],
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
) -> tuple[list[Article], str | None]:
    """Fetch and parse a single RSS feed.

    Args:
        source: source configuration dict
        client: httpx async client
        semaphore: concurrency limiter

    Returns:
        Tuple of (articles list, error message or None)
    """
    async with semaphore:
        try:
            response = await client.get(
                source["url"],
                timeout=_REQUEST_TIMEOUT,
                follow_redirects=True,
            )
            response.raise_for_status()
            articles = parse_rss_feed(response.text, source)
            return articles, None
        except Exception as e:
            error_msg = f"{source['name']}: {type(e).__name__}: {str(e)}"
            return [], error_msg


def parse_html_listing(html_content: str, source_config: dict[str, Any]) -> list[Article]:
    """Extract articles from a server-rendered listing page (feed-less sites).

    Finds anchors whose href matches ``link_pattern`` (a regex), uses the inner
    heading (h1-h4) as the title — falling back to the anchor text — and resolves
    relative links against ``base_url`` (defaults to the source ``url``).

    Args:
        html_content: Raw HTML of the listing page
        source_config: source config with name, url, link_pattern, category,
            language, and optional base_url

    Returns:
        List of Article instances (deduplicated by URL)
    """
    pattern = re.compile(source_config["link_pattern"])
    base = source_config.get("base_url") or source_config["url"]

    try:
        doc = lxml.html.fromstring(html_content)
    except (lxml.etree.ParserError, ValueError):
        return []

    articles: list[Article] = []
    seen: set[str] = set()
    for anchor in doc.xpath("//a[@href]"):
        href = anchor.get("href", "")
        if not pattern.search(href):
            continue
        url = urljoin(base, href)
        if url in seen:
            continue

        heading = anchor.xpath(".//h1 | .//h2 | .//h3 | .//h4")
        raw_title = heading[0].text_content() if heading else anchor.text_content()
        title = " ".join(raw_title.split())
        if not title:
            continue

        seen.add(url)
        articles.append(
            Article(
                url=url,
                title=title,
                source=source_config["name"],
                content="",
                categories=[source_config["category"]],
                language=source_config["language"],
            )
        )

    return articles


async def _fetch_single_html(
    source: dict[str, Any],
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
) -> tuple[list[Article], str | None]:
    """Fetch and scrape a single HTML listing page.

    Args:
        source: source configuration dict (with link_pattern)
        client: httpx async client
        semaphore: concurrency limiter

    Returns:
        Tuple of (articles list, error message or None)
    """
    async with semaphore:
        try:
            response = await client.get(
                source["url"],
                timeout=_REQUEST_TIMEOUT,
                follow_redirects=True,
            )
            response.raise_for_status()
            articles = parse_html_listing(response.text, source)
            return articles, None
        except Exception as e:
            error_msg = f"{source['name']}: {type(e).__name__}: {str(e)}"
            return [], error_msg


async def fetch_html_sources(
    sources: list[dict[str, Any]],
) -> tuple[list[Article], list[str]]:
    """Scrape multiple HTML listing pages in parallel (feed-less sources).

    Args:
        sources: list of HTML source config dicts (with url, name, link_pattern,
            category, language)

    Returns:
        Tuple of (all articles, error messages)
    """
    all_articles: list[Article] = []
    errors: list[str] = []

    semaphore = asyncio.Semaphore(_MAX_CONCURRENT)

    async with httpx.AsyncClient(
        headers={"User-Agent": "NewsReader/1.0 (HTML Source Fetcher)"}
    ) as client:
        tasks = [_fetch_single_html(source, client, semaphore) for source in sources]
        results = await asyncio.gather(*tasks)

        for articles, error in results:
            all_articles.extend(articles)
            if error:
                errors.append(error)

    return all_articles, errors


async def fetch_rss_feeds(
    sources: list[dict[str, Any]],
) -> tuple[list[Article], list[str]]:
    """Fetch multiple RSS feeds in parallel.

    Args:
        sources: list of source configuration dicts with url, name, category, tier, language

    Returns:
        Tuple of (all articles, error messages)
    """
    all_articles: list[Article] = []
    errors: list[str] = []

    semaphore = asyncio.Semaphore(_MAX_CONCURRENT)

    async with httpx.AsyncClient(
        headers={"User-Agent": "NewsReader/1.0 (RSS Feed Fetcher)"}
    ) as client:
        tasks = [_fetch_single_feed(source, client, semaphore) for source in sources]
        results = await asyncio.gather(*tasks)

        for articles, error in results:
            all_articles.extend(articles)
            if error:
                errors.append(error)

    return all_articles, errors


async def fetch_all_sources(
    sources: dict[str, Any],
) -> tuple[list[Article], list[str]]:
    """Fetch a profile's RSS feeds plus any feed-less HTML listing sources.

    Single entry point for a profile's full source set: reads ``rss_feeds`` and
    the optional ``html_sources`` list from the loaded sources.yaml dict, so a
    profile gains a feed-less site just by adding ``html_sources:`` to its YAML.

    Args:
        sources: loaded sources config (the sources.yaml dict)

    Returns:
        Tuple of (all articles, error messages) merged across both source types
    """
    articles, errors = await fetch_rss_feeds(sources.get("rss_feeds", []))

    html_sources = sources.get("html_sources", [])
    if html_sources:
        logger.info(f"Scraping {len(html_sources)} HTML sources")
        html_articles, html_errors = await fetch_html_sources(html_sources)
        logger.info(f"Scraped {len(html_articles)} articles from HTML sources")
        articles.extend(html_articles)
        errors.extend(html_errors)

    return articles, errors
