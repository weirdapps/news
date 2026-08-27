"""SQLite storage layer for news articles and digests."""

import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from news.models import Article, Digest

logger = logging.getLogger(__name__)


def get_connection(db_path: str | Path) -> sqlite3.Connection:
    """Open SQLite connection with row_factory, WAL mode, and foreign keys enabled."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _dt_to_str(dt: datetime | None) -> str | None:
    """Convert datetime to ISO string."""
    if dt is None:
        return None
    return dt.isoformat()


def _str_to_dt(s: str | None) -> datetime | None:
    """Convert ISO string to datetime."""
    if s is None:
        return None
    return datetime.fromisoformat(s)


def _migrate_db(conn: sqlite3.Connection) -> None:
    """Add new columns to existing tables (safe to run repeatedly).

    Each ALTER TABLE is wrapped in a try/except because SQLite raises
    OperationalError if the column already exists. All values here are
    hardcoded schema constants, not user input.
    """
    stmts = [
        "ALTER TABLE articles ADD COLUMN pipeline TEXT DEFAULT 'digest'",
        "ALTER TABLE articles ADD COLUMN sentiment TEXT",
        "ALTER TABLE articles ADD COLUMN mention_type TEXT",
        "ALTER TABLE articles ADD COLUMN urgency TEXT",
        "ALTER TABLE articles ADD COLUMN transcript_abstract TEXT",
        "ALTER TABLE articles ADD COLUMN changelog_digest TEXT",
        "ALTER TABLE articles ADD COLUMN changelog_digest_source TEXT",
        "ALTER TABLE digests ADD COLUMN pipeline TEXT DEFAULT 'digest'",
        "CREATE TABLE IF NOT EXISTS article_tickers (article_url TEXT NOT NULL, ticker TEXT NOT NULL, PRIMARY KEY (article_url, ticker), FOREIGN KEY (article_url) REFERENCES articles(url) ON DELETE CASCADE)",
        "CREATE INDEX IF NOT EXISTS idx_article_tickers_ticker ON article_tickers(ticker)",
    ]
    for stmt in stmts:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # Column already exists
    conn.commit()


def _backfill_fts(conn: sqlite3.Connection) -> None:
    """Backfill FTS5 index from existing articles (idempotent)."""
    row = conn.execute("SELECT COUNT(*) as cnt FROM articles_fts").fetchone()
    if row["cnt"] == 0:
        article_count = conn.execute("SELECT COUNT(*) as cnt FROM articles").fetchone()["cnt"]
        if article_count > 0:
            conn.execute("""
                INSERT INTO articles_fts(rowid, title, content, source)
                SELECT rowid, title, content, source FROM articles
            """)
            conn.commit()


def init_db(conn: sqlite3.Connection) -> None:
    """Initialize database schema with all required tables and indexes."""
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS articles (
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
            transcript_abstract TEXT,
            changelog_digest TEXT,
            changelog_digest_source TEXT,
            FOREIGN KEY (included_in_digest_id) REFERENCES digests(id)
        );

        CREATE TABLE IF NOT EXISTS article_categories (
            article_url TEXT NOT NULL,
            category TEXT NOT NULL,
            PRIMARY KEY (article_url, category),
            FOREIGN KEY (article_url) REFERENCES articles(url) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS article_tickers (
            article_url TEXT NOT NULL,
            ticker TEXT NOT NULL,
            PRIMARY KEY (article_url, ticker),
            FOREIGN KEY (article_url) REFERENCES articles(url) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS digests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            digest_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            article_count INTEGER NOT NULL,
            synthesis_text TEXT,
            html_output TEXT,
            sent_at TEXT,
            pipeline TEXT DEFAULT 'digest'
        );

        CREATE TABLE IF NOT EXISTS sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT UNIQUE NOT NULL,
            category TEXT,
            tier TEXT,
            language TEXT,
            last_fetched TEXT,
            fetch_count INTEGER DEFAULT 0,
            error_count INTEGER DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_content_hash ON articles(content_hash);
        CREATE INDEX IF NOT EXISTS idx_fetched_at ON articles(fetched_at);
        CREATE INDEX IF NOT EXISTS idx_category ON article_categories(category);
        CREATE INDEX IF NOT EXISTS idx_article_tickers_ticker ON article_tickers(ticker);
    """)
    conn.commit()

    # Migrate existing databases that lack new columns, then add index
    _migrate_db(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pipeline ON articles(pipeline)")
    conn.commit()

    # FTS5 index for full-text search on articles
    conn.executescript("""
        CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
            title, content, source,
            content='articles',
            content_rowid='rowid'
        );

        CREATE TRIGGER IF NOT EXISTS articles_fts_insert AFTER INSERT ON articles BEGIN
            INSERT INTO articles_fts(rowid, title, content, source)
            VALUES (NEW.rowid, NEW.title, NEW.content, NEW.source);
        END;

        CREATE TRIGGER IF NOT EXISTS articles_fts_delete AFTER DELETE ON articles BEGIN
            INSERT INTO articles_fts(articles_fts, rowid, title, content, source)
            VALUES ('delete', OLD.rowid, OLD.title, OLD.content, OLD.source);
        END;

        CREATE TRIGGER IF NOT EXISTS articles_fts_update AFTER UPDATE ON articles BEGIN
            INSERT INTO articles_fts(articles_fts, rowid, title, content, source)
            VALUES ('delete', OLD.rowid, OLD.title, OLD.content, OLD.source);
            INSERT INTO articles_fts(rowid, title, content, source)
            VALUES (NEW.rowid, NEW.title, NEW.content, NEW.source);
        END;
    """)

    _backfill_fts(conn)


def _row_to_article(conn: sqlite3.Connection, row: sqlite3.Row) -> Article:
    """Convert database row to Article, loading categories from junction table."""
    # Load categories for this article
    cursor = conn.execute(
        "SELECT category FROM article_categories WHERE article_url = ?", (row["url"],)
    )
    categories = [cat_row["category"] for cat_row in cursor.fetchall()]

    # Load tickers for this article
    ticker_rows = conn.execute(
        "SELECT ticker FROM article_tickers WHERE article_url = ? ORDER BY ticker",
        (row["url"],),
    ).fetchall()
    tickers = [r["ticker"] for r in ticker_rows]

    # Parse also_reported_by JSON if present
    also_reported_by: list[str] = []
    if row["also_reported_by"]:
        also_reported_by = json.loads(row["also_reported_by"])

    # fetched_at column is NOT NULL in schema; default just in case
    fetched_at = _str_to_dt(row["fetched_at"]) or datetime.now(UTC)

    return Article(
        url=row["url"],
        title=row["title"],
        source=row["source"],
        author=row["author"],
        published_at=_str_to_dt(row["published_at"]),
        content=row["content"],
        summary=row["summary"],
        content_hash=row["content_hash"],
        categories=categories,
        language=row["language"],
        relevance_score=row["relevance_score"],
        fetched_at=fetched_at,
        included_in_digest_id=row["included_in_digest_id"],
        also_reported_by=also_reported_by,
        pipeline=row["pipeline"] or "digest",
        sentiment=row["sentiment"] or "",
        mention_type=row["mention_type"] or "",
        urgency=row["urgency"] or "",
        transcript_abstract=(
            row["transcript_abstract"] if "transcript_abstract" in row.keys() else ""
        )
        or "",
        changelog_digest=(row["changelog_digest"] if "changelog_digest" in row.keys() else "")
        or "",
        changelog_digest_source=(
            row["changelog_digest_source"] if "changelog_digest_source" in row.keys() else ""
        )
        or "",
        tickers=tickers,
    )


def backfill_transcript_abstracts(conn: sqlite3.Connection, articles: list[Article]) -> int:
    """Write abstracts onto already-stored rows that lack one. Returns rows updated.

    Necessary because the abstract deliberately stays out of ``compute_hash()``.
    That keeps dedup stable, but it also means a video first stored before the
    harvester reached it -- published shortly before a run, or harvested while
    the Mac was asleep -- is dropped as a duplicate on every later run and its
    row would keep NULL forever. The enrichment would be computed and then
    silently thrown away.

    Only fills empty values, so a good abstract is never churned by a later one.
    """
    updated = 0
    for article in articles:
        if not article.transcript_abstract:
            continue
        cursor = conn.execute(
            "UPDATE articles SET transcript_abstract = ? "
            "WHERE url = ? AND (transcript_abstract IS NULL OR transcript_abstract = '')",
            (article.transcript_abstract, article.url),
        )
        updated += cursor.rowcount
    if updated:
        conn.commit()
    return updated


# SQLite caps host parameters per statement, so a run that re-parses every
# changelog entry at once cannot be asked in a single IN clause.
_URL_CHUNK_SIZE = 500


def urls_awaiting_changelog_upgrade(
    conn: sqlite3.Connection, urls: list[str], since: datetime
) -> set[str]:
    """Find stored rows still carrying the parse-time fallback digest.

    This is what makes graceful degradation temporary. An entry whose LLM
    upgrade timed out is stored with the deterministic delta, and from the next
    run onwards it is a dedup drop -- ``insert_article`` never sees it again, so
    without this lookup it would keep its fallback forever.

    Args:
        conn: Open database connection.
        urls: Candidate URLs, typically the changelog entries dedup dropped.
        since: Lower bound on ``fetched_at``. ``get_articles_since`` filters on
            that same column, so a row older than the digest window can never
            reach the email again and spending an LLM call on it buys nothing.

    Returns:
        The subset of ``urls`` whose row is stored with a deterministic digest.
    """
    pending: set[str] = set()
    cutoff = _dt_to_str(since)
    for start in range(0, len(urls), _URL_CHUNK_SIZE):
        chunk = urls[start : start + _URL_CHUNK_SIZE]
        # Only the placeholder count is interpolated; every value is bound.
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            "SELECT url FROM articles "
            "WHERE changelog_digest_source = 'deterministic' AND fetched_at >= ? "
            f"AND url IN ({placeholders})",
            [cutoff, *chunk],
        ).fetchall()
        pending.update(row[0] for row in rows)
    return pending


