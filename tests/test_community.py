"""Weekly community block for the stack brief: Reddit, Hacker News, Bluesky, new GitHub repos."""

import asyncio
import json
from datetime import UTC, datetime

import httpx

from news.community import (
    CommunityItem,
    drop_seen,
    fetch_bluesky,
    fetch_github,
    fetch_hn,
    fetch_reddit,
    gather_community,
    is_community_day,
)

NOW = datetime(2026, 11, 22, 11, 0, tzinfo=UTC)  # a Sunday, 13:00 Athens
WEEK_AGO = int(NOW.timestamp()) - 7 * 86400


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _run(coro):
    return asyncio.run(coro)


# --- schedule ---------------------------------------------------------------


def test_community_day_is_the_configured_athens_weekday():
    assert is_community_day(NOW, "sunday", "Europe/Athens")
    assert not is_community_day(NOW, "monday", "Europe/Athens")


def test_community_day_uses_athens_not_utc():
    # 23:30 UTC Saturday is already 01:30 Sunday in Athens (winter, UTC+2).
    late_saturday_utc = datetime(2026, 11, 21, 23, 30, tzinfo=UTC)
    assert is_community_day(late_saturday_utc, "sunday", "Europe/Athens")


# --- reddit -----------------------------------------------------------------


def _post(title, ups, flair="Discussion", created=None, comments=10, hint="self"):
    return {
        "title": title,
        "post_hint": hint,
        "ups": ups,
        "num_comments": comments,
        "link_flair_text": flair,
        "created_utc": created if created is not None else WEEK_AGO + 3600,
        "permalink": f"/r/ClaudeCode/comments/{title[:4]}/x/",
    }


def test_reddit_keeps_top_posts_and_drops_humor_old_and_low_votes():
    seen_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        posts = [
            _post("Meme post", 5000, flair="Humor"),
            _post("Real incident", 1200),
            _post("Too old", 3000, created=WEEK_AGO - 3600),
            _post("Quiet one", 40),
            _post("Workflow tip", 800),
        ]
        return httpx.Response(200, json={"success": True, "posts": posts})

    async def go():
        async with _client(handler) as client:
            return await fetch_reddit(
                client,
                api_key="k",
                subreddits=["ClaudeCode"],
                since_ts=WEEK_AGO,
                exclude_flairs=["humor", "meme"],
                min_upvotes=100,
                per_subreddit=3,
                exclude_post_types=[],
            )

    items = _run(go())

    assert [i.title for i in items] == ["Real incident", "Workflow tip"]
    assert items[0].source == "r/ClaudeCode"
    assert items[0].url == "https://www.reddit.com/r/ClaudeCode/comments/Real/x/"
    assert items[0].score == 1200
    req = seen_requests[0]
    assert req.headers["x-api-key"] == "k"
    assert req.url.params["sort"] == "top"
    assert req.url.params["timeframe"] == "week"
    assert req.url.params["subreddit"] == "ClaudeCode"


def test_reddit_caps_each_subreddit():
    def handler(request):
        return httpx.Response(200, json={"posts": [_post(f"P{n}", 1000 - n) for n in range(10)]})

    async def go():
        async with _client(handler) as client:
            return await fetch_reddit(
                client,
                api_key="k",
                subreddits=["A", "B"],
                since_ts=WEEK_AGO,
                exclude_flairs=[],
                min_upvotes=0,
                per_subreddit=2,
                exclude_post_types=[],
            )

    items = _run(go())
    assert [(i.source, i.title) for i in items] == [
        ("r/A", "P0"),
        ("r/A", "P1"),
        ("r/B", "P0"),
        ("r/B", "P1"),
    ]


def test_reddit_drops_image_posts_which_are_mostly_memes():
    def handler(request):
        posts = [
            _post("NEED MONEY FOR CLAUDE MAX", 1059, flair="Built with Claude", hint="image"),
            _post("Everything Anthropic told Claude to stop saying", 927, hint="link"),
        ]
        return httpx.Response(200, json={"posts": posts})

    async def go():
        async with _client(handler) as client:
            return await fetch_reddit(
                client,
                api_key="k",
                subreddits=["ClaudeCode"],
                since_ts=WEEK_AGO,
                exclude_flairs=[],
                min_upvotes=0,
                per_subreddit=3,
                exclude_post_types=["image"],
            )

    assert [i.title for i in _run(go())] == ["Everything Anthropic told Claude to stop saying"]


# --- hacker news ------------------------------------------------------------


