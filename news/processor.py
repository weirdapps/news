"""Article processing: deduplication, classification, scoring, and quality filtering."""

import re
from datetime import datetime, timezone, timedelta
from news.models import Article


def deduplicate(
    articles: list[Article], existing_hashes: set[str]
) -> tuple[list[Article], list[Article]]:
    """
    Deduplicate articles based on content hash.

    Args:
        articles: List of articles to deduplicate
        existing_hashes: Set of hashes from previously processed articles

    Returns:
        Tuple of (unique_articles, duplicate_articles)
    """
    unique = []
    dupes = []
    seen: dict[str, Article] = {}  # hash -> first article with that hash

    for article in articles:
        # Ensure hash is computed
        if not article.content_hash:
            article.compute_hash()

        # Check if hash already exists in previous runs
        if article.content_hash in existing_hashes:
            dupes.append(article)
            continue

        # Check if we've seen this hash in current batch
        if article.content_hash in seen:
            # It's a duplicate - track source on original
            original = seen[article.content_hash]
            if article.source not in original.also_reported_by:
                original.also_reported_by.append(article.source)
            dupes.append(article)
        else:
            # First time seeing this hash
            seen[article.content_hash] = article
            unique.append(article)

    return unique, dupes


def _keyword_matches(text: str, keyword: str) -> bool:
    """Check if keyword matches in text with appropriate boundary logic."""
    kw = keyword.lower()
    if len(kw) <= 3:
        return bool(re.search(r"\b" + re.escape(kw) + r"\b", text))
    return kw in text


def classify_article(article: Article, categories_config: dict) -> None:
    """
    Classify article into categories based on keyword matching.

    Modifies article.categories in place.

    Args:
        article: Article to classify
        categories_config: Dict with "categories" key containing category definitions
    """
    text = (article.title + " " + article.content).lower()
    categories = categories_config.get("categories", {})

    for category_key, category_info in categories.items():
        keywords = category_info.get("keywords", [])
        for keyword in keywords:
            if _keyword_matches(text, keyword):
                if category_key not in article.categories:
                    article.categories.append(category_key)
                break


def compute_relevance_score(
    article: Article,
    scoring: dict,
    source_tier: int = 2,
    keywords_config: dict | None = None,
) -> int:
    """
    Compute relevance score for an article.

    Sets article.relevance_score and returns the score.

    Args:
        article: Article to score
        scoring: Scoring configuration dict
        source_tier: Tier of the source (1=premium, 2=standard, 3=supplementary)
        keywords_config: Optional brand-monitoring config. When provided, the
            company-mention bonus uses ``company.names`` and the sector bonus
            uses ``competitors.*.names``. When None (digest profile), neither
            bonus applies.

    Returns:
        Computed relevance score
    """
    score = 0
    text = (article.title + " " + article.content).lower()

    # Company-mention bonus — patterns come from keywords_config.company.names.
    # When keywords_config is None (digest profile), no company bonus applies.
    if keywords_config:
        company_names = [
            n.lower() for n in keywords_config.get("company", {}).get("names", [])
        ]
        if any(name in text for name in company_names):
            score += scoring.get("company_mention", 0)

    # Sector / competitor-mention bonus — patterns derived from
    # keywords_config.competitors. The scoring YAML key is still named
    # ``greek_banking`` (legacy; it is just an internal label for the bonus
    # and does not need to be brand-specific).
    if keywords_config:
        competitor_patterns: list[str] = []
        for _, comp in keywords_config.get("competitors", {}).items():
            for name in comp.get("names", []):
                competitor_patterns.append(name.lower())
        if competitor_patterns and any(p in text for p in competitor_patterns):
            score += scoring.get("greek_banking", 0)

    # Check for Claude/AI tools mentions
    claude_patterns = [
        "claude code",
        "claude ai",
        "anthropic",
        "model context protocol",
        "mcp server",
        "agentic ai",
    ]
    if any(pattern in text for pattern in claude_patterns):
        score += scoring.get("claude_mention", 0)

    # Category match bonus
    if article.categories:
        score += scoring.get("category_match", 0)

    # Source tier bonus
    tier_key = f"tier_{source_tier}_bonus"
    score += scoring.get(tier_key, 0)

    # Recency bonus
    if article.published_at:
        age = datetime.now(timezone.utc) - article.published_at
        hours = age.total_seconds() / 3600

        if hours <= 1:
            score += scoring.get("recency_1h", 0)
        elif hours <= 4:
            score += scoring.get("recency_4h", 0)
        elif hours <= 12:
            score += scoring.get("recency_12h", 0)
        elif hours <= 24:
            score += scoring.get("recency_24h", 0)

    article.relevance_score = score
    return score


