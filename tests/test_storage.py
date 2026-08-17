import shutil
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from news.models import Article, Digest
from news.storage import (
    _migrate_db,
    _row_to_article,
    backfill_changelog_digests,
    backfill_transcript_abstracts,
    get_article_by_hash,
    get_article_by_url,
    get_articles_since,
    get_last_digest,
    get_run_stats,
    insert_article,
    insert_digest,
    update_digest_sent,
    urls_already_upgraded,
    urls_awaiting_changelog_upgrade,
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


# --- Changelog digest columns -------------------------------------------------

# The exact 18-column shape of the production articles table before this change,
# taken verbatim from `SELECT sql FROM sqlite_master` on data/news.db. Used so the
# migration is exercised against the schema it will actually meet, not a stand-in.
_LEGACY_SCHEMA = """
    CREATE TABLE articles (
        url TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        source TEXT NOT NULL,
        author TEXT,
        published_at TEXT NOT NULL,
        content TEXT NOT NULL,
        summary TEXT,
        content_hash TEXT NOT NULL,
        language TEXT,
        relevance_score INTEGER,
        fetched_at TEXT NOT NULL,
        included_in_digest_id INTEGER,
        also_reported_by TEXT,
        pipeline TEXT DEFAULT 'digest',
        sentiment TEXT,
        mention_type TEXT,
        urgency TEXT,
        transcript_abstract TEXT
    );

    CREATE TABLE article_categories (
        article_url TEXT NOT NULL,
        category TEXT NOT NULL,
        PRIMARY KEY (article_url, category)
    );

    CREATE TABLE article_tickers (
        article_url TEXT NOT NULL,
        ticker TEXT NOT NULL,
        PRIMARY KEY (article_url, ticker)
    );
"""

_PRODUCTION_DB = Path(__file__).resolve().parent.parent / "data" / "news.db"


def _legacy_db() -> sqlite3.Connection:
    """Open an in-memory database whose articles table predates the new columns."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_LEGACY_SCHEMA)
    return conn


def _changelog_article(**overrides: Any) -> Article:
    """A stored-shaped changelog entry; overrides win over the defaults."""
    fields: dict[str, Any] = {
        "url": "https://claude.com/en/release-notes/system-prompts#claude-opus-5-july-24-2026",
        "title": "Claude Opus 5 system prompt (July 24, 2026)",
        "source": "Claude System Prompts",
        "content": "The assistant is Claude, made by Anthropic.",
        "categories": ["ai"],
        "language": "en",
        "published_at": datetime.now(UTC),
        "changelog_digest": "DELTA vs Claude Opus 4.5: 12 of 340 sentences/tags changed.",
        "changelog_digest_source": "deterministic",
    }
    fields.update(overrides)
    article = Article(**fields)
    article.compute_hash()
    return article


def test_insert_article_round_trips_the_changelog_digest(db):
    """A column added to CREATE TABLE but forgotten in insert_article is invisible
    until the digest silently stops reaching the email, so pin the whole path."""
    article = _changelog_article()
    insert_article(db, article)

    restored = get_article_by_url(db, article.url)

    assert restored is not None
    assert restored.changelog_digest == (
        "DELTA vs Claude Opus 4.5: 12 of 340 sentences/tags changed."
    )
    assert restored.changelog_digest_source == "deterministic"


def test_migrate_db_adds_the_changelog_columns_to_a_legacy_table():
    """The 646 MB production table is created once and never re-created.

    Adding the columns to init_db alone would leave every existing database
    unable to store the field, and init_db's CREATE TABLE IF NOT EXISTS would
    quietly do nothing about it.
    """
    conn = _legacy_db()

    _migrate_db(conn)

    cols = {r[1] for r in conn.execute("PRAGMA table_info(articles)")}
    assert "changelog_digest" in cols
    assert "changelog_digest_source" in cols
    conn.close()


def test_migrate_db_is_idempotent_for_the_changelog_columns():
    """_migrate_db runs on every init_db, so every run after the first is a re-run."""
    conn = _legacy_db()

    _migrate_db(conn)
    first = [r[1] for r in conn.execute("PRAGMA table_info(articles)")]
    _migrate_db(conn)
    second = [r[1] for r in conn.execute("PRAGMA table_info(articles)")]

    assert first == second
    conn.close()


def test_row_to_article_tolerates_a_row_without_the_changelog_columns():
    """Mirrors the transcript_abstract guard: an unmigrated row must not raise.

    _migrate_db swallows every OperationalError, since that is how it detects a
    column it has already added. So an ALTER that fails for any other reason --
    a locked database, a read-only file -- leaves the column silently absent,
    and an unguarded row["changelog_digest"] turns that into an IndexError on
    every single read instead of a missing digest.
    """
    conn = _legacy_db()
    conn.execute(
        "INSERT INTO articles (url, title, source, author, published_at, content, summary, "
        "content_hash, language, relevance_score, fetched_at, included_in_digest_id, "
        "also_reported_by, pipeline, sentiment, mention_type, urgency, transcript_abstract) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "https://techcrunch.com/legacy-story",
            "A Legacy Story",
            "TechCrunch",
            "Jane Doe",
            datetime.now(UTC).isoformat(),
            "words",
            "a summary",
            "deadbeef",
            "en",
            30,
            datetime.now(UTC).isoformat(),
            None,
            None,
            "digest",
            None,
            None,
            None,
            None,
        ),
    )
    row = conn.execute("SELECT * FROM articles").fetchone()

    article = _row_to_article(conn, row)

    assert article.changelog_digest == ""
    assert article.changelog_digest_source == ""
    conn.close()


def test_backfill_changelog_digests_upgrades_a_deterministic_row(db):
    """The whole point of the source column.

    The deterministic delta is written at parse time, so the row is never empty
    and transcript-style ``IS NULL OR = ''`` would be a permanent no-op: an entry
    whose LLM upgrade landed after it was first stored is a dedup drop on every
    later run and would keep its fallback forever.
    """
    stored = _changelog_article()
    insert_article(db, stored)

    upgraded = _changelog_article(
        changelog_digest="Opus 5 replaces Opus 4.5 in the claude.ai lineup.",
        changelog_digest_source="llm",
    )
    updated = backfill_changelog_digests(db, [upgraded])

    assert updated == 1
    row = db.execute(
        "SELECT changelog_digest, changelog_digest_source FROM articles WHERE url = ?",
        (stored.url,),
    ).fetchone()
    assert row[0] == "Opus 5 replaces Opus 4.5 in the claude.ai lineup."
    assert row[1] == "llm"


def test_backfill_changelog_digests_never_churns_an_llm_row(db):
    """The parser re-derives the deterministic delta on every run.

    Without the ``!= 'llm'`` guard, each of the five daily stack runs would
    overwrite yesterday's good prose with the fallback it was upgraded from.
    """
    stored = _changelog_article(
        changelog_digest="Opus 5 replaces Opus 4.5 in the claude.ai lineup.",
        changelog_digest_source="llm",
    )
    insert_article(db, stored)

    reparsed = _changelog_article()
    updated = backfill_changelog_digests(db, [reparsed])

    assert updated == 0
    row = db.execute(
        "SELECT changelog_digest, changelog_digest_source FROM articles WHERE url = ?",
        (stored.url,),
    ).fetchone()
    assert row[0] == "Opus 5 replaces Opus 4.5 in the claude.ai lineup."
    assert row[1] == "llm"


def test_backfill_changelog_digests_fills_a_row_stored_before_the_column_existed(db):
    """A row whose digest is NULL still has to be fillable, or the columns can
    only ever be populated by an insert that dedup has already ruled out."""
    stored = _changelog_article(changelog_digest="", changelog_digest_source="")
    insert_article(db, stored)

    updated = backfill_changelog_digests(db, [_changelog_article()])

    assert updated == 1
    row = db.execute(
        "SELECT changelog_digest_source FROM articles WHERE url = ?", (stored.url,)
    ).fetchone()
    assert row[0] == "deterministic"


def test_backfill_changelog_digests_ignores_articles_without_a_digest(db):
    """Every non-changelog article in the run is handed to the backfill too."""
    plain = Article(
        url="https://techcrunch.com/story",
        title="A Story",
        source="TechCrunch",
        content="words",
        categories=["tech"],
        language="en",
        published_at=datetime.now(UTC),
    )
    plain.compute_hash()
    insert_article(db, plain)

    assert backfill_changelog_digests(db, [plain]) == 0


def test_urls_awaiting_changelog_upgrade_finds_a_fresh_deterministic_row(db):
    stored = _changelog_article()
    insert_article(db, stored)

    since = datetime.now(UTC) - timedelta(hours=36)
    assert urls_awaiting_changelog_upgrade(db, [stored.url], since=since) == {stored.url}


def test_urls_awaiting_changelog_upgrade_ignores_rows_outside_the_window(db):
    """get_articles_since filters on fetched_at, so a row older than the digest
    window can never reach the email again and retrying it is pure LLM budget."""
    stale = _changelog_article(fetched_at=datetime.now(UTC) - timedelta(hours=40))
    insert_article(db, stale)

    since = datetime.now(UTC) - timedelta(hours=36)
    assert urls_awaiting_changelog_upgrade(db, [stale.url], since=since) == set()


def test_urls_awaiting_changelog_upgrade_ignores_an_already_upgraded_row(db):
    """Re-upgrading an 'llm' row would spend 36 s a call to change nothing."""
    stored = _changelog_article(changelog_digest_source="llm")
    insert_article(db, stored)

    since = datetime.now(UTC) - timedelta(hours=36)
    assert urls_awaiting_changelog_upgrade(db, [stored.url], since=since) == set()


def test_urls_awaiting_changelog_upgrade_chunks_a_long_url_list(db):
    """An upstream reformat can put every changelog entry in one run's candidate
    list, and an unchunked IN clause then dies on 'too many SQL variables'.

    The limit is lowered for the duration of the test on purpose. This build
    allows 250k host parameters, so a realistic list could never reach the
    ceiling here and the test would pass against an implementation that does no
    chunking at all -- while the ceiling is a per-build constant that other
    SQLite packagings set far lower.
    """
    stored = _changelog_article()
    insert_article(db, stored)
    db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 600)

    urls = [f"https://example.com/{i}" for i in range(1200)] + [stored.url]
    since = datetime.now(UTC) - timedelta(hours=36)

    assert urls_awaiting_changelog_upgrade(db, urls, since=since) == {stored.url}


def test_urls_awaiting_changelog_upgrade_returns_empty_for_no_urls(db):
    since = datetime.now(UTC) - timedelta(hours=36)
    assert urls_awaiting_changelog_upgrade(db, [], since=since) == set()


@pytest.mark.skipif(not _PRODUCTION_DB.exists(), reason="production database not present")
def test_migration_is_additive_and_idempotent_on_a_copy_of_the_production_database():
    """Run the real migration against a throwaway copy of the real database.

    Three properties no synthetic fixture can establish, because all three are
    claims about the 97k rows that already exist:

    additive -- the pre-existing columns keep their names AND their order, so
    the migration is pure ALTER TABLE ADD COLUMN. A rebuild step (the usual way
    to add a NOT NULL column) would rewrite 646 MB under a live WAL reader.

    idempotent -- _migrate_db runs on every init_db, so the second pass is the
    normal case, not the exception.

    hash-stable -- every sampled real row still reproduces its stored
    content_hash through _row_to_article + compute_hash. That is the single
    check standing between this change and re-inserting the whole corpus as new
    articles.

    Copies to a temp dir that is deleted on the way out; data/news.db is never
    opened for writing.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        copy = Path(tmpdir) / "news.db"
        shutil.copyfile(_PRODUCTION_DB, copy)
        conn = sqlite3.connect(copy)
        conn.row_factory = sqlite3.Row
        try:
            before = [r["name"] for r in conn.execute("PRAGMA table_info(articles)")]
            rows_before = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
            assert rows_before > 90_000, "not the production database"
            assert "changelog_digest" not in before

            _migrate_db(conn)
            once = [r["name"] for r in conn.execute("PRAGMA table_info(articles)")]
            _migrate_db(conn)
            twice = [r["name"] for r in conn.execute("PRAGMA table_info(articles)")]

            assert once == twice, "second migration pass changed the schema"
            assert once[: len(before)] == before, "existing columns moved or were renamed"
            assert once[len(before) :] == ["changelog_digest", "changelog_digest_source"]
            assert conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == rows_before

            # Oldest and newest rows: the formula has to hold across the whole
            # corpus, and the two ends are where a drift would show first.
            sample = conn.execute("SELECT * FROM articles ORDER BY rowid LIMIT 500").fetchall()
            sample += conn.execute(
                "SELECT * FROM articles ORDER BY rowid DESC LIMIT 500"
            ).fetchall()
            assert len(sample) == 1000

            for row in sample:
                article = _row_to_article(conn, row)
                assert article.changelog_digest == ""
                assert article.changelog_digest_source == ""
                stored_hash = article.content_hash
                article.compute_hash()
                assert article.content_hash == stored_hash, f"hash drifted for {article.url}"
        finally:
            conn.close()


def test_urls_already_upgraded_reports_rows_holding_llm_prose(db):
    """A drifted content_hash lets an already-upgraded entry survive dedup and
    reach the enrichment candidates, where it buys a paid CLI call whose prose
    insert_article then discards against the url PRIMARY KEY. Every run, forever."""
    upgraded = _changelog_article(changelog_digest_source="llm")
    insert_article(db, upgraded)
    pending = _changelog_article(url="https://example.com/notes#b")
    insert_article(db, pending)

    result = urls_already_upgraded(db, [upgraded.url, pending.url, "https://example.com/absent"])

    assert result == {upgraded.url}
