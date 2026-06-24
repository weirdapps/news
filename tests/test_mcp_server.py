"""Tests for news MCP server tool wiring."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from news.models import Article, Digest
from news.storage import get_connection, init_db, insert_article, insert_digest


@pytest.fixture
def populated_db(tmp_path):
    """Create a populated database file for MCP tests."""
    db_path = tmp_path / "news.db"
    conn = get_connection(str(db_path))
    init_db(conn)

    now = datetime.now(UTC)
    article = Article(
        url="https://example.com/test-article",
        title="ECB rate decision impacts Greek banks",
        source="Reuters",
        content="The European Central Bank announced...",
        categories=["banking"],
        language="en",
        relevance_score=85,
        fetched_at=now,
        published_at=now - timedelta(hours=1),
        pipeline="digest",
    )
    article.compute_hash()
    insert_article(conn, article)

    synthesis = {
        "executive_brief": ["ECB held rates steady"],
        "sections": [],
    }
    digest = Digest(
        digest_type="scheduled",
        created_at=now,
        article_count=1,
        synthesis_text=json.dumps(synthesis),
        html_output="<html>test</html>",
        sent_at=now,
        pipeline="digest",
    )
    insert_digest(conn, digest)
    conn.close()

    return str(db_path)


class TestMcpTools:
    def test_search_news_returns_results(self, populated_db):
        with patch("news.mcp_server._DB_PATH", populated_db):
            from news.mcp_server import search_news

            results = search_news(query="ECB")
            assert len(results) >= 1
            assert "ECB" in results[0]["title"]

    def test_digest_history_returns_briefs(self, populated_db):
        with patch("news.mcp_server._DB_PATH", populated_db):
            from news.mcp_server import digest_history

            results = digest_history()
            assert len(results) >= 1
            assert "executive_brief" in results[0]

    def test_news_stats_returns_counts(self, populated_db):
        with patch("news.mcp_server._DB_PATH", populated_db):
            from news.mcp_server import news_stats

            result = news_stats()
            assert result["total_articles"] >= 1
            assert result["total_digests"] >= 1
