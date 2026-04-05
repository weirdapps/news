from datetime import datetime, timezone
from news.models import Article, Digest, Source

def test_article_creation():
    article = Article(
        url="https://example.com/article-1", title="Test Article",
        source="TestSource", author="John Doe",
        published_at=datetime(2026, 4, 5, 9, 0, tzinfo=timezone.utc),
        content="This is the full article content for testing purposes.",
        summary="Test summary of the article.",
        categories=["tech", "ai"], language="en",
    )
    assert article.url == "https://example.com/article-1"
    assert article.categories == ["tech", "ai"]
    assert article.relevance_score == 0
    assert article.content_hash == ""
    assert article.included_in_digest_id is None

def test_article_compute_hash():
    article = Article(url="https://example.com/1", title="Test Article", source="Src",
        content="Some content here that is long enough to hash properly for dedup.",
        categories=["tech"], language="en")
    article.compute_hash()
    assert len(article.content_hash) == 64  # SHA-256 hex digest
    article2 = Article(url="https://example.com/2", title="Test Article", source="OtherSrc",
        content="Some content here that is long enough to hash properly for dedup.",
        categories=["tech"], language="en")
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
    source = Source(name="TechCrunch", url="https://techcrunch.com/feed/",
        category="tech", tier=1, language="en")
    assert source.name == "TechCrunch"
    assert source.tier == 1
    assert source.fetch_count == 0
    assert source.error_count == 0
