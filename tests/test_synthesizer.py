"""Tests for AI synthesis layer."""

import json
import subprocess
from datetime import datetime, timezone
from unittest.mock import Mock, patch


from news.models import Article
from news.synthesizer import (
    build_fallback_digest,
    build_prompt,
    invoke_claude,
    parse_synthesis_output,
)


def _make_articles():
    """Helper to create test articles."""
    now = datetime.now(timezone.utc)
    return [
        Article(
            url="https://example.com/1",
            title="NBG Record Profits",
            source="Reuters",
            content="National Bank of Greece reported record profits.",
            categories=["banking"],
            language="en",
            relevance_score=70,
            fetched_at=now,
            published_at=now,
        ),
        Article(
            url="https://example.com/2",
            title="Claude Code Update",
            source="TechCrunch",
            content="Anthropic released a major Claude Code update.",
            categories=["ai"],
            language="en",
            relevance_score=45,
            fetched_at=now,
            published_at=now,
        ),
    ]


def test_build_prompt_includes_articles():
    """Verify prompt contains article titles, time window, previous highlights."""
    articles = _make_articles()
    previous_highlights = ["Previous highlight 1", "Previous highlight 2"]
    time_window = "last 24 hours"

    prompt = build_prompt(articles, previous_highlights, time_window)

    assert "NBG Record Profits" in prompt
    assert "Claude Code Update" in prompt
    assert "last 24 hours" in prompt
    assert "Previous highlight 1" in prompt
    assert "Previous highlight 2" in prompt


def test_build_prompt_requests_json_output():
    """Verify 'JSON' appears in prompt."""
    articles = _make_articles()

    prompt = build_prompt(articles, [], "24h")

    assert "JSON" in prompt or "json" in prompt


def test_parse_synthesis_output_valid_json():
    """Parse a clean JSON string with all required fields."""
    raw = json.dumps({
        "executive_brief": ["Item 1", "Item 2", "Item 3", "Item 4", "Item 5"],
        "what_changed": "Major changes occurred.",
        "sections": [
            {
                "category": "banking",
                "display_name": "Banking",
                "synthesis": "Banking sector analysis.",
                "opposing_views": "Some disagree.",
                "fact_check": "All facts verified.",
                "sources": ["Reuters"],
            }
        ],
    })

    result = parse_synthesis_output(raw)

    assert result["executive_brief"] == ["Item 1", "Item 2", "Item 3", "Item 4", "Item 5"]
    assert result["what_changed"] == "Major changes occurred."
    assert len(result["sections"]) == 1
    assert result["sections"][0]["category"] == "banking"
    assert result["sections"][0]["display_name"] == "Banking"


def test_parse_synthesis_output_extracts_json_from_prose():
    """Parse JSON embedded in markdown code block."""
    data = {
        "executive_brief": ["Brief 1", "Brief 2", "Brief 3", "Brief 4", "Brief 5"],
        "what_changed": "Changes noted.",
        "sections": [],
    }
    raw = f"Here is the analysis:\n```json\n{json.dumps(data)}\n```\nEnd of response."

    result = parse_synthesis_output(raw)

    assert result["executive_brief"] == ["Brief 1", "Brief 2", "Brief 3", "Brief 4", "Brief 5"]
    assert result["what_changed"] == "Changes noted."


def test_build_fallback_digest():
    """Verify fallback contains article titles from sources."""
    articles = _make_articles()

    fallback = build_fallback_digest(articles)

    assert "SYNTHESIS UNAVAILABLE" in fallback
    assert "NBG Record Profits" in fallback
    assert "Claude Code Update" in fallback
    assert "Reuters" in fallback
    assert "TechCrunch" in fallback
    assert "https://example.com/1" in fallback
    assert "https://example.com/2" in fallback


@patch("news.synthesizer.subprocess.run")
def test_invoke_claude_success(mock_run):
    """Mock subprocess.run returning success with JSON stdout."""
    expected_output = '{"executive_brief": [], "sections": []}'
    mock_run.return_value = Mock(stdout=expected_output, returncode=0)

    result = invoke_claude("test prompt", timeout=60)

    assert result == expected_output
    mock_run.assert_called_once()
    args = mock_run.call_args
    assert args[1]["input"] == "test prompt"
    assert args[1]["capture_output"] is True
    assert args[1]["text"] is True
    assert args[1]["timeout"] == 60


@patch("news.synthesizer.subprocess.run")
def test_invoke_claude_timeout(mock_run):
    """Mock subprocess.run raising TimeoutError."""
    mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=60)

    result = invoke_claude("test prompt", timeout=60)

    assert result is None
