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

# API source walk: page size per section listing, and sections that carry
# editorial boilerplate rather than articles.
_API_PAGE_SIZE = 100
_API_SKIP_SECTIONS = frozenset({"about"})


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


def parse_api_items(
    items: list[dict[str, Any]],
    source_config: dict[str, Any],
    view_id: str,
    section_id: str,
) -> list[Article]:
    """Convert content-platform API items into Articles.

    Speaks the public ``/api/views/:view/sections/:section/items`` contract used by
    the-agent-daily.org: each item is a metadata sidecar carrying title, summary,
    author and publishedAt, with the public permalink assembled from view, section
    and slug. The item's ``summary`` becomes the article content — it is real
    editorial prose, unlike the empty anchors the listing pages serve.

    Args:
        items: item dicts from a section listing response
        source_config: source config with name, base_url, category, language
        view_id: the view the items belong to
        section_id: the section the items belong to

    Returns:
        List of Article instances (items missing title or slug are skipped)
    """
    base = source_config["base_url"].rstrip("/")
    articles: list[Article] = []

    for item in items:
        title = (item.get("title") or "").strip()
        slug = (item.get("slug") or "").strip()
        if not title or not slug:
            continue

        published_at = None
        raw_published = item.get("publishedAt")
        if raw_published:
            try:
                published_at = datetime.fromisoformat(raw_published.replace("Z", "+00:00"))
            except (AttributeError, ValueError):
                pass

        summary = item.get("summary") or ""
        articles.append(
            Article(
                url=f"{base}/{view_id}/{section_id}/{slug}",
                title=title,
                source=source_config["name"],
                content=summary,
                categories=[source_config["category"]],
                language=source_config["language"],
                author=item.get("author") or "",
                published_at=published_at,
                summary=summary,
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


async def _fetch_single_api_source(
    source: dict[str, Any],
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
) -> tuple[list[Article], str | None]:
    """Walk one content platform's views and sections, collecting every item.

    Three hops: ``/api/views`` for the view list, ``/api/views/:view`` for that
    view's sections, then ``/api/views/:view/sections/:section/items`` for the
    items. ``about`` sections are editorial boilerplate, not news, so they are
    skipped. A failure anywhere aborts this source rather than returning a
    partial set that would look like a quiet publishing day.

    Args:
        source: source config with name, base_url, category, language
        client: httpx async client
        semaphore: concurrency limiter

    Returns:
        Tuple of (articles list, error message or None)
    """
    base = source["base_url"].rstrip("/")

    async def _get_json(path: str) -> Any:
        response = await client.get(f"{base}{path}", timeout=_REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()

    async with semaphore:
        try:
            articles: list[Article] = []
            views = await _get_json("/api/views")
            for view in views.get("views", []):
                view_id = view.get("id")
                if not view_id:
                    continue
                sections = await _get_json(f"/api/views/{view_id}")
                for section in sections.get("sections", []):
                    section_id = section.get("id")
                    if not section_id or section_id in _API_SKIP_SECTIONS:
                        continue
                    listing = await _get_json(
                        f"/api/views/{view_id}/sections/{section_id}/items?size={_API_PAGE_SIZE}"
                    )
                    articles.extend(
                        parse_api_items(listing.get("items", []), source, view_id, section_id)
                    )
            return articles, None
        except Exception as e:
            error_msg = f"{source['name']}: {type(e).__name__}: {str(e)}"
            return [], error_msg


async def fetch_api_sources(
    sources: list[dict[str, Any]],
) -> tuple[list[Article], list[str]]:
    """Fetch multiple content-platform API sources in parallel.

    Args:
        sources: list of API source config dicts (with name, base_url, category,
            language)

    Returns:
        Tuple of (all articles, error messages)
    """
    all_articles: list[Article] = []
    errors: list[str] = []

    semaphore = asyncio.Semaphore(_MAX_CONCURRENT)

    async with httpx.AsyncClient(
        headers={"User-Agent": "NewsReader/1.0 (API Source Fetcher)"}
    ) as client:
        tasks = [_fetch_single_api_source(source, client, semaphore) for source in sources]
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
    """Fetch a profile's RSS feeds plus any feed-less HTML or API sources.

    Single entry point for a profile's full source set: reads ``rss_feeds`` and
    the optional ``html_sources`` and ``api_sources`` lists from the loaded
    sources.yaml dict, so a profile gains a feed-less site just by adding
    ``html_sources:`` or ``api_sources:`` to its YAML.

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

    api_sources = sources.get("api_sources", [])
    if api_sources:
        logger.info(f"Querying {len(api_sources)} API sources")
        api_articles, api_errors = await fetch_api_sources(api_sources)
        logger.info(f"Retrieved {len(api_articles)} articles from API sources")
        articles.extend(api_articles)
        errors.extend(api_errors)

    _warn_on_silent_sources(sources, articles, errors)

    return articles, errors


def _warn_on_silent_sources(
    sources: dict[str, Any],
    articles: list[Article],
    errors: list[str],
) -> None:
    """Log sources that produced nothing without raising.

    A source whose markup or contract changes under us returns an empty list and
    no error, which reads identically to a quiet publishing day. Naming those
    sources is the difference between noticing in a day and noticing in a month.
    """
    configured = {
        source["name"]
        for key in ("rss_feeds", "html_sources", "api_sources")
        for source in sources.get(key, [])
        if source.get("name")
    }
    produced = {article.source for article in articles}
    # Errors are formatted "<name>: <ExcType>: <msg>"; match on the name prefix
    # rather than splitting, since source names themselves contain colons.
    errored = {name for name in configured if any(e.startswith(f"{name}:") for e in errors)}

    silent = sorted(configured - produced - errored)
    if silent:
        logger.warning(
            f"{len(silent)} source(s) returned no articles and no error "
            f"(possible dead source): {', '.join(silent)}"
        )
