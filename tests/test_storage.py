from datetime import UTC, datetime, timedelta

from news.models import Article, Digest
from news.storage import (
    _migrate_db,
    _row_to_article,
    backfill_transcript_abstracts,
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


# --- Transcript abstract column ----------------------------------------------


def test_insert_article_round_trips_the_transcript_abstract(db):
    article = Article(
        url="https://www.youtube.com/watch?v=abc12345678",
        title="A Video",
        source="YouTube: Fireship",
        content="Short marketing blurb.",
        categories=["ai"],
        language="en",
        published_at=datetime.now(UTC),
        transcript_abstract="Meta released Muse Glimmer, a 30B agentic model under Apache 2.0.",
    )
    article.compute_hash()
    insert_article(db, article)

    row = db.execute("SELECT * FROM articles WHERE url = ?", (article.url,)).fetchone()
    restored = _row_to_article(db, row)

    assert restored.transcript_abstract == (
        "Meta released Muse Glimmer, a 30B agentic model under Apache 2.0."
    )


def test_migrate_db_is_idempotent_on_an_already_migrated_database(db):
    """_migrate_db runs on every init_db; a second pass must not raise."""
    _migrate_db(db)
    _migrate_db(db)

    cols = {r[1] for r in db.execute("PRAGMA table_info(articles)")}
    assert "transcript_abstract" in cols


def test_backfill_writes_an_abstract_onto_a_row_stored_without_one(db):
    """A video stored before the harvester reached it must still get its abstract.

    Dedup drops the re-fetched article (its hash is unchanged by design), so
    insert_article never runs a second time. Without a backfill the row keeps
    NULL forever and the enrichment is silently lost.
    """
    article = Article(
        url="https://www.youtube.com/watch?v=G55HSGpuh1M",
        title="Muse Glimmer",
        source="YouTube: Fireship",
        content="Subscribe for more!",
        categories=["ai"],
        language="en",
        published_at=datetime.now(UTC),
    )
    article.compute_hash()
    insert_article(db, article)

    enriched = Article(**{**article.__dict__, "transcript_abstract": "The distilled facts."})
    updated = backfill_transcript_abstracts(db, [enriched])

    assert updated == 1
    row = db.execute("SELECT transcript_abstract FROM articles WHERE url = ?", (article.url,))
    assert row.fetchone()[0] == "The distilled facts."


def test_backfill_does_not_overwrite_an_abstract_that_is_already_there(db):
    article = Article(
        url="https://www.youtube.com/watch?v=G55HSGpuh1M",
        title="Muse Glimmer",
        source="YouTube: Fireship",
        content="blurb",
        categories=["ai"],
        language="en",
        published_at=datetime.now(UTC),
        transcript_abstract="The original abstract.",
    )
    article.compute_hash()
    insert_article(db, article)

    replacement = Article(**{**article.__dict__, "transcript_abstract": "A different abstract."})
    updated = backfill_transcript_abstracts(db, [replacement])

    assert updated == 0
    row = db.execute("SELECT transcript_abstract FROM articles WHERE url = ?", (article.url,))
    assert row.fetchone()[0] == "The original abstract."


def test_backfill_ignores_articles_without_an_abstract(db):
    article = Article(
        url="https://techcrunch.com/story",
        title="A Story",
        source="TechCrunch",
        content="words",
        categories=["tech"],
        language="en",
        published_at=datetime.now(UTC),
    )
    article.compute_hash()
    insert_article(db, article)

    assert backfill_transcript_abstracts(db, [article]) == 0
