"""Weekly community block for the stack brief.

Once a week (Sunday by default) the stack brief adds what developers said and built in
the last seven days, from four places the RSS feeds do not reach:

- Reddit top-of-week per subreddit, through ScrapeCreators (Reddit blocks datacenter
  IPs, and the VPS is one). 1 credit per subreddit. Needs SCRAPECREATORS_API_KEY.
- Hacker News stories over a points floor, through the free Algolia search API.
- Bluesky top posts, through the AT Protocol API. Needs BSKY_HANDLE and
  BSKY_APP_PASSWORD; skipped quietly without them.
- New GitHub repos (created inside the window) per topic, sorted by stars, through the
  unauthenticated search API (GITHUB_TOKEN raises the rate limit when set).

Rendered without an LLM call: the synthesis budget (main._llm_budget_seconds) stays
untouched, and a failure here costs this block, never the brief. Items whose url a
stack brief already cited inside the window are dropped, so the block only carries
what the daily briefs missed.

Chose direct APIs over running the last30days engine: tested 2026-11-22, the engine
without its LLM reranker returned Reddit items from a single day with no upvote counts,
and it would have needed its own install on the VPS.
"""

import asyncio
import logging
import re
from collections.abc import Coroutine
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

_SC_REDDIT = "https://api.scrapecreators.com/v1/reddit/subreddit"
_HN_SEARCH = "https://hn.algolia.com/api/v1/search"
_GH_SEARCH = "https://api.github.com/search/repositories"
_BSKY = "https://bsky.social/xrpc"

_TIMEOUT = httpx.Timeout(20.0)
_OVERALL_SECONDS = 120


@dataclass
class CommunityItem:
    source: str
    title: str
    url: str
    score: int
    comments: int
    # Every url that identifies the item, for the already-cited check. Defaults to (url,).
    urls: tuple[str, ...] = ()

    def __post_init__(self):
        if not self.urls:
            self.urls = (self.url,)


@dataclass
class CommunityResult:
    groups: dict[str, list[CommunityItem]] = field(default_factory=dict)
    failed: list[str] = field(default_factory=list)


def is_community_day(now: datetime, weekday: str, tz_name: str) -> bool:
    return now.astimezone(ZoneInfo(tz_name)).strftime("%A").lower() == weekday.lower()


