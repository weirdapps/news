import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from news.llm_policy import ReauthResult

# Ensure project root is on sys.path so 'main' can be imported
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from news.models import Article
from news.storage import init_db


@pytest.fixture(autouse=True)
def _fast_synthesizer_policy(request, monkeypatch):
    """Suppress real backoff sleeps and gcloud reauth calls in synthesizer tests.

    ``time.sleep`` is stubbed so TIMEOUT/RATE_LIMIT backoff does not make the
    suite take three minutes (182 s observed without this fixture).

    ``reauth`` defaults to FAILED, the pessimistic choice: a test that accidentally
    passes because reauth quietly succeeds would be testing an assumption its author
    never intended. FAILED exercises the latch (``with_reauth_used``) immediately, so
    a second auth error hits UNRECOVERABLE_AUTH and the test goes red — the correct
    outcome for a test that does not explicitly control auth. Tests that need a
    successful reauth must say so with ``@patch("news.synthesizer.reauth")``, which
    runs inside the fixture scope and wins over this monkeypatch.
    """
    if "test_synthesizer" not in getattr(request.module, "__name__", ""):
        return
    monkeypatch.setattr("news.synthesizer.time.sleep", lambda _: None)
    monkeypatch.setattr("news.synthesizer.reauth", lambda: ReauthResult.FAILED)


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
