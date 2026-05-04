from unittest.mock import patch
from news.tagger import tag_article
from news.models import Article
from datetime import datetime, timezone

TICKER_DICT = {"apple": "AAPL", "aapl": "AAPL", "microsoft": "MSFT", "msft": "MSFT"}

def _art(title, content="", categories=None):
    return Article(
        url="http://x/1", title=title, source="s", author=None,
        published_at=datetime(2026, 5, 3, tzinfo=timezone.utc), content=content,
        summary=None, content_hash="h", language="en",
        relevance_score=0, fetched_at=datetime(2026, 5, 3, tzinfo=timezone.utc),
        categories=categories or [], tickers=[],
    )

def test_rules_only_when_match_found():
    art = _art("Apple beats earnings", "Apple Inc. ($AAPL) reported...", categories=["business"])
    with patch("news.tagger.load_ticker_dict", return_value=TICKER_DICT), \
         patch("news.tagger.extract_tickers_llm") as mock_llm:
        tag_article(art, llm_fallback_categories={"business"})
        assert art.tickers == ["AAPL"]
        mock_llm.assert_not_called()

def test_llm_fallback_when_rules_empty_and_market_category():
    art = _art("Mystery firm soars", "A previously unknown company...", categories=["business"])
    with patch("news.tagger.load_ticker_dict", return_value=TICKER_DICT), \
         patch("news.tagger.extract_tickers_llm", return_value=["XYZ"]):
        tag_article(art, llm_fallback_categories={"business"})
        assert art.tickers == ["XYZ"]

def test_no_llm_when_not_market_category():
    art = _art("Claude Code 4.7 ships", "Anthropic released...", categories=["claude_code"])
    with patch("news.tagger.load_ticker_dict", return_value=TICKER_DICT), \
         patch("news.tagger.extract_tickers_llm") as mock_llm:
        tag_article(art, llm_fallback_categories={"business", "trading", "banking"})
        assert art.tickers == []
        mock_llm.assert_not_called()

def test_processor_invokes_tagger():
    """Smoke: process_articles populates article.tickers."""
    from news.processor import process_articles
    art = _art("Apple Q3 earnings", "Apple Inc. reported strong revenue.", categories=[])
    with patch("news.tagger.load_ticker_dict", return_value=TICKER_DICT):
        result = process_articles(
            [art], existing_hashes=set(), categories_config={"categories": {}},
            scoring_config={}, source_tiers={}, min_words=1, max_age_hours=999999,
        )
        # process_articles return signature varies — handle both possibilities
        if isinstance(result, tuple):
            kept = result[0]
        else:
            kept = result
    assert len(kept) > 0
    assert kept[0].tickers == ["AAPL"]