def test_hn_merges_queries_dedupes_and_sorts_by_points():
    def handler(request):
        q = request.url.params["query"]
        hits = {
            "Claude": [
                {
                    "objectID": "1",
                    "title": "Goldman pulls Claude Code",
                    "url": "https://x.com/a",
                    "points": 905,
                    "num_comments": 497,
                },
                {
                    "objectID": "2",
                    "title": "Ask HN: Claude with no url",
                    "url": None,
                    "points": 150,
                    "num_comments": 40,
                },
            ],
            "MCP": [
                {
                    "objectID": "1",
                    "title": "Goldman pulls Claude Code",
                    "url": "https://x.com/a",
                    "points": 905,
                    "num_comments": 497,
                },
                {
                    "objectID": "3",
                    "title": "MCP backdoor",
                    "url": "https://y.com/b",
                    "points": 300,
                    "num_comments": 90,
                },
            ],
        }[q]
        assert f"created_at_i>{WEEK_AGO}" in request.url.params["numericFilters"]
        assert "points>=100" in request.url.params["numericFilters"]
        # Title only: body matches pulled in "How to make almost anything" (2026-11-22).
        assert request.url.params["restrictSearchableAttributes"] == "title"
        return httpx.Response(200, json={"hits": hits})

    async def go():
        async with _client(handler) as client:
            return await fetch_hn(
                client, queries=["Claude", "MCP"], since_ts=WEEK_AGO, min_points=100, limit=10
            )

    items = _run(go())

    assert [i.title for i in items] == [
        "Goldman pulls Claude Code",
        "MCP backdoor",
        "Ask HN: Claude with no url",
    ]
    # The link goes to the discussion; the story url is kept for the seen-check.
    assert items[0].url == "https://news.ycombinator.com/item?id=1"
    assert "https://x.com/a" in items[0].urls
    assert items[2].urls == ("https://news.ycombinator.com/item?id=2",)


def test_hn_drops_typo_matches_the_search_lets_through():
    def handler(request):
        assert request.url.params["typoTolerance"] == "false"
        hits = [
            {
                "objectID": "1",
                "title": "How to make almost anything",
                "points": 921,
                "num_comments": 1,
            },
            {
                "objectID": "2",
                "title": "LLMs can read executables",
                "points": 276,
                "num_comments": 1,
            },
            {
                "objectID": "3",
                "title": "Protomaps: map tiles are cheap",
                "points": 471,
                "num_comments": 1,
            },
        ]
        return httpx.Response(200, json={"hits": hits})

    async def go():
        async with _client(handler) as client:
            return await fetch_hn(
                client, queries=["LLM", "MCP"], since_ts=WEEK_AGO, min_points=0, limit=10
            )

    # Measured 2026-11-22: "LLM" matched "almost" and "MCP" matched "map"/"CPU".
    assert [i.title for i in _run(go())] == ["LLMs can read executables"]


# --- github -----------------------------------------------------------------


def test_github_searches_new_repos_per_topic_and_dedupes():
    queries = []

    def handler(request):
        queries.append(request.url.params["q"])
        repo = {
            "full_name": "acme/agent-kit",
            "html_url": "https://github.com/acme/agent-kit",
            "stargazers_count": 420,
            "description": "Agents",
        }
        return httpx.Response(200, json={"items": [repo]})

    async def go():
        async with _client(handler) as client:
            return await fetch_github(
                client,
                topics=["claude-code", "mcp"],
                since_date="2026-11-15",
                min_stars=50,
                per_topic=5,
                token=None,
            )

    items = _run(go())

    assert queries == [
        "topic:claude-code created:>=2026-11-15 stars:>=50",
        "topic:mcp created:>=2026-11-15 stars:>=50",
    ]
    assert len(items) == 1
    assert items[0].title == "acme/agent-kit: Agents"
    assert items[0].score == 420


def test_github_drops_a_repo_name_cloned_under_several_owners():
    def handler(request):
        def repo(full, stars):
            return {
                "full_name": full,
                "html_url": f"https://github.com/{full}",
                "stargazers_count": stars,
                "description": "",
            }

        return httpx.Response(
            200,
            json={
                "items": [
                    repo("ownerA/Config-Checker-", 471),
                    repo("ownerB/Config-Checker-", 470),
                    repo("acme/real-tool", 300),
                ]
            },
        )

    async def go():
        async with _client(handler) as client:
            return await fetch_github(
                client,
                topics=["mcp"],
                since_date="2026-11-15",
                min_stars=0,
                per_topic=5,
                token=None,
            )

    assert [i.title for i in _run(go())] == ["acme/real-tool"]


