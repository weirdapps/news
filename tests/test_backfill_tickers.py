import sqlite3
from unittest.mock import patch
from news.storage import init_db, insert_article
from news.models import Article
from datetime import datetime
from scripts.backfill_tickers import backfill

TICKER_DICT = {"apple": "AAPL", "aapl": "AAPL"}


def _art(url, title, content):
    return Article(
        url=url, title=title, source="s", author=None,
        published_at=datetime(2026, 5, 3), content=content,
        summary=None, content_hash=url, language="en",
        relevance_score=0, fetched_at=datetime(2026, 5, 3),
        categories=["business"], tickers=[],
    )


def test_backfill_tags_articles_without_tickers():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    insert_article(conn, _art("http://x/1", "Apple earnings", "Apple Inc reports..."))
    insert_article(conn, _art("http://x/2", "Weather", "Sunny in Athens"))
    with patch("news.tagger.load_ticker_dict", return_value=TICKER_DICT), \
         patch("news.tagger.extract_tickers_llm", return_value=[]):
        n_processed, n_tagged = backfill(conn, batch_size=10, max_articles=None)
    assert n_processed == 2
    assert n_tagged == 1  # only the Apple article had a ticker
    rows = conn.execute("SELECT article_url, ticker FROM article_tickers").fetchall()
    assert ("http://x/1", "AAPL") in [(r[0], r[1]) for r in rows]


def test_backfill_skips_already_tagged():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    insert_article(conn, _art("http://x/1", "Apple earnings", "Apple Inc reports..."))
    conn.execute("INSERT INTO article_tickers VALUES ('http://x/1', 'AAPL')")
    with patch("news.tagger.load_ticker_dict", return_value=TICKER_DICT), \
         patch("news.tagger.extract_tickers_llm", return_value=[]):
        n_processed, n_tagged = backfill(conn, batch_size=10, max_articles=None)
    assert n_processed == 0  # already tagged, skipped
    assert n_tagged == 0
