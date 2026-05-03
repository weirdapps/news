"""SQLite storage layer for news articles and digests."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from news.models import Article, Digest


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
        article_count = conn.execute("SELECT COUNT(*) as cnt FROM articles").fetchone()[
            "cnt"
        ]
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

    # Parse also_reported_by JSON if present
    also_reported_by = None
    if row["also_reported_by"]:
        also_reported_by = json.loads(row["also_reported_by"])

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
        fetched_at=_str_to_dt(row["fetched_at"]),
        included_in_digest_id=row["included_in_digest_id"],
        also_reported_by=also_reported_by,
        pipeline=row["pipeline"] or "digest",
        sentiment=row["sentiment"] or "",
        mention_type=row["mention_type"] or "",
        urgency=row["urgency"] or "",
    )


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
                pipeline, sentiment, mention_type, urgency
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                json.dumps(article.also_reported_by)
                if article.also_reported_by
                else None,
                article.pipeline,
                article.sentiment or None,
                article.mention_type or None,
                article.urgency or None,
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

        conn.commit()
        return True

    except sqlite3.IntegrityError:
        # Duplicate URL
        conn.rollback()
        return False


def get_article_by_url(conn: sqlite3.Connection, url: str) -> Optional[Article]:
    """Retrieve article by URL."""
    cursor = conn.execute("SELECT * FROM articles WHERE url = ?", (url,))
    row = cursor.fetchone()
    if row is None:
        return None
    return _row_to_article(conn, row)


def get_article_by_hash(
    conn: sqlite3.Connection, content_hash: str
) -> Optional[Article]:
    """Retrieve article by content hash."""
    cursor = conn.execute(
        "SELECT * FROM articles WHERE content_hash = ?", (content_hash,)
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return _row_to_article(conn, row)


def get_articles_since(
    conn: sqlite3.Connection,
    since: datetime,
    min_score: int = 0,
    category: Optional[str] = None,
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
    return cursor.lastrowid


def get_last_digest(
    conn: sqlite3.Connection, pipeline: str = "digest"
) -> Optional[Digest]:
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

    return Digest(
        id=row["id"],
        digest_type=row["digest_type"],
        created_at=_str_to_dt(row["created_at"]),
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
        (_dt_to_str(datetime.now(timezone.utc)), digest_id),
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
