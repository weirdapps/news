"""Tests for the topic profile — ad-hoc topical news briefs from a CLI query."""

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from news.models import Article


def _make_article(
    title="Test",
    content="Long content. " * 50,
    url="https://example.com/1",
    source="Src",
    language="en",
    published_at=None,
):
    if published_at is None:
        published_at = datetime.now(timezone.utc)
    return Article(
        url=url,
        title=title,
        source=source,
        content=content,
        categories=[],
        language=language,
        published_at=published_at,
    )


# --- URL construction ---


def test_build_google_news_url_default_window():
    from news.topic_synth import build_google_news_url

    url = build_google_news_url("greek elections", hours=24)
    assert "google.com/rss/search" in url
    assert "when%3A24h" in url or "when:24h" in url
    assert "greek" in url.lower()
    assert "elections" in url.lower()


def test_build_google_news_url_custom_window():
    from news.topic_synth import build_google_news_url

    url = build_google_news_url("topic", hours=48)
    assert "when%3A48h" in url or "when:48h" in url


def test_build_google_news_url_handles_special_chars():
    from news.topic_synth import build_google_news_url

    url = build_google_news_url('M&A "deals" in Europe', hours=24)
    # Must be percent-encoded — & and " would break the URL otherwise
    # The literal "&deals" sequence should not appear unescaped in the q= param
    assert "&deals" not in url
    # The q= param should have a percent-encoded ampersand or quote
    assert "%26" in url or "%22" in url


def test_build_google_news_url_includes_hl_and_ceid():
    from news.topic_synth import build_google_news_url

    url = build_google_news_url("topic", hours=24)
    assert "hl=en-US" in url
    assert "ceid=US%3Aen" in url or "ceid=US:en" in url


# --- Section builders ---


def test_topic_base_prompt_includes_query_and_hours():
    from news.topic_synth import _topic_base_prompt

    out = _topic_base_prompt("Greek elections", 48)
    assert "Greek elections" in out
    assert "48" in out


def test_topic_output_format_specifies_json_schema():
    from news.topic_synth import _topic_output_format

    out = _topic_output_format()
    assert "executive_brief" in out
    assert "sections" in out


# --- Prompt composition ---


def test_build_topic_prompt_includes_articles():
    from news.topic_synth import build_topic_prompt

    articles = [_make_article(title="Test article 1")]
    prompt = build_topic_prompt(articles, "test query", 24)
    assert "Test article 1" in prompt
    assert "test query" in prompt


def test_build_topic_prompt_includes_name_handling_rules():
    """Topic prompt should include generic name-handling rules from build_roster()."""
    from news.topic_synth import build_topic_prompt

    prompt = build_topic_prompt([], "x", 24)
    assert "NAME HANDLING" in prompt


def test_build_topic_prompt_no_brand_roster():
    """Topic prompt must not include any brand-aware roster (no leadership/competitors)."""
    from news.topic_synth import build_topic_prompt

    prompt = build_topic_prompt([], "ECB rates", 24)
    assert "EXECUTIVE NAME ROSTER" not in prompt


# --- Fallback ---


def test_build_topic_fallback_includes_titles():
    from news.topic_synth import build_topic_fallback

    articles = [_make_article(title="Some headline")]
    out = build_topic_fallback(articles)
    assert "Some headline" in out


# --- CLI validation ---


def test_topic_profile_requires_query():
    """python3 main.py --profile topic (without --query) exits non-zero."""
    result = subprocess.run(
        ["python3", "main.py", "--profile", "topic"],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    assert result.returncode != 0
    assert "query" in result.stderr.lower() or "query" in result.stdout.lower()


def test_query_flag_requires_topic_profile():
    """python3 main.py --query 'x' without --profile topic exits non-zero."""
    result = subprocess.run(
        ["python3", "main.py", "--query", "x", "--profile", "digest"],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    assert result.returncode != 0


# --- Profile registration ---


def test_topic_profile_in_valid_profiles():
    from news.config import VALID_PROFILES

    assert "topic" in VALID_PROFILES


def test_topic_settings_loads():
    from news.config import get_settings

    s = get_settings(profile="topic")
    assert "pipeline" in s
    assert "email" in s
    assert "scoring" in s
    assert "synthesis" in s


# --- Brand-leak guard ---


def test_topic_synth_module_has_no_brand_specific_literals():
    """topic_synth must contain zero NBG/Greek-bank literals — it's brand-neutral by design."""
    import news.topic_synth as ts

    src = open(ts.__file__).read()
    forbidden = [
        "NBG",
        "National Bank of Greece",
        "Mylonas",
        "Theofilidi",
        "Plessas",
        "Piraeus",
        "Alpha Bank",
        "Eurobank",
        "Ethniki",
        "Εθνική",
    ]
    for f in forbidden:
        assert f not in src, f"Found brand literal in topic_synth: {f}"
