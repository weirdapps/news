import sqlite3
from datetime import datetime
from news.storage import init_db, insert_article
from news.models import Article
from news.query import search_articles


def _art(url, title, tickers):
    return Article(
        url=url, title=title, source="s", author=None,
        published_at=datetime(2026, 5, 3), content=title,
        summary=None, content_hash=url, language="en",
        relevance_score=10, fetched_at=datetime(2026, 5, 3),
        categories=["business"], tickers=tickers,
    )


def test_search_filters_by_ticker():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    insert_article(conn, _art("http://x/1", "Apple earnings cloud report", ["AAPL"]))
    insert_article(conn, _art("http://x/2", "Microsoft cloud earnings", ["MSFT"]))
    insert_article(conn, _art("http://x/3", "Cloud earnings both Apple and Microsoft", ["AAPL", "MSFT"]))
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