# --- bluesky ----------------------------------------------------------------


def test_bluesky_logs_in_then_searches_top_posts():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("createSession"):
            body = json.loads(request.content)
            assert body == {"identifier": "me.bsky.social", "password": "pw"}
            return httpx.Response(200, json={"accessJwt": "jwt"})
        assert request.headers["authorization"] == "Bearer jwt"
        assert request.url.params["sort"] == "top"
        post = {
            "uri": "at://did:plc:abc/app.bsky.feed.post/3kxyz",
            "author": {"handle": "simonw.net"},
            "record": {"text": "Claude Code now has hooks\nsecond line"},
            "likeCount": 210,
            "replyCount": 12,
        }
        weak = dict(post, uri="at://did:plc:abc/app.bsky.feed.post/weak", likeCount=3)
        return httpx.Response(200, json={"posts": [post, weak]})

    async def go():
        async with _client(handler) as client:
            return await fetch_bluesky(
                client,
                handle="me.bsky.social",
                app_password="pw",
                queries=["claude code"],
                since_iso="2026-11-15T11:00:00Z",
                min_likes=50,
                limit=5,
            )

    items = _run(go())

    assert calls[0].endswith("com.atproto.server.createSession")
    assert len(items) == 1
    assert items[0].url == "https://bsky.app/profile/simonw.net/post/3kxyz"
    assert items[0].title == "@simonw.net: Claude Code now has hooks second line"
    assert items[0].score == 210


# --- seen filter and orchestration ------------------------------------------


def test_drop_seen_removes_items_whose_any_url_was_cited():
    a = CommunityItem(
        "Hacker News", "A", "https://hn/1", 1, 0, urls=("https://hn/1", "https://s/a")
    )
    b = CommunityItem("Hacker News", "B", "https://hn/2", 1, 0)
    assert drop_seen([a, b], "brief cited https://s/a yesterday") == [b]


def _settings():
    return {
        "enabled": True,
        "weekday": "sunday",
        "timezone": "Europe/Athens",
        "lookback_days": 7,
        "reddit": {
            "subreddits": ["ClaudeCode"],
            "exclude_flairs": [],
            "min_upvotes": 0,
            "per_subreddit": 3,
        },
        "hackernews": {"queries": ["Claude"], "min_points": 0, "limit": 5},
        "github": {"topics": ["mcp"], "min_stars": 0, "per_topic": 5},
        "bluesky": {"queries": ["claude"], "min_likes": 0, "limit": 5},
    }


def _ok_handler(request):
    host = request.url.host
    if host == "api.scrapecreators.com":
        return httpx.Response(200, json={"posts": [_post("Reddit item", 500)]})
    if host == "hn.algolia.com":
        return httpx.Response(500, json={})  # one source down must not sink the others
    if host == "api.github.com":
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "full_name": "a/b",
                        "html_url": "https://github.com/a/b",
                        "stargazers_count": 60,
                        "description": None,
                    }
                ]
            },
        )
    raise AssertionError(f"unexpected host {host}")


def test_gather_skips_sources_without_credentials_and_reports_failures():
    async def go():
        async with _client(_ok_handler) as client:
            return await gather_community(
                _settings(), NOW, seen_text="", env={"SCRAPECREATORS_API_KEY": "k"}, client=client
            )

    result = _run(go())

    assert [i.title for i in result.groups["Reddit"]] == ["Reddit item"]
    assert result.groups["New GitHub repos"][0].title == "a/b"
    assert "Hacker News" not in result.groups
    assert "Bluesky" not in result.groups  # no BSKY_* credentials: skipped quietly
    assert result.failed == ["Hacker News"]


def test_gather_returns_nothing_on_other_days():
    monday = datetime(2026, 11, 23, 11, 0, tzinfo=UTC)

    def handler(request):
        raise AssertionError("no request on a non-community day")

    async def go():
        async with _client(handler) as client:
            return await gather_community(_settings(), monday, seen_text="", env={}, client=client)

    result = _run(go())
    assert result.groups == {} and result.failed == []


def test_gather_returns_nothing_when_disabled():
    settings = dict(_settings(), enabled=False)

    def handler(request):
        raise AssertionError("no request when disabled")

    async def go():
        async with _client(handler) as client:
            return await gather_community(settings, NOW, seen_text="", env={}, client=client)

    assert _run(go()).groups == {}
