import sqlite3
from datetime import datetime

from news.models import Article
from news.query import search_articles
from news.storage import init_db, insert_article


def _art(url, title, tickers):
    return Article(
        url=url,
        title=title,
        source="s",
        author=None,
        published_at=datetime.now(),
        content=title,
        summary=None,
        content_hash=url,
        language="en",
        relevance_score=10,
        fetched_at=datetime.now(),
        categories=["business"],
        tickers=tickers,
    )


def test_search_filters_by_ticker():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    insert_article(conn, _art("http://x/1", "Apple earnings cloud report", ["AAPL"]))
    insert_article(conn, _art("http://x/2", "Microsoft cloud earnings", ["MSFT"]))
    insert_article(
        conn,
        _art("http://x/3", "Cloud earnings both Apple and Microsoft", ["AAPL", "MSFT"]),
    )
    results = search_articles(conn, "earnings cloud", ticker="AAPL", days=30, limit=10)
    urls = [r["url"] for r in results]
    assert "http://x/1" in urls
    assert "http://x/3" in urls
    assert "http://x/2" not in urls


def test_search_no_ticker_filter_returns_all_matches():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    insert_article(conn, _art("http://x/1", "Apple earnings cloud", ["AAPL"]))
    insert_article(conn, _art("http://x/2", "Microsoft cloud earnings", ["MSFT"]))
    results = search_articles(conn, "earnings cloud", days=30, limit=10)
    assert len(results) == 2


def test_search_ticker_filter_with_no_matches():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    insert_article(conn, _art("http://x/1", "Apple earnings", ["AAPL"]))
    results = search_articles(conn, "earnings", ticker="NVDA", days=30, limit=10)
    assert results == []


def test_recent_for_tickers_returns_articles_for_any_ticker_in_list():
    from news.query import recent_for_tickers

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    insert_article(conn, _art("http://x/1", "Apple news", ["AAPL"]))
    insert_article(conn, _art("http://x/2", "Microsoft news", ["MSFT"]))
    insert_article(conn, _art("http://x/3", "Google news", ["GOOG"]))
    results = recent_for_tickers(conn, tickers=["AAPL", "MSFT"], hours=72, limit=10)
    urls = sorted(r["url"] for r in results)
    assert urls == ["http://x/1", "http://x/2"]


def test_recent_for_tickers_respects_hours_window():
    from news.query import recent_for_tickers

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    # Insert old article (manipulate fetched_at directly)
    insert_article(conn, _art("http://x/1", "Old Apple", ["AAPL"]))
    conn.execute("UPDATE articles SET fetched_at='2020-01-01T00:00:00' WHERE url='http://x/1'")
    # Recent article
    insert_article(conn, _art("http://x/2", "Fresh Apple", ["AAPL"]))
    results = recent_for_tickers(conn, tickers=["AAPL"], hours=24, limit=10)
    assert [r["url"] for r in results] == ["http://x/2"]


def test_recent_for_tickers_empty_list():
    from news.query import recent_for_tickers

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    insert_article(conn, _art("http://x/1", "Apple", ["AAPL"]))
    assert recent_for_tickers(conn, tickers=[], hours=72, limit=10) == []