async def fetch_reddit(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    subreddits: list[str],
    since_ts: int,
    exclude_flairs: list[str],
    min_upvotes: int,
    per_subreddit: int,
    exclude_post_types: list[str],
) -> list[CommunityItem]:
    excluded = {f.lower() for f in exclude_flairs}
    excluded_types = {t.lower() for t in exclude_post_types}
    items: list[CommunityItem] = []
    for sub in subreddits:
        resp = await client.get(
            _SC_REDDIT,
            params={"subreddit": sub, "sort": "top", "timeframe": "week"},
            headers={"x-api-key": api_key},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        kept = []
        for post in resp.json().get("posts") or []:
            ups = int(post.get("ups") or post.get("score") or 0)
            if (post.get("link_flair_text") or "").lower() in excluded:
                continue
            if (post.get("post_hint") or "").lower() in excluded_types:
                continue
            if float(post.get("created_utc") or 0) < since_ts or ups < min_upvotes:
                continue
            kept.append(
                CommunityItem(
                    source=f"r/{sub}",
                    title=post.get("title") or "",
                    url=f"https://www.reddit.com{post.get('permalink', '')}",
                    score=ups,
                    comments=int(post.get("num_comments") or 0),
                )
            )
        kept.sort(key=lambda i: i.score, reverse=True)
        items += kept[:per_subreddit]
    return items


async def fetch_hn(
    client: httpx.AsyncClient,
    *,
    queries: list[str],
    since_ts: int,
    min_points: int,
    limit: int,
) -> list[CommunityItem]:
    by_id: dict[str, CommunityItem] = {}
    for query in queries:
        resp = await client.get(
            _HN_SEARCH,
            params={
                "query": query,
                "tags": "story",
                "restrictSearchableAttributes": "title",
                "typoTolerance": "false",
                "numericFilters": f"created_at_i>{since_ts},points>={min_points}",
                "hitsPerPage": 30,
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        # Belt and braces over typoTolerance: every query word must start a word in the
        # title (2026-11-22, typo matching let "LLM" hit "almost" and "MCP" hit "map").
        words = [re.compile(rf"\b{re.escape(w)}", re.IGNORECASE) for w in query.split()]
        for hit in resp.json().get("hits") or []:
            if not all(w.search(hit.get("title") or "") for w in words):
                continue
            hn_url = f"https://news.ycombinator.com/item?id={hit['objectID']}"
            story = hit.get("url")
            by_id[hit["objectID"]] = CommunityItem(
                source="Hacker News",
                title=hit.get("title") or "",
                url=hn_url,
                score=int(hit.get("points") or 0),
                comments=int(hit.get("num_comments") or 0),
                urls=(hn_url, story) if story else (hn_url,),
            )
    return sorted(by_id.values(), key=lambda i: i.score, reverse=True)[:limit]


async def fetch_github(
    client: httpx.AsyncClient,
    *,
    topics: list[str],
    since_date: str,
    min_stars: int,
    per_topic: int,
    token: str | None,
) -> list[CommunityItem]:
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    by_name: dict[str, CommunityItem] = {}
    for topic in topics:
        resp = await client.get(
            _GH_SEARCH,
            params={
                "q": f"topic:{topic} created:>={since_date} stars:>={min_stars}",
                "sort": "stars",
                "order": "desc",
                "per_page": per_topic,
            },
            headers=headers,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        for repo in resp.json().get("items") or []:
            desc = (repo.get("description") or "").strip()
            name = repo["full_name"]
            by_name[name] = CommunityItem(
                source="GitHub",
                title=f"{name}: {desc}" if desc else name,
                url=repo["html_url"],
                score=int(repo.get("stargazers_count") or 0),
                comments=0,
            )
    # One repo name under several fresh owners is a star-farmed spam pattern
    # (2026-11-22: "Gemini-Config-Checker-" under three accounts at ~470 stars each).
    owners: dict[str, int] = {}
    for name in by_name:
        short = name.split("/", 1)[-1].lower()
        owners[short] = owners.get(short, 0) + 1
    kept = [i for n, i in by_name.items() if owners[n.split("/", 1)[-1].lower()] == 1]
    return sorted(kept, key=lambda i: i.score, reverse=True)


async def fetch_bluesky(
    client: httpx.AsyncClient,
    *,
    handle: str,
    app_password: str,
    queries: list[str],
    since_iso: str,
    min_likes: int,
    limit: int,
) -> list[CommunityItem]:
    session = await client.post(
        f"{_BSKY}/com.atproto.server.createSession",
        json={"identifier": handle, "password": app_password},
        timeout=_TIMEOUT,
    )
    session.raise_for_status()
    auth = {"Authorization": f"Bearer {session.json()['accessJwt']}"}
    by_uri: dict[str, CommunityItem] = {}
    for query in queries:
        resp = await client.get(
            f"{_BSKY}/app.bsky.feed.searchPosts",
            params={"q": query, "sort": "top", "since": since_iso, "limit": 25},
            headers=auth,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        for post in resp.json().get("posts") or []:
            likes = int(post.get("likeCount") or 0)
            if likes < min_likes:
                continue
            author = post["author"]["handle"]
            rkey = post["uri"].rsplit("/", 1)[-1]
            text = " ".join((post.get("record", {}).get("text") or "").split())
            by_uri[post["uri"]] = CommunityItem(
                source="Bluesky",
                title=f"@{author}: {text[:200]}",
                url=f"https://bsky.app/profile/{author}/post/{rkey}",
                score=likes,
                comments=int(post.get("replyCount") or 0),
            )
    return sorted(by_uri.values(), key=lambda i: i.score, reverse=True)[:limit]


def drop_seen(items: list[CommunityItem], seen_text: str) -> list[CommunityItem]:
    return [i for i in items if not any(u in seen_text for u in i.urls)]


async def _collect(settings: dict, now: datetime, env: dict, client: httpx.AsyncClient):
    since = now - timedelta(days=int(settings.get("lookback_days", 7)))
    since_ts = int(since.timestamp())
    jobs: dict[str, Coroutine[Any, Any, list[CommunityItem]]] = {}

    reddit = settings.get("reddit") or {}
    if reddit and env.get("SCRAPECREATORS_API_KEY"):
        jobs["Reddit"] = fetch_reddit(
            client,
            api_key=env["SCRAPECREATORS_API_KEY"],
            subreddits=reddit.get("subreddits", []),
            since_ts=since_ts,
            exclude_flairs=reddit.get("exclude_flairs", []),
            min_upvotes=int(reddit.get("min_upvotes", 100)),
            per_subreddit=int(reddit.get("per_subreddit", 3)),
            exclude_post_types=reddit.get("exclude_post_types", []),
        )
    hn = settings.get("hackernews") or {}
    if hn:
        jobs["Hacker News"] = fetch_hn(
            client,
            queries=hn.get("queries", []),
            since_ts=since_ts,
            min_points=int(hn.get("min_points", 100)),
            limit=int(hn.get("limit", 8)),
        )
    bsky = settings.get("bluesky") or {}
    if bsky and env.get("BSKY_HANDLE") and env.get("BSKY_APP_PASSWORD"):
        jobs["Bluesky"] = fetch_bluesky(
            client,
            handle=env["BSKY_HANDLE"],
            app_password=env["BSKY_APP_PASSWORD"],
            queries=bsky.get("queries", []),
            since_iso=since.strftime("%Y-%m-%dT%H:%M:%SZ"),
            min_likes=int(bsky.get("min_likes", 50)),
            limit=int(bsky.get("limit", 5)),
        )
    gh = settings.get("github") or {}
    if gh:
        jobs["New GitHub repos"] = fetch_github(
            client,
            topics=gh.get("topics", []),
            since_date=since.strftime("%Y-%m-%d"),
            min_stars=int(gh.get("min_stars", 50)),
            per_topic=int(gh.get("per_topic", 5)),
            token=env.get("GITHUB_TOKEN"),
        )

    names = list(jobs)
    outcomes = await asyncio.gather(*jobs.values(), return_exceptions=True)
    return dict(zip(names, outcomes, strict=True))


async def gather_community(
    settings: dict,
    now: datetime,
    *,
    seen_text: str,
    env: dict,
    client: httpx.AsyncClient | None = None,
) -> CommunityResult:
    """Fetch every configured source on the community day; never raises."""
    result = CommunityResult()
    if not settings.get("enabled"):
        return result
    if not is_community_day(
        now, settings.get("weekday", "sunday"), settings.get("timezone", "Europe/Athens")
    ):
        return result

    async def run(c: httpx.AsyncClient):
        return await asyncio.wait_for(_collect(settings, now, env, c), _OVERALL_SECONDS)

    try:
        if client is not None:
            outcomes = await run(client)
        else:
            async with httpx.AsyncClient(
                headers={"User-Agent": "NewsReader/1.0 (Stack Community)"}
            ) as own:
                outcomes = await run(own)
    except Exception as exc:  # timeout or anything unforeseen: lose the block, not the brief
        logger.warning(f"Community block skipped: {exc!r}")
        result.failed.append("all sources")
        return result

    for name, outcome in outcomes.items():
        if isinstance(outcome, BaseException):
            logger.warning(f"Community source {name} failed: {outcome!r}")
            result.failed.append(name)
            continue
        kept = drop_seen(outcome, seen_text)
        if kept:
            result.groups[name] = kept
    return result
