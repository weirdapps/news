import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

# Ensure project root is on sys.path so 'main' can be imported
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from news.models import Article
from news.storage import init_db


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    yield conn
    conn.close()


@pytest.fixture
def sample_article():
    article = Article(
        url="https://example.com/test-article",
        title="Test Article About AI Agents",
        source="TechCrunch",
        author="Jane Doe",
        published_at=datetime(2026, 4, 5, 8, 0, tzinfo=UTC),
        content="This is a test article about AI agents and their impact on the industry. " * 10,
        summary="AI agents are changing the industry.",
        categories=["ai", "tech"],
        language="en",
        relevance_score=35,
    )
    article.compute_hash()
    return article
