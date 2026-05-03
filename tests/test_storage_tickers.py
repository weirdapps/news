import sqlite3
import pytest
from news.storage import init_db


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
