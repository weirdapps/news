"""Query functions for the news MCP server."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone


def _sanitize_fts_query(query: str) -> str:
    """Escape a user query for safe FTS5 MATCH usage.

    Wraps each word in double quotes to treat as literals,
    preventing FTS5 syntax errors from operators like OR, NOT, AND.
    """
    words = query.split()
    return " ".join(f'"{w}"' for w in words if w)


def search_articles(
    conn: sqlite3.Connection,
    query: str,
    pipeline: str | None = None,
    category: str | None = None,
    days: int = 30,
    limit: int = 20,
) -> list[dict]:
    """Search articles by keyword using FTS5 full-text search.

    Args:
        conn: SQLite connection
        query: Search keyword (FTS5 syntax supported)
        pipeline: Optional filter: 'digest' or 'monitor'
        category: Optional category filter
        days: Lookback period in days (default: 30)
        limit: Maximum results (default: 20)

    Returns:
        List of article dicts with title, source, url, categories, published_at, pipeline
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    since_str = since.isoformat()
    fts_query = _sanitize_fts_query(query)

    # Use FTS5 for search
    if category:
        sql = """
            SELECT DISTINCT a.url, a.title, a.source, a.published_at,
                   a.relevance_score, a.pipeline, a.fetched_at
            FROM articles a
            JOIN articles_fts fts ON a.rowid = fts.rowid
            JOIN article_categories ac ON a.url = ac.article_url
            WHERE articles_fts MATCH ?
              AND a.fetched_at >= ?
              AND ac.category = ?
        """
        params: list = [fts_query, since_str, category]
    else:
        sql = """
            SELECT a.url, a.title, a.source, a.published_at,
                   a.relevance_score, a.pipeline, a.fetched_at
            FROM articles a
            JOIN articles_fts fts ON a.rowid = fts.rowid
            WHERE articles_fts MATCH ?
              AND a.fetched_at >= ?
        """
        params = [fts_query, since_str]

    if pipeline:
        sql += " AND a.pipeline = ?"
        params.append(pipeline)

    sql += " ORDER BY a.fetched_at DESC LIMIT ?"
    params.append(limit)

    cursor = conn.execute(sql, params)
    rows = cursor.fetchall()

    results = []
    for row in rows:
        cat_cursor = conn.execute(
            "SELECT category FROM article_categories WHERE article_url = ?",
            (row["url"],),
        )
        categories = [c["category"] for c in cat_cursor.fetchall()]

        results.append(
            {
                "title": row["title"],
                "source": row["source"],
                "url": row["url"],
                "published_at": row["published_at"],
                "relevance_score": row["relevance_score"],
                "pipeline": row["pipeline"],
                "categories": categories,
            }
        )

    return results


def get_digest_history(
    conn: sqlite3.Connection,
    pipeline: str = "digest",
    days: int = 7,
    limit: int = 10,
) -> list[dict]:
    """Get recent digest/monitor synthesis history.

    Returns the structured synthesis (executive briefs, sections) without
    the bulky HTML output.

    Args:
        conn: SQLite connection
        pipeline: 'digest' or 'monitor' (default: 'digest')
        days: Lookback period in days (default: 7)
        limit: Maximum results (default: 10)

    Returns:
        List of digest dicts with created_at, article_count, executive_brief, sections
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    since_str = since.isoformat()

    cursor = conn.execute(
        """
        SELECT id, digest_type, created_at, article_count, synthesis_text, sent_at, pipeline
        FROM digests
        WHERE pipeline = ? AND created_at >= ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (pipeline, since_str, limit),
    )

    results = []
    for row in cursor.fetchall():
        entry = {
            "id": row["id"],
            "digest_type": row["digest_type"],
            "created_at": row["created_at"],
            "article_count": row["article_count"],
            "sent_at": row["sent_at"],
            "pipeline": row["pipeline"],
        }

        if row["synthesis_text"]:
            try:
                synthesis = json.loads(row["synthesis_text"])
                entry["executive_brief"] = synthesis.get("executive_brief", [])
                entry["what_changed"] = synthesis.get("what_changed", "")
                entry["sections"] = synthesis.get("sections", [])
                if pipeline == "monitor":
                    entry["alerts"] = synthesis.get("alerts", [])
                    entry["sentiment_summary"] = synthesis.get("sentiment_summary")
            except json.JSONDecodeError:
                entry["executive_brief"] = []
                entry["raw_text"] = row["synthesis_text"][:500]

        results.append(entry)

    return results


def get_news_stats(conn: sqlite3.Connection) -> dict:
    """Get database statistics.

    Returns:
        Dict with total_articles, total_digests, digest_articles, monitor_articles,
        categories, sources, date_range
    """
    stats = {}

    row = conn.execute("SELECT COUNT(*) as cnt FROM articles").fetchone()
    stats["total_articles"] = row["cnt"]

    row = conn.execute("SELECT COUNT(*) as cnt FROM digests").fetchone()
    stats["total_digests"] = row["cnt"]

    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM articles WHERE pipeline = 'digest'"
    ).fetchone()
    stats["digest_articles"] = row["cnt"]

    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM articles WHERE pipeline = 'monitor'"
    ).fetchone()
    stats["monitor_articles"] = row["cnt"]

    cat_rows = conn.execute(
        "SELECT category, COUNT(*) as cnt FROM article_categories GROUP BY category ORDER BY cnt DESC"
    ).fetchall()
    stats["categories"] = {r["category"]: r["cnt"] for r in cat_rows}

    src_rows = conn.execute(
        "SELECT source, COUNT(*) as cnt FROM articles GROUP BY source ORDER BY cnt DESC LIMIT 10"
    ).fetchall()
    stats["top_sources"] = {r["source"]: r["cnt"] for r in src_rows}

    row = conn.execute(
        "SELECT MIN(fetched_at) as earliest, MAX(fetched_at) as latest FROM articles"
    ).fetchone()
    stats["earliest_article"] = row["earliest"]
    stats["latest_article"] = row["latest"]

    return stats