def filter_quality(
    articles: list[Article], min_words: int = 100, max_age_hours: int = 36
) -> tuple[list[Article], list[Article]]:
    """
    Filter articles by quality: word count and age.

    Args:
        articles: Articles to filter
        min_words: Minimum word count
        max_age_hours: Maximum age in hours

    Returns:
        Tuple of (kept_articles, dropped_articles)
    """
    kept = []
    dropped = []
    cutoff_time = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)

    for article in articles:
        # Check word count
        word_count = len(article.content.split())
        if word_count < min_words:
            dropped.append(article)
            continue

        # Check age
        if article.published_at and article.published_at < cutoff_time:
            dropped.append(article)
            continue

        kept.append(article)

    return kept, dropped


def extract_content(article: Article) -> None:
    """
    Extract full content from article URL if content is too short.

    Uses trafilatura first, falls back to readability-lxml.
    Modifies article.content in place.

    Args:
        article: Article to extract content for
    """
    # Check if content is already long enough
    word_count = len(article.content.split())
    if word_count >= 100:
        return

    try:
        # Try trafilatura first
        import trafilatura

        downloaded = trafilatura.fetch_url(article.url)
        if downloaded:
            extracted = trafilatura.extract(downloaded)
            if extracted and len(extracted.split()) >= 100:
                article.content = extracted
                return
    except Exception:
        pass

    try:
        # Fallback to readability
        from readability import Document
        import requests

        response = requests.get(article.url, timeout=10)
        doc = Document(response.content)
        content = doc.summary()
        # Strip HTML tags
        import re

        content = re.sub(r"<[^>]+>", "", content)
        if len(content.split()) >= 100:
            article.content = content
    except Exception:
        pass  # Keep original content if extraction fails


def process_articles(
    articles: list[Article],
    existing_hashes: set[str],
    categories_config: dict,
    scoring_config: dict,
    source_tiers: dict[str, int],
    min_words: int = 100,
    max_age_hours: int = 36,
    keywords_config: dict | None = None,
) -> tuple[list[Article], dict]:
    """
    Process articles through the full pipeline.

    Pipeline:
    1. Filter by quality (word count, age)
    2. Deduplicate
    3. Classify into categories
    4. Compute relevance scores

    Args:
        articles: Articles to process
        existing_hashes: Set of hashes from previously processed articles
        categories_config: Category definitions
        scoring_config: Scoring configuration
        source_tiers: Mapping of source name to tier
        min_words: Minimum word count for quality filter
        max_age_hours: Maximum age in hours for quality filter
        keywords_config: Optional brand-monitoring config; threaded to
            ``compute_relevance_score`` for company/competitor bonuses.

    Returns:
        Tuple of (processed_articles, stats_dict)
    """
    stats = {
        "input_count": len(articles),
        "quality_dropped": 0,
        "duplicates": 0,
        "output_count": 0,
    }

    # Step 1: Filter quality
    kept, dropped = filter_quality(articles, min_words, max_age_hours)
    stats["quality_dropped"] = len(dropped)

    # Step 2: Deduplicate
    unique, dupes = deduplicate(kept, existing_hashes)
    stats["duplicates"] = len(dupes)

    # Step 3: Classify and score each unique article
    for article in unique:
        classify_article(article, categories_config)
        tier = source_tiers.get(article.source, 2)
        compute_relevance_score(
            article, scoring_config, tier, keywords_config=keywords_config
        )
        from news.tagger import tag_article

        tag_article(article)

    stats["output_count"] = len(unique)
    return unique, stats
