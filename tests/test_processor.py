from datetime import datetime, timezone, timedelta
from news.models import Article
from news.processor import (
    deduplicate,
    classify_article,
    compute_relevance_score,
    filter_quality,
)


def _make_article(
    title="Test",
    content="Long enough content. " * 50,  # 150 words total
    url="https://example.com/1",
    source="Src",
    categories=None,
    language="en",
    published_at=None,
) -> Article:
    """Helper to create test articles."""
    if categories is None:
        categories = []
    if published_at is None:
        published_at = datetime.now(timezone.utc)
    return Article(
        url=url,
        title=title,
        source=source,
        content=content,
        categories=categories,
        language=language,
        published_at=published_at,
    )


def test_deduplicate_removes_exact_duplicates():
    """Test that exact duplicate articles are detected and separated."""
    # Create 3 articles: 2 with same title+content, 1 different
    article1 = _make_article(
        title="Same Title", content="Same content. " * 20, url="https://example.com/1"
    )
    article2 = _make_article(
        title="Same Title", content="Same content. " * 20, url="https://example.com/2"
    )
    article3 = _make_article(
        title="Different",
        content="Different content. " * 20,
        url="https://example.com/3",
    )

    # Compute hashes
    article1.compute_hash()
    article2.compute_hash()
    article3.compute_hash()

    articles = [article1, article2, article3]
    unique, dupes = deduplicate(articles, set())

    assert len(unique) == 2  # article1 and article3
    assert len(dupes) == 1  # article2
    assert dupes[0].url == "https://example.com/2"
    # Check that also_reported_by was updated on the original
    assert (
        "https://example.com/2" in unique[0].also_reported_by
        or "Src" in unique[0].also_reported_by
    )


def test_deduplicate_respects_existing_hashes():
    """Test that articles whose hash exists in existing_hashes are filtered out."""
    article1 = _make_article(
        title="Test", content="Test content. " * 20, url="https://example.com/1"
    )
    article1.compute_hash()

    # Hash already exists
    existing_hashes = {article1.content_hash}

    unique, dupes = deduplicate([article1], existing_hashes)

    assert len(unique) == 0
    assert len(dupes) == 1
    assert dupes[0].url == "https://example.com/1"


def test_classify_article_adds_categories():
    """Test that article gets classified into categories based on keyword matching."""
    article = _make_article(
        title="Claude AI Agent Used by NBG",
        content="National Bank of Greece announces use of AI agents for customer service. "
        * 10,
    )

    categories_config = {
        "categories": {
            "ai": {
                "display_name": "Artificial Intelligence",
                "keywords": ["ai", "artificial intelligence", "claude", "agent", "llm"],
                "priority": 1,
            },
            "banking": {
                "display_name": "Banking",
                "keywords": ["bank", "national bank of greece", "nbg", "finance"],
                "priority": 2,
            },
        }
    }

    classify_article(article, categories_config)

    assert "ai" in article.categories
    assert "banking" in article.categories


def test_compute_relevance_score():
    """Test relevance scoring with NBG mention, category, tier, and recency."""
    # Article mentioning NBG, in banking category, tier 1, published 2h ago
    two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    article = _make_article(
        title="NBG Quarterly Results",
        content="National Bank of Greece reports strong quarterly results. " * 20,
        categories=["banking"],
        published_at=two_hours_ago,
    )

    scoring_config = {
        "nbg_mention": 30,
        "greek_banking": 20,
        "category_match": 10,
        "tier_1_bonus": 15,
        "tier_2_bonus": 10,
        "tier_3_bonus": 5,
        "recency_1h": 20,
        "recency_4h": 15,
        "recency_12h": 10,
        "recency_24h": 5,
    }

    score = compute_relevance_score(article, scoring_config, source_tier=1)

    # Expected: nbg_mention(30) + category_match(10) + tier_1_bonus(15) + recency_4h(15) = 70
    assert score >= 70
    assert article.relevance_score >= 70


def test_filter_quality_drops_short_articles():
    """Test that short articles are dropped and long ones kept."""
    short = _make_article(content="Too short.")
    long_content = "Long enough content. " * 50  # 150 words
    long = _make_article(content=long_content)

    articles = [short, long]
    kept, dropped = filter_quality(articles, min_words=100, max_age_hours=36)

    assert len(kept) == 1
    assert kept[0].content == long_content
    assert len(dropped) == 1
    assert dropped[0].content == "Too short."


def test_filter_quality_drops_old_articles():
    """Test that old articles are dropped and recent ones kept."""
    forty_eight_hours_ago = datetime.now(timezone.utc) - timedelta(hours=48)
    two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)

    old = _make_article(published_at=forty_eight_hours_ago)
    recent = _make_article(published_at=two_hours_ago)

    articles = [old, recent]
    kept, dropped = filter_quality(articles, min_words=100, max_age_hours=36)

    assert len(kept) == 1
    assert kept[0].published_at == two_hours_ago
    assert len(dropped) == 1
    assert dropped[0].published_at == forty_eight_hours_ago
