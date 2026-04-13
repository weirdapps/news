"""Tests for NBG monitor pipeline components."""

import sqlite3
from datetime import datetime, timezone

import pytest

from news.config import get_keywords, get_settings, get_sources, _profile_config_dir
from news.deliver import build_monitor_subject, render_monitor_html
from news.models import Article, Digest
from news.monitor_synth import build_monitor_prompt, build_monitor_fallback
from news.storage import (
    get_articles_since,
    get_last_digest,
    init_db,
    insert_article,
    insert_digest,
)


# --- Config tests ---


def test_profile_config_dir_digest():
    """Digest profile loads from config/ root."""
    path = _profile_config_dir("digest")
    assert path.name == "config"


def test_profile_config_dir_monitor():
    """Monitor profile loads from config/monitor/."""
    path = _profile_config_dir("monitor")
    assert path.name == "monitor"
    assert path.parent.name == "config"


def test_invalid_profile_raises():
    """Invalid profile name raises ValueError."""
    with pytest.raises(ValueError, match="Unknown profile"):
        _profile_config_dir("invalid")


def test_monitor_sources_loads():
    """Monitor sources.yaml loads with expected structure."""
    sources = get_sources(profile="monitor")
    assert "rss_feeds" in sources
    assert len(sources["rss_feeds"]) > 0
    # Verify NBG-specific feeds exist
    names = [s["name"] for s in sources["rss_feeds"]]
    assert "NBG English" in names
    assert "NBG Greek" in names


def test_monitor_settings_loads():
    """Monitor settings.yaml loads with expected structure."""
    settings = get_settings(profile="monitor")
    assert "pipeline" in settings
    assert "email" in settings
    assert "scoring" in settings
    # Monitor-specific scoring should have higher NBG bonus
    assert settings["scoring"]["nbg_mention"] >= 50


def test_monitor_keywords_loads():
    """Monitor keywords.yaml loads with expected structure."""
    keywords = get_keywords(profile="monitor")
    assert "nbg" in keywords
    assert "competitors" in keywords
    assert "categories" in keywords
    # Check NBG name variants
    assert "National Bank of Greece" in keywords["nbg"]["names"]
    assert "Εθνική Τράπεζα" in keywords["nbg"]["names"]
    # Check false positive filters
    assert "Εθνική Ομάδα" in keywords["nbg"]["false_positives"]


# --- Model tests ---


def test_article_monitor_fields():
    """Article model has monitor-specific fields with defaults."""
    article = Article(
        url="https://example.com/nbg-news",
        title="NBG Q1 Earnings",
        source="Reuters",
        content="National Bank of Greece reported...",
        categories=["nbg_direct"],
        language="en",
    )
    assert article.pipeline == "digest"
    assert article.sentiment == ""
    assert article.mention_type == ""
    assert article.urgency == ""

    # Set monitor fields
    article.pipeline = "monitor"
    article.sentiment = "positive"
    article.mention_type = "news"
    article.urgency = "important"
    assert article.pipeline == "monitor"


def test_digest_pipeline_field():
    """Digest model has pipeline field."""
    digest = Digest(digest_type="scheduled", pipeline="monitor")
    assert digest.pipeline == "monitor"


# --- Storage tests ---


def test_insert_and_retrieve_monitor_article():
    """Monitor articles store and retrieve with pipeline field."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)

    article = Article(
        url="https://example.com/nbg",
        title="NBG Reports Strong Q1",
        source="Reuters NBG",
        content="National Bank of Greece reported strong Q1 results " * 10,
        categories=["nbg_direct"],
        language="en",
        published_at=datetime.now(timezone.utc),
        pipeline="monitor",
        sentiment="positive",
        mention_type="news",
        urgency="important",
    )
    article.compute_hash()
    insert_article(conn, article)

    # Retrieve by pipeline
    since = datetime(2020, 1, 1, tzinfo=timezone.utc)
    monitor_articles = get_articles_since(conn, since, pipeline="monitor")
    digest_articles = get_articles_since(conn, since, pipeline="digest")

    assert len(monitor_articles) == 1
    assert len(digest_articles) == 0
    assert monitor_articles[0].pipeline == "monitor"
    assert monitor_articles[0].sentiment == "positive"
    assert monitor_articles[0].mention_type == "news"
    conn.close()


def test_get_last_digest_by_pipeline():
    """get_last_digest filters by pipeline."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)

    # Insert a digest for each pipeline
    digest_d = Digest(digest_type="scheduled", article_count=50, pipeline="digest")
    digest_m = Digest(digest_type="scheduled", article_count=10, pipeline="monitor")
    insert_digest(conn, digest_d)
    insert_digest(conn, digest_m)

    last_digest = get_last_digest(conn, pipeline="digest")
    last_monitor = get_last_digest(conn, pipeline="monitor")

    assert last_digest.article_count == 50
    assert last_digest.pipeline == "digest"
    assert last_monitor.article_count == 10
    assert last_monitor.pipeline == "monitor"
    conn.close()


