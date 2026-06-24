from datetime import UTC, datetime, timedelta

from news.models import Digest
from news.storage import (
    get_article_by_hash,
    get_article_by_url,
    get_articles_since,
    get_last_digest,
    get_run_stats,
    insert_article,
    insert_digest,
    update_digest_sent,
)


def test_init_db_creates_tables(db):
    """Verify all required tables exist."""
    cursor = db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cursor.fetchall()}
    assert "articles" in tables
    assert "article_categories" in tables
    assert "digests" in tables
    assert "sources" in tables


def test_insert_and_retrieve_article(db, sample_article):
    """Insert article and retrieve by URL, verifying all fields including categories."""
    result = insert_article(db, sample_article)
    assert result is True

    retrieved = get_article_by_url(db, sample_article.url)
    assert retrieved is not None
    assert retrieved.url == sample_article.url
    assert retrieved.title == sample_article.title
    assert retrieved.source == sample_article.source
    assert retrieved.author == sample_article.author
    assert retrieved.published_at == sample_article.published_at
    assert retrieved.content == sample_article.content
    assert retrieved.summary == sample_article.summary
    assert retrieved.content_hash == sample_article.content_hash
    assert retrieved.language == sample_article.language
    assert retrieved.relevance_score == sample_article.relevance_score
    assert set(retrieved.categories) == set(sample_article.categories)


def test_duplicate_article_skipped(db, sample_article):
    """Insert same article twice, verify only 1 in DB."""
    result1 = insert_article(db, sample_article)
    assert result1 is True

    result2 = insert_article(db, sample_article)
    assert result2 is False  # Duplicate should be skipped

    # Verify only 1 article in DB
    cursor = db.execute("SELECT COUNT(*) FROM articles")
    count = cursor.fetchone()[0]
    assert count == 1


def test_get_article_by_hash(db, sample_article):
    """Insert article and find by content_hash."""
    insert_article(db, sample_article)

    retrieved = get_article_by_hash(db, sample_article.content_hash)
    assert retrieved is not None
    assert retrieved.url == sample_article.url
    assert retrieved.content_hash == sample_article.content_hash


def test_get_articles_since(db, sample_article):
    """Query articles since a timestamp."""
    insert_article(db, sample_article)

    # Query since 1 hour ago (should find article)
    one_hour_ago = datetime.now(UTC) - timedelta(hours=1)
    articles = get_articles_since(db, one_hour_ago)
    assert len(articles) == 1
    assert articles[0].url == sample_article.url

    # Query since 1 hour in future (should be empty)
    one_hour_future = datetime.now(UTC) + timedelta(hours=1)
    articles = get_articles_since(db, one_hour_future)
    assert len(articles) == 0


def test_insert_and_get_digest(db):
    """Insert Digest and retrieve with get_last_digest."""
    digest = Digest(
        digest_type="morning",
        created_at=datetime.now(UTC),
        article_count=5,
        synthesis_text="Morning digest synthesis",
        html_output="<html>Digest</html>",
    )

    digest_id = insert_digest(db, digest)
    assert digest_id > 0

    retrieved = get_last_digest(db)
    assert retrieved is not None
    assert retrieved.id == digest_id
    assert retrieved.digest_type == "morning"
    assert retrieved.article_count == 5
    assert retrieved.synthesis_text == "Morning digest synthesis"
    assert retrieved.html_output == "<html>Digest</html>"
    assert retrieved.sent_at is None


def test_update_digest_sent(db):
    """Insert digest, update sent_at, verify it's set."""
    digest = Digest(
        digest_type="evening",
        created_at=datetime.now(UTC),
        article_count=3,
        synthesis_text="Evening digest",
        html_output="<html>Evening</html>",
    )

    digest_id = insert_digest(db, digest)

    # Update sent_at
    update_digest_sent(db, digest_id)

    # Verify sent_at is now set
    retrieved = get_last_digest(db)
    assert retrieved is not None
    assert retrieved.sent_at is not None
    assert isinstance(retrieved.sent_at, datetime)


def test_get_run_stats_empty(db):
    """Verify stats return zeros on empty database."""
    stats = get_run_stats(db)
    assert stats["total_articles"] == 0
    assert stats["total_digests"] == 0
