"""Fetch article bodies for feeds that publish headlines without them.

WHY

Several sources have been configured for months and have never contributed a single
article, not because the feed is dead but because it carries no body. Measured
2026-08-27 against the live feeds: Hugging Face Blog serves 851 entries with a
MEDIAN CONTENT LENGTH OF ZERO WORDS, Google Research Blog serves 100 entries with a
median of 3. The stack profile's quality gate requires 10 words, so every item from
both was dropped before it could be scored, every run, in silence.

``processor.extract_content`` was written for exactly this and was never called from
anywhere: definition only, zero call sites, for the life of the repo. It could not
be wired as-is. It is synchronous, it calls ``trafilatura.fetch_url`` which takes no
timeout, and it triggers on any article under 100 words. Calling it from
``process_articles`` would have put several thousand blocking network fetches into a
pipeline whose whole run budget is ~240s.

DESIGN

Opt-in per source, never global. A source declares ``extract_content: true`` in its
sources.yaml entry, and only those are eligible. This is the difference between
fetching a dozen bodies and fetching six thousand.

Bounded three ways: only articles actually below the word floor are candidates, the
number of fetches per run is capped, and the whole pass runs under a wall-clock
budget after which it stops and keeps whatever it got. Reuses the fetcher's async
client and semaphore so the concurrency limit is shared with feed fetching rather
than stacked on top of it.

Failure is always partial and never fatal: an article whose body cannot be fetched
keeps the content it had and is dropped by the quality gate exactly as before.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 15
_MAX_CONCURRENT = 5
# A run that spends longer than this on bodies is stealing from synthesis.
DEFAULT_BUDGET_SECONDS = 60
DEFAULT_MAX_FETCHES = 40

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) newsreader/1.0",
    "Accept": "text/html,application/xhtml+xml",
}


def sources_opting_in(sources: dict[str, Any]) -> set[str]:
    """Names of sources that declared `extract_content: true`."""
    opted: set[str] = set()
    for key in ("rss_feeds", "html_sources", "api_sources", "changelog_sources"):
        for source in sources.get(key) or []:
            if isinstance(source, dict) and source.get("extract_content") and source.get("name"):
                opted.add(source["name"])
    return opted


def _word_count(text: str | None) -> int:
    return len((text or "").split())


def _age_ceilings(
    sources: dict[str, Any], opted_in: set[str], default: float | None
) -> dict[str, float]:
    """Each opted-in source's age ceiling: its own max_age_hours, else the profile's."""
    ceilings: dict[str, float] = {}
    for key in ("rss_feeds", "html_sources", "api_sources", "changelog_sources"):
        for source in sources.get(key) or []:
            if not isinstance(source, dict) or source.get("name") not in opted_in:
                continue
            hours = source.get("max_age_hours", default)
            if hours is not None:
                ceilings[source["name"]] = float(hours)
    return ceilings


def candidates(
    articles: list[Any],
    opted_in: set[str],
    min_words: int,
    max_age_hours: dict[str, float] | None = None,
) -> list[Any]:
    """Articles from opted-in sources whose body is too thin to survive the gate.

    Newest first, and nothing older than its source's age ceiling, because the fetch
    cap is spent in list order. Measured 2026-11-22: Google Research Blog's 100 thin
    entries came first and took all 40 fetches, 35 of them for posts older than 14 days
    that the age gate then dropped, so Hugging Face Blog's fresh posts were never
    fetched and that source produced nothing for months.
    """
    if not opted_in:
        return []
    ceilings = max_age_hours or {}
    now = datetime.now(UTC)
    out: list[Any] = []
    for article in articles:
        source = getattr(article, "source", None)
        if source not in opted_in:
            continue
        url = getattr(article, "url", None)
        if not isinstance(url, str) or not url:
            continue
        published = getattr(article, "published_at", None)
        ceiling = ceilings.get(source)
        if ceiling is not None and isinstance(published, datetime):
            if published.tzinfo is None:
                published = published.replace(tzinfo=UTC)
            if now - published > timedelta(hours=ceiling):
                continue
        if _word_count(getattr(article, "content", None)) < min_words:
            out.append(article)
    out.sort(key=_published_key, reverse=True)
    return out


def _published_key(article: Any) -> datetime:
    published = getattr(article, "published_at", None)
    if not isinstance(published, datetime):
        return datetime.min.replace(tzinfo=UTC)
    return published if published.tzinfo else published.replace(tzinfo=UTC)


def _extract(html: str) -> str:
    """HTML -> plain text. Parsing only; the fetch already happened.

    trafilatura.extract is pure CPU, unlike trafilatura.fetch_url which does its own
    untimeoutable network call. Keeping the fetch in httpx is what makes this
    cancellable and bounded.
    """
    try:
        import trafilatura

        extracted = trafilatura.extract(html)
        if extracted:
            return extracted
    except Exception as exc:  # noqa: BLE001 - a parser failure is a thin article, not a dead run
        logger.debug("trafilatura extract failed: %s", exc)

    try:
        import re

        from readability import Document

        summary = Document(html).summary()
        return re.sub(r"<[^>]+>", " ", summary)
    except Exception as exc:  # noqa: BLE001
        logger.debug("readability extract failed: %s", exc)
    return ""


async def enrich_thin_articles(
    articles: list[Any],
    sources: dict[str, Any],
    *,
    min_words: int,
    max_fetches: int = DEFAULT_MAX_FETCHES,
    budget_seconds: int = DEFAULT_BUDGET_SECONDS,
    default_max_age_hours: float | None = None,
) -> tuple[int, int]:
    """Fill in bodies in place. Returns (enriched, attempted).

    Mirrors transcripts.enrich_articles' (done, total) contract. A gap between the
    two is a degraded run, never a failed one.
    """
    opted_in = sources_opting_in(sources)
    targets = candidates(
        articles, opted_in, min_words, _age_ceilings(sources, opted_in, default_max_age_hours)
    )
    if not targets:
        return (0, 0)

    if len(targets) > max_fetches:
        # Say what was dropped. A silent cap reads as "covered everything".
        logger.warning(
            "content enrichment: %d thin articles from opted-in sources, fetching the "
            "first %d (cap); the rest keep their feed body this run",
            len(targets),
            max_fetches,
        )
        targets = targets[:max_fetches]

    deadline = time.monotonic() + budget_seconds
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
    enriched = 0

    async def one(article: Any, client: httpx.AsyncClient) -> bool:
        if time.monotonic() > deadline:
            return False
        async with semaphore:
            if time.monotonic() > deadline:
                return False
            try:
                response = await client.get(article.url, timeout=_TIMEOUT, follow_redirects=True)
                response.raise_for_status()
            except Exception as exc:  # noqa: BLE001 - one dead URL must not stop the pass
                logger.debug("content fetch failed for %s: %s", article.url, exc)
                return False
            text = _extract(response.text)
            if _word_count(text) > _word_count(getattr(article, "content", "")):
                article.content = text
                return True
            return False

    async with httpx.AsyncClient(headers=_HEADERS) as client:
        results = await asyncio.gather(*(one(a, client) for a in targets), return_exceptions=True)
    enriched = sum(1 for r in results if r is True)

    if time.monotonic() > deadline:
        logger.warning(
            "content enrichment hit its %ds budget; %d/%d enriched before the stop",
            budget_seconds,
            enriched,
            len(targets),
        )
    else:
        logger.info("content enrichment: %d/%d thin articles filled", enriched, len(targets))
    return (enriched, len(targets))
