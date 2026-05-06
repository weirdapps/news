from news.config import get_categories
from news.processor import classify_article
from news.models import Article
from datetime import datetime


def _art(title, content=""):
    return Article(
        url="http://x/1",
        title=title,
        source="s",
        author=None,
        published_at=datetime.now(),
        content=content,
        summary=None,
        content_hash="h",
        language="en",
        relevance_score=0,
        fetched_at=datetime.now(),
        categories=[],
        tickers=[],
    )


def test_claude_code_release_classified_as_claude_code():
    cfg = get_categories()
    assert "claude_code" in cfg["categories"]
    art = _art("Claude Code 4.7 introduces 1M context", "Anthropic released...")
    classify_article(art, cfg)
    assert "claude_code" in art.categories


def test_general_ai_news_not_in_claude_code():
    cfg = get_categories()
    art = _art("OpenAI launches new GPT-5", "OpenAI announced...")
    classify_article(art, cfg)
    assert "claude_code" not in art.categories
