import sqlite3
import pytest
from datetime import datetime
from news.models import Article
from news.storage import init_db, insert_article, get_article_by_url


def _make_article(url="http://x/1", tickers=None):
    return Article(
        url=url, title="t", source="s", author=None,
        published_at=datetime(2026, 5, 3), content="c",
        summary=None, content_hash="h", language="en",
        relevance_score=10, fetched_at=datetime(2026, 5, 3),
        categories=["business"], tickers=tickers or [],
    )


def test_article_tickers_table_exists():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='article_tickers'"
    )
    assert cursor.fetchone() is not None


def test_article_tickers_has_index():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_article_tickers_ticker'"
    )
    assert cursor.fetchone() is not None


def test_article_tickers_cascade_delete():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT INTO articles (url, title, source, published_at, content, content_hash, fetched_at) "
        "VALUES ('http://x/1', 't', 's', '2026-05-03', 'c', 'h', '2026-05-03')"
    )
    conn.execute("INSERT INTO article_tickers VALUES ('http://x/1', 'AAPL')")
    conn.execute("DELETE FROM articles WHERE url='http://x/1'")
    cursor = conn.execute("SELECT COUNT(*) FROM article_tickers")
    assert cursor.fetchone()[0] == 0


def test_insert_article_persists_tickers():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    art = _make_article(tickers=["AAPL", "MSFT"])
    insert_article(conn, art)
    rows = conn.execute(
        "SELECT ticker FROM article_tickers WHERE article_url=? ORDER BY ticker",
        (art.url,)
    ).fetchall()
    assert [r[0] for r in rows] == ["AAPL", "MSFT"]


def test_get_article_by_url_loads_tickers():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    art = _make_article(tickers=["GOOG"])
    insert_article(conn, art)
    loaded = get_article_by_url(conn, art.url)
    assert loaded.tickers == ["GOOG"]


def test_insert_article_with_no_tickers():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    art = _make_article(tickers=[])
    insert_article(conn, art)
    loaded = get_article_by_url(conn, art.url)
    assert loaded.tickers == []
