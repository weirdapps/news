from datetime import UTC, datetime

from news.models import Article, Digest, Source


def test_article_creation():
    article = Article(
        url="https://example.com/article-1",
        title="Test Article",
        source="TestSource",
        author="John Doe",
        published_at=datetime(2026, 4, 5, 9, 0, tzinfo=UTC),
        content="This is the full article content for testing purposes.",
        summary="Test summary of the article.",
        categories=["tech", "ai"],
        language="en",
    )
    assert article.url == "https://example.com/article-1"
    assert article.categories == ["tech", "ai"]
    assert article.relevance_score == 0
    assert article.content_hash == ""
    assert article.included_in_digest_id is None


def test_article_compute_hash():
    article = Article(
        url="https://example.com/1",
        title="Test Article",
        source="Src",
        content="Some content here that is long enough to hash properly for dedup.",
        categories=["tech"],
        language="en",
    )
    article.compute_hash()
    assert len(article.content_hash) == 64  # SHA-256 hex digest
    article2 = Article(
        url="https://example.com/2",
        title="Test Article",
        source="OtherSrc",
        content="Some content here that is long enough to hash properly for dedup.",
        categories=["tech"],
        language="en",
    )
    article2.compute_hash()
    assert article.content_hash == article2.content_hash


def test_digest_creation():
    digest = Digest(digest_type="scheduled", article_count=47)
    assert digest.id is None
    assert digest.digest_type == "scheduled"
    assert digest.article_count == 47
    assert digest.synthesis_text == ""
    assert digest.html_output == ""
    assert digest.sent_at is None


def test_source_creation():
    source = Source(
        name="TechCrunch",
        url="https://techcrunch.com/feed/",
        category="tech",
        tier=1,
        language="en",
    )
    assert source.name == "TechCrunch"
    assert source.tier == 1
    assert source.fetch_count == 0
    assert source.error_count == 0


def test_compute_hash_is_unchanged_for_a_known_article():
    """Pin the hash of one known article against the formula shipping today.

    data/news.db holds 97k rows keyed on exactly ``title|content[:200]``, both
    stripped and lowercased. Adding a salt, a discriminator or a different
    separator to make some new field participate in dedup would orphan every
    one of those rows and re-insert the whole corpus as new articles. Verified
    against the real database: 6,000 sampled rows all reproduce their stored
    content_hash under this formula, and 0 reproduce it under a salted variant.
    """
    article = Article(
        url="https://claude.com/en/release-notes/system-prompts#claude-opus-4-5-january-18-2026",
        title="Claude Opus 4.5 system prompt (January 18, 2026)",
        source="Claude System Prompts",
        content="The assistant is Claude, made by Anthropic.",
        categories=["ai"],
        language="en",
    )

    article.compute_hash()

    assert (
        article.content_hash == "b9fc55062810de325684931fc3f796afd61ec7a4d3e9f70885e2d803c1e53866"
    )


def test_transcript_abstract_does_not_affect_the_content_hash():
    """The abstract must stay out of the hash input, or dedup double-stores videos."""
    common = {
        "url": "https://www.youtube.com/watch?v=abc12345678",
        "title": "A Video",
        "source": "YouTube: Fireship",
        "content": "Short marketing blurb.",
        "categories": ["ai"],
        "language": "en",
    }
    base = Article(**common)
    enriched = Article(**common, transcript_abstract="A completely different 800 char abstract.")

    base.compute_hash()
    enriched.compute_hash()

    assert base.content_hash == enriched.content_hash


def test_changelog_digest_does_not_affect_the_content_hash():
    """Neither changelog field may reach the hash input.

    The digest is computed after the entry is first seen, and the LLM upgrade
    can land a run later still. Folding either field into the hash would
    re-insert the same changelog entry as a new row every time it changed.
    """
    common = {
        "url": "https://claude.com/en/release-notes/system-prompts#claude-opus-5-july-24-2026",
        "title": "Claude Opus 5 system prompt (July 24, 2026)",
        "source": "Claude System Prompts",
        "content": "The assistant is Claude, made by Anthropic.",
        "categories": ["ai"],
        "language": "en",
    }
    base = Article(**common)
    enriched = Article(
        **common,
        changelog_digest="DELTA vs Claude Opus 4.5: 12 of 340 sentences/tags changed.",
        changelog_digest_source="llm",
    )

    base.compute_hash()
    enriched.compute_hash()

    assert base.content_hash == enriched.content_hash


def test_changelog_digest_fields_default_to_empty_strings():
    """The pipeline reads both unconditionally; None would break ``[:2000]``."""
    article = Article(
        url="https://techcrunch.com/story",
        title="A Story",
        source="TechCrunch",
        content="words",
        categories=["tech"],
        language="en",
    )

    assert article.changelog_digest == ""
    assert article.changelog_digest_source == ""