# --- Monitor synthesis tests ---


def test_build_monitor_prompt_includes_articles():
    """Monitor prompt includes article data."""
    articles = [
        Article(
            url="https://example.com/1",
            title="NBG Q1 Results",
            source="Reuters",
            content="National Bank of Greece reported strong earnings...",
            categories=["nbg_direct"],
            language="en",
        ),
    ]

    prompt = build_monitor_prompt(articles, None, "last hour")
    assert "NBG Q1 Results" in prompt
    assert (
        "brand intelligence" in prompt.lower() or "brand monitoring" in prompt.lower()
    )
    assert "false positive" in prompt.lower()
    assert "competitor" in prompt.lower()


def test_build_monitor_prompt_with_previous_summary():
    """Monitor prompt includes previous sentiment when provided."""
    articles = [
        Article(
            url="https://example.com/1",
            title="NBG News",
            source="Bloomberg",
            content="NBG content " * 20,
            categories=["nbg_direct"],
            language="en",
        ),
    ]
    previous = {
        "sentiment_summary": {
            "positive": 3,
            "negative": 1,
            "neutral": 5,
            "trend": "stable",
        },
    }

    prompt = build_monitor_prompt(articles, previous, "last hour")
    assert "previous_sentiment" in prompt


def test_build_monitor_fallback():
    """Monitor fallback produces structured text."""
    articles = [
        Article(
            url="https://example.com/1",
            title="NBG Q1",
            source="Reuters",
            content="x " * 20,
            categories=["nbg_direct"],
            language="en",
        ),
    ]
    fallback = build_monitor_fallback(articles)
    assert "MONITOR SYNTHESIS UNAVAILABLE" in fallback
    assert "NBG Q1" in fallback


# --- Delivery tests ---


def test_build_monitor_subject():
    """Monitor subject line includes NBG Monitor label."""
    dt = datetime(2026, 4, 8, 15, 0, tzinfo=timezone.utc)
    subject = build_monitor_subject(dt, mention_count=12, source_count=25)
    assert "NBG Monitor" in subject
    assert "12 mentions" in subject


def test_build_monitor_subject_with_alert():
    """Monitor subject includes ALERT when has_alerts is True."""
    dt = datetime(2026, 4, 8, 15, 0, tzinfo=timezone.utc)
    subject = build_monitor_subject(dt, has_alerts=True)
    assert "ALERT" in subject


def test_render_monitor_html_produces_valid_html():
    """Monitor template renders with all sections."""
    synthesis = {
        "mention_count": 5,
        "sentiment_summary": {
            "positive": 2,
            "negative": 1,
            "neutral": 2,
            "trend": "stable",
        },
        "alerts": ["Negative press about NBG lending practices"],
        "executive_brief": ["Key insight about NBG"],
        "nbg_mentions": [
            {
                "title": "NBG Q1 Results",
                "source": "Reuters",
                "type": "news",
                "sentiment": "positive",
                "summary": "Strong Q1 earnings reported",
                "relevance": "high",
            }
        ],
        "sector_context": "Greek banking sector remains stable.",
        "competitor_watch": {
            "piraeus": "Piraeus reported mixed results",
            "alpha": None,
            "eurobank": "Eurobank expanded digital services",
        },
    }

    html = render_monitor_html(
        synthesis=synthesis,
        mention_count=5,
        source_count=25,
        time_display="15:00",
        date_display="tue 8 apr",
        next_scan="16:00",
        subject="NBG Monitor | Tue 8 APR 15:00",
    )

    assert "NBG MONITOR" in html
    assert "KEY TAKEAWAYS" in html
    assert "ALERT" in html
    assert "SENTIMENT" in html
    assert "NBG MENTIONS" in html
    assert "COMPETITOR WATCH" in html
    assert "Piraeus" in html
    assert "Eurobank" in html
    assert "15:00" in html


def test_render_monitor_html_empty_synthesis():
    """Monitor template handles no-mention case gracefully."""
    synthesis = {
        "mention_count": 0,
        "nbg_mentions": [],
        "sentiment_summary": {"positive": 0, "negative": 0, "neutral": 0},
    }

    html = render_monitor_html(
        synthesis=synthesis,
        mention_count=0,
        source_count=25,
        time_display="10:00",
        date_display="wed 9 apr",
    )

    assert "NBG MONITOR" in html
    assert "0 mentions" in html
