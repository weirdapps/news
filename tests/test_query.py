"""Tests for news query functions."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from news.query import get_digest_history, get_news_stats, search_articles
from news.storage import get_connection, init_db, insert_article, insert_digest
from news.models import Article, Digest


@pytest.fixture
def db():
    """In-memory SQLite database with schema and sample data."""
    conn = get_connection(":memory:")
    init_db(conn)

    now = datetime.now(timezone.utc)
    articles = [
        Article(
            url="https://example.com/ecb-rates",
            title="ECB holds rates steady amid inflation concerns",
            source="Reuters",
            content="The European Central Bank decided to hold interest rates...",
            categories=["banking"],
            language="en",
            relevance_score=80,
            fetched_at=now - timedelta(hours=2),
            published_at=now - timedelta(hours=3),
            pipeline="digest",
        ),
        Article(
            url="https://example.com/nbg-results",
            title="NBG reports strong Q1 results",
            source="Kathimerini",
            content="National Bank of Greece announced quarterly results...",
            categories=["banking", "greece"],
            language="en",
            relevance_score=95,
            fetched_at=now - timedelta(hours=1),
            published_at=now - timedelta(hours=2),
            pipeline="digest",
        ),
        Article(
            url="https://example.com/claude-update",
            title="Claude Code adds new MCP features",
            source="TechCrunch",
            content="Anthropic released new features for Claude Code...",
            categories=["ai"],
            language="en",
            relevance_score=60,
            fetched_at=now - timedelta(hours=5),
            published_at=now - timedelta(hours=6),
            pipeline="digest",
        ),
        Article(
            url="https://example.com/nbg-monitor",
            title="NBG digital banking expansion",
            source="Capital.gr",
            content="NBG expands digital services...",
            categories=["banking"],
            language="el",
            relevance_score=70,
            fetched_at=now - timedelta(hours=1),
            published_at=now - timedelta(hours=2),
            pipeline="monitor",
        ),
    ]
    for article in articles:
        article.compute_hash()
        insert_article(conn, article)

    synthesis = {
        "executive_brief": [
            "ECB holds rates steady",
            "NBG reports strong Q1",
        ],
        "sections": [
            {
                "category": "banking",
                "display_name": "Banking & ECB",
                "synthesis": "ECB held rates...",
            }
        ],
    }
    digest = Digest(
        digest_type="scheduled",
        created_at=now - timedelta(hours=1),
        article_count=3,
        synthesis_text=json.dumps(synthesis),
        html_output="<html>...</html>",
        sent_at=now - timedelta(hours=1),
        pipeline="digest",
    )
    insert_digest(conn, digest)

    monitor_synthesis = {
        "executive_brief": ["NBG digital expansion noted"],
        "alerts": [],
    }
    monitor_digest = Digest(
        digest_type="scheduled",
        created_at=now - timedelta(hours=1),
        article_count=1,
        synthesis_text=json.dumps(monitor_synthesis),
        html_output="<html>monitor</html>",
        sent_at=now - timedelta(hours=1),
        pipeline="monitor",
    )
    insert_digest(conn, monitor_digest)

    yield conn
    conn.close()


class TestSearchArticles:
    def test_search_by_keyword(self, db):
        results = search_articles(db, query="ECB")
        assert len(results) >= 1
        assert any("ECB" in r["title"] for r in results)

    def test_search_by_keyword_case_insensitive(self, db):
        results = search_articles(db, query="ecb")
        assert len(results) >= 1

    def test_search_with_pipeline_filter(self, db):
        results = search_articles(db, query="NBG", pipeline="monitor")
        assert all(r["pipeline"] == "monitor" for r in results)

    def test_search_with_category_filter(self, db):
        results = search_articles(db, query="NBG", category="banking")
        assert len(results) >= 1
        assert all("banking" in r["categories"] for r in results)

    def test_search_with_days_filter(self, db):
        results = search_articles(db, query="ECB", days=1)
        assert len(results) >= 1

    def test_search_no_results(self, db):
        results = search_articles(db, query="cryptocurrency")
        assert results == []

    def test_search_respects_limit(self, db):
        results = search_articles(db, query="NBG", limit=1)
        assert len(results) <= 1


class TestGetDigestHistory:
    def test_get_digest_history_default(self, db):
        results = get_digest_history(db, pipeline="digest")
        assert len(results) >= 1
        assert "executive_brief" in results[0]
        assert "created_at" in results[0]

    def test_get_monitor_history(self, db):
        results = get_digest_history(db, pipeline="monitor")
        assert len(results) >= 1

    def test_get_digest_history_limit(self, db):
        results = get_digest_history(db, pipeline="digest", limit=1)
        assert len(results) <= 1

    def test_digest_excludes_html(self, db):
        results = get_digest_history(db, pipeline="digest")
        for r in results:
            assert "html_output" not in r


class TestGetNewsStats:
    def test_stats_returns_counts(self, db):
        result = get_news_stats(db)
        assert result["total_articles"] == 4
        assert result["total_digests"] == 2
        assert result["digest_articles"] == 3
        assert result["monitor_articles"] == 1
