"""One-time (and resumable) backfill of article_tickers for existing articles.

Iterates articles that have NO entry in article_tickers, runs the tagger,
inserts results. Safe to run repeatedly — skips already-tagged rows.

Usage:
    python scripts/backfill_tickers.py            # process all untagged
    python scripts/backfill_tickers.py --max 100  # process up to 100 (smoke test)
    python scripts/backfill_tickers.py --batch 500 --commit-every 100
"""

from __future__ import annotations
import argparse
import sqlite3
import sys
import time
from pathlib import Path

# Ensure news package importable when run from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

from news.storage import get_connection, init_db
from news.tagger import tag_article
from news.models import Article
from datetime import datetime


def _row_to_minimal_article(row) -> Article:
    """Build minimal Article for the tagger — only needs title, content, categories."""
    return Article(
        url=row["url"],
        title=row["title"] or "",
        source=row["source"],
        author=None,
        published_at=datetime.fromisoformat(row["published_at"]),
        content=row["content"] or "",
        summary=None,
        content_hash=row["content_hash"],
        language=None,
        relevance_score=0,
        fetched_at=datetime.fromisoformat(row["fetched_at"]),
        categories=[],
        tickers=[],
    )


def backfill(
    conn: sqlite3.Connection,
    batch_size: int = 500,
    max_articles: int | None = None,
    commit_every: int = 100,
) -> tuple[int, int]:
    """Process untagged articles. Returns (n_processed, n_tagged).

    Note: For articles with no tickers, this script inserts nothing into
    article_tickers, meaning they remain "untagged". This is fine for a
    one-time backfill of the existing corpus - the goal is to tag articles
    that HAVE tickers. Articles with no tickers don't need junction rows.
    """
    # Load category map per article in one query
    cat_rows = conn.execute(
        "SELECT article_url, category FROM article_categories"
    ).fetchall()
    cat_map: dict[str, list[str]] = {}
    for r in cat_rows:
        cat_map.setdefault(r["article_url"], []).append(r["category"])

    # Fetch ALL untagged articles (or up to max_articles)
    limit = max_articles if max_articles else -1
    rows = conn.execute(
        """
        SELECT a.* FROM articles a
        WHERE NOT EXISTS (
            SELECT 1 FROM article_tickers at WHERE at.article_url = a.url
        )
        ORDER BY a.fetched_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    n_processed = 0
    n_tagged = 0
    for row in rows:
        art = _row_to_minimal_article(row)
        art.categories = cat_map.get(art.url, [])
        tag_article(art)
        for ticker in art.tickers:
            conn.execute(
                "INSERT OR IGNORE INTO article_tickers (article_url, ticker) VALUES (?, ?)",
                (art.url, ticker),
            )
        if art.tickers:
            n_tagged += 1
        n_processed += 1
        if n_processed % commit_every == 0:
            conn.commit()
            print(f"  ... {n_processed} processed, {n_tagged} tagged", flush=True)
    conn.commit()
    return n_processed, n_tagged


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, default=None)
    parser.add_argument("--batch", type=int, default=500)
    parser.add_argument("--commit-every", type=int, default=100)
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="Path to news.db (default: <repo_root>/data/news.db)",
    )
    args = parser.parse_args()

    if args.db_path:
        db_path = Path(args.db_path).expanduser()
    else:
        db_path = Path(__file__).parent.parent / "data" / "news.db"
    print(f"Using DB: {db_path}", flush=True)
    conn = get_connection(db_path)
    init_db(conn)
    start = time.time()
    n_processed, n_tagged = backfill(conn, args.batch, args.max, args.commit_every)
    elapsed = time.time() - start
    print(
        f"Done. Processed {n_processed} in {elapsed:.0f}s, tagged {n_tagged} ({100 * n_tagged / max(n_processed, 1):.0f}%)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
