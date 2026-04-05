"""RSS feed fetcher with parallel async fetching."""

import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import feedparser
import httpx

from news.models import Article

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
        published_at = datetime(*entry["published_parsed"][:6], tzinfo=timezone.utc)
    elif "updated_parsed" in entry and entry["updated_parsed"]:
        published_at = datetime(*entry["updated_parsed"][:6], tzinfo=timezone.utc)
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


async def fetch_rss_feeds(
    sources: list[dict[str, Any]]
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
        tasks = [
            _fetch_single_feed(source, client, semaphore)
            for source in sources
        ]
        results = await asyncio.gather(*tasks)

        for articles, error in results:
            all_articles.extend(articles)
            if error:
                errors.append(error)

    return all_articles, errors
