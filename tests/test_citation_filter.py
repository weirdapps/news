"""Tests for synthesis citation filter."""

from datetime import UTC, datetime

from news.citation_filter import (
    enrich_mentions,
    enrich_section_articles,
    filter_competitor_watch,
    filter_unsourced_bullets,
    filter_unsourced_sections,
)
from news.models import Article


def _articles():
    now = datetime.now(UTC)
    return [
        Article(
            url=f"https://example.com/{i}",
            title=f"Title {i}",
            source=f"Source{i}",
            content="x",
            categories=["banking"],
            language="en",
            relevance_score=50,
            fetched_at=now,
            published_at=now,
        )
        for i in range(5)
    ]


def test_filter_unsourced_bullets_drops_no_ids():
    bullets = [
        {"text": "kept", "article_ids": [0, 1]},
        {"text": "dropped", "article_ids": []},
        {"text": "also dropped"},  # no article_ids field
    ]
    result = filter_unsourced_bullets(bullets, _articles())
    assert result == ["kept"]


def test_filter_unsourced_bullets_drops_out_of_range():
    bullets = [
        {"text": "kept", "article_ids": [0, 99]},  # 99 invalid, 0 valid → keep
        {"text": "dropped", "article_ids": [99, 100]},  # all invalid → drop
    ]
    result = filter_unsourced_bullets(bullets, _articles())
    assert result == ["kept"]


def test_filter_unsourced_bullets_accepts_string_ids():
    """LLM sometimes emits IDs as strings — coerce to int."""
    bullets = [{"text": "kept", "article_ids": ["0", "2"]}]
    result = filter_unsourced_bullets(bullets, _articles())
    assert result == ["kept"]


def test_filter_unsourced_bullets_passes_through_plain_strings():
    """Backward compatibility: bullet that is just a string gets dropped (unsourced)."""
    bullets = ["legacy string bullet", {"text": "ok", "article_ids": [0]}]
    result = filter_unsourced_bullets(bullets, _articles())
    assert result == ["ok"]


def test_filter_unsourced_sections_drops_no_ids():
    sections = [
        {"display_name": "kept", "synthesis": "...", "article_ids": [0, 1]},
        {"display_name": "dropped", "synthesis": "...", "article_ids": []},
        {"display_name": "also dropped", "synthesis": "..."},
    ]
    result = filter_unsourced_sections(sections, _articles())
    assert [s["display_name"] for s in result] == ["kept"]


def test_enrich_section_articles_attaches_title_url_source():
    sections = [
        {"display_name": "x", "synthesis": "...", "article_ids": [0, 2]},
    ]
    enriched = enrich_section_articles(sections, _articles())
    assert enriched[0]["articles"] == [
        {"title": "Title 0", "url": "https://example.com/0", "source": "Source0"},
        {"title": "Title 2", "url": "https://example.com/2", "source": "Source2"},
    ]


def test_enrich_mentions_attaches_url_from_first_id():
    mentions = [
        {"title": "kept", "source": "x", "article_ids": [3]},
        {"title": "dropped", "source": "x", "article_ids": []},
    ]
    enriched = enrich_mentions(mentions, _articles())
    assert len(enriched) == 1
    assert enriched[0]["title"] == "kept"
    assert enriched[0]["url"] == "https://example.com/3"


def test_filter_competitor_watch_drops_legacy_strings_and_invalid():
    """Legacy schema (key→str) always dropped; new schema (key→{summary,article_ids}) survives only with valid ids."""
    cw = {
        "alpha": "legacy string summary — must drop",
        "piraeus": {"summary": "kept", "article_ids": [1]},
        "eurobank": {"summary": "no ids", "article_ids": []},
        "optima": {"summary": "bad id", "article_ids": [99]},
    }
    result = filter_competitor_watch(cw, _articles())
    assert result == {"piraeus": "kept"}