def urls_already_upgraded(conn: sqlite3.Connection, urls: list[str]) -> set[str]:
    """Find stored rows whose digest is already LLM prose.

    The mirror image of ``urls_awaiting_changelog_upgrade``, and the guard on the
    other half of the candidate list. ``compute_hash()`` reads title plus the
    first 200 characters, while the url is built from the unchanged model-and-date
    label, so a vendor edit anywhere else in the body moves the hash but not the
    url. Such an entry survives dedup, looks new, earns a paid CLI call, and then
    loses its prose to the url PRIMARY KEY on insert. Without this lookup that
    repeats on every run for as long as the entry stays in window.

    Args:
        conn: Open database connection.
        urls: Candidate URLs, typically the changelog entries that survived dedup.

    Returns:
        The subset of ``urls`` already stored with an LLM-upgraded digest.
    """
    upgraded: set[str] = set()
    for start in range(0, len(urls), _URL_CHUNK_SIZE):
        chunk = urls[start : start + _URL_CHUNK_SIZE]
        # Only the placeholder count is interpolated; every value is bound.
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            "SELECT url FROM articles "
            f"WHERE changelog_digest_source = 'llm' AND url IN ({placeholders})",
            chunk,
        ).fetchall()
        upgraded.update(row[0] for row in rows)
    return upgraded


