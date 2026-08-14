from datetime import UTC, datetime, timedelta

from news.models import Article
from news.processor import (
    classify_article,
    compute_relevance_score,
    deduplicate,
    filter_quality,
    process_articles,
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
        published_at = datetime.now(UTC)
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
        "https://example.com/2" in unique[0].also_reported_by or "Src" in unique[0].also_reported_by
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
        content="National Bank of Greece announces use of AI agents for customer service. " * 10,
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
    """Test relevance scoring with company mention, category, tier, and recency."""
    # Article mentioning the configured company name, in banking category,
    # tier 1, published 2h ago.
    two_hours_ago = datetime.now(UTC) - timedelta(hours=2)
    article = _make_article(
        title="NBG Quarterly Results",
        content="National Bank of Greece reports strong quarterly results. " * 20,
        categories=["banking"],
        published_at=two_hours_ago,
    )

    scoring_config = {
        "company_mention": 30,
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
    keywords = {"company": {"names": ["NBG", "National Bank of Greece"]}}

    score = compute_relevance_score(
        article, scoring_config, source_tier=1, keywords_config=keywords
    )

    # Expected: company_mention(30) + category_match(10) + tier_1_bonus(15) + recency_4h(15) = 70
    assert score >= 70
    assert article.relevance_score >= 70


def test_compute_relevance_score_uses_keywords_config_for_company_match():
    """Company-mention bonus is awarded based on keywords_config patterns, not a hardcoded list."""
    article = _make_article(
        title="AcmeCorp Q1 results",
        content="AcmeCorp posted strong results. " * 20,
    )
    scoring = {
        "company_mention": 50,
        "category_match": 0,
        "tier_1_bonus": 0,
        "tier_2_bonus": 0,
        "tier_3_bonus": 0,
        "recency_1h": 0,
        "recency_4h": 0,
        "recency_12h": 0,
        "recency_24h": 0,
        "claude_mention": 0,
    }
    keywords = {"company": {"names": ["AcmeCorp"]}}

    score = compute_relevance_score(article, scoring, source_tier=2, keywords_config=keywords)
    assert score >= 50


def test_compute_relevance_score_no_keywords_config_skips_company_bonus():
    """Without keywords_config (digest profile), the company_mention bonus does not apply."""
    article = _make_article(
        title="National Bank of Greece Q1",
        content="NBG news. " * 20,
    )
    scoring = {
        "company_mention": 50,
        "category_match": 0,
        "tier_1_bonus": 0,
        "tier_2_bonus": 0,
        "tier_3_bonus": 0,
        "recency_1h": 0,
        "recency_4h": 0,
        "recency_12h": 0,
        "recency_24h": 0,
        "claude_mention": 0,
        "greek_banking": 0,
    }

    score = compute_relevance_score(article, scoring, source_tier=2)  # no keywords_config
    assert score == 0


def test_compute_relevance_score_uses_competitors_for_greek_banking_bonus():
    """Sector bonus comes from keywords_config.competitors, not hardcoded names."""
    article = _make_article(
        title="XYZ Bank reports strong results",
        content="XYZ Bank had a great quarter. " * 20,
    )
    scoring = {
        "company_mention": 0,
        "greek_banking": 30,
        "category_match": 0,
        "tier_1_bonus": 0,
        "tier_2_bonus": 0,
        "tier_3_bonus": 0,
        "recency_1h": 0,
        "recency_4h": 0,
        "recency_12h": 0,
        "recency_24h": 0,
        "claude_mention": 0,
    }
    keywords = {"competitors": {"xyz": {"names": ["XYZ Bank"]}}}

    score = compute_relevance_score(article, scoring, source_tier=2, keywords_config=keywords)
    assert score >= 30


def test_processor_module_has_no_brand_specific_literals():
    """processor.py source contains no brand-specific company or competitor literals."""
    import news.processor as proc_mod

    src = open(proc_mod.__file__).read()
    forbidden = [
        "national bank of greece",
        "nbg",
        "ethniki trapeza",
        "piraeus",
        "alpha bank",
        "eurobank",
        "hellenic bank",
        "greek bank",  # phrase — was hardcoded in the old greek_banking_patterns
    ]
    for f in forbidden:
        assert f.lower() not in src.lower(), f"Found brand-specific literal in processor.py: {f}"


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
    forty_eight_hours_ago = datetime.now(UTC) - timedelta(hours=48)
    two_hours_ago = datetime.now(UTC) - timedelta(hours=2)

    old = _make_article(published_at=forty_eight_hours_ago)
    recent = _make_article(published_at=two_hours_ago)

    articles = [old, recent]
    kept, dropped = filter_quality(articles, min_words=100, max_age_hours=36)

    assert len(kept) == 1
    assert kept[0].published_at == two_hours_ago
    assert len(dropped) == 1
    assert dropped[0].published_at == forty_eight_hours_ago


# --- Per-source age override --------------------------------------------------
# Curated, slow-publishing sources (evergreen deep dives) would be wiped out by
# the news-wire age window, so a source may declare a longer one of its own.


def test_filter_quality_keeps_old_articles_from_a_source_with_an_age_override():
    sixty_hours_ago = datetime.now(UTC) - timedelta(hours=60)
    evergreen = _make_article(source="The Agent Daily", published_at=sixty_hours_ago)

    kept, dropped = filter_quality(
        [evergreen],
        min_words=100,
        max_age_hours=36,
        source_max_age={"The Agent Daily": 720},
    )

    assert kept == [evergreen]
    assert dropped == []


def test_filter_quality_still_drops_old_articles_from_sources_without_an_override():
    sixty_hours_ago = datetime.now(UTC) - timedelta(hours=60)
    wire = _make_article(source="TechCrunch", published_at=sixty_hours_ago)
    evergreen = _make_article(source="The Agent Daily", published_at=sixty_hours_ago)

    kept, dropped = filter_quality(
        [wire, evergreen],
        min_words=100,
        max_age_hours=36,
        source_max_age={"The Agent Daily": 720},
    )

    assert kept == [evergreen]
    assert dropped == [wire]


def test_filter_quality_applies_the_override_ceiling_not_an_exemption():
    """An override is a longer window, not a licence to keep anything forever."""
    ancient = _make_article(
        source="The Agent Daily",
        published_at=datetime.now(UTC) - timedelta(hours=800),
    )

    kept, dropped = filter_quality(
        [ancient],
        min_words=100,
        max_age_hours=36,
        source_max_age={"The Agent Daily": 720},
    )

    assert kept == []
    assert dropped == [ancient]


def test_process_articles_threads_the_source_age_override_through():
    sixty_hours_ago = datetime.now(UTC) - timedelta(hours=60)
    evergreen = _make_article(source="The Agent Daily", published_at=sixty_hours_ago)

    processed, stats = process_articles(
        articles=[evergreen],
        existing_hashes=set(),
        categories_config={"categories": {}},
        scoring_config={},
        source_tiers={},
        min_words=10,
        max_age_hours=36,
        source_max_age={"The Agent Daily": 720},
    )

    assert stats["quality_dropped"] == 0
    assert len(processed) == 1


def test_relevance_score_counts_a_claude_mention_found_only_in_the_abstract():
    """The description is marketing copy; the substance is in the transcript."""
    article = _make_article(content="Subscribe for more videos every week!")
    article.transcript_abstract = (
        "A walkthrough of building an MCP server and wiring it into Claude Code."
    )

    score = compute_relevance_score(article, {"claude_mention": 30}, source_tier=2)

    assert score == 30