def backfill_changelog_digests(conn: sqlite3.Connection, articles: list[Article]) -> int:
    """Write digests onto already-stored rows. Returns rows updated.

    Necessary for the same reason as ``backfill_transcript_abstracts``: the
    digest deliberately stays out of ``compute_hash()``, so an entry first
    stored before its digest was upgraded is dropped as a duplicate on every
    later run and the enrichment would be computed and then thrown away.

    The predicate is NOT that function's ``IS NULL OR = ''``. The deterministic
    delta is written at parse time, so the column is never empty and that test
    would be a permanent no-op. Matching on ``!= 'llm'`` instead lets a genuine
    prose upgrade land on top of a fallback, while still refusing to churn a
    good prose digest back down to the deterministic delta the parser re-derives
    on every one of the five daily runs.
    """
    updated = 0
    for article in articles:
        if not article.changelog_digest:
            continue
        cursor = conn.execute(
            "UPDATE articles SET changelog_digest = ?, changelog_digest_source = ? "
            "WHERE url = ? AND (changelog_digest IS NULL OR changelog_digest = '' "
            "OR changelog_digest_source != 'llm')",
            (article.changelog_digest, article.changelog_digest_source, article.url),
        )
        updated += cursor.rowcount
    if updated:
        conn.commit()
    return updated


def insert_article(conn: sqlite3.Connection, article: Article) -> bool:
    """
    Insert article and its categories.

    Returns:
        True if inserted, False if duplicate (IntegrityError).
    """
    try:
        # Insert article
        conn.execute(
            """
            INSERT INTO articles (
                url, title, source, author, published_at, content, summary,
                content_hash, language, relevance_score, fetched_at,
                included_in_digest_id, also_reported_by,
                pipeline, sentiment, mention_type, urgency, transcript_abstract,
                changelog_digest, changelog_digest_source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                article.url,
                article.title,
                article.source,
                article.author,
                _dt_to_str(article.published_at),
                article.content,
                article.summary,
                article.content_hash,
                article.language,
                article.relevance_score,
                _dt_to_str(article.fetched_at),
                article.included_in_digest_id,
                json.dumps(article.also_reported_by) if article.also_reported_by else None,
                article.pipeline,
                article.sentiment or None,
                article.mention_type or None,
                article.urgency or None,
                article.transcript_abstract or None,
                article.changelog_digest or None,
                article.changelog_digest_source or None,
            ),
        )

        # Insert categories
        for category in article.categories:
            conn.execute(
                """
                INSERT INTO article_categories (article_url, category)
                VALUES (?, ?)
            """,
                (article.url, category),
            )

        # Insert tickers
        if article.tickers:
            for ticker in article.tickers:
                conn.execute(
                    "INSERT OR IGNORE INTO article_tickers (article_url, ticker) VALUES (?, ?)",
                    (article.url, ticker.upper()),
                )

        conn.commit()
        return True

    except sqlite3.IntegrityError as exc:
        conn.rollback()
        # A duplicate URL is the expected, boring case and stays silent: the fetch
        # window overlaps by design and most runs re-see most articles.
        #
        # Every OTHER integrity failure used to return the same quiet False, and one
        # of them was load-bearing. A feed carrying no date field yields
        # published_at=None (fetcher.normalize_rss_entry), which filter_quality waves
        # through because None is falsy, and which this INSERT then puts into a
        # NOT NULL column. The article was counted as a duplicate, dropped forever,
        # and its source reported as "silent" in the digest footer -- for months, for
        # every dateless feed. Naming the constraint is the difference between a
        # statistic and a bug report.
        if "UNIQUE constraint failed" not in str(exc):
            logger.warning(
                "insert_article dropped %s from %s: %s", article.url, article.source, exc
            )
        return False


def get_article_by_url(conn: sqlite3.Connection, url: str) -> Article | None:
    """Retrieve article by URL."""
    cursor = conn.execute("SELECT * FROM articles WHERE url = ?", (url,))
    row = cursor.fetchone()
    if row is None:
        return None
    return _row_to_article(conn, row)


def get_article_by_hash(conn: sqlite3.Connection, content_hash: str) -> Article | None:
    """Retrieve article by content hash."""
    cursor = conn.execute("SELECT * FROM articles WHERE content_hash = ?", (content_hash,))
    row = cursor.fetchone()
    if row is None:
        return None
    return _row_to_article(conn, row)


def get_articles_since(
    conn: sqlite3.Connection,
    since: datetime,
    min_score: int = 0,
    category: str | None = None,
    pipeline: str = "digest",
) -> list[Article]:
    """
    Get articles fetched since a timestamp, with optional filtering.

    Args:
        since: Minimum fetched_at timestamp
        min_score: Minimum relevance_score (default 0)
        category: Optional category filter
        pipeline: Pipeline filter ('digest' or 'monitor')
    """
    query = "SELECT * FROM articles WHERE fetched_at >= ? AND relevance_score >= ? AND pipeline = ?"
    params: list = [_dt_to_str(since), min_score, pipeline]

    if category:
        query = """
            SELECT a.* FROM articles a
            JOIN article_categories ac ON a.url = ac.article_url
            WHERE a.fetched_at >= ? AND a.relevance_score >= ? AND a.pipeline = ? AND ac.category = ?
        """
        params.append(category)

    cursor = conn.execute(query, params)
    return [_row_to_article(conn, row) for row in cursor.fetchall()]


def insert_digest(conn: sqlite3.Connection, digest: Digest) -> int:
    """
    Insert digest and return its ID.

    Returns:
        The digest ID (lastrowid)
    """
    cursor = conn.execute(
        """
        INSERT INTO digests (
            digest_type, created_at, article_count, synthesis_text, html_output, sent_at,
            pipeline
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """,
        (
            digest.digest_type,
            _dt_to_str(digest.created_at),
            digest.article_count,
            digest.synthesis_text,
            digest.html_output,
            _dt_to_str(digest.sent_at),
            digest.pipeline,
        ),
    )
    conn.commit()
    # lastrowid is None only if no row was inserted; right after a successful
    # INSERT with AUTOINCREMENT it is always populated.
    return cursor.lastrowid or 0


def get_last_digest(conn: sqlite3.Connection, pipeline: str = "digest") -> Digest | None:
    """Get the most recent digest for a given pipeline."""
    cursor = conn.execute(
        """
        SELECT * FROM digests
        WHERE pipeline = ?
        ORDER BY created_at DESC
        LIMIT 1
    """,
        (pipeline,),
    )
    row = cursor.fetchone()
    if row is None:
        return None

    # created_at column is NOT NULL in schema; default just in case
    created_at = _str_to_dt(row["created_at"]) or datetime.now(UTC)

    return Digest(
        id=row["id"],
        digest_type=row["digest_type"],
        created_at=created_at,
        article_count=row["article_count"],
        synthesis_text=row["synthesis_text"],
        html_output=row["html_output"],
        sent_at=_str_to_dt(row["sent_at"]),
        pipeline=row["pipeline"] or "digest",
    )


def update_digest_sent(conn: sqlite3.Connection, digest_id: int) -> None:
    """Mark digest as sent with current timestamp."""
    conn.execute(
        """
        UPDATE digests
        SET sent_at = ?
        WHERE id = ?
    """,
        (_dt_to_str(datetime.now(UTC)), digest_id),
    )
    conn.commit()


def get_run_stats(conn: sqlite3.Connection) -> dict:
    """Get statistics about articles and digests."""
    cursor = conn.execute("SELECT COUNT(*) FROM articles")
    total_articles = cursor.fetchone()[0]

    cursor = conn.execute("SELECT COUNT(*) FROM digests")
    total_digests = cursor.fetchone()[0]

    return {
        "total_articles": total_articles,
        "total_digests": total_digests,
    }
